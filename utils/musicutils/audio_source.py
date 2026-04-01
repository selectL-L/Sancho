"""Custom audio sources for discord.py voice playback.

This module provides audio source implementations with behavior beyond
discord.py's built-in ``FFmpegPCMAudio``. The important design points are:

1. Seeking is owned by the source rather than the voice client.
2. FFmpeg stderr is captured and parsed continuously for recovery decisions.
3. The decoded PCM output is archived in memory while playback is happening.

That archive is the key to the rebuilt loop-one behavior. Once FFmpeg has
decoded the track into the archive, loop-one replay is just a read-cursor
rewind inside the same source object. No fresh yt-dlp resolution, no new
source construction, and no callback-driven playback restart are needed.
"""

import dataclasses
import io
import os
import logging
import shlex
import subprocess
import sys
import threading
from typing import Optional

import discord

import config
from utils.musicutils.ffmpeg_parser import FFmpegStderrParser
from utils.musicutils.music_data import FFmpegHealth
from utils.musicutils.music_helpers import FFMPEG_OPTIONS, get_ffmpeg_path, get_ffmpeg_stderr_loglevel

logger = logging.getLogger(__name__)

# Discord voice uses 48kHz, 2 channels, 16-bit audio.
# 20 ms of audio = 48_000 * 2 * 2 * 0.02 = 3840 bytes.
FRAME_SIZE = 3840


def _shallow_copy_health(health: FFmpegHealth) -> FFmpegHealth:
    """Create a cheap defensive copy of an FFmpegHealth report.

    Every field is either a primitive, an enum, or ``list[str]``.  A shallow
    dataclass copy plus a fresh list for ``stderr_lines`` is sufficient --
    ``copy.deepcopy`` is needlessly expensive here because it walks every
    object recursively (including enum singletons that can't be meaningfully
    copied anyway).
    """
    snapshot = dataclasses.replace(health, stderr_lines=health.stderr_lines.copy())
    return snapshot
FRAMES_PER_SECOND = 50
PCM_BYTES_PER_SECOND = FRAME_SIZE * FRAMES_PER_SECOND
ARCHIVE_READ_BLOCK_SIZE = FRAME_SIZE * 250  # 5 seconds of PCM per producer read.


class SeekableAudioSource(discord.AudioSource):
    """FFmpeg-backed PCM source with seeking and in-memory archive replay.

    Unlike ``discord.FFmpegPCMAudio``, this source owns a producer/consumer
    archive of decoded PCM:

    - A background producer drains FFmpeg stdout into an in-memory PCM archive.
    - `read()` serves 20 ms frames from that archive at Discord playback speed.

    This keeps the currently playing track replayable without any fresh source
    acquisition. When loop one is enabled, the source simply rewinds its own
    read cursor instead of returning EOF.

    The source still supports manual ``seek()`` by respawning FFmpeg from a new
    starting position, but normal clean loop-one repetition never leaves this
    source object.
    """

    def __init__(
        self,
        source: str,
        *,
        start_position: float = 0.0,
        http_headers: Optional[dict[str, str]] = None,
        volume: float = 1.0,
    ) -> None:
        """Initialize a seekable audio source.

        Args:
            source: Path to local file or streaming URL.
            start_position: Position in seconds to start playback from.
            http_headers: HTTP headers for streaming URLs.
            volume: Volume multiplier (0.0 to 1.0).
        """
        self.source = source
        self._http_headers = http_headers
        self._volume = max(0.0, min(1.0, volume))

        self._process: Optional[subprocess.Popen[bytes]] = None
        self._start_position = start_position
        self._is_paused = False

        # Silence frame for paused state.
        self._silence = b'\x00' * FRAME_SIZE

        self._cleaned_up = False
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_lines: list[str] = []
        self._health: FFmpegHealth = FFmpegHealth()
        self._parser = FFmpegStderrParser()

        # Archive state: producer appends PCM blocks, read() advances an
        # independent play cursor through those archived blocks.
        self._archive_condition = threading.Condition()
        self._archive_chunks: list[bytes] = []
        self._archive_total_bytes = 0
        self._archive_complete = False
        self._play_chunk_index = 0
        self._play_chunk_offset = 0
        self._play_absolute_bytes = 0
        self._repeat_one_enabled = False
        self._frames_read = 0  # Total frames read from FFmpeg into the archive.

        self._spawn_ffmpeg(start_position)

    def _spawn_ffmpeg(self, start_position: float) -> None:
        """Spawn FFmpeg starting at the requested position.

        Seeking still restarts FFmpeg, but replay after a clean end remains
        source-owned because the archived PCM is retained until cleanup.
        """
        self._cleanup_process()

        ffmpeg_path = get_ffmpeg_path()
        is_local = not self.source.startswith(('http://', 'https://'))

        before_parts = []

        # Seek position (must come before input)
        if start_position > 0:
            before_parts.append(f'-ss {start_position:.2f}')

        # For streaming URLs, add reconnect options and user-agent
        if not is_local:
            before_parts.append(FFMPEG_OPTIONS['before_options'])
            if self._http_headers:
                user_agent = self._http_headers.get('User-Agent', '')
                if user_agent:
                    # Escape quotes for shell
                    user_agent = user_agent.replace('"', '\\"')
                    before_parts.append(f'-user_agent "{user_agent}"')

        before_options = ' '.join(before_parts)

        # Build the command
        # Output format: signed 16-bit little-endian PCM, 48kHz, stereo
        cmd = (
            f'{ffmpeg_path} -hide_banner -loglevel {get_ffmpeg_stderr_loglevel()} '
            f'{before_options} -i "{self.source}" '
            f'{FFMPEG_OPTIONS["options"]} '
            '-f s16le -ar 48000 -ac 2 pipe:1'
        )

        if sys.platform == 'win32':
            # On Windows, use CREATE_NO_WINDOW to hide console
            # Security note: source is always a trusted local path or YouTube URL
            # extracted by yt-dlp, not user input. shell=True is safe here.
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            self._process = subprocess.Popen(  # noqa: S602
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                shell=True,  # Needed for complex command with quotes on Windows
            )
        else:
            # On Unix, shlex.split handles quoting properly
            self._process = subprocess.Popen(  # noqa: S603
                shlex.split(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )

        self._start_position = start_position
        self._is_paused = False

        self._stderr_lines = []  # Reset for new process
        self._health = FFmpegHealth()  # Reset health for new process
        self._parser = FFmpegStderrParser()

        with self._archive_condition:
            self._archive_chunks = []
            self._archive_total_bytes = 0
            self._archive_complete = False
            self._play_chunk_index = 0
            self._play_chunk_offset = 0
            self._play_absolute_bytes = 0
            self._frames_read = 0
            self._archive_condition.notify_all()

        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            name=f"FFmpeg-stderr-{id(self)}",
            daemon=True,
        )
        self._stderr_thread.start()

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader_loop,
            name=f"FFmpeg-stdout-{id(self)}",
            daemon=True,
        )
        self._stdout_thread.start()

        logger.debug(f"[AudioSource] Spawned FFmpeg at position {start_position:.1f}s")

    def _cleanup_process(self) -> None:
        """Terminate and clean up the current FFmpeg process."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.debug(f"FFmpeg terminate failed, killing: {exc}")
                try:
                    self._process.kill()
                except OSError as kill_exc:
                    logger.debug(f"FFmpeg kill failed (likely already dead): {kill_exc}")
            self._process = None

        with self._archive_condition:
            self._archive_complete = True
            self._archive_condition.notify_all()

        if self._stdout_thread and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=0.5)
        self._stdout_thread = None

        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)
        self._stderr_thread = None

        # Log captured stderr if anything interesting was captured
        if self._stderr_lines:
            logger.debug(
                f"[AudioSource] FFmpeg stderr ({len(self._stderr_lines)} lines): "
                f"{self._stderr_lines[:5]}{'...' if len(self._stderr_lines) > 5 else ''}"
            )

    def _stderr_reader_loop(self) -> None:
        """Read FFmpeg stderr in the background and keep parser state live."""
        if not self._process or not self._process.stderr:
            return

        try:
            while True:
                line = self._process.stderr.readline()
                if not line:
                    # EOF - process terminated
                    break

                decoded = line.decode('utf-8', errors='replace').strip()
                if decoded:
                    self._stderr_lines.append(decoded)
                    if config.DEV_MODE:
                        logger.debug(f"[FFmpeg raw] {decoded}")
                    self._parser.consume_line(decoded)
                    self._health = self._snapshot_health()
        except Exception as exc:
            logger.debug(f"[AudioSource] Stderr reader error: {exc}")

    def _stdout_reader_loop(self) -> None:
        """Drain FFmpeg stdout into the in-memory PCM archive.

        FFmpeg is allowed to run ahead of playback speed. The archive grows as
        quickly as FFmpeg can decode, while Discord consumes 20 ms frames at
        real-time speed through ``read()``. That lets short tracks finish fully
        archiving during prefetch and lets longer tracks continue archiving in
        the background while the first pass is already playing.
        """
        if not self._process or not self._process.stdout:
            with self._archive_condition:
                self._archive_complete = True
                self._archive_condition.notify_all()
            return

        remainder = b''

        try:
            while True:
                block = self._process.stdout.read(ARCHIVE_READ_BLOCK_SIZE)
                if not block:
                    break

                combined = remainder + block
                complete_bytes = len(combined) - (len(combined) % FRAME_SIZE)
                if complete_bytes <= 0:
                    remainder = combined
                    continue

                archive_chunk = combined[:complete_bytes]
                remainder = combined[complete_bytes:]

                with self._archive_condition:
                    self._archive_chunks.append(archive_chunk)
                    self._archive_total_bytes += len(archive_chunk)
                    self._frames_read += len(archive_chunk) // FRAME_SIZE
                    self._archive_condition.notify_all()
        except Exception as exc:
            logger.warning(f"[AudioSource] Producer read error: {exc}")
        finally:
            if remainder:
                logger.debug(
                    f"[AudioSource] Dropping trailing partial PCM block of {len(remainder)} bytes"
                )
            with self._archive_condition:
                self._archive_complete = True
                self._archive_condition.notify_all()

    def read(self) -> bytes:
        """Read the next 20 ms frame of audio data.

        Called by discord.py's voice client about 50 times per second.

        The read path serves frames from the in-memory archive, not directly
        from FFmpeg stdout.  This gives us three important behaviors:

        1. Prefetched audio can start instantly because frames are already in
           memory.
        2. Playback can keep going even if FFmpeg has already finished and the
           URL would otherwise expire later.
        3. Loop one can be seamless because the source can rewind its archive
           cursor instead of returning EOF.

        IMPORTANT: This method must NEVER block.  discord.py's voice sending
        thread calls read() on a tight 20 ms cadence.  If the archive hasn't
        caught up yet (producer is still decoding), we return a silence frame
        so the voice connection stays healthy.  The next call will try again.

        Returns:
            Exactly ``FRAME_SIZE`` bytes of PCM audio, or ``b''`` only when
            the source has truly ended and loop one is disabled.
        """
        if self._is_paused:
            return self._silence

        with self._archive_condition:
            # Fast path: archive has a full frame ready at the current cursor.
            if self._play_absolute_bytes + FRAME_SIZE <= self._archive_total_bytes:
                # Advance past any fully-consumed chunks.
                while (
                    self._play_chunk_index < len(self._archive_chunks)
                    and self._play_chunk_offset >= len(self._archive_chunks[self._play_chunk_index])
                ):
                    self._play_chunk_index += 1
                    self._play_chunk_offset = 0

                if self._play_chunk_index >= len(self._archive_chunks):
                    # Byte accounting says data exists but the chunk list
                    # disagrees -- return silence and let the next call retry
                    # rather than blocking the voice thread.
                    return self._silence

                chunk = self._archive_chunks[self._play_chunk_index]
                data = chunk[self._play_chunk_offset:self._play_chunk_offset + FRAME_SIZE]
                if len(data) != FRAME_SIZE:
                    logger.warning(
                        f"[AudioSource] Archive alignment error at chunk {self._play_chunk_index}"
                    )
                    return b''

                self._play_chunk_offset += FRAME_SIZE
                self._play_absolute_bytes += FRAME_SIZE

                if self._volume != 1.0:
                    return self._apply_volume(data)
                return data

            # Archive is complete -- either loop or signal EOF.
            if self._archive_complete:
                if self._repeat_one_enabled and self._archive_total_bytes >= FRAME_SIZE:
                    self._play_chunk_index = 0
                    self._play_chunk_offset = 0
                    self._play_absolute_bytes = 0
                    # Recurse once to serve the first frame immediately.
                    # _archive_complete + data present guarantees no infinite loop.
                    return self.read()
                return b''

            # Producer is still decoding -- return silence so the voice
            # connection doesn't stall.  The next read() 20 ms from now will
            # pick up the newly archived data.
            return self._silence

    def _apply_volume(self, data: bytes) -> bytes:
        """Apply volume scaling to PCM data.

        Args:
            data: Raw PCM bytes (signed 16-bit little-endian).

        Returns:
            Volume-adjusted PCM bytes.
        """
        # Convert bytes to 16-bit samples
        samples = []
        for i in range(0, len(data), 2):
            sample = int.from_bytes(data[i:i + 2], 'little', signed=True)
            # Apply volume and clamp to 16-bit range
            sample = int(sample * self._volume)
            sample = max(-32768, min(32767, sample))
            samples.append(sample)

        # Convert back to bytes
        result = io.BytesIO()
        for sample in samples:
            result.write(sample.to_bytes(2, 'little', signed=True))
        return result.getvalue()

    @property
    def position(self) -> float:
        """Current playback position in seconds relative to the source start."""
        with self._archive_condition:
            return self._start_position + (self._play_absolute_bytes / PCM_BYTES_PER_SECOND)

    @property
    def is_paused(self) -> bool:
        """Whether the source is paused."""
        return self._is_paused

    def pause(self) -> None:
        """Pause playback so read() returns silence."""
        if not self._is_paused:
            self._is_paused = True
            logger.debug(f"[AudioSource] Paused at {self.position:.1f}s")

    def resume(self) -> None:
        """Resume playback from the current archive cursor."""
        if self._is_paused:
            self._is_paused = False
            logger.debug(f"[AudioSource] Resumed at {self.position:.1f}s")

    def set_repeat_one(self, enabled: bool) -> None:
        """Enable or disable source-owned loop-one rewind behavior.

        This is updated live by ``ManagedPlayer`` so mid-playback loop mode
        changes take effect at the next end-of-archive boundary.
        """
        with self._archive_condition:
            self._repeat_one_enabled = enabled
            self._archive_condition.notify_all()

    def rewind(self, seconds: float) -> bool:
        """Move the play cursor backward within already-archived audio.

        This is the safe path used by pause/resume. It preserves the source's
        full-track archive and therefore does not change where loop one will
        wrap when the current pass reaches the true end of the track.

        Args:
            seconds: How far to move the play cursor backward.

        Returns:
            ``True`` if the requested rewind could be satisfied from the
            existing archive, otherwise ``False``.
        """
        if seconds <= 0:
            return True

        rewind_bytes = round(seconds * PCM_BYTES_PER_SECOND)

        with self._archive_condition:
            target_bytes = max(0, self._play_absolute_bytes - rewind_bytes)
            target_bytes -= target_bytes % FRAME_SIZE

            chunk_index, chunk_offset = self._locate_archive_offset_locked(target_bytes)
            self._play_chunk_index = chunk_index
            self._play_chunk_offset = chunk_offset
            self._play_absolute_bytes = target_bytes
            self._archive_condition.notify_all()
            return True

    def seek(self, position: float) -> None:
        """Seek by respawning FFmpeg from a new starting position.

        This resets the archive to the new start position. Manual seeks are
        still supported, even though loop-one replay remains source-owned.

        NOTE: Seeking is not currently exposed to users via any command.  When
        it is eventually wired up, be aware that a seek resets the archive
        anchor -- loop-one will repeat from the *seek point*, not from the
        original start of the track.  This may or may not be the desired UX.
        """
        position = max(0.0, position)
        was_paused = self._is_paused
        logger.debug(f"[AudioSource] Seeking to {position:.1f}s")
        self._spawn_ffmpeg(position)
        if was_paused:
            self.pause()

    def _locate_archive_offset_locked(self, target_bytes: int) -> tuple[int, int]:
        """Translate an absolute byte offset into chunk index + chunk offset.

        The caller must already hold ``self._archive_condition``.
        """
        if target_bytes <= 0 or not self._archive_chunks:
            return 0, 0

        remaining = target_bytes
        for chunk_index, chunk in enumerate(self._archive_chunks):
            if remaining < len(chunk):
                return chunk_index, remaining
            remaining -= len(chunk)

        last_index = len(self._archive_chunks) - 1
        return last_index, len(self._archive_chunks[last_index])

    def prebuffer(self, target_seconds: float = 30.0, min_valid_seconds: float = 30.0) -> bool:
        """Wait until the archive reaches the requested lead buffer.

        This is the rebuilt version of prefetch validation.

        The old source kept a separate deque of one-shot prebuffer frames and
        then fell back to direct FFmpeg reads during playback. The new source
        does not split those concepts. Prefetch and live playback both use the
        same archive:

        - If a track is short, callers pass the full duration so the entire
          song is archived before handoff.
        - If a track is larger, callers pass a shorter lead buffer so playback
          can start promptly while the producer keeps archiving the rest.

        Args:
            target_seconds: How much audio to wait for before returning.
                - For short tracks this is usually the entire duration.
                - For larger tracks this is the "ready enough" lead buffer.
            min_valid_seconds: Minimum archived audio required to trust the
                current FFmpeg source as valid.

        Returns:
            ``True`` if at least ``min_valid_seconds`` of audio was archived.
            ``False`` if the archive completed or failed before reaching that
            minimum.
        """
        target_bytes = int(target_seconds * PCM_BYTES_PER_SECOND)
        min_valid_bytes = int(min_valid_seconds * PCM_BYTES_PER_SECOND)

        logger.info(
            f"[AudioSource] Prebuffering: target={target_seconds:.0f}s, "
            f"min_valid={min_valid_seconds:.0f}s"
        )

        with self._archive_condition:
            while self._archive_total_bytes < target_bytes and not self._archive_complete:
                self._archive_condition.wait(timeout=0.05)

            buffered_bytes = self._archive_total_bytes

        buffered_seconds = buffered_bytes / PCM_BYTES_PER_SECOND
        is_valid = buffered_bytes >= min_valid_bytes
        logger.debug(
            f"[AudioSource] Prebuffer finished: {buffered_seconds:.1f}s archived, valid={is_valid}"
        )
        return is_valid

    @property
    def buffered_seconds(self) -> float:
        """Seconds of PCM currently archived in memory."""
        with self._archive_condition:
            return self._archive_total_bytes / PCM_BYTES_PER_SECOND

    @property
    def frames_read(self) -> int:
        """Total frames read from FFmpeg into the archive."""
        return self._frames_read

    def cleanup(self) -> None:
        """Clean up process and archive resources.

        Idempotent — discord.py's AudioPlayer.run() calls cleanup() in its
        own finally block after the after-callback fires, and our
        ManagedPlayer also calls it when managing source transitions.  We
        own the lifecycle; the guard just makes the second call a no-op.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # Build a short identifier for this source in logs.
        if self.source.startswith(('http://', 'https://')):
            source_label = f"stream:{self.source[:60]}..."
        else:
            source_label = f"local:{os.path.basename(self.source)}"

        with self._archive_condition:
            archived_bytes = self._archive_total_bytes

        if archived_bytes > 0:
            mem_mb = archived_bytes / (1024 * 1024)
            logger.info(f"[AudioSource] Releasing archive (~{mem_mb:.1f}MB) for {source_label}")

        self._cleanup_process()

        with self._archive_condition:
            self._archive_chunks = []
            self._archive_total_bytes = 0
            self._play_chunk_index = 0
            self._play_chunk_offset = 0
            self._play_absolute_bytes = 0
            self._archive_complete = True
            self._archive_condition.notify_all()

        logger.info(f"[AudioSource] Cleaned up {source_label}")

    @property
    def stderr_lines(self) -> list[str]:
        """Captured stderr output from FFmpeg."""
        return self._stderr_lines.copy()

    def build_ffmpeg_health(
        self,
        *,
        context: str,
        elapsed: Optional[float] = None,
        expected_duration: Optional[int] = None,
        explicit_error: bool = False,
    ) -> FFmpegHealth:
        """Finalize current FFmpeg parser state for a caller decision."""
        process_returncode = self._process.poll() if self._process else None
        report = self._parser.finalize(
            frames_read=self._frames_read,
            process_returncode=process_returncode,
            context=context,
            elapsed=elapsed,
            expected_duration=expected_duration,
            explicit_error=explicit_error,
        )
        self._health = _shallow_copy_health(report)
        return _shallow_copy_health(self._health)

    @property
    def health(self) -> FFmpegHealth:
        """Current FFmpeg parser state without playback-end heuristics."""
        return self._snapshot_health()

    def is_opus(self) -> bool:
        """Whether this source produces Opus packets (it doesn't)."""
        return False

    def _snapshot_health(self) -> FFmpegHealth:
        """Create a defensive copy of the current parser state."""
        snapshot = _shallow_copy_health(self._parser.report)
        snapshot.frames_read = self._frames_read
        snapshot.stderr_lines = self._stderr_lines.copy()
        return snapshot

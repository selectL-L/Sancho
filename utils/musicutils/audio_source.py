"""Custom audio sources for discord.py voice playback.

This module provides audio source implementations with behavior beyond
discord.py's built-in ``FFmpegPCMAudio``. The important design points are:

1. Seeking is owned by the source rather than the voice client.
2. FFmpeg stderr is captured and parsed continuously for recovery decisions.
3. The decoded PCM is Opus-encoded by a background producer thread and
   archived as pre-encoded packets for direct transmission.

By encoding Opus in the producer thread (decoupled from playback speed),
the discord.py AudioPlayer's hot loop only has to send packets — no
per-frame Opus encoding jitter feeding into its cumulative timing
correction. Once a track is fully archived, loop-one replay is just a
read-cursor rewind inside the same source object with zero encoding
overhead. No fresh yt-dlp resolution, no new source construction, and
no callback-driven playback restart are needed.
"""

import ctypes
import ctypes.util
import dataclasses
import io
import os
import logging
import shlex
import struct
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
# 20 ms of audio = 48_000 * 2 * 2 * 0.02 = 3840 bytes of PCM input per Opus frame.
FRAME_SIZE = 3840

# Standard Opus silence frame — same bytes discord.py's send_silence() uses
# and what the Discord voice docs specify for data interpolation gaps.
OPUS_SILENCE = b'\xf8\xff\xfe'


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


# ==========================================================================
# Opus encoder — our own ctypes wrapper, independent of discord.py internals.
#
# We load the same libopus shared library that discord.py requires for voice
# but talk to the C ABI directly. This insulates us from discord.py internal
# changes while depending only on the stable Opus C API (RFC 6716, unchanged
# since 2012).
# ==========================================================================

# Opus C API constants (from opus_defines.h)
_OPUS_OK = 0
_OPUS_APPLICATION_AUDIO = 2049
_OPUS_SET_BITRATE = 4002
_OPUS_SET_BANDWIDTH = 4008
_OPUS_SET_FEC = 4012
_OPUS_SET_PLP = 4014
_OPUS_SET_SIGNAL = 4024
_OPUS_BANDWIDTH_FULLBAND = 1105
_OPUS_SIGNAL_AUTO = -1000

_OPUS_SAMPLING_RATE = 48000
_OPUS_CHANNELS = 2
_OPUS_SAMPLES_PER_FRAME = 960  # 20ms at 48kHz

_opus_lib: Optional[ctypes.CDLL] = None


def _setup_opus_functions(lib: ctypes.CDLL) -> None:
    """Configure ctypes argtypes/restype for the Opus C functions we call.

    Only the functions used by ``_OpusEncoder`` are configured.

    ``opus_encoder_ctl`` is variadic in C so we cannot set full argtypes.
    Only restype is set here; callers MUST pass the encoder state as a
    ``ctypes.c_void_p`` instance (not a bare int) so ctypes preserves
    pointer width on 64-bit platforms. See ``_OpusEncoder.__init__``.
    """
    c_int_p = ctypes.POINTER(ctypes.c_int)
    c_int16_p = ctypes.POINTER(ctypes.c_int16)

    lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, c_int_p]
    lib.opus_encoder_create.restype = ctypes.c_void_p

    lib.opus_encode.argtypes = [ctypes.c_void_p, c_int16_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
    lib.opus_encode.restype = ctypes.c_int32

    lib.opus_encoder_ctl.restype = ctypes.c_int32

    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encoder_destroy.restype = None

    lib.opus_strerror.argtypes = [ctypes.c_int]
    lib.opus_strerror.restype = ctypes.c_char_p


def _get_opus_lib() -> ctypes.CDLL:
    """Load and cache our own handle to the libopus shared library.

    The OS deduplicates shared library pages, so loading independently of
    discord.py's handle costs only a lightweight Python CDLL wrapper — not
    a second copy of libopus in memory. This avoids coupling to discord.py's
    errcheck callbacks and argtypes that are set on its shared handle.

    Discovery order:
      1. System library via ``ctypes.util.find_library('opus')``
      2. Windows: discord.py's bundled DLL path (same native file, separate
         Python wrapper)

    Raises:
        RuntimeError: If libopus cannot be found anywhere.
    """
    global _opus_lib
    if _opus_lib is not None:
        return _opus_lib

    # System library (works on Linux, macOS, sometimes Windows)
    lib_name = ctypes.util.find_library('opus')
    if lib_name:
        try:
            _opus_lib = ctypes.cdll.LoadLibrary(lib_name)
            _setup_opus_functions(_opus_lib)
            logger.info(f"[Opus] Loaded system libopus: {lib_name}")
            return _opus_lib
        except OSError:
            pass

    # Windows: discord.py bundles the DLL in its package directory
    if sys.platform == 'win32':
        try:
            basedir = os.path.dirname(os.path.abspath(discord.__file__))
            bitness = struct.calcsize('P') * 8
            target = 'x64' if bitness > 32 else 'x86'
            dll_path = os.path.join(basedir, 'bin', f'libopus-0.{target}.dll')
            _opus_lib = ctypes.cdll.LoadLibrary(dll_path)
            _setup_opus_functions(_opus_lib)
            logger.info(f"[Opus] Loaded bundled libopus: {dll_path}")
            return _opus_lib
        except OSError:
            pass

    raise RuntimeError(
        "Could not find libopus. Install it (apt install libopus0 / brew install opus) "
        "or ensure discord.py's bundled copy is available."
    )


class _OpusEncoder:
    """Minimal ctypes wrapper around libopus for producer-side Opus encoding.

    Created per-producer-thread — one encoder per audio source spawn. Loads
    its own ctypes CDLL handle independently of discord.py (the OS deduplicates
    the native shared library pages; only the lightweight Python wrapper is new).

    Parameters match discord.py's Encoder defaults to produce Opus packets
    identical in format to what discord.py's AudioPlayer would have generated.
    """

    def __init__(self, *, bitrate_kbps: int = 128) -> None:
        lib = _get_opus_lib()
        self._lib = lib

        err = ctypes.c_int()
        # opus_encoder_create's restype is c_void_p, which ctypes returns as
        # a plain Python int. We must wrap it back in c_void_p so that
        # opus_encoder_ctl (variadic, no argtypes) receives a pointer-width
        # value instead of trying to squeeze a 64-bit address into a C int.
        raw_ptr = lib.opus_encoder_create(
            _OPUS_SAMPLING_RATE, _OPUS_CHANNELS,
            _OPUS_APPLICATION_AUDIO, ctypes.byref(err),
        )
        if err.value != _OPUS_OK:
            msg = lib.opus_strerror(err.value).decode('utf-8', errors='replace')
            raise RuntimeError(f"opus_encoder_create failed ({err.value}): {msg}")
        self._state = ctypes.c_void_p(raw_ptr)

        # Match discord.py's Encoder defaults exactly
        lib.opus_encoder_ctl(self._state, _OPUS_SET_BITRATE, bitrate_kbps * 1024)
        lib.opus_encoder_ctl(self._state, _OPUS_SET_FEC, 1)
        lib.opus_encoder_ctl(self._state, _OPUS_SET_PLP, 15)  # 15% expected packet loss
        lib.opus_encoder_ctl(self._state, _OPUS_SET_BANDWIDTH, _OPUS_BANDWIDTH_FULLBAND)
        lib.opus_encoder_ctl(self._state, _OPUS_SET_SIGNAL, _OPUS_SIGNAL_AUTO)

    def encode(self, pcm: bytes) -> bytes:
        """Encode one 20ms PCM frame to an Opus packet.

        Args:
            pcm: Exactly ``FRAME_SIZE`` (3840) bytes of signed 16-bit LE
                stereo PCM at 48kHz.

        Returns:
            Variable-size Opus packet (typically 80-320 bytes at 128kbps).

        Raises:
            RuntimeError: If the Opus encoder returns an error.
        """
        pcm_ptr = ctypes.cast(pcm, ctypes.POINTER(ctypes.c_int16))  # type: ignore[arg-type]  # ctypes accepts bytes at runtime
        max_bytes = len(pcm)  # Conservative upper bound
        out_buf = (ctypes.c_char * max_bytes)()

        ret = self._lib.opus_encode(
            self._state, pcm_ptr, _OPUS_SAMPLES_PER_FRAME,
            out_buf, max_bytes,
        )
        if ret < 0:
            msg = self._lib.opus_strerror(ret).decode('utf-8', errors='replace')
            raise RuntimeError(f"opus_encode failed ({ret}): {msg}")

        return bytes(out_buf[:ret])

    def destroy(self) -> None:
        """Explicitly release the encoder. Safe to call multiple times."""
        if self._state is not None:
            self._lib.opus_encoder_destroy(self._state)
            self._state = None

    def __del__(self) -> None:
        self.destroy()


class SeekableAudioSource(discord.AudioSource):
    """FFmpeg-backed Opus source with seeking and in-memory archive replay.

    Unlike ``discord.FFmpegPCMAudio``, this source decodes audio via FFmpeg,
    Opus-encodes each 20ms frame in a background producer thread, and stores
    the resulting packets in an in-memory archive:

    - A background producer drains FFmpeg stdout, applies volume, encodes
      each PCM frame to Opus, and appends the packet to the archive.
    - ``read()`` serves pre-encoded Opus packets from that archive — the
      discord.py AudioPlayer sends them directly with no per-frame encoding.

    This eliminates Opus VBR encoding jitter from the real-time playback
    loop, producing smoother timing at track starts. It also keeps the track
    replayable without fresh source acquisition. When loop one is enabled,
    the source simply rewinds its archive cursor instead of returning EOF.

    The source still supports manual ``seek()`` by respawning FFmpeg from a
    new starting position, but normal clean loop-one repetition never leaves
    this source object.
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

        # Opus silence frame for paused state and producer-not-ready gaps.
        self._silence = OPUS_SILENCE

        self._cleaned_up = False
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_lines: list[str] = []
        self._health: FFmpegHealth = FFmpegHealth()
        self._parser = FFmpegStderrParser()

        # Archive state: the producer thread Opus-encodes each 20ms PCM frame
        # from FFmpeg and appends the resulting packet here. read() advances
        # a simple frame index through the flat packet list.
        self._archive_condition = threading.Condition()
        self._archive_packets: list[bytes] = []
        self._archive_total_frames = 0
        self._archive_complete = False
        self._play_frame_index = 0
        self._repeat_one_enabled = False
        self._frames_read = 0  # Total PCM frames consumed from FFmpeg.

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
            self._archive_packets = []
            self._archive_total_frames = 0
            self._archive_complete = False
            self._play_frame_index = 0
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
        """Drain FFmpeg stdout, Opus-encode each frame, and archive the packets.

        FFmpeg is allowed to run (and be encoded) ahead of playback speed.
        The archive grows as quickly as FFmpeg + our encoder can produce,
        while Discord consumes one Opus packet per 20ms through ``read()``.
        Short tracks may finish archiving entirely during prefetch; longer
        tracks archive in the background while the first pass is already
        playing.

        The Opus encoder is created and destroyed within this thread — it is
        never shared across threads.
        """
        if not self._process or not self._process.stdout:
            with self._archive_condition:
                self._archive_complete = True
                self._archive_condition.notify_all()
            return

        encoder: Optional[_OpusEncoder] = None
        remainder = b''

        try:
            encoder = _OpusEncoder()

            while True:
                # Read enough for exactly one PCM frame, prepending any
                # leftover bytes from the previous iteration.
                needed = FRAME_SIZE - len(remainder)
                block = self._process.stdout.read(needed)
                if not block:
                    break

                combined = remainder + block
                if len(combined) < FRAME_SIZE:
                    # Partial frame — stash and read more.
                    remainder = combined
                    continue

                # Exactly one frame. Apply volume if non-unity, then encode.
                pcm_frame = combined[:FRAME_SIZE]
                remainder = combined[FRAME_SIZE:]

                if self._volume != 1.0:
                    pcm_frame = self._apply_volume(pcm_frame)

                opus_packet = encoder.encode(pcm_frame)

                with self._archive_condition:
                    self._archive_packets.append(opus_packet)
                    self._archive_total_frames += 1
                    self._frames_read += 1
                    self._archive_condition.notify_all()
        except Exception as exc:
            logger.warning(f"[AudioSource] Producer error: {exc}", exc_info=True)
        finally:
            if encoder is not None:
                encoder.destroy()
            if remainder:
                logger.debug(
                    f"[AudioSource] Dropping trailing partial PCM of {len(remainder)} bytes"
                )
            with self._archive_condition:
                self._archive_complete = True
                self._archive_condition.notify_all()

    def read(self) -> bytes:
        """Read the next pre-encoded Opus packet from the archive.

        Called by discord.py's voice client about 50 times per second. Since
        ``is_opus()`` returns ``True``, the AudioPlayer sends the returned
        packet directly over UDP without per-frame Opus encoding — eliminating
        encoding jitter from the playback timing loop.

        IMPORTANT: This method must NEVER block.  discord.py's voice sending
        thread calls read() on a tight 20 ms cadence.  If the archive hasn't
        caught up yet (producer is still encoding), we return an Opus silence
        frame so the voice connection stays healthy.  The next call will try
        again.

        Returns:
            A pre-encoded Opus packet, or ``b''`` only when the source has
            truly ended and loop one is disabled.
        """
        if self._is_paused:
            return self._silence

        with self._archive_condition:
            # Fast path: archive has a packet ready at the current cursor.
            if self._play_frame_index < self._archive_total_frames:
                packet = self._archive_packets[self._play_frame_index]
                self._play_frame_index += 1
                return packet

            # Archive is complete -- either loop or signal EOF.
            if self._archive_complete:
                if self._repeat_one_enabled and self._archive_total_frames > 0:
                    self._play_frame_index = 0
                    # Recurse once to serve the first frame immediately.
                    # _archive_complete + data present guarantees no infinite loop.
                    return self.read()
                return b''

            # Producer is still encoding -- return Opus silence so the voice
            # connection doesn't stall.  The next read() 20 ms from now will
            # pick up the newly archived packet.
            return self._silence

    def _apply_volume(self, data: bytes) -> bytes:
        """Apply volume scaling to a PCM frame before Opus encoding.

        Called in the producer thread, not on the real-time playback path.

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
            return self._start_position + (self._play_frame_index / FRAMES_PER_SECOND)

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

        rewind_frames = round(seconds * FRAMES_PER_SECOND)

        with self._archive_condition:
            self._play_frame_index = max(0, self._play_frame_index - rewind_frames)
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
        target_frames = int(target_seconds * FRAMES_PER_SECOND)
        min_valid_frames = int(min_valid_seconds * FRAMES_PER_SECOND)

        logger.info(
            f"[AudioSource] Prebuffering: target={target_seconds:.0f}s, "
            f"min_valid={min_valid_seconds:.0f}s"
        )

        with self._archive_condition:
            while self._archive_total_frames < target_frames and not self._archive_complete:
                self._archive_condition.wait(timeout=0.05)

            buffered_frames = self._archive_total_frames

        buffered_seconds = buffered_frames / FRAMES_PER_SECOND
        is_valid = buffered_frames >= min_valid_frames
        logger.debug(
            f"[AudioSource] Prebuffer finished: {buffered_seconds:.1f}s archived, valid={is_valid}"
        )
        return is_valid

    @property
    def buffered_seconds(self) -> float:
        """Seconds of audio currently archived in memory."""
        with self._archive_condition:
            return self._archive_total_frames / FRAMES_PER_SECOND

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
            archived_frames = self._archive_total_frames

        if archived_frames > 0:
            # Estimate memory: Opus packets are much smaller than PCM.
            # Rough estimate at 128kbps: ~320 bytes/packet average.
            est_mb = (archived_frames * 320) / (1024 * 1024)
            logger.info(f"[AudioSource] Releasing archive (~{est_mb:.1f}MB est) for {source_label}")

        self._cleanup_process()

        with self._archive_condition:
            self._archive_packets = []
            self._archive_total_frames = 0
            self._play_frame_index = 0
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
        """Whether this source produces Opus packets.

        Returns ``True`` — the producer thread pre-encodes all audio to Opus
        so discord.py's AudioPlayer can skip its per-frame encoding step.
        """
        return True

    def _snapshot_health(self) -> FFmpegHealth:
        """Create a defensive copy of the current parser state."""
        snapshot = _shallow_copy_health(self._parser.report)
        snapshot.frames_read = self._frames_read
        snapshot.stderr_lines = self._stderr_lines.copy()
        return snapshot

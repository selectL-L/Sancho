"""Custom audio sources for discord.py voice playback.

This module provides audio source implementations with features beyond
discord.py's built-in FFmpegPCMAudio, such as seeking support.
"""

import collections
import io
import logging
import shlex
import subprocess
import sys
import threading
import time
from typing import Optional

import discord

from utils.musicutils.music_data import AudioErrorType, FFmpegHealth
from utils.musicutils.music_helpers import FFMPEG_OPTIONS, get_ffmpeg_path

logger = logging.getLogger(__name__)

# Discord voice uses 48kHz, 2 channels, 16-bit audio
# 20ms of audio = 48000 * 2 * 2 * 0.02 = 3840 bytes
FRAME_SIZE = 3840


class SeekableAudioSource(discord.AudioSource):
    """FFmpeg audio source with seek support.

    Unlike discord.FFmpegPCMAudio, this source:
    - Supports seeking without involving VoiceClient
    - Tracks playback position
    - Can be paused/resumed at the source level

    The key insight is that VoiceClient.play() just calls read() repeatedly.
    We control the FFmpeg subprocess directly, so seek() can restart FFmpeg
    with a new -ss position without the VoiceClient knowing.

    Attributes:
        source: Path to local file or streaming URL.
        position: Current playback position in seconds.
        is_paused: Whether the source is paused (read() returns silence).
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
            http_headers: HTTP headers for streaming URLs (e.g., User-Agent).
            volume: Volume multiplier (0.0 to 1.0). Applied via PCM scaling.
        """
        self.source = source
        self._http_headers = http_headers
        self._volume = max(0.0, min(1.0, volume))

        self._process: Optional[subprocess.Popen[bytes]] = None
        self._start_position = start_position
        self._started_at: float = 0.0
        self._paused_at: Optional[float] = None
        self._is_paused = False

        # Silence frame for paused state
        self._silence = b'\x00' * FRAME_SIZE

        # Stderr capture for error diagnosis (Phase 1: observation only)
        # The stderr thread reads FFmpeg's stderr output and logs it.
        # This helps us understand what FFmpeg reports when URLs fail (403, etc.)
        # without changing any behavior - we're just collecting data.
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_lines: list[str] = []
        self._health: FFmpegHealth = FFmpegHealth()

        # Prebuffer for validated prefetch (Phase 2).
        # When prebuffer() is called, we read frames into this deque.
        # read() serves from here first, then falls back to live FFmpeg reads.
        # This enables:
        # 1. URL validation - if we can read 30s, the URL works
        # 2. Instant playback - buffer is ready when track starts
        # The FFmpeg process sits blocked on stdout write while buffer is full,
        # which is fine - it uses minimal resources while waiting.
        self._prebuffer: collections.deque[bytes] = collections.deque()
        self._frames_read: int = 0  # Total frames read (for stats)

        self._spawn_ffmpeg(start_position)

    def _spawn_ffmpeg(self, start_position: float) -> None:
        """Spawn FFmpeg subprocess starting at given position.

        Args:
            start_position: Position in seconds to start from.
        """
        self._cleanup_process()

        ffmpeg_path = get_ffmpeg_path()
        is_local = not self.source.startswith(('http://', 'https://'))

        # Build before_options
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
            f'{ffmpeg_path} -hide_banner -loglevel warning '
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
        self._started_at = time.time()
        self._paused_at = None
        self._is_paused = False

        # Start stderr reader thread.
        # This thread reads FFmpeg's stderr in the background so we can see
        # what errors FFmpeg reports (403, connection reset, etc.).
        # The thread exits naturally when the process terminates.
        self._stderr_lines = []  # Reset for new process
        self._health = FFmpegHealth()  # Reset health for new process
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            name=f"FFmpeg-stderr-{id(self)}",
            daemon=True,
        )
        self._stderr_thread.start()

        logger.debug(f"[SeekableAudioSource] Spawned FFmpeg at position {start_position:.1f}s")

    def _cleanup_process(self) -> None:
        """Terminate and clean up the current FFmpeg process."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.debug(f"FFmpeg terminate failed, killing: {e}")
                try:
                    self._process.kill()
                except OSError as e:
                    logger.debug(f"FFmpeg kill failed (likely already dead): {e}")
            self._process = None

        # Wait for stderr thread to finish (it will exit when process dies)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)
        self._stderr_thread = None

        # Log captured stderr if anything interesting was captured
        if self._stderr_lines:
            logger.debug(
                f"[SeekableAudioSource] FFmpeg stderr ({len(self._stderr_lines)} lines): "
                f"{self._stderr_lines[:5]}{'...' if len(self._stderr_lines) > 5 else ''}"
            )

    def _stderr_reader_loop(self) -> None:
        """Background thread: read FFmpeg stderr and log for diagnosis.

        Captures stderr and parses for error patterns. When an error is
        detected, self._health is updated with the error type.

        The thread exits naturally when the FFmpeg process terminates
        (stderr read returns empty).
        """
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
                    self._health.stderr_lines.append(decoded)
                    # Log at DEBUG so we can see what FFmpeg says during failures
                    logger.debug(f"[FFmpeg stderr] {decoded}")
                    # Parse for error patterns
                    self._parse_stderr_line(decoded)

        except Exception as e:
            # Thread must not raise - just log and exit
            logger.debug(f"[SeekableAudioSource] Stderr reader error: {e}")

    def _parse_stderr_line(self, line: str) -> None:
        """Parse a stderr line for error patterns and update health.

        TODO(Phase 3): These patterns are PLACEHOLDERS based on expected
        FFmpeg output. Once we collect real stderr from failures, update
        these patterns to match actual error formats.

        Expected patterns (to be verified):
        - "[https @ 0x...] HTTP error 403 Forbidden"
        - "[https @ 0x...] HTTP error 404 Not Found"
        - "[https @ 0x...] Connection reset by peer"
        - "[https @ 0x...] Connection refused"
        - "Server returned 4XX/5XX"

        Args:
            line: A single line from FFmpeg stderr.
        """
        # Only update if we haven't already found an error
        # (first error is usually the root cause)
        if self._health.error_type != AudioErrorType.NONE:
            return

        line_lower = line.lower()

        # TODO: Verify these patterns against real FFmpeg output
        # HTTP 403 - auth failure (URL expired or blocked)
        if '403' in line and ('http' in line_lower or 'forbidden' in line_lower):
            self._health.error_type = AudioErrorType.HTTP_403
            self._health.error_detail = line
            logger.debug(f"[SeekableAudioSource] Detected HTTP 403: {line}")

        # HTTP 404 - track removed
        elif '404' in line and ('http' in line_lower or 'not found' in line_lower):
            self._health.error_type = AudioErrorType.HTTP_404
            self._health.error_detail = line
            logger.debug(f"[SeekableAudioSource] Detected HTTP 404: {line}")

        # Other HTTP errors (5xx, etc.)
        elif 'http error' in line_lower or 'server returned' in line_lower:
            self._health.error_type = AudioErrorType.HTTP_OTHER
            self._health.error_detail = line
            logger.debug(f"[SeekableAudioSource] Detected HTTP error: {line}")

        # Connection errors
        elif any(pattern in line_lower for pattern in [
            'connection reset',
            'connection refused',
            'connection timed out',
            'network is unreachable',
            'no route to host',
        ]):
            self._health.error_type = AudioErrorType.CONNECTION
            self._health.error_detail = line
            logger.debug(f"[SeekableAudioSource] Detected connection error: {line}")

        # Format/stream errors
        elif any(pattern in line_lower for pattern in [
            'invalid data',
            'corrupt',
            'moov atom not found',
            'invalid stream',
        ]):
            self._health.error_type = AudioErrorType.FORMAT
            self._health.error_detail = line
            logger.debug(f"[SeekableAudioSource] Detected format error: {line}")

    def read(self) -> bytes:
        """Read the next frame of audio data.

        Called by discord.py's voice client ~50 times per second.
        Serves from prebuffer first (if any), then reads live from FFmpeg.

        Returns:
            3840 bytes of PCM audio data, or empty bytes if EOF/incomplete.
        """
        if self._is_paused:
            return self._silence

        # Serve from prebuffer first (instant, no FFmpeg wait)
        if self._prebuffer:
            data = self._prebuffer.popleft()
            # Apply volume if not 1.0
            if self._volume != 1.0:
                data = self._apply_volume(data)
            return data

        # Fall back to live FFmpeg read
        if not self._process or not self._process.stdout:
            return b''

        try:
            data = self._process.stdout.read(FRAME_SIZE)

            # CRITICAL: Discord.py expects exactly FRAME_SIZE bytes.
            # Returning partial frames causes audio corruption/static.
            # This can happen at stream start while FFmpeg is buffering,
            # or at stream end. Return empty to signal EOF for partial data.
            if len(data) != FRAME_SIZE:
                return b''

            self._frames_read += 1

            # Apply volume if not 1.0
            if self._volume != 1.0:
                data = self._apply_volume(data)

            return data
        except Exception as e:
            logger.warning(f"[SeekableAudioSource] Read error: {e}")
            return b''

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
        """Current playback position in seconds."""
        if self._is_paused and self._paused_at is not None:
            return self._start_position + (self._paused_at - self._started_at)
        return self._start_position + (time.time() - self._started_at)

    @property
    def is_paused(self) -> bool:
        """Whether the source is paused."""
        return self._is_paused

    def pause(self) -> None:
        """Pause playback (read() returns silence)."""
        if not self._is_paused:
            self._is_paused = True
            self._paused_at = time.time()
            logger.debug(f"[SeekableAudioSource] Paused at {self.position:.1f}s")

    def resume(self) -> None:
        """Resume playback from paused position."""
        if self._is_paused:
            self._is_paused = False
            # Adjust started_at to account for pause duration
            if self._paused_at is not None:
                pause_duration = time.time() - self._paused_at
                self._started_at += pause_duration
            self._paused_at = None
            logger.debug(f"[SeekableAudioSource] Resumed at {self.position:.1f}s")

    def seek(self, position: float) -> None:
        """Seek to a specific position.

        This restarts FFmpeg with a new -ss value. The VoiceClient
        continues calling read() and doesn't know a seek happened.

        Args:
            position: Target position in seconds.
        """
        position = max(0.0, position)
        was_paused = self._is_paused
        logger.debug(f"[SeekableAudioSource] Seeking to {position:.1f}s")
        self._spawn_ffmpeg(position)
        if was_paused:
            self.pause()

    def prebuffer(self, target_seconds: float = 30.0, min_valid_seconds: float = 30.0) -> bool:
        """Buffer audio frames to validate URL and enable instant playback.

        This is the core of prefetch validation. We read frames from FFmpeg
        until we hit the target OR encounter an error. If we read at least
        min_valid_seconds, the URL is considered valid.

        The FFmpeg process continues running after this returns - it will
        block on stdout write (pipe buffer full) until read() drains the buffer.
        This is intentional: the process sits ready to continue feeding audio.

        Args:
            target_seconds: How much audio to try buffering.
                - For short tracks (≤10min): pass track duration to buffer whole song
                - For long tracks: pass 30.0 to just validate
            min_valid_seconds: Minimum buffered to consider URL "valid".
                YouTube can throw 403s 15-25s into playback, so 30s proves the URL works.

        Returns:
            True if we buffered at least min_valid_seconds (URL is valid).
            False if we hit EOF/error before min_valid_seconds.

        After success:
            - self._prebuffer contains frames ready for read()
            - self.buffered_seconds shows how much we have
            - FFmpeg process is alive, blocked, ready to continue

        After failure:
            - self._stderr_lines contains FFmpeg's error output
            - Caller should cleanup() and handle the error
        """
        # Calculate frame targets (50 frames = 1 second at 20ms/frame)
        frames_per_second = 50
        target_frames = int(target_seconds * frames_per_second)
        min_valid_frames = int(min_valid_seconds * frames_per_second)

        logger.debug(
            f"[SeekableAudioSource] Prebuffering: target={target_seconds}s "
            f"({target_frames} frames), min_valid={min_valid_seconds}s ({min_valid_frames} frames)"
        )

        if not self._process or not self._process.stdout:
            logger.warning("[SeekableAudioSource] Prebuffer called with no process")
            return False

        frames_buffered = 0

        while frames_buffered < target_frames:
            try:
                data = self._process.stdout.read(FRAME_SIZE)

                # EOF or partial frame = stream ended
                if len(data) != FRAME_SIZE:
                    logger.debug(
                        f"[SeekableAudioSource] Prebuffer EOF after {frames_buffered} frames "
                        f"({frames_buffered / frames_per_second:.1f}s)"
                    )
                    break

                self._prebuffer.append(data)
                self._frames_read += 1
                frames_buffered += 1

            except Exception as e:
                logger.warning(f"[SeekableAudioSource] Prebuffer read error: {e}")
                break

        # Did we get enough to consider URL valid?
        is_valid = frames_buffered >= min_valid_frames

        logger.info(
            f"[SeekableAudioSource] Prebuffer complete: {frames_buffered} frames "
            f"({frames_buffered / frames_per_second:.1f}s), valid={is_valid}"
        )

        return is_valid

    @property
    def buffered_seconds(self) -> float:
        """Seconds of audio currently in the prebuffer."""
        return len(self._prebuffer) / 50.0  # 50 frames per second

    @property
    def frames_read(self) -> int:
        """Total frames read from FFmpeg (prebuffer + live)."""
        return self._frames_read

    def cleanup(self) -> None:
        """Clean up resources. Called when source is no longer needed."""
        frame_count = len(self._prebuffer)
        if frame_count > 0:
            # Estimate memory: ~3840 bytes per frame (20ms of stereo 48kHz audio)
            mem_mb = (frame_count * 3840) / (1024 * 1024)
            logger.info(f"[SeekableAudioSource] Releasing {frame_count} frames (~{mem_mb:.1f}MB)")
        self._prebuffer.clear()  # Release buffer memory
        self._cleanup_process()
        logger.info("[SeekableAudioSource] Cleaned up")

    @property
    def stderr_lines(self) -> list[str]:
        """Captured stderr output from FFmpeg.

        Useful for diagnosing playback failures. Contains lines like:
        - "[https @ 0x...] HTTP error 403 Forbidden"
        - "[https @ 0x...] Connection reset by peer"
        """
        return self._stderr_lines.copy()

    @property
    def health(self) -> FFmpegHealth:
        """Health status of the FFmpeg process.

        Populated by the stderr reader thread as it parses error patterns.
        Check this after playback ends or prebuffer() fails to understand
        what went wrong.

        TODO(Phase 3): The error classification depends on placeholder patterns
        in _parse_stderr_line(). Refine once we have real error samples.

        Returns:
            FFmpegHealth with error_type, error_detail, and stderr_lines.
        """
        # Update frames_read in health before returning
        self._health.frames_read = self._frames_read
        return self._health

    def is_opus(self) -> bool:
        """Whether this source produces Opus packets (it doesn't)."""
        return False

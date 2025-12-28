"""Custom audio sources for discord.py voice playback.

This module provides audio source implementations with features beyond
discord.py's built-in FFmpegPCMAudio, such as seeking support.
"""

import io
import logging
import shlex
import subprocess
import sys
import time
from typing import Optional

import discord

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

        logger.debug(f"[SeekableAudioSource] Spawned FFmpeg at position {start_position:.1f}s")

    def _cleanup_process(self) -> None:
        """Terminate and clean up the current FFmpeg process."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=1.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def read(self) -> bytes:
        """Read the next frame of audio data.

        Called by discord.py's voice client ~50 times per second.

        Returns:
            3840 bytes of PCM audio data, or empty bytes if EOF.
        """
        if self._is_paused:
            return self._silence

        if not self._process or not self._process.stdout:
            return b''

        try:
            data = self._process.stdout.read(FRAME_SIZE)
            if not data:
                return b''

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

    def cleanup(self) -> None:
        """Clean up resources. Called when source is no longer needed."""
        self._cleanup_process()
        logger.debug("[SeekableAudioSource] Cleaned up")

    def is_opus(self) -> bool:
        """Whether this source produces Opus packets (it doesn't)."""
        return False

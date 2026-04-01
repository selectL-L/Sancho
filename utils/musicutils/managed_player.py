"""Managed audio player for discord.py voice playback.

This module provides a high-level player abstraction that owns playback state.
VoiceClient becomes just a dumb output pipe - the ManagedPlayer controls all
state transitions and only exposes a single callback for natural track end.
"""

import asyncio
import enum
import logging
import threading
import time
from typing import Callable, Optional, Protocol

import discord

from utils.musicutils.audio_source import SeekableAudioSource
from utils.musicutils.music_data import FFmpegHealth, PlaybackEndReport

logger = logging.getLogger(__name__)


class TrackInfo(Protocol):
    """Protocol for track objects passed to ManagedPlayer.

    The player doesn't need to know about Track dataclass internals,
    just the fields needed for playback.
    """

    @property
    def url(self) -> str:
        """YouTube URL or other source identifier."""
        ...

    @property
    def title(self) -> str:
        """Track title for logging."""
        ...

    @property
    def duration(self) -> int:
        """Track duration in seconds."""
        ...


class PlayerState(enum.Enum):
    """Playback state machine states."""

    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class ManagedPlayer:
    """High-level audio player that owns playback state.

    This player abstracts away discord.py's callback-based model and provides
    a clean, direct-control interface. Key design principles:

    1. **Single callback path**: Only natural track end (or errors) trigger
       the on_track_end callback. Intentional stops (skip, seek, etc.) never
       trigger callbacks.

    2. **Generation-based callback filtering**: Instead of a fragile boolean
       flag, we track a "generation" counter. Each play() increments it.
       Callbacks carry the generation they were created with - if it doesn't
       match the current generation, the callback is stale and ignored.
       This eliminates race conditions between the main thread and callback thread.

    3. **Source-level control**: Pause/resume/seek happen at the audio source
       level. VoiceClient keeps playing the same "source" - it just produces
       silence when paused or restarts FFmpeg when seeking.

    Usage:
        def handle_track_end(error: Optional[Exception]) -> None:
            # Called only when track naturally ends
            cog.advance_and_play()

        player = ManagedPlayer(voice_client, handle_track_end)
        player.play(track, audio_url)
        player.pause()
        player.seek(30.0)  # No callback triggered
        player.resume()
        player.stop()  # Stops current, no callback - caller decides what to do

    Attributes:
        state: Current playback state (STOPPED, PLAYING, PAUSED).
        position: Current playback position in seconds.
        current_track: The track currently loaded (may be stopped/paused).
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        on_track_end: Callable[[PlaybackEndReport], None],
    ) -> None:
        """Initialize the managed player.

        Args:
            voice_client: Discord voice client to play audio through.
            on_track_end: Callback for when track naturally ends or errors.
                Called with a typed playback report.
                NOT called for intentional stops (skip, stop, etc.).
        """
        self._vc = voice_client
        self._on_track_end_callback = on_track_end
        self._loop = asyncio.get_running_loop()

        self._source: Optional[SeekableAudioSource] = None
        self._current_track: Optional[TrackInfo] = None
        self._state = PlayerState.STOPPED
        self._repeat_one_enabled = False

        # Generation counter for filtering stale callbacks.
        # Each play()/stop() increments this. Callbacks carry the generation they
        # were created with. If it doesn't match current, the callback is stale.
        # This is thread-safe because we use a lock for read-modify-write.
        self._generation = 0
        self._generation_lock = threading.Lock()

        # For detecting suspiciously fast failures (stale URLs)
        self._play_started_at: float = 0.0

    @property
    def state(self) -> PlayerState:
        """Current playback state."""
        return self._state

    @property
    def is_playing(self) -> bool:
        """Whether audio is actively playing (not paused or stopped)."""
        return self._state == PlayerState.PLAYING

    @property
    def is_paused(self) -> bool:
        """Whether playback is paused."""
        return self._state == PlayerState.PAUSED

    @property
    def is_stopped(self) -> bool:
        """Whether player is stopped (no track loaded)."""
        return self._state == PlayerState.STOPPED

    @property
    def position(self) -> float:
        """Current playback position in seconds."""
        if self._source:
            return self._source.position
        return 0.0

    @property
    def current_track(self) -> Optional[TrackInfo]:
        """The currently loaded track."""
        return self._current_track

    def play(
        self,
        track: TrackInfo,
        audio_source: Optional[str] = None,
        *,
        source: Optional[SeekableAudioSource] = None,
        http_headers: Optional[dict[str, str]] = None,
        start_position: float = 0.0,
    ) -> None:
        """Start playing a track.

        Provide EITHER audio_source (URL/path to create new source) OR source
        (pre-validated SeekableAudioSource from prefetch). Not both.

        If already playing, stops current playback (without triggering callback)
        and starts the new track.

        Args:
            track: Track info object.
            audio_source: Path to local file or streaming URL. Creates new source.
            source: Pre-validated SeekableAudioSource with buffered audio.
                Ownership transfers to ManagedPlayer - caller must NOT cleanup.
            http_headers: HTTP headers for streaming URLs (only with audio_source).
            start_position: Position in seconds to start from (only with audio_source).
        """
        # Validate args: need exactly one of audio_source or source
        if source is not None and audio_source is not None:
            raise ValueError("Provide audio_source OR source, not both")
        if source is None and audio_source is None:
            raise ValueError("Must provide audio_source or source")

        # Increment generation to invalidate any pending callbacks from previous play.
        # This must happen BEFORE we call vc.stop() so callbacks from the old source
        # see the new generation and know they're stale.
        with self._generation_lock:
            self._generation += 1
            current_gen = self._generation

        # Stop current playback if any. The callback from this stop will see
        # the incremented generation and exit early.
        # IMPORTANT: We need to wait for the old AudioPlayer thread to fully stop
        # before starting a new one. discord.py's vc.stop() just signals the thread
        # to stop but doesn't wait. If we start a new player immediately, the old
        # thread might still be sending silence packets, corrupting the encoder state.
        #
        # NOTE: _player is a private attribute on VoiceClient (an AudioPlayer thread).
        # Verified present in discord.py 2.7.0 (voice_client.py). If a future version
        # renames/removes it, the getattr guard returns None and we skip the join —
        # worst case is occasional audio glitches between track transitions.
        old_player = getattr(self._vc, '_player', None)
        if self._vc.is_playing():
            self._vc.stop()

        # Wait for old AudioPlayer thread to finish (up to 100ms)
        # This prevents race conditions between the old thread's send_silence()
        # and the new thread's audio output, which can cause static/glitches.
        if old_player is not None and hasattr(old_player, 'is_alive') and old_player.is_alive():
            old_player.join(timeout=0.1)

        # Clean up old source (but NOT the new prebuffered one we're taking ownership of)
        if self._source:
            self._source.cleanup()
            self._source = None

        # Set up the source
        self._current_track = track
        if source is not None:
            # Use pre-validated source from prefetch (instant playback)
            self._source = source
            is_prebuffered = True
        else:
            # Create new source from URL
            # Type assertion: we validated above that audio_source is set when source is None
            assert audio_source is not None, "audio_source must be set when source is None"
            self._source = SeekableAudioSource(
                audio_source,
                start_position=start_position,
                http_headers=http_headers,
            )
            is_prebuffered = False

        self._source.set_repeat_one(self._repeat_one_enabled)

        self._play_started_at = time.time()

        # Create callback that captures the current generation.
        # When this callback fires, it will only proceed if generation still matches.
        def after_callback(error: Optional[Exception]) -> None:
            self._after_callback(error, current_gen)

        self._vc.play(self._source, after=after_callback)
        self._state = PlayerState.PLAYING

        if is_prebuffered:
            logger.info(
                f"[ManagedPlayer] Playing (prebuffered): {track.title} "
                f"({self._source.buffered_seconds:.1f}s buffered)"
            )
        else:
            logger.info(f"[ManagedPlayer] Playing: {track.title}")

    def pause(self) -> bool:
        """Pause playback.

        Returns:
            True if paused, False if not playing.
        """
        if self._state != PlayerState.PLAYING or not self._source:
            return False

        self._source.pause()
        self._state = PlayerState.PAUSED
        logger.info(f"[ManagedPlayer] Paused at {self.position:.1f}s")
        return True

    def resume(self) -> bool:
        """Resume playback from paused position.

        Returns:
            True if resumed, False if not paused.
        """
        if self._state != PlayerState.PAUSED or not self._source:
            return False

        self._source.resume()
        self._state = PlayerState.PLAYING
        logger.info(f"[ManagedPlayer] Resumed at {self.position:.1f}s")
        return True

    def seek(self, position: float) -> bool:
        """Seek to a specific position.

        Works whether playing or paused. Does NOT trigger callback.

        Args:
            position: Target position in seconds.

        Returns:
            True if seek succeeded, False if no track loaded.
        """
        if not self._source:
            return False

        self._source.seek(position)
        logger.info(f"[ManagedPlayer] Seeked to {position:.1f}s")
        return True

    def rewind(self, seconds: float) -> bool:
        """Rewind within the current source archive without rebuilding it.

        This is primarily used by pause/resume so the player can step back one
        second for smooth continuation without changing the source's loop-one
        anchor.

        Args:
            seconds: How far to rewind.

        Returns:
            ``True`` if the rewind succeeded from the existing archive.
        """
        if not self._source:
            return False

        rewound = self._source.rewind(seconds)
        if rewound:
            logger.info(f"[ManagedPlayer] Rewound by {seconds:.1f}s")
        return rewound

    def stop(self) -> None:
        """Stop playback entirely.

        Does NOT trigger the on_track_end callback. Use this for intentional
        stops (leaving voice, switching tracks, etc.).
        """
        # Increment generation to invalidate any pending callbacks
        with self._generation_lock:
            self._generation += 1

        # Stop voice client playback
        if self._vc.is_playing():
            self._vc.stop()

        # Clean up source
        if self._source:
            self._source.cleanup()
            self._source = None

        self._current_track = None
        self._state = PlayerState.STOPPED
        logger.info("[ManagedPlayer] Stopped")

    def set_repeat_one(self, enabled: bool) -> None:
        """Update loop-one behavior on the active source and future plays."""
        self._repeat_one_enabled = enabled
        if self._source is not None:
            self._source.set_repeat_one(enabled)
        logger.debug(f"[ManagedPlayer] Repeat-one {'enabled' if enabled else 'disabled'}")

    def _after_callback(self, error: Optional[Exception], callback_generation: int) -> None:
        """Called by discord.py when the audio source is exhausted or errors.

        This runs in a thread pool, so we schedule the actual handling
        on the event loop.

        Args:
            error: Exception if playback failed, None if track ended normally.
            callback_generation: The generation this callback was created for.
        """
        # Check if this callback is stale (from a previous play/stop cycle)
        with self._generation_lock:
            if callback_generation != self._generation:
                # Stale callback - a new play() or stop() happened after this callback
                # was created but before it ran. Ignore it.
                logger.debug(
                    f"[ManagedPlayer] Ignoring stale callback "
                    f"(gen {callback_generation} != current {self._generation})"
                )
                return

        # Calculate elapsed time for failure detection
        elapsed = time.time() - self._play_started_at

        # Schedule callback on event loop
        asyncio.run_coroutine_threadsafe(
            self._handle_track_end(error, elapsed),
            self._loop,
        )

    async def _handle_track_end(
        self,
        error: Optional[Exception],
        elapsed: float,
    ) -> None:
        """Handle track end on the event loop.

        Args:
            error: Exception if playback failed (from discord.py).
            elapsed: Time since play started.
        """
        track = self._current_track
        ffmpeg_report = FFmpegHealth()
        if self._source:
            # Finalize the captured FFmpeg transcript into a typed outcome before
            # cleanup so the cog receives more than a bare retry signal.
            ffmpeg_report = self._source.build_ffmpeg_health(
                context='playback',
                elapsed=elapsed,
                expected_duration=track.duration if track else None,
                explicit_error=error is not None,
            )
        stderr_lines = ffmpeg_report.stderr_lines.copy()

        # Clean up source. discord.py's AudioPlayer.run() also calls
        # cleanup() in its finally block, but we own the lifecycle --
        # SeekableAudioSource.cleanup() is idempotent so the second call
        # from discord.py is a no-op.
        if self._source:
            self._source.cleanup()
            self._source = None

        self._state = PlayerState.STOPPED

        if ffmpeg_report.has_error:
            logger.warning(
                f"[ManagedPlayer] Playback failed: "
                f"{ffmpeg_report.summary or 'FFmpeg reported a playback failure.'}"
            )
            logger.info(
                f"[ManagedPlayer] Failure analysis | "
                f"elapsed={elapsed:.1f}s | "
                f"type={ffmpeg_report.error_type.value} | "
                f"action={ffmpeg_report.response_action.value} | "
                f"heuristic={ffmpeg_report.used_heuristic} | "
                f"reconnects={ffmpeg_report.reconnect_count} | "
                f"detail={ffmpeg_report.error_detail}"
            )
            if stderr_lines:
                logger.debug(
                    f"[ManagedPlayer] FFmpeg stderr ({len(stderr_lines)} lines): "
                    f"{stderr_lines}"
                )

        report = PlaybackEndReport(
            error=error,
            ffmpeg=ffmpeg_report,
            elapsed=elapsed,
        )
        self._on_track_end_callback(report)

    def update_voice_client(self, voice_client: discord.VoiceClient) -> None:
        """Update the voice client reference.

        Use this after reconnecting to voice.

        Args:
            voice_client: New voice client.
        """
        self._vc = voice_client
        logger.info("[ManagedPlayer] Voice client updated (reconnect recovery)")

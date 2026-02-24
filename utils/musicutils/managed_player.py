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
        on_track_end: Callable[[Optional[Exception]], None],
    ) -> None:
        """Initialize the managed player.

        Args:
            voice_client: Discord voice client to play audio through.
            on_track_end: Callback for when track naturally ends or errors.
                Called with the exception if playback failed, None otherwise.
                NOT called for intentional stops (skip, stop, etc.).
        """
        self._vc = voice_client
        self._on_track_end_callback = on_track_end
        self._loop = asyncio.get_running_loop()

        self._source: Optional[SeekableAudioSource] = None
        self._current_track: Optional[TrackInfo] = None
        self._state = PlayerState.STOPPED

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
        old_player = self._vc._player if hasattr(self._vc, '_player') else None
        if self._vc.is_playing():
            self._vc.stop()

        # Wait for old AudioPlayer thread to finish (up to 100ms)
        # This prevents race conditions between the old thread's send_silence()
        # and the new thread's audio output, which can cause static/glitches.
        if old_player is not None and old_player.is_alive():
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

        Error detection pipeline:
        1. Capture parsed error from FFmpeg stderr (may be NONE with placeholder patterns)
        2. ALWAYS run heuristic check (lightweight, ensures we always get *something*)
        3. Final error type = parsed if available, else heuristic's guess
        4. Log both for pattern refinement

        The heuristic is a safety net that ensures we never silently fail
        without knowing FFmpeg died. It's not meant to be replaced - parsed
        errors just give us more detail when available.

        Args:
            error: Exception if playback failed (from discord.py).
            elapsed: Time since play started, for heuristic detection.
        """
        from utils.musicutils.music_data import AudioErrorType

        # Capture stderr and health before cleanup (for diagnosis)
        # These are critical for refining error patterns - we need to see
        # what FFmpeg actually says vs what we parse/guess.
        stderr_lines = self._source.stderr_lines if self._source else []
        health = self._source.health if self._source else None
        parsed_error_type = health.error_type if health else AudioErrorType.NONE
        parsed_error_detail = health.error_detail if health else None

        # Clean up source
        if self._source:
            self._source.cleanup()
            self._source = None

        self._state = PlayerState.STOPPED

        track = self._current_track

        # -------------------------------------------------------------------
        # HEURISTIC CHECK - ALWAYS RUNS
        # -------------------------------------------------------------------
        # This is a lightweight pass-through that ensures we always detect
        # when FFmpeg died unexpectedly. Even if we have a parsed error,
        # we want to know what the heuristic would have said for comparison.
        heuristic_would_trigger = (
            elapsed < 3.0 and
            track is not None and
            track.duration > 10
        )

        # -------------------------------------------------------------------
        # DETERMINE FINAL ERROR STATE
        # -------------------------------------------------------------------
        # Pipeline: FFmpeg died → Read parsed error → Pass through heuristic
        # → Use parsed if available, heuristic if not

        if error:
            # Explicit error from discord.py (FFmpeg crashed, pipe broken, etc.)
            if elapsed < 3.0:
                logger.warning(
                    f"[ManagedPlayer] Playback failed after {elapsed:.1f}s "
                    f"(likely stale URL): {error}"
                )
            else:
                logger.error(f"[ManagedPlayer] Playback error: {error}")

        elif heuristic_would_trigger and track is not None:
            # Silent failure: track "ended" way too fast with no explicit error.
            # Heuristic caught this - FFmpeg died without telling discord.py.
            logger.warning(
                f"[ManagedPlayer] Track ended suspiciously fast ({elapsed:.1f}s) "
                f"for {track.duration}s track - heuristic triggered"
            )
            # Convert to error so retry logic kicks in
            error = ConnectionError("Playback ended too fast (likely 403)")

        # -------------------------------------------------------------------
        # FINAL ERROR TYPE RESOLUTION
        # -------------------------------------------------------------------
        # Use parsed error if FFmpeg told us something, otherwise fall back
        # to heuristic's best guess (HTTP_403 for silent fast failures).
        final_error_type = parsed_error_type

        if error and parsed_error_type == AudioErrorType.NONE:
            # FFmpeg gave us nothing - use heuristic's guess
            final_error_type = AudioErrorType.HTTP_403
            logger.info(
                "[ManagedPlayer] No FFmpeg error parsed, "
                "using heuristic guess: HTTP_403"
            )

        # -------------------------------------------------------------------
        # LOGGING FOR PATTERN REFINEMENT
        # -------------------------------------------------------------------
        # ALWAYS log on failures - this is how we collect real error patterns
        # to refine the placeholder patterns in _parse_stderr_line()
        if error:
            # Log structured data for pattern analysis:
            # - What FFmpeg said (parsed error type)
            # - What heuristic would say (elapsed-based check)
            # - What we're actually using (final_error_type)
            logger.info(
                f"[ManagedPlayer] Failure analysis | "
                f"elapsed={elapsed:.1f}s | "
                f"parsed={parsed_error_type.value} | "
                f"heuristic_triggered={heuristic_would_trigger} | "
                f"final={final_error_type.value} | "
                f"detail={parsed_error_detail}"
            )
            if stderr_lines:
                # Log full stderr separately so it's easy to grep
                logger.info(
                    f"[ManagedPlayer] FFmpeg stderr ({len(stderr_lines)} lines): "
                    f"{stderr_lines}"
                )

        # Invoke the callback
        self._on_track_end_callback(error)

    def update_voice_client(self, voice_client: discord.VoiceClient) -> None:
        """Update the voice client reference.

        Use this after reconnecting to voice.

        Args:
            voice_client: New voice client.
        """
        self._vc = voice_client

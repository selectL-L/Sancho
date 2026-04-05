"""Source acquisition mixin for the Music cog.

This file owns every code path that can trigger paid residential proxy usage.
If a spending decision isn't made here, it doesn't spend money.

Architecture
------------
The Music cog inherits from this mixin just like it inherits from
``MusicCommandsMixin``. The mixin provides ``_acquire_source()`` which walks
a priority chain (local cache -> direct URL -> residential yt-dlp URL ->
residential file download) and returns a playable source or ``None``.

The cog keeps the playback loop. It calls ``_acquire_source``, hands the
result to ``ManagedPlayer``, and reads ``report.ffmpeg.bucket`` when FFmpeg
dies. The mixin never touches Discord, the player, or the playlist -- it
only knows how to get audio and how much it's allowed to spend doing so.

Contract with host cog
----------------------
The host cog must provide::

    self.cache_manager   — MusicCacheManager
    self.db_manager      — Optional[DatabaseManager]  (for bandwidth tracking)
    self.logger          — logging.Logger

    async def _send_system_message(self, content: str) -> Optional[discord.Message]
        Send a text message in the active session's origin channel.

This mixin provides::

    self._acquire_source(track) -> Optional[PlayableSource]
    self._resolve_url(track, attempts) -> Optional[SourceResolutionResult]
    self._get_attempts(video_id) -> TrackAttempts
    self._clear_attempts(video_id) -> None
    self._clear_all_attempts() -> None
    self._delete_failed_residential_file(path) -> None

Module-level data types::

    TrackAttempts, PlayableSource
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.musicutils.music_auth import (
    SourceResolutionResult,
    resolve_track_source,
)
from utils.musicutils.music_data import (
    MAX_RESIDENTIAL_PLAYBACK_DURATION_SECONDS,
    Track,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spending policy constants
# ---------------------------------------------------------------------------

#: Maximum fresh-URL streaming attempts (each gets a new yt-dlp resolution).
MAX_DIRECT_PLAYS = 2

#: Maximum residential yt-dlp resolution attempts (separate from direct).
MAX_RESIDENTIAL_YTDLP = 1

#: Maximum full-file residential downloads per track.
MAX_RESIDENTIAL_DOWNLOADS = 3

#: Minimum seconds between paid residential download attempts.
RESIDENTIAL_MIN_DELAY = 2.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TrackAttempts:
    """Per-track retry state.  This is the entire state machine.

    The two-stage pipeline:

    * **Stage 1 (URL acquisition):** Can yt-dlp get us a playable URL?
      ``needs_residential_ytdlp`` is a sticky flag -- once direct yt-dlp fails,
      all subsequent URL requests go through the residential proxy.
    * **Stage 2 (Playback):** Can FFmpeg stream the URL or play a local file?
      ``direct_plays`` counts fresh-URL attempts; ``residential_downloads``
      counts paid file downloads.
    """
    # Stage 1
    needs_residential_ytdlp: bool = False
    unavailable: bool = False

    # Stage 2
    direct_plays: int = 0
    residential_downloads: int = 0

    # UX
    notified_residential: bool = False

    # Rate limiting
    last_residential_time: float = 0.0

    # Cleanup -- tracks the last residential file path so the cog can delete
    # it if FFmpeg fails on a corrupt download.
    last_residential_path: Optional[str] = None


@dataclass
class PlayableSource:
    """A concrete source that can be handed to ManagedPlayer."""
    url: Optional[str] = None
    http_headers: Optional[Dict[str, str]] = None
    local_path: Optional[str] = None
    prebuffered: Optional[Any] = None  # SeekableAudioSource when prebuffer validated
    residential_bytes: int = 0

    def cleanup(self) -> None:
        """Release any pre-validated FFmpeg source."""
        if self.prebuffered is not None:
            self.prebuffered.cleanup()
            self.prebuffered = None


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class SourceAcquisitionMixin:
    """Mixin that owns every code path capable of spending proxy money.

    See module docstring for the full contract.
    """

    # -- Stubs: provided by the host Music cog ---------------------------------
    cache_manager: Any
    db_manager: Any
    logger: Any

    async def _send_system_message(self, content: str) -> Optional[Any]: ...

    # -- State owned by this mixin ---------------------------------------------
    _track_attempts: Dict[str, TrackAttempts]

    # --------------------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------------------

    async def _acquire_source(
        self, track: Track, *, prefetch: bool = False,
    ) -> Optional[PlayableSource]:
        """Walk the priority chain and return the first playable source.

        Args:
            track: Track to acquire a source for.
            prefetch: If True, stop after free options (ambient cache, direct
                streaming, residential cache).  Never spends money on
                residential downloads.  The shared attempt counter still
                advances so live play knows what was already tried.

        Priority:
        1. Ambient cache (high-quality local files from playlist downloads)
        2. Fresh streaming URL via yt-dlp (direct, then residential if needed)
        3. Residential cache (lower-quality local file, free)
        4. Full file download via residential proxy (costs money, live only)

        Returns ``None`` when all options are exhausted.  The caller can
        inspect ``self._get_attempts(track.video_id).unavailable`` to choose
        between a SKIP and REMOVE prompt.
        """
        if not track.video_id:
            return None

        attempts = self._get_attempts(track.video_id)

        # ---- Priority 1: ambient cache (high quality) ------------------------
        local = self.cache_manager.get_any_local_path(
            track.video_id, residential_allowed=False,
        )
        if local:
            logger.info(f"[Acquisition] Ambient cache hit: {track.title}")
            return PlayableSource(local_path=local)

        # ---- Priority 2: stream via fresh URL --------------------------------
        if attempts.direct_plays < MAX_DIRECT_PLAYS:
            result = await self._resolve_url(track, attempts)
            if result is None:
                # Track confirmed unavailable -- caller will prompt REMOVE.
                return None
            if result.has_source and result.direct_url:
                attempts.direct_plays += 1
                logger.info(f"[Acquisition] Direct URL resolved: {track.title}")
                return PlayableSource(
                    url=result.direct_url,
                    http_headers=result.http_headers,
                )

        # ---- Priority 3: residential cache (free local file) -----------------
        residential_local = self.cache_manager.get_any_local_path(
            track.video_id, residential_allowed=True,
        )
        if residential_local:
            logger.info(f"[Acquisition] Residential cache hit: {track.title}")
            attempts.last_residential_path = residential_local
            return PlayableSource(local_path=residential_local)

        # Everything below here spends money.  Prefetch stops here.
        if prefetch:
            logger.info(
                f"[Acquisition] Free options exhausted in prefetch, "
                f"deferring to live play: {track.title}"
            )
            return None

        # Notify the user before we start spending on residential downloads.
        if not attempts.notified_residential:
            attempts.notified_residential = True
            await self._send_system_message(
                f"\U0001f504 Hmm, having some trouble with **{track.title}**... "
                f"Let me try another way!"
            )

        # ---- Duration policy gate (before spending money) --------------------
        if track.duration > MAX_RESIDENTIAL_PLAYBACK_DURATION_SECONDS:
            self.logger.info(
                f"[Acquisition] Refusing residential download — track too long: "
                f"{track.title} ({track.duration}s > {MAX_RESIDENTIAL_PLAYBACK_DURATION_SECONDS}s)"
            )
            attempts.unavailable = True
            return None

        # ---- Priority 4: residential file download ---------------------------
        while attempts.residential_downloads < MAX_RESIDENTIAL_DOWNLOADS:
            # Rate limit between paid download attempts.
            elapsed = time.time() - attempts.last_residential_time
            if elapsed < RESIDENTIAL_MIN_DELAY and attempts.last_residential_time > 0:
                await asyncio.sleep(RESIDENTIAL_MIN_DELAY - elapsed)
            attempts.last_residential_time = time.time()

            attempts.residential_downloads += 1
            logger.info(
                f"[Acquisition] Residential download attempt "
                f"{attempts.residential_downloads}/{MAX_RESIDENTIAL_DOWNLOADS}: {track.title}"
            )
            success, _err, bytes_dl, path = (
                await self.cache_manager.download_live_residential(track)
            )

            if success and path:
                if bytes_dl > 0 and self.db_manager:
                    await self.db_manager.increment_proxy_usage(bytes_dl)
                logger.info(
                    f"[Acquisition] Residential download complete: {track.title} "
                    f"({bytes_dl / 1024 / 1024:.2f} MB)"
                )
                attempts.last_residential_path = path
                return PlayableSource(local_path=path)

        # Exhausted.
        return None

    def _get_attempts(self, video_id: str) -> TrackAttempts:
        """Get or create per-track attempt state."""
        existing = self._track_attempts.get(video_id)
        if existing is None:
            existing = TrackAttempts()
            self._track_attempts[video_id] = existing
        return existing

    def _clear_attempts(self, video_id: str) -> None:
        """Clear attempt state for a finished or removed track."""
        self._track_attempts.pop(video_id, None)

    def _clear_all_attempts(self) -> None:
        """Wipe all per-track state.  Called on session teardown."""
        self._track_attempts.clear()

    def _delete_failed_residential_file(self, path: str) -> None:
        """Delete a residential cache file that failed FFmpeg playback.

        Without this, ``get_any_local_path`` finds the same broken file on
        the next retry, creating an infinite play-fail loop.
        """
        try:
            if os.path.exists(path):
                os.remove(path)
                self.logger.info(f"[Acquisition] Deleted failed residential file: {path}")
        except OSError as exc:
            self.logger.debug(f"[Acquisition] Could not delete residential file: {exc}")

    # --------------------------------------------------------------------------
    # Internal: Stage 1 URL acquisition
    # --------------------------------------------------------------------------

    async def _resolve_url(
        self, track: Track, attempts: TrackAttempts,
    ) -> Optional[SourceResolutionResult]:
        """Get a playable URL from yt-dlp, routing through residential if needed.

        This is Stage 1 only -- it determines whether yt-dlp can resolve the
        track at all, and from which network path.  It sets the sticky
        ``needs_residential_ytdlp`` flag if direct resolution fails.

        Returns:
            A resolution result with ``has_source=True`` and a URL, or a result
            with ``has_source=False`` if yt-dlp could not resolve.
            Returns ``None`` only when the track is confirmed unavailable
            (sets ``attempts.unavailable``).
        """
        if not attempts.needs_residential_ytdlp:
            result = await resolve_track_source(track, use_residential_ytdlp=False)
            if result.has_source:
                return result
            if result.unavailable:
                attempts.unavailable = True
                return None
            # 403 health tracking.
            if result.is_auth_failure:
                self._record_403_if_applicable()
            # Direct yt-dlp failed but track isn't gone -- flip the sticky flag.
            attempts.needs_residential_ytdlp = True

        # Residential yt-dlp.
        result = await resolve_track_source(track, use_residential_ytdlp=True)
        if result.has_source:
            return result
        if result.unavailable:
            attempts.unavailable = True
            return None

        # yt-dlp couldn't get a URL from either path.
        return result

    def _record_403_if_applicable(self) -> None:
        """Bump the global 403 counter and log if the threshold is reached."""
        from utils.musicutils.music_auth import get_youtube_auth_status
        auth = get_youtube_auth_status()
        if auth.record_403():
            self.logger.warning("[Acquisition] High 403 rate — check YouTube auth")

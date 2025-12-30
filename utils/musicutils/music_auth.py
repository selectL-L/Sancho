"""YouTube authentication and audio fetching with retry orchestration.

Handles:
- YouTube authentication detection (PO Token Server, Cookies)
- 403 error tracking and alerting
- AudioFetcher class: context-aware retry orchestration (Phase 12)

AudioFetcher Architecture:
- Cog owns buffer storage (knows playlist context for ambient cache)
- AudioFetcher owns retry strategy (tracks attempts per video_id)
- FetchContext enum communicates intent: PREFETCH (conservative) vs LIVE/RETRY (aggressive)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .music_data import Track

from .music_data import FetchContext
from .music_helpers import (
    YTDLP_OPTIONS,
    get_audio_url,
    get_residential_proxy_url,
    is_403_error,
    is_video_unavailable,
)


# ==========================================================================
# PO TOKEN SYSTEM CHECKS
# ==========================================================================


def _check_pot_plugin_installed() -> bool:
    """Check if the bgutil PO token plugin is installed via pip."""
    try:
        # Check via pip metadata (works for yt-dlp plugins that register via entry points)
        from importlib.metadata import distributions
        for dist in distributions():
            if dist.metadata.get('Name', '').lower() == 'bgutil-ytdlp-pot-provider':
                return True
        return False
    except Exception:
        return False


def _check_pot_server_running(port: int = 4416) -> bool:
    """Check if the POT HTTP server is responding.

    Args:
        port: The port to check (default 4416).

    Returns:
        True if server is responding, False otherwise.
    """
    import socket
    try:
        # Quick TCP check first
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(('127.0.0.1', port))
            return result == 0
    except Exception:
        return False


def _check_pot_provider_script() -> bool:
    """Check if the POT provider script exists (for setup detection)."""
    import config
    pot_script = getattr(config, 'POT_PROVIDER_PATH', None)
    return pot_script is not None and os.path.isfile(pot_script)


def _check_node_available() -> bool:
    """Check if Node.js is available in PATH."""
    return shutil.which('node') is not None


def _check_pot_system_functional() -> Tuple[bool, Optional[str]]:
    """Check if the PO token system can work.

    Checks for: pip plugin installed, Node.js available, provider script exists,
    and HTTP server responding.

    Returns:
        Tuple of (is_functional, error_message or status).
    """
    import config
    pot_port = getattr(config, 'POT_PROVIDER_PORT', 4416)

    # Check if server is already running (best case)
    if _check_pot_server_running(pot_port):
        return True, None

    # Server not running - diagnose why
    if not _check_pot_plugin_installed():
        return False, "pip plugin not installed"

    if not _check_node_available():
        return False, "Node.js not found"

    if not _check_pot_provider_script():
        return False, "Provider script not built"

    # Everything looks set up but server isn't running
    return False, "Server not running (will start with bot)"


# ==========================================================================
# YOUTUBE AUTH STATUS TRACKER
# ==========================================================================


class YouTubeAuthStatus:
    """Tracks YouTube authentication state and 403 error rates."""

    def __init__(self) -> None:
        self.auth_method: Optional[str] = None  # 'pot_server', 'cookies', or None
        self.auth_path: Optional[str] = None  # Path to cookie file if using cookies
        self.po_token: Optional[str] = None  # Manual PO token (legacy, rarely needed)
        self.cache_dir: Optional[str] = None  # yt-dlp cache directory
        self.pot_plugin_installed: bool = False  # bgutil pip plugin installed?
        self.pot_server_running: bool = False  # POT HTTP server responding?
        self.pot_provider_ready: bool = False  # Script built and ready?
        self.pot_plugin_error: Optional[str] = None  # Why plugin isn't working
        self.last_check: float = 0.0  # Timestamp of last auth file check
        self.check_interval: float = 300.0  # Re-check auth files every 5 minutes

        # 403 tracking for alerting
        self._403_timestamps: List[float] = []  # Recent 403 occurrences
        self._403_window: float = 300.0  # 5-minute sliding window
        self._403_threshold: int = 5  # Alert after 5 failures in window
        self._last_alert: float = 0.0  # Prevent alert spam
        self._alert_cooldown: float = 600.0  # 10 minutes between alerts

    def record_403(self) -> bool:
        """Records a 403 error and returns True if alert threshold reached.

        Returns:
            True if the 403 rate exceeds threshold and alert should be sent.
        """
        now = time.time()
        self._403_timestamps.append(now)

        # Prune old timestamps outside window
        cutoff = now - self._403_window
        self._403_timestamps = [t for t in self._403_timestamps if t > cutoff]

        # Check if we should alert
        if len(self._403_timestamps) >= self._403_threshold:
            if now - self._last_alert > self._alert_cooldown:
                self._last_alert = now
                return True
        return False

    def get_403_rate(self) -> tuple[int, float]:
        """Returns (count, window_seconds) of recent 403 errors."""
        now = time.time()
        cutoff = now - self._403_window
        self._403_timestamps = [t for t in self._403_timestamps if t > cutoff]
        return len(self._403_timestamps), self._403_window

    def reset_403_tracking(self) -> None:
        """Clears 403 history (e.g., after auth refresh)."""
        self._403_timestamps.clear()


# Global auth status tracker
_youtube_auth = YouTubeAuthStatus()


def get_youtube_auth_status() -> YouTubeAuthStatus:
    """Returns the global YouTube auth status tracker."""
    return _youtube_auth


# ==========================================================================
# AUTHENTICATION DETECTION
# ==========================================================================


def _detect_youtube_auth() -> Dict[str, Any]:
    """Detects available YouTube authentication and returns yt-dlp options.

    Checks for PO token HTTP server first (auto-generates tokens), then cookie file.
    Results are cached for 5 minutes to avoid excessive filesystem access.

    Priority: PO Token Server > Cookies > No auth

    Returns:
        Dict of yt-dlp options to merge with YTDLP_OPTIONS.
    """
    # Import here to avoid circular dependency
    import config

    now = time.time()
    pot_port = getattr(config, 'POT_PROVIDER_PORT', 4416)

    # Get ytdlp cache directory and ensure it exists
    ytdlp_cache = getattr(config, 'YTDLP_CACHE_PATH', None)
    if ytdlp_cache:
        os.makedirs(ytdlp_cache, exist_ok=True)
        _youtube_auth.cache_dir = ytdlp_cache

    # Always check POT system status (quick checks)
    _youtube_auth.pot_plugin_installed = _check_pot_plugin_installed()
    _youtube_auth.pot_server_running = _check_pot_server_running(pot_port)
    _youtube_auth.pot_provider_ready = _check_pot_provider_script()

    # Determine POT system error message
    if _youtube_auth.pot_server_running:
        _youtube_auth.pot_plugin_error = None  # Working!
    elif not _youtube_auth.pot_plugin_installed:
        _youtube_auth.pot_plugin_error = "pip plugin not installed"
    elif not _check_node_available():
        _youtube_auth.pot_plugin_error = "Node.js not found"
    elif not _youtube_auth.pot_provider_ready:
        _youtube_auth.pot_plugin_error = "Provider script not built"
    else:
        _youtube_auth.pot_plugin_error = "Server not running"

    # Use cached result if recent enough (for cookie file checks)
    if now - _youtube_auth.last_check < _youtube_auth.check_interval and _youtube_auth.auth_method is not None:
        opts: Dict[str, Any] = {}
        if ytdlp_cache:
            opts['cachedir'] = ytdlp_cache
        if _youtube_auth.auth_method == 'pot_server':
            # Server handles everything automatically
            return opts
        elif _youtube_auth.auth_method == 'cookies' and _youtube_auth.auth_path:
            opts['cookiefile'] = _youtube_auth.auth_path
            if _youtube_auth.po_token:
                opts['extractor_args'] = {'youtube': {'po_token': [f'web+{_youtube_auth.po_token}']}}
            return opts
        return opts if ytdlp_cache else {}

    _youtube_auth.last_check = now
    auth_opts: Dict[str, Any] = {}

    # Always set cache directory if configured
    if ytdlp_cache:
        auth_opts['cachedir'] = ytdlp_cache

    # Check for manual PO token file (legacy, used with cookies)
    po_token_path = getattr(config, 'YOUTUBE_PO_TOKEN_PATH', None)
    if po_token_path and os.path.isfile(po_token_path):
        try:
            with open(po_token_path, 'r', encoding='utf-8') as f:
                po_token = f.read().strip()
                if po_token:
                    _youtube_auth.po_token = po_token
                    logging.getLogger('music_auth').debug(f"YouTube auth: Loaded manual PO token from {po_token_path}")
        except Exception as e:
            logging.getLogger('music_auth').warning(f"Failed to read PO token: {e}")
            _youtube_auth.po_token = None
    else:
        _youtube_auth.po_token = None

    # Priority 1: PO Token Server (if running)
    # The server auto-generates tokens - no extra yt-dlp options needed, plugin connects automatically
    if _youtube_auth.pot_server_running:
        _youtube_auth.auth_method = 'pot_server'
        _youtube_auth.auth_path = None
        logging.getLogger('music_auth').debug("YouTube auth: Using PO token server (auto-generation)")
        return auth_opts

    # Priority 2: Cookie file (fallback)
    cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
    if cookie_path and os.path.isfile(cookie_path):
        _youtube_auth.auth_method = 'cookies'
        _youtube_auth.auth_path = cookie_path
        auth_opts['cookiefile'] = cookie_path
        # Add manual PO token if available (helps with datacenter IPs)
        if _youtube_auth.po_token:
            auth_opts['extractor_args'] = {'youtube': {'po_token': [f'web+{_youtube_auth.po_token}']}}
            logging.getLogger('music_auth').debug("YouTube auth: Using cookies + manual PO token")
        else:
            logging.getLogger('music_auth').debug(f"YouTube auth: Using cookies from {cookie_path}")
        return auth_opts

    # No auth available
    _youtube_auth.auth_method = None
    _youtube_auth.auth_path = None
    logging.getLogger('music_auth').debug("YouTube auth: No authentication configured")
    return auth_opts


def get_ytdlp_options(extra_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Returns yt-dlp options with authentication merged in.

    Args:
        extra_opts: Additional options to merge (overrides base options).

    Returns:
        Complete yt-dlp options dict ready to use.
    """
    opts = {**YTDLP_OPTIONS}
    auth_opts = _detect_youtube_auth()
    opts.update(auth_opts)
    if extra_opts:
        opts.update(extra_opts)

    # Log what auth method is being used for debugging
    logger = logging.getLogger('music_auth')
    if _youtube_auth.auth_method == 'pot_server':
        logger.debug(f"yt-dlp: Using POT server (pot_server_running={_youtube_auth.pot_server_running})")
    elif _youtube_auth.auth_method == 'cookies':
        logger.debug(f"yt-dlp: Using cookies from {_youtube_auth.auth_path}")
    else:
        logger.debug("yt-dlp: No auth method active")

    return opts


# ==========================================================================
# AUDIO FETCHER WITH RETRY ORCHESTRATION
# ==========================================================================


@dataclass
class AudioFetchResult:
    """Result from AudioFetcher.fetch().

    Contains everything the Music cog needs to play audio or handle failures.
    Enriched with failure details so cog knows what happened.
    """
    success: bool
    url: Optional[str] = None  # Direct streaming URL
    http_headers: Optional[Dict[str, str]] = None
    local_path: Optional[str] = None  # Residential/ambient cache path
    thumbnail: Optional[str] = None
    thumbnail_needs_crop: bool = False
    thumbnail_bytes: Optional[bytes] = None  # Pre-fetched thumbnail

    # Failure details
    is_unavailable: bool = False  # Track permanently gone (remove from playlist)
    is_auth_failure: bool = False  # yt-dlp 403 - auth issue, not stale URL
    residential_used: bool = False  # Did we use residential proxy?
    residential_bytes: int = 0  # Bytes downloaded (for cost tracking)
    error: Optional[str] = None

    def __bool__(self) -> bool:
        """Allow `if result:` to check success."""
        return self.success


@dataclass
class TrackFetchState:
    """Per-track state tracked by AudioFetcher.

    Tracks what has been attempted for a specific video_id so that
    subsequent fetch() calls know where to pick up.
    """
    video_id: str
    direct_attempts: int = 0
    auth_failed: bool = False  # yt-dlp 403 (not FFmpeg stale)
    residential_attempts: int = 0


@dataclass
class AudioFetcher:
    """Context-aware audio URL fetcher with retry orchestration.

    Design (Phase 12):
    - Cog owns buffer storage (knows playlist context)
    - AudioFetcher owns retry strategy (tracks attempts per video_id)
    - FetchContext tells AudioFetcher how aggressive to be:
        - PREFETCH: Conservative - stops at direct failure, no residential
        - LIVE: Aggressive - full retry including residential
        - RETRY: Aggressive - FFmpeg failed, need fresh URL or residential

    Usage:
        # Prefetch (background, conservative)
        result = await fetcher.fetch(track, FetchContext.PREFETCH)

        # Live play (aggressive, full retry)
        result = await fetcher.fetch(track, FetchContext.LIVE)

        # Retry after FFmpeg 403 (aggressive)
        result = await fetcher.fetch(track, FetchContext.RETRY)

    The cog stores results and decides when to use them. AudioFetcher
    just executes the fetch with appropriate retry strategy.
    """
    # Class-level constants
    DIRECT_MAX: ClassVar[int] = 2  # Max direct (yt-dlp) attempts
    RESIDENTIAL_MAX: ClassVar[int] = 3  # Max residential proxy attempts
    RESIDENTIAL_MIN_DELAY: ClassVar[float] = 2.0  # Min seconds between residential

    # Instance fields
    cache_manager: Any = field(repr=False)  # MusicCacheManager
    logger: Any = field(repr=False)

    # Per-track state (keyed by video_id)
    _track_states: Dict[str, TrackFetchState] = field(default_factory=dict, init=False)

    # Cross-track rate limiting for residential
    _last_residential_time: float = field(default=0.0, init=False)

    def _get_state(self, video_id: str) -> TrackFetchState:
        """Get or create state for a track."""
        if video_id not in self._track_states:
            self._track_states[video_id] = TrackFetchState(video_id=video_id)
        return self._track_states[video_id]

    def clear_state(self, video_id: Optional[str] = None) -> None:
        """Clear state for a track (or all tracks).

        Args:
            video_id: Specific track to clear, or None to clear all.
        """
        if video_id:
            self._track_states.pop(video_id, None)
        else:
            self._track_states.clear()

    def reset(self) -> None:
        """Reset all fetcher state. Alias for clear_state()."""
        self.clear_state()

    async def fetch(
        self,
        track: 'Track',
        context: FetchContext
    ) -> AudioFetchResult:
        """Get playable audio URL or path for a track.

        Args:
            track: Track to get audio for.
            context: PREFETCH (conservative), LIVE (aggressive), or RETRY (aggressive).

        Returns:
            AudioFetchResult with url/local_path on success, failure details otherwise.
        """
        if not track.video_id:
            return AudioFetchResult(
                success=False,
                error="Track has no video_id"
            )

        state = self._get_state(track.video_id)

        self.logger.debug(
            f"[AudioFetcher] fetch({context.value}) for {track.title[:30]}... "
            f"(direct={state.direct_attempts}, residential={state.residential_attempts})"
        )

        # Check residential cache first (permanent, proxy-downloaded)
        cached = self.cache_manager.check_residential(track.video_id)
        if cached:
            self.logger.info(f"[AudioFetcher] Residential cache hit: {track.title}")
            return AudioFetchResult(success=True, local_path=cached)

        # Check all ambient cache locations (playlists + orphaned)
        # If we hit an orphanhed file, we can use it, but this isn't guranteed
        # considering that orphanhed files have a limited lifetime.
        any_cached = self.cache_manager.get_any_local_path(track.video_id)
        if any_cached:
            self.logger.info(f"[AudioFetcher] Ambient Cache hit: {track.title}")
            return AudioFetchResult(success=True, local_path=any_cached)

        # Try direct if under limit
        if state.direct_attempts < self.DIRECT_MAX:
            state.direct_attempts += 1
            self.logger.info(
                f"[AudioFetcher] Direct fetch attempt {state.direct_attempts}/{self.DIRECT_MAX}: {track.title}"
            )
            result = await self._try_direct(track)

            if result.success:
                return result

            if result.is_auth_failure:
                state.auth_failed = True
                # Track 403 for alerting
                should_alert = _youtube_auth.record_403()
                if should_alert:
                    self.logger.warning(
                        "[AudioFetcher] High 403 rate detected - check YouTube auth"
                    )

            # PREFETCH stops here on failure
            if context == FetchContext.PREFETCH:
                self.logger.info(
                    "[AudioFetcher] PREFETCH mode - stopping (will retry LIVE if played)"
                )
                return result

        # LIVE/RETRY continue to residential
        if context in (FetchContext.LIVE, FetchContext.RETRY):
            return await self._try_residential(track, state)

        # PREFETCH with direct exhausted - return failure
        return AudioFetchResult(
            success=False,
            is_auth_failure=state.auth_failed,
            error="Direct attempts exhausted (PREFETCH mode)"
        )

    async def _try_direct(self, track: 'Track') -> AudioFetchResult:
        """Attempt yt-dlp fetch."""
        ydl_opts = get_ytdlp_options({'extract_flat': False})

        try:
            url, is_unavailable, thumbnail, needs_crop, headers = await get_audio_url(
                track, self.logger, ydl_opts
            )

            if url:
                self.logger.info(f"[AudioFetcher] Direct fetch success: {track.title}")
                return AudioFetchResult(
                    success=True,
                    url=url,
                    http_headers=headers,
                    thumbnail=thumbnail,
                    thumbnail_needs_crop=needs_crop,
                )

            # No URL but no exception - likely unavailable or extraction failed
            is_auth = not is_unavailable  # If not unavailable, assume auth issue
            self.logger.info(
                f"[AudioFetcher] Direct fetch failed: {track.title} "
                f"(unavailable={is_unavailable}, auth_issue={is_auth})"
            )
            return AudioFetchResult(
                success=False,
                is_unavailable=is_unavailable,
                is_auth_failure=is_auth,
                error="Failed to get audio URL"
            )

        except Exception as e:
            is_auth = is_403_error(e)
            is_gone = is_video_unavailable(e)
            self.logger.info(
                f"[AudioFetcher] Direct fetch exception: {track.title} "
                f"(403={is_auth}, unavailable={is_gone}, error={str(e)[:100]})"
            )
            return AudioFetchResult(
                success=False,
                is_auth_failure=is_auth,
                is_unavailable=is_gone,
                error=str(e)
            )

    async def _try_residential(
        self,
        track: 'Track',
        state: TrackFetchState
    ) -> AudioFetchResult:
        """Attempt residential proxy download."""
        # Check attempt limit
        if state.residential_attempts >= self.RESIDENTIAL_MAX:
            self.logger.debug(
                f"[AudioFetcher] Residential exhausted ({state.residential_attempts}/{self.RESIDENTIAL_MAX})"
            )
            return AudioFetchResult(
                success=False,
                residential_used=True,
                error="Residential retries exhausted"
            )

        # Check if proxy is configured
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            self.logger.warning("[AudioFetcher] Residential proxy not configured")
            return AudioFetchResult(
                success=False,
                error="Residential proxy not configured"
            )

        # Rate limiting between residential attempts (cross-track)
        elapsed = time.time() - self._last_residential_time
        if elapsed < self.RESIDENTIAL_MIN_DELAY and self._last_residential_time > 0:
            delay = self.RESIDENTIAL_MIN_DELAY - elapsed
            self.logger.debug(f"[AudioFetcher] Rate limiting: waiting {delay:.1f}s")
            await asyncio.sleep(delay)

        state.residential_attempts += 1
        self._last_residential_time = time.time()

        self.logger.info(
            f"[AudioFetcher] Residential download {state.residential_attempts}/{self.RESIDENTIAL_MAX}: {track.title}"
        )

        # Download via cache_manager
        success, error_msg, bytes_downloaded, cached_path = await self.cache_manager.download_residential(
            track
        )

        if success and cached_path:
            self.logger.info(
                f"[AudioFetcher] Residential success: {track.title} "
                f"({bytes_downloaded / 1024 / 1024:.2f} MB)"
            )
            return AudioFetchResult(
                success=True,
                local_path=cached_path,
                residential_used=True,
                residential_bytes=bytes_downloaded,
            )

        self.logger.info(
            f"[AudioFetcher] Residential failed: {track.title} - {error_msg}"
        )
        return AudioFetchResult(
            success=False,
            residential_used=True,
            error=error_msg or "Residential download failed"
        )

    def get_state_info(self, video_id: str) -> Optional[TrackFetchState]:
        """Get state info for debugging/logging."""
        return self._track_states.get(video_id)

    @property
    def active_tracks(self) -> int:
        """Number of tracks with state being tracked."""
        return len(self._track_states)

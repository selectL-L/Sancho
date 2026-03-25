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

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Optional, TYPE_CHECKING

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

logger = logging.getLogger(__name__)


# ==========================================================================
# PO TOKEN SYSTEM CHECKS
# ==========================================================================


def _check_pot_server_running(port: int = 4416) -> bool:
    """Check if the POT server is accepting TCP connections.

    This is the single probe function for POT server liveness.

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
    except (OSError, socket.error) as e:
        logger.debug(f"POT server TCP check failed: {e}")
        return False


# Cached result for plugin installation check (cannot change at runtime)
_pot_plugin_installed_cache: Optional[bool] = None


def _check_pot_system_functional(auth_status: 'YouTubeAuthStatus') -> None:
    """Run all POT system diagnostics and update auth_status in-place.

    Checks (in order): port configured, server responding, plugin installed,
    binary exists. Sets pot_server_running, pot_plugin_installed,
    pot_provider_ready, and pot_plugin_error on the auth_status object.

    Plugin installation is cached after the first successful detection
    since packages aren't installed/uninstalled at runtime.

    Args:
        auth_status: The YouTubeAuthStatus instance to update.
    """
    global _pot_plugin_installed_cache
    import config

    pot_port = getattr(config, 'POT_PROVIDER_PORT', None)

    # If port not configured, POT system is disabled
    if pot_port is None:
        auth_status.pot_plugin_installed = False
        auth_status.pot_server_running = False
        auth_status.pot_provider_ready = False
        auth_status.pot_plugin_error = "Port not configured"
        return

    # TCP probe
    running = _check_pot_server_running(pot_port)
    auth_status.update_pot_health(running)

    # Plugin installed check (cached after first True)
    if _pot_plugin_installed_cache is True:
        auth_status.pot_plugin_installed = True
    else:
        try:
            from importlib.metadata import distributions
            found = any(
                dist.metadata.get('Name', '').lower() == 'bgutil-ytdlp-pot-provider'
                for dist in distributions()
            )
            auth_status.pot_plugin_installed = found
            if found:
                _pot_plugin_installed_cache = True
        except Exception as e:
            logger.debug(f"PO token plugin check failed: {e}")
            auth_status.pot_plugin_installed = False

    # Binary exists check
    pot_binary = getattr(config, 'POT_PROVIDER_PATH', None)
    auth_status.pot_provider_ready = pot_binary is not None and os.path.isfile(pot_binary)

    # Determine error message
    if auth_status.pot_server_running:
        auth_status.pot_plugin_error = None
    elif not auth_status.pot_plugin_installed:
        auth_status.pot_plugin_error = "yt-dlp plugin not installed"
    elif not auth_status.pot_provider_ready:
        auth_status.pot_plugin_error = "Binary not found"
    else:
        auth_status.pot_plugin_error = "Server not running"


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
        self.pot_provider_ready: bool = False  # Binary exists and ready?
        self.pot_plugin_error: Optional[str] = None  # Why plugin isn't working
        self.last_check: float = 0.0  # Timestamp of last auth file check
        self.check_interval: float = 300.0  # Re-check auth files every 5 minutes
        self.last_pot_confirmed: float = 0.0  # Timestamp of last confirmed POT health

        # Internal state for transition detection
        self._was_pot_healthy: Optional[bool] = None

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

    def update_pot_health(self, running: bool) -> Optional[bool]:
        """Update POT server running state and detect transitions.

        Args:
            running: Whether the server is currently responding.

        Returns:
            True if server just died (was healthy, now dead).
            False if server just recovered (was dead, now healthy).
            None if no transition (state unchanged or first check).
        """
        self.pot_server_running = running
        if running:
            self.last_pot_confirmed = time.time()

        previous = self._was_pot_healthy
        self._was_pot_healthy = running

        if previous is None:
            return None  # First check, no transition
        if previous and not running:
            return True  # Just died
        if not previous and running:
            return False  # Just recovered
        return None  # No change


# Global auth status tracker
_youtube_auth = YouTubeAuthStatus()


def get_youtube_auth_status() -> YouTubeAuthStatus:
    """Returns the global YouTube auth status tracker."""
    return _youtube_auth


# ==========================================================================
# AUTHENTICATION DETECTION
# ==========================================================================


def detect_youtube_auth(reason: str = 'periodic') -> None:
    """Detects available YouTube authentication and updates global state.

    Runs a fresh POT system probe and checks for cookie/PO token files.
    Cookie file checks are cached for 5 minutes to avoid excessive
    filesystem access. POT checks always run fresh (the caller decides
    when to call this).

    Priority: PO Token Server > Cookies > No auth

    Args:
        reason: Why this probe was triggered (for logging). Common values:
            'status_command', 'cookie_upload', 'cookie_info'.
    """
    logger.debug(f"YouTube auth probe triggered (reason={reason})")
    # Import here to avoid circular dependency
    import config

    now = time.time()

    # Ensure cache directory exists
    ytdlp_cache = getattr(config, 'YTDLP_CACHE_PATH', None)
    if ytdlp_cache:
        os.makedirs(ytdlp_cache, exist_ok=True)
        _youtube_auth.cache_dir = ytdlp_cache

    # Fresh POT system probe
    _check_pot_system_functional(_youtube_auth)

    # Check for manual PO token file (legacy, used with cookies)
    # Only re-check files if cache interval has elapsed
    if now - _youtube_auth.last_check >= _youtube_auth.check_interval or _youtube_auth.auth_method is None:
        _youtube_auth.last_check = now

        po_token_path = getattr(config, 'YOUTUBE_PO_TOKEN_PATH', None)
        if po_token_path and os.path.isfile(po_token_path):
            try:
                with open(po_token_path, 'r', encoding='utf-8') as f:
                    po_token = f.read().strip()
                    if po_token:
                        _youtube_auth.po_token = po_token
                        logger.debug(f"YouTube auth: Loaded manual PO token from {po_token_path}")
            except Exception as e:
                logger.warning(f"Failed to read PO token: {e}")
                _youtube_auth.po_token = None
        else:
            _youtube_auth.po_token = None

    # Determine auth method
    # Priority 1: PO Token Server (if running)
    # The server auto-generates tokens - no extra yt-dlp options needed, plugin connects automatically
    if _youtube_auth.pot_server_running:
        _youtube_auth.auth_method = 'pot_server'
        _youtube_auth.auth_path = None
        logger.debug("YouTube auth: Using PO token server (auto-generation)")
        logger.info("[Auth] YouTube auth method selected: pot_server (auth_path=none, pot_server=True)")
        return

    # Priority 2: Cookie file (fallback)
    cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
    if cookie_path and os.path.isfile(cookie_path):
        _youtube_auth.auth_method = 'cookies'
        _youtube_auth.auth_path = cookie_path
        if _youtube_auth.po_token:
            logger.debug("YouTube auth: Using cookies + manual PO token")
        else:
            logger.debug(f"YouTube auth: Using cookies from {cookie_path}")
        logger.info(f"[Auth] YouTube auth method selected: cookies (auth_path={cookie_path}, pot_server=False)")
        return

    # No auth available
    _youtube_auth.auth_method = None
    _youtube_auth.auth_path = None
    logger.debug("YouTube auth: No authentication configured")
    logger.info("[Auth] YouTube auth method selected: none (auth_path=none, pot_server=False)")


def get_ytdlp_options(extra_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Returns yt-dlp options with authentication merged in.

    Reads cached auth state — does not probe. State is kept current by:
    - Startup initialization (detect_youtube_auth in cog_ready)
    - Background health watchdog (every 30 minutes)
    - Explicit calls (status command, cookie upload)

    Args:
        extra_opts: Additional options to merge (overrides base options).

    Returns:
        Complete yt-dlp options dict ready to use.
    """
    opts = {**YTDLP_OPTIONS}

    # Add cache directory if configured
    if _youtube_auth.cache_dir:
        opts['cachedir'] = _youtube_auth.cache_dir

    # Add auth based on cached state
    if _youtube_auth.auth_method == 'pot_server':
        # Plugin handles everything automatically, no extra options needed
        pass
    elif _youtube_auth.auth_method == 'cookies' and _youtube_auth.auth_path:
        opts['cookiefile'] = _youtube_auth.auth_path
        if _youtube_auth.po_token:
            opts['extractor_args'] = {'youtube': {'po_token': [f'web+{_youtube_auth.po_token}']}}

    if extra_opts:
        opts.update(extra_opts)

    logger.debug(
        f"yt-dlp options: auth={_youtube_auth.auth_method}, "
        f"pot_running={_youtube_auth.pot_server_running}"
    )
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
    thumbnail_is_square: bool = False
    thumbnail_bytes: Optional[bytes] = None  # Pre-fetched thumbnail

    # Failure details
    is_unavailable: bool = False  # Track permanently gone (remove from playlist)
    is_auth_failure: bool = False  # yt-dlp 403 - auth issue, not stale URL
    residential_used: bool = False  # Did we use residential proxy?
    residential_bytes: int = 0  # Bytes downloaded (for cost tracking)
    error: Optional[str] = None

    # Prebuffered source for instant playback (Phase 5)
    # When prefetch validates a URL, it stores the live FFmpeg source here.
    # The source has audio buffered and is ready for immediate playback.
    # Caller must call cleanup() if not using the prebuffered source.
    prebuffered_source: Optional[Any] = None  # SeekableAudioSource (Any to avoid circular import)

    # FFmpeg validation error (if prebuffer failed)
    # Tells LIVE fetch what went wrong so it can retry smartly
    ffmpeg_error_type: Optional[str] = None  # AudioErrorType.value

    def __bool__(self) -> bool:
        """Allow `if result:` to check success."""
        return self.success

    def cleanup(self) -> None:
        """Clean up prebuffered source if not used.

        Call this when:
        - Prefetch is invalidated (playlist changed)
        - Prefetch failed and source needs cleanup
        - Result is being discarded
        """
        if self.prebuffered_source is not None:
            buffered_secs = getattr(self.prebuffered_source, 'buffered_seconds', 0.0)
            frame_count = len(getattr(self.prebuffered_source, '_prebuffer', []))
            # Estimate memory: ~3840 bytes per frame (20ms of stereo 48kHz audio)
            mem_mb = (frame_count * 3840) / (1024 * 1024)
            logger.info(
                f"[AudioFetchResult] Cleaning up prebuffered source: "
                f"{buffered_secs:.1f}s buffered, {frame_count} frames (~{mem_mb:.1f}MB)"
            )
            self.prebuffered_source.cleanup()
            self.prebuffered_source = None


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

    Residential Callback:
        Set `on_residential_attempt` to receive notification BEFORE residential
        proxy is attempted. This allows the cog to notify users that an
        alternative method is being tried. Signature: async def callback(track_title: str)
    """
    # Class-level constants
    DIRECT_MAX: ClassVar[int] = 2  # Max direct (yt-dlp) attempts
    RESIDENTIAL_MAX: ClassVar[int] = 3  # Max residential proxy attempts
    RESIDENTIAL_MIN_DELAY: ClassVar[float] = 2.0  # Min seconds between residential

    # Instance fields
    cache_manager: Any = field(repr=False)  # MusicCacheManager

    # Optional callback fired BEFORE residential proxy attempt (not after)
    # Signature: async def callback(track_title: str) -> None
    on_residential_attempt: Optional[Callable[[str], Awaitable[None]]] = field(default=None, repr=False)

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

        Priority order (quality-first strategy):
        1. Ambient cache - High quality local files (playlist downloads)
        2. YouTube direct - Best quality streaming
        3. Residential cache - Lower quality fallback (already downloaded)
        4. Residential download - Last resort, costs bandwidth

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

        logger.debug(
            f"[AudioFetcher] fetch({context.value}) for {track.title[:30]}... "
            f"(direct={state.direct_attempts}, residential={state.residential_attempts})"
        )

        # ---------------------------------------------------------------------
        # Priority 1: Ambient cache (HIGH QUALITY)
        # Check playlist downloads and orphaned files first - these are the
        # best quality local files we have. Orphaned files have limited lifetime
        # but are still usable while they exist.
        # ---------------------------------------------------------------------
        any_cached = self.cache_manager.get_any_local_path(
            track.video_id,
            residential_allowed=False,
        )
        if any_cached:
            logger.info(f"[AudioFetcher] Ambient cache hit: {track.title}")
            self.clear_state(track.video_id)  # Clean up - no retry state needed
            return AudioFetchResult(success=True, local_path=any_cached)

        # ---------------------------------------------------------------------
        # Priority 2: YouTube direct streaming (BEST QUALITY)
        # Try to stream directly from YouTube - this gives the best audio
        # quality without using local storage or proxy bandwidth.
        # ---------------------------------------------------------------------
        if state.direct_attempts < self.DIRECT_MAX:
            state.direct_attempts += 1
            logger.info(
                f"[AudioFetcher] Direct fetch attempt {state.direct_attempts}/{self.DIRECT_MAX}: {track.title}"
            )
            result = await self._try_direct(track)

            if result.success:
                # PREFETCH: Keep state - URL might go stale before playback
                # LIVE: Clear state - first play, fresh start
                # RETRY: Keep state - counter must accumulate across FFmpeg
                #   rejections (yt-dlp "succeeds" but URL may 403 at FFmpeg
                #   level). Without this, the retry loop is infinite.
                if context == FetchContext.LIVE:
                    self.clear_state(track.video_id)
                return result

            # Unavailable video - no point checking cache or trying residential
            if result.is_unavailable:
                self.clear_state(track.video_id)
                return result

            if result.is_auth_failure:
                state.auth_failed = True
                # Track 403 for alerting
                should_alert = _youtube_auth.record_403()
                if should_alert:
                    logger.warning(
                        "[AudioFetcher] High 403 rate detected - check YouTube auth"
                    )

        # ---------------------------------------------------------------------
        # Priority 3: Residential cache (LOWER QUALITY FALLBACK)
        # If direct failed, check if we have a residential-downloaded file.
        # These are lower quality but instant - no download needed.
        # Note: No "having trouble" message here - cache hit is instant,
        # we don't want users expecting residential downloads to be fast.
        # ---------------------------------------------------------------------
        cached = self.cache_manager.get_any_local_path(
            track.video_id,
            residential_allowed=True,
        )
        if cached:
            logger.info(f"[AudioFetcher] Residential cache hit: {track.title}")
            self.clear_state(track.video_id)  # Clean up - no retry state needed
            return AudioFetchResult(success=True, local_path=cached)

        # ---------------------------------------------------------------------
        # PREFETCH stops here - we've checked all local/free sources.
        # Don't spend proxy bandwidth on speculative prefetching.
        # ---------------------------------------------------------------------
        if context == FetchContext.PREFETCH:
            logger.info(
                "[AudioFetcher] PREFETCH mode - no local cache, stopping (will retry LIVE if played)"
            )
            return AudioFetchResult(
                success=False,
                is_auth_failure=state.auth_failed,
                error="No local cache available (PREFETCH mode)"
            )

        # ---------------------------------------------------------------------
        # Priority 4: Residential proxy download (LAST RESORT)
        # LIVE/RETRY contexts only. This costs proxy bandwidth, so we only
        # do it when the track is actually being played, not for prefetch.
        # The "having trouble" notification fires inside _try_residential().
        # ---------------------------------------------------------------------
        if context in (FetchContext.LIVE, FetchContext.RETRY):
            return await self._try_residential(track, state)

        # Shouldn't reach here, but handle gracefully
        logger.error(
            f"[AudioFetcher] All fetch strategies exhausted for {track.title} — {track.artist}"
        )
        return AudioFetchResult(
            success=False,
            is_auth_failure=state.auth_failed,
            error="All fetch methods exhausted"
        )

    async def _try_direct(self, track: 'Track') -> AudioFetchResult:
        """Attempt yt-dlp fetch."""
        ydl_opts = get_ytdlp_options({'extract_flat': False})

        try:
            result = await get_audio_url(track, ydl_opts)

            if result.success:
                logger.info(f"[AudioFetcher] Direct fetch success: {track.title}")
                return AudioFetchResult(
                    success=True,
                    url=result.url,
                    http_headers=result.http_headers,
                    thumbnail=result.thumbnail,
                    thumbnail_is_square=result.thumbnail_is_square,
                )

            # No URL but no exception - likely unavailable or extraction failed
            is_auth = not result.is_unavailable  # If not unavailable, assume auth issue
            logger.info(
                f"[AudioFetcher] Direct fetch failed: {track.title} "
                f"(unavailable={result.is_unavailable}, auth_issue={is_auth})"
            )
            return AudioFetchResult(
                success=False,
                is_unavailable=result.is_unavailable,
                is_auth_failure=is_auth,
                error=result.error or "Failed to get audio URL"
            )

        except Exception as e:
            is_auth = is_403_error(e)
            is_gone = is_video_unavailable(e)
            logger.info(
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
            logger.error(
                f"[AudioFetcher] Residential proxy failed — "
                f"escalated to paid residential and it STILL failed "
                f"({state.residential_attempts}/{self.RESIDENTIAL_MAX} attempts)"
            )
            return AudioFetchResult(
                success=False,
                residential_used=True,
                error="Residential retries exhausted"
            )

        # Check if proxy is configured
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            logger.warning("[AudioFetcher] Residential proxy not configured")
            return AudioFetchResult(
                success=False,
                error="Residential proxy not configured"
            )

        # Rate limiting between residential attempts (cross-track)
        elapsed = time.time() - self._last_residential_time
        if elapsed < self.RESIDENTIAL_MIN_DELAY and self._last_residential_time > 0:
            delay = self.RESIDENTIAL_MIN_DELAY - elapsed
            logger.debug(f"[AudioFetcher] Rate limiting: waiting {delay:.1f}s")
            await asyncio.sleep(delay)

        state.residential_attempts += 1
        self._last_residential_time = time.time()

        logger.info(
            f"[AudioFetcher] Residential download {state.residential_attempts}/{self.RESIDENTIAL_MAX}: {track.title}"
        )

        # Notify cog BEFORE attempting residential (so user sees "trying another way" BEFORE success/failure)
        if self.on_residential_attempt and state.residential_attempts == 1:
            try:
                await self.on_residential_attempt(track.title)
            except Exception as e:
                logger.debug(f"[AudioFetcher] Residential callback error: {e}")

        # Download via cache_manager
        success, error_msg, bytes_downloaded, cached_path = await self.cache_manager.download_live_residential(
            track
        )

        if success and cached_path:
            logger.info(
                f"[AudioFetcher] Residential success: {track.title} "
                f"({bytes_downloaded / 1024 / 1024:.2f} MB)"
            )
            self.clear_state(track.video_id)  # Clean up - fetch succeeded
            return AudioFetchResult(
                success=True,
                local_path=cached_path,
                residential_used=True,
                residential_bytes=bytes_downloaded,
            )

        logger.info(
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

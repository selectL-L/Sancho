"""YouTube authentication helpers and low-level source resolution.

Handles:
- YouTube authentication detection (PO Token Server, cookies)
- 403 error tracking and alerting
- yt-dlp option construction for authenticated extraction
- Single-attempt source resolution for the track coordinator
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .music_data import Track

from .music_helpers import (
    YTDLP_OPTIONS,
    get_audio_url,
    get_residential_proxy_url,
    is_403_error,
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


@dataclass
class SourceResolutionResult:
    """Result from a single yt-dlp resolution attempt."""

    has_source: bool
    direct_url: Optional[str] = None
    http_headers: Optional[Dict[str, str]] = None
    thumbnail: Optional[str] = None
    thumbnail_is_square: bool = False
    unavailable: bool = False
    is_auth_failure: bool = False
    summary: Optional[str] = None


async def resolve_track_source(
    track: 'Track',
    *,
    use_residential_ytdlp: bool,
) -> SourceResolutionResult:
    """Resolve a fresh direct media source for a track.

    This helper does not own retry policy. It performs a single yt-dlp
    extraction attempt using either the normal network path or the residential
    proxy path and reports whether a source was found.

    Args:
        track: Track to resolve.
        use_residential_ytdlp: Whether to route yt-dlp through the configured
            residential proxy.

    Returns:
        A reduced source-resolution result for coordinator policy.
    """
    if use_residential_ytdlp:
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            return SourceResolutionResult(
                has_source=False,
                summary='Residential proxy is not configured.',
            )
        ydl_opts = get_ytdlp_options({'extract_flat': False, 'proxy': proxy_url})
    else:
        ydl_opts = get_ytdlp_options({'extract_flat': False})

    result = await get_audio_url(track, ydl_opts)

    if result.success and result.url:
        return SourceResolutionResult(
            has_source=True,
            direct_url=result.url,
            http_headers=result.http_headers,
            thumbnail=result.thumbnail,
            thumbnail_is_square=result.thumbnail_is_square,
            summary='Resolved a direct media source for playback.',
        )

    is_auth = bool(result.error and is_403_error(Exception(result.error)))
    summary = result.error or 'yt-dlp could not resolve a playable source.'

    return SourceResolutionResult(
        has_source=False,
        unavailable=result.is_unavailable,
        is_auth_failure=is_auth,
        thumbnail=result.thumbnail,
        thumbnail_is_square=result.thumbnail_is_square,
        summary=summary,
    )



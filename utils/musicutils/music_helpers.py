"""yt-dlp wrappers, thumbnail processing, FFmpeg utilities, and M4A downloads.

This module contains all the core music functionality that doesn't involve
state management or authentication. For auth-aware operations, see music_auth.
"""

from .music_data import AudioUrlResult, DownloadResult, Track

import asyncio
import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Optional imports for runtime
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore[assignment]
    YTDLP_AVAILABLE = False

try:
    from mutagen.mp4 import MP4, MP4Cover  # type: ignore[attr-defined]
    MUTAGEN_AVAILABLE = True
except ImportError:
    MP4 = MP4Cover = None  # type: ignore[assignment,misc]
    MUTAGEN_AVAILABLE = False

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

if TYPE_CHECKING:
    from .music_data import Track

# Import Track for runtime use


# ==========================================================================
# FFMPEG OPTIONS & PATH
# ==========================================================================

# FFmpeg options for Discord audio streaming
# Key flags explained:
#   -reconnect 1              : Reconnect on connection loss
#   -reconnect_streamed 1     : Reconnect even for streamed content
#   -reconnect_delay_max 2    : Max 2 seconds between reconnect attempts
#   -fflags +genpts+discardcorrupt : Generate fresh timestamps (prevents speed drift),
#                                    discard corrupt frames instead of trying to fix them
#   -analyzeduration 0        : Don't spend time analyzing the stream format
#   -probesize 32768          : Minimal probe size (32KB) since we know it's audio
#   -thread_queue_size 512    : Larger input queue to absorb network jitter
#   -nostdin                  : Don't read from stdin (minor optimization)
FFMPEG_BEFORE_OPTIONS = (
    '-reconnect 1 '
    '-reconnect_streamed 1 '
    '-reconnect_delay_max 2 '
    '-fflags +genpts+discardcorrupt '
    '-analyzeduration 0 '
    '-probesize 32768 '
    '-thread_queue_size 512 '
    '-nostdin'
)

FFMPEG_OPTIONS = {
    'before_options': FFMPEG_BEFORE_OPTIONS,
    # loudnorm: EBU R128 volume normalization for consistent loudness across tracks
    # I=target integrated loudness (-14 LUFS is typical for streaming)
    # LRA=loudness range (7 is moderate dynamic range preservation)
    # TP=true peak limit (-1 dB prevents clipping)
    # volume=0.5: additional headroom reduction for safety
    'options': '-vn -filter:a "loudnorm=I=-14:LRA=7:TP=-1,volume=0.5"'
}

# Cached FFmpeg path (set on first call to get_ffmpeg_path)
_ffmpeg_path: Optional[str] = None


def get_ffmpeg_path() -> str:
    """Gets the path to FFmpeg executable.

    For bundled builds, checks for FFmpeg in the app directory.
    Otherwise, assumes FFmpeg is in system PATH.
    Result is cached after first call.

    Platform behavior:
        - Windows: Looks for ffmpeg.exe in bundled path
        - Linux: Looks for ffmpeg (no extension) in bundled path

    Returns:
        str: Path to FFmpeg executable.
    """
    global _ffmpeg_path

    if _ffmpeg_path:
        return _ffmpeg_path

    import shutil
    import config

    # Check for bundled FFmpeg (PyInstaller build)
    if getattr(sys, 'frozen', False):
        ffmpeg_name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
        bundled_path = os.path.join(config.APP_PATH, ffmpeg_name)
        if os.path.exists(bundled_path):
            _ffmpeg_path = bundled_path
            return _ffmpeg_path

    # Fallback to system PATH
    ffmpeg_in_path = shutil.which('ffmpeg')
    if ffmpeg_in_path:
        _ffmpeg_path = ffmpeg_in_path
        return _ffmpeg_path

    # Last resort - just return 'ffmpeg' and let it fail with a clear error
    _ffmpeg_path = 'ffmpeg'
    return _ffmpeg_path


# ==========================================================================
# RESIDENTIAL PROXY
# ==========================================================================


def get_residential_proxy_url() -> Optional[str]:
    """Build residential proxy URL from config.

    Returns:
        Proxy URL string in format http://user:pass@host:port, or None if not configured.
        Returns None if RESIDENTIAL_PROXY_ENABLED is False (incomplete config).
    """
    import config

    # Check the enabled flag first - this validates all 4 fields are set
    if not getattr(config, 'RESIDENTIAL_PROXY_ENABLED', False):
        return None

    user = config.RESIDENTIAL_PROXY_USER
    password = config.RESIDENTIAL_PROXY_PASSWORD
    host = config.RESIDENTIAL_PROXY_HOST
    port = config.RESIDENTIAL_PROXY_PORT

    return f"http://{user}:{password}@{host}:{port}"


# ==========================================================================
# YT-DLP LOGGER ADAPTER
# ==========================================================================


class _YtdlpLoggerAdapter:
    """Routes yt-dlp warnings and errors through our logging system.

    yt-dlp calls debug(), warning(), and error() on this object.
    With quiet=True, only warnings and errors flow through — these are
    the only actionable signals (403s, extraction failures, plugin errors).
    The POT HTTP provider is opaque and produces no log output on success,
    so verbose/debug output is pure noise.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger('yt-dlp')

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def warning(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)


_ytdlp_logger = _YtdlpLoggerAdapter()


# ==========================================================================
# YT-DLP BASE OPTIONS
# ==========================================================================

# Base yt-dlp options (without auth - auth is applied by music_auth module)
YTDLP_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'opus',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,  # We want playlist support
    'nocheckcertificate': True,
    'ignoreerrors': True,  # Skip unavailable videos
    'logtostderr': False,
    'quiet': True,
    'no_warnings': False,
    'logger': _ytdlp_logger,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}


# ==========================================================================
# YOUTUBE ERROR HANDLING
# ==========================================================================

# Indicators that a video is permanently unavailable and should be removed
UNAVAILABLE_INDICATORS = [
    'video unavailable', 'this video is unavailable',
    'video is private', 'private video',
    'video has been removed', 'been removed',
    'this video is no longer available',
    'sign in to confirm your age',  # Age-restricted without workaround
    'join this channel to get access',  # Members-only
    'this video requires payment',  # Paid content
    'copyright claim', 'blocked',
]


def is_video_unavailable(error: Exception) -> bool:
    """Checks if an error indicates a video is permanently unavailable.

    Args:
        error: The exception to check.

    Returns:
        True if the video is permanently unavailable (should be removed).
    """
    error_str = str(error).lower()
    return any(indicator in error_str for indicator in UNAVAILABLE_INDICATORS)


def is_403_error(error: Exception) -> bool:
    """Checks if an exception indicates a 403 Forbidden error.

    Args:
        error: The exception to check.

    Returns:
        True if this is a 403/auth-related error.
    """
    error_str = str(error).lower()
    indicators = [
        '403', 'forbidden',
        'sign in to confirm your age',
        'video is age-restricted',
        'confirm your age',
        'login required',
    ]
    return any(ind in error_str for ind in indicators)


def format_youtube_error(error: Exception) -> str:
    """Formats a YouTube error into a user-friendly message.

    Args:
        error: The exception to format.

    Returns:
        A user-friendly error message string.
    """
    error_str = str(error).lower()

    if 'video unavailable' in error_str or 'unavailable' in error_str:
        return "This video is unavailable. It may be private, deleted, or region-locked."
    elif 'private video' in error_str:
        return "This video is private."
    elif 'sign in' in error_str:
        return "This video is age-restricted and cannot be played."
    elif 'copyright' in error_str or 'blocked' in error_str:
        return "This video is blocked due to copyright."
    else:
        return f"Could not access that URL: {error}"


# ==========================================================================
# VIDEO ID EXTRACTION
# ==========================================================================


def extract_video_id(url: str) -> Optional[str]:
    """Extracts the YouTube video ID from a URL.

    Handles various YouTube URL formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - URLs with additional parameters

    Args:
        url: YouTube video URL.

    Returns:
        11-character video ID, or None if not found.
    """
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'  # Raw video ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ==========================================================================
# THUMBNAIL FUNCTIONS
# ==========================================================================


def extract_m4a_thumbnail(m4a_path: str) -> Optional[bytes]:
    """Extracts embedded cover art from an M4A file.

    Reads the 'covr' atom from the MP4 container's metadata.
    This is used to retrieve thumbnails from cached M4A files without
    hitting YouTube.

    Args:
        m4a_path: Path to the M4A file.

    Returns:
        Image bytes if found, None otherwise.
    """
    if not MUTAGEN_AVAILABLE or MP4 is None or not os.path.exists(m4a_path):
        return None

    try:
        audio = MP4(m4a_path)
        covers = audio.tags.get('covr')  # type: ignore[union-attr]
        if covers:
            cover = covers[0]
            logger.debug(f"[Thumbnail] Extracted {len(cover)} bytes from M4A")
            return bytes(cover)

        logger.debug(f"[Thumbnail] No covr atom in {os.path.basename(m4a_path)}")
    except Exception as e:
        logger.debug(f"[Thumbnail] Failed to extract from M4A: {e}")

    return None


# Thumbnail processing lives in search.py
from .search import (  # noqa: E402
    extract_best_thumbnail_from_info,
    MUSIC_VIDEO_TYPE_ATV,
)


# ==========================================================================
# YT-DLP WRAPPER FUNCTIONS
# ==========================================================================


async def get_audio_url(
    track: 'Track',
    ydl_opts: Optional[Dict[str, Any]] = None
) -> AudioUrlResult:
    """Gets the actual streamable audio URL for a track.

    NOTE: This is a low-level function. For production use with retry logic
    and auth handling, use AudioFetcher from music_auth module instead.

    Args:
        track: The track to get the audio URL for.
        ydl_opts: Optional yt-dlp options dict. If None, uses YTDLP_OPTIONS.

    Returns:
        AudioUrlResult with url, availability status, thumbnail info, and headers.
    """
    if not yt_dlp:
        return AudioUrlResult(error="yt-dlp not available")

    if ydl_opts is None:
        ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': False}

    try:
        logger.info(f"[Audio] Extracting URL for: {track.title}")

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(track.url, download=False)  # type: ignore[return-value]

        info = await asyncio.to_thread(extract)

        if not info:
            logger.warning(f"No info returned for {track.title}")
            return AudioUrlResult(is_unavailable=True, error="No info returned")

        # Extract best thumbnail - prefer square (for album art)
        thumbnail_url, is_square = await extract_best_thumbnail_from_info(info)

        # Extract HTTP headers from info (needed for FFmpeg to fetch the URL)
        # yt-dlp stores these at the top level, formats may override
        http_headers = info.get('http_headers', {})

        formats = info.get('formats', [])

        # Priority 1: Find audio-only formats (no video codec)
        # These are smallest and most efficient for Discord
        audio_only = [
            fmt for fmt in formats
            if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none' and fmt.get('url')
        ]
        if audio_only:
            # Prefer higher audio bitrate among audio-only formats
            best = max(audio_only, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            # Format may have its own headers that override
            fmt_headers = best.get('http_headers', http_headers)
            logger.info(f"[Audio] Got audio-only URL for: {track.title}")
            return AudioUrlResult(
                url=best.get('url'),
                thumbnail=thumbnail_url,
                thumbnail_is_square=is_square,
                http_headers=fmt_headers or None
            )

        # Priority 2: Video+audio combined formats (muxed)
        # Less efficient but necessary for some videos that lack audio-only streams
        combined = [
            fmt for fmt in formats
            if fmt.get('acodec') != 'none' and fmt.get('vcodec') != 'none' and fmt.get('url')
        ]
        if combined:
            # Prefer by audio bitrate, then lowest video bitrate (less bandwidth waste)
            best = max(combined, key=lambda f: (f.get('abr') or 0, -(f.get('vbr') or f.get('tbr') or 0)))
            logger.info(f"[Audio] Using combined format for {track.title} (no audio-only available)")
            fmt_headers = best.get('http_headers', http_headers)
            return AudioUrlResult(
                url=best.get('url'),
                thumbnail=thumbnail_url,
                thumbnail_is_square=is_square,
                http_headers=fmt_headers or None
            )

        # Priority 3: Direct URL fallback (rare, usually livestreams or direct file links)
        if info.get('url'):
            logger.info(f"[Audio] Using direct URL fallback for {track.title}")
            return AudioUrlResult(
                url=info.get('url'),
                thumbnail=thumbnail_url,
                thumbnail_is_square=is_square,
                http_headers=http_headers or None
            )

        # No usable format found
        logger.warning(f"No playable format found for {track.title}")
        return AudioUrlResult(
            thumbnail=thumbnail_url,
            thumbnail_is_square=is_square,
            error="No playable format found"
        )

    except Exception as e:
        unavailable = is_video_unavailable(e)

        if unavailable:
            logger.warning(f"Video unavailable (will be removed): {track.title} - {e}")
        else:
            logger.error(f"Error getting audio URL for {track.title}: {e}")

        return AudioUrlResult(is_unavailable=unavailable, error=str(e))


def detect_mix_in_url(url: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Detects if a URL contains both a video ID and a mix playlist.

    This is used to prompt users whether they want just the single video
    or the entire mix playlist.

    Args:
        url: The YouTube URL to check.

    Returns:
        A tuple of (has_mix, single_video_url, mix_playlist_url) where:
        - has_mix: True if the URL has both a video ID and a mix playlist
        - single_video_url: URL for just the single video (stripped of playlist param)
        - mix_playlist_url: URL for fetching the mix playlist (stripped of video param)
    """
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)

    # Check for playlist parameter that's a mix (starts with RD)
    list_param = query_params.get('list', [None])[0]
    if not list_param or not list_param.startswith('RD'):
        return False, None, None

    # Check if there's also a video ID
    video_id = None
    if 'youtu.be' in parsed.netloc:
        video_id = parsed.path.strip('/')
    elif 'v' in query_params:
        video_id = query_params.get('v', [None])[0]
    elif '/shorts/' in parsed.path:
        video_id = parsed.path.split('/shorts/')[-1].split('/')[0]

    if not video_id:
        return False, None, None

    # Build single video URL (no playlist param)
    single_params = {k: v[0] for k, v in query_params.items() if k != 'list'}
    single_query = urlencode(single_params)
    single_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', single_query, ''))

    # For mix playlists, keep the original URL format - mixes require video context
    # (playlist?list=RD... URLs don't work, you need watch?v=VIDEO&list=RD...)
    # Just return the original URL; fetch_url_info with force_playlist=True will handle it
    mix_url = url

    return True, single_url, mix_url


async def fetch_url_info(
    url: str,
    logger: Any,
    force_playlist: bool = False,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> tuple[List['Track'], Optional[str], Optional[str]]:
    """Fetches track info from a YouTube URL (video or playlist).

    Behavior:
    - If the URL contains both a video ID and a playlist ID (e.g., a video link
      with ?list= param), only the single video is extracted UNLESS force_playlist=True.
    - Pure playlist URLs (no video context) extract the entire playlist.
    - "Mix" playlists (list=RD...) are limited to 60 tracks to prevent crashes.

    Args:
        url: The YouTube URL to fetch.
        logger: Logger instance for error messages.
        force_playlist: If True, extract the playlist even if URL has a video ID.
        ydl_opts: Optional yt-dlp options dict. If None, uses YTDLP_OPTIONS.

    Returns:
        A tuple of (tracks, error_message, warning_message) where:
        - tracks: List of Track objects (single for video, multiple for playlist)
        - error_message: Human-readable error if failed, None if success
        - warning_message: Non-fatal warning (e.g., mix truncation), None if none
    """
    if not yt_dlp:
        return [], "yt-dlp is not available.", None

    try:
        # Detect URL type and extract relevant parts
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # Check for playlist parameter
        list_param = query_params.get('list', [None])[0]

        # Determine if URL points to a specific video or is a pure playlist link
        has_video_id = False

        # youtu.be/VIDEO_ID format
        if 'youtu.be' in parsed.netloc:
            has_video_id = bool(parsed.path and parsed.path.strip('/'))
        # youtube.com/watch?v=VIDEO_ID format
        elif 'v' in query_params:
            has_video_id = bool(query_params.get('v', [None])[0])
        # youtube.com/shorts/VIDEO_ID format
        elif '/shorts/' in parsed.path:
            has_video_id = True

        # Check if this is a Mix playlist (auto-generated, potentially infinite)
        is_mix_playlist = bool(list_param and list_param.startswith('RD'))

        # Determine if we should extract as playlist
        # - If force_playlist is True, always treat as playlist (for mix prompts)
        # - Otherwise, only treat as playlist if there's no video ID
        if force_playlist and list_param:
            is_playlist = True
        else:
            # If URL has both video ID and playlist param, treat as single video
            # User linked a specific video, just happens to be from a playlist
            is_playlist = bool(list_param) and not has_video_id

        # Playlist limits: mixes capped at 60, regular playlists at 1000
        if is_mix_playlist:
            playlist_limit = 60
        elif is_playlist:
            playlist_limit = 1000
        else:
            playlist_limit = None

        if ydl_opts is None:
            ydl_opts = {**YTDLP_OPTIONS}

        ydl_opts = {
            **ydl_opts,
            'extract_flat': 'in_playlist' if is_playlist else False,
            'noplaylist': not is_playlist,  # Only extract playlist if pure playlist URL
        }

        # Add playlist limit
        if playlist_limit:
            ydl_opts['playlistend'] = playlist_limit
            if is_mix_playlist:
                logger.info(f"Mix playlist detected - limiting to {playlist_limit} tracks")
            else:
                logger.info(f"Playlist detected - limiting to {playlist_limit} tracks")

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)  # type: ignore[return-value]

        info = await asyncio.to_thread(extract)

        if not info:
            return [], "Could not fetch video information. The URL may be invalid or the video unavailable.", None

        tracks: List[Track] = []
        was_truncated = False

        # Check if it's a playlist result
        if info.get('_type') == 'playlist' or 'entries' in info:
            entries = info.get('entries', [])

            # Check if playlist was truncated
            if playlist_limit and len(entries) >= playlist_limit:
                was_truncated = True

            for entry in entries:
                if not entry:  # Skip unavailable videos
                    continue

                # Get video ID from entry or extract from URL
                video_id = entry.get('id')
                video_url = entry.get('url') or f"https://www.youtube.com/watch?v={video_id or ''}"
                if not video_id:
                    video_id = extract_video_id(video_url)

                track = Track(
                    title=entry.get('title', 'Unknown Title'),
                    artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                    url=video_url,
                    duration=int(entry.get('duration', 180) or 180),
                    thumbnail=entry.get('thumbnail'),
                    user_added=True,
                    video_id=video_id
                )
                tracks.append(track)

            if not tracks:
                return [], "The playlist is empty or all videos are unavailable.", None

            logger.info(f"[URL] Fetched playlist with {len(tracks)} tracks")

            # Return warning if playlist was truncated
            warning = None
            if was_truncated:
                if is_mix_playlist:
                    warning = (
                        f"⚠️ This is a Mix playlist - I only loaded the first {len(tracks)} tracks. "
                        "Mix playlists grow indefinitely and could crash the bot!"
                    )
                else:
                    warning = f"⚠️ This playlist was truncated to {len(tracks)} tracks (limit: {playlist_limit})."

            return tracks, None, warning

        else:
            # Single video
            # Get video ID from info or extract from URL
            video_id = info.get('id')
            video_url = info.get('webpage_url', url)
            if not video_id:
                video_id = extract_video_id(video_url)

            track = Track(
                title=info.get('title', 'Unknown Title'),
                artist=info.get('uploader', info.get('channel', 'Unknown Artist')),
                url=video_url,
                duration=int(info.get('duration', 180) or 180),
                thumbnail=info.get('thumbnail'),
                user_added=True,
                video_id=video_id
            )
            tracks.append(track)
            logger.info(f"[URL] Fetched single video: {track.title}")

        return tracks, None, None

    except Exception as e:
        logger.error(f"Error fetching URL info: {e}", exc_info=True)
        return [], format_youtube_error(e), None


async def fetch_playlist_metadata(
    playlist_url: str,
    logger: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> List['Track']:
    """Fetches playlist metadata from YouTube using yt-dlp (flat extraction).

    This is used for loading the ambient playlist at startup. It only fetches
    metadata (title, artist, duration) without extracting audio URLs.

    Args:
        playlist_url: The YouTube playlist URL to fetch.
        logger: Logger instance for info/error messages.
        ydl_opts: Optional yt-dlp options dict. If None, uses YTDLP_OPTIONS.

    Returns:
        A list of Track objects representing the playlist entries.
    """
    if not yt_dlp:
        return []

    try:
        if ydl_opts is None:
            ydl_opts = {**YTDLP_OPTIONS}

        ydl_opts = {**ydl_opts, 'extract_flat': 'in_playlist'}

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(playlist_url, download=False)  # type: ignore[return-value]

        info = await asyncio.to_thread(extract)

        if not info:
            logger.error("Failed to extract playlist info.")
            return []

        tracks: List[Track] = []
        entries = info.get('entries', [])

        for entry in entries:
            if not entry:  # Skip unavailable videos
                continue

            # Get video ID from entry or extract from URL
            video_id = entry.get('id')
            video_url = entry.get('url') or f"https://www.youtube.com/watch?v={video_id or ''}"
            if not video_id:
                video_id = extract_video_id(video_url)

            track = Track(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                url=video_url,
                duration=entry.get('duration', 180),  # Default 3 min if unknown
                thumbnail=entry.get('thumbnail'),
                video_id=video_id
            )
            tracks.append(track)

        logger.info(f"[Playlist] Fetched {len(tracks)} tracks from playlist")
        return tracks

    except Exception as e:
        logger.error(f"Error fetching playlist: {e}", exc_info=True)
        return []


# ==========================================================================
# AMBIENT FILENAME GENERATION
# ==========================================================================

# Maximum filename length in bytes (Linux ext4 limit)
_MAX_FILENAME_BYTES = 255

# Characters unsafe for filenames on Windows and Linux
_UNSAFE_FILENAME_CHARS = frozenset('/\\:*?"<>|')


def generate_ambient_filename(
    title: str,
    video_id: str,
    video_type: str,
    artist: Optional[str] = None,
) -> tuple[str, bool, Optional[str]]:
    """Generate a human-readable M4A filename for an ambient track.

    Produces filenames in the form:
        ATV:       "Artist - Title [video_id].m4a"
        UGC/OMV:   "Title [video_id].m4a"

    The video ID is always present in square brackets for collision immunity
    and manual traceability. Filenames are sanitized for both Linux and
    Windows filesystem safety.

    Args:
        title: Track title.
        video_id: Full 11-char YouTube video ID.
        video_type: One of MUSIC_VIDEO_TYPE_ATV, _OMV, _UGC, _OFFICIAL_SOURCE.
        artist: Artist name (included in filename only for ATV video types).

    Returns:
        Tuple of (filename, was_modified, original_unsanitized_name_portion).
        original_unsanitized_name_portion is the "Artist - Title" or "Title"
        string before sanitization, or None if no modification was needed.
    """
    # Build base name depending on video type
    if video_type == MUSIC_VIDEO_TYPE_ATV and artist:
        original_base = f"{artist} - {title}"
    else:
        original_base = title

    # Sanitize: strip unsafe characters (not replace)
    sanitized_base = ''.join(c for c in original_base if c not in _UNSAFE_FILENAME_CHARS)
    # Remove control characters
    sanitized_base = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', sanitized_base)
    # Strip leading/trailing whitespace and dots
    sanitized_base = sanitized_base.strip(' .')

    if not sanitized_base:
        sanitized_base = 'untitled'

    # Build full filename
    suffix = f" [{video_id}].m4a"  # " [xxxxxxxxxxx].m4a" = 18 bytes for ASCII ID
    filename = f"{sanitized_base}{suffix}"

    # Check byte length and truncate title portion if needed
    truncated = False
    while len(filename.encode('utf-8')) > _MAX_FILENAME_BYTES:
        truncated = True
        # For ATVs, only truncate the title part, keep artist intact
        if video_type == MUSIC_VIDEO_TYPE_ATV and artist:
            sanitized_artist = ''.join(c for c in artist if c not in _UNSAFE_FILENAME_CHARS).strip(' .')
            artist_prefix = f"{sanitized_artist} - "
            # Calculate how many bytes are available for the title
            prefix_bytes = len(artist_prefix.encode('utf-8'))
            suffix_bytes = len(suffix.encode('utf-8'))
            available = _MAX_FILENAME_BYTES - prefix_bytes - suffix_bytes
            if available < 10:
                # Artist itself is too long, truncate the whole base
                sanitized_base = sanitized_base[:-1].rstrip(' .')
            else:
                sanitized_title = sanitized_base[len(artist_prefix):]
                # Trim one character at a time from the title end
                while len(sanitized_title.encode('utf-8')) > available and sanitized_title:
                    sanitized_title = sanitized_title[:-1]
                sanitized_title = sanitized_title.rstrip(' .')
                sanitized_base = f"{artist_prefix}{sanitized_title}"
        else:
            # Non-ATV: trim the whole base
            sanitized_base = sanitized_base[:-1].rstrip(' .')

        filename = f"{sanitized_base}{suffix}"

        if not sanitized_base:
            sanitized_base = 'untitled'
            filename = f"{sanitized_base}{suffix}"
            break

    was_modified = (sanitized_base != original_base) or truncated
    original_if_modified = original_base if was_modified else None

    return filename, was_modified, original_if_modified


# ==========================================================================
# M4A DOWNLOAD WITH METADATA
# ==========================================================================


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitizes a string for use as a filename.

    Args:
        name: The string to sanitize.
        max_length: Maximum length of the resulting filename.

    Returns:
        A filesystem-safe filename string.
    """
    # Remove or replace problematic characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    # Remove control characters
    name = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', name)
    # Collapse multiple underscores/spaces
    name = re.sub(r'[_\s]+', '_', name)
    # Strip leading/trailing whitespace and dots
    name = name.strip(' ._')
    # Truncate if too long
    if len(name) > max_length:
        name = name[:max_length].rstrip(' ._')
    return name or 'untitled'


def ytdlp_temp_finder(output_dir: str, video_id: str, ext: str) -> Optional[str]:
    """Find yt-dlp temp output file for a given video ID and extension.

    Args:
        output_dir: Directory where yt-dlp writes temp files.
        video_id: YouTube video ID.
        ext: File extension (e.g., ".mp3", ".m4a").

    Returns:
        Full path to the temp file if found, None otherwise.
    """
    temp_path = os.path.join(output_dir, f"temp_{video_id}{ext}")
    if os.path.exists(temp_path):
        return temp_path

    for filename in os.listdir(output_dir):
        if filename.startswith(f"temp_{video_id}") and filename.endswith(ext):
            return os.path.join(output_dir, filename)

    return None


def ytdlp_move_temp_file(
    temp_path: str,
    final_path: str,
    *,
    overwrite: bool,
    logger: Any,
) -> bool:
    """Move a yt-dlp temp file to its final destination.

    Args:
        temp_path: Full path to the temp file.
        final_path: Destination path for the final file.
        overwrite: Whether to overwrite an existing final file.
        logger: Logger for warnings.

    Returns:
        True if the move succeeded, False otherwise.
    """
    try:
        if temp_path != final_path:
            if overwrite and os.path.exists(final_path):
                os.remove(final_path)
            os.rename(temp_path, final_path)
        return True
    except OSError as e:
        logger.warning(f"[Download] Failed to move temp file: {e}")
        return False


def ytdlp_cleanup_temp_files(output_dir: str, video_id: str, keep_ext: str) -> None:
    """Remove leftover yt-dlp temp files for a video ID.

    Args:
        output_dir: Directory where yt-dlp wrote temp files.
        video_id: YouTube video ID.
        keep_ext: Extension to keep (e.g., ".mp3", ".m4a").
    """
    for filename in os.listdir(output_dir):
        if filename.startswith(f"temp_{video_id}") and not filename.endswith(keep_ext):
            try:
                os.remove(os.path.join(output_dir, filename))
            except OSError as e:
                logger.debug(f"Failed to cleanup temp file {filename}: {e}")


async def download_track_as_m4a(
    url: str,
    output_dir: str,
    logger: Any,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    custom_album: Optional[str] = None,
    custom_album_artist: Optional[str] = None,
    custom_genre: Optional[str] = None,
    custom_year: Optional[str] = None,
    custom_track_num: Optional[tuple[int, int]] = None,
    custom_comment: Optional[str] = None,
    is_explicit: Optional[bool] = None,
    thumbnail_bytes: Optional[bytes] = None,
    target_filename: Optional[str] = None,
    proxy: Optional[str] = None,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> DownloadResult:
    """Downloads a track from YouTube as M4A with full MP4 metadata.

    Downloads audio from a YouTube URL, produces an M4A file (AAC in MP4
    container), and embeds metadata via MP4 atoms. If the source audio is
    already AAC, yt-dlp remuxes (no quality loss). If Opus/WebM, yt-dlp
    transcodes to AAC.

    Args:
        url: YouTube URL to download.
        output_dir: Directory to save the M4A file.
        logger: Logger instance for messages.
        custom_title: Override the track title (None = use YouTube title).
        custom_artist: Override the artist (None = use uploader/channel).
        custom_album: Album name to embed.
        custom_album_artist: Album artist to embed.
        custom_genre: Genre tag to embed.
        custom_year: Year tag to embed (None = auto-detect from upload date).
        custom_track_num: Track number as (track, total) tuple.
        custom_comment: Comment tag to embed.
        is_explicit: Explicit content flag (True/False/None).
        thumbnail_bytes: Pre-processed PNG thumbnail bytes to embed as cover art.
        target_filename: Exact filename for the output (e.g., 'Artist - Title [id].m4a').
            If None, uses yt-dlp's default Artist - Title naming.
        proxy: Optional proxy URL for the download (e.g., residential proxy).
        ydl_opts: Optional base yt-dlp options dict for auth keys.

    Returns:
        DownloadResult with success status, file path, and metadata.
    """
    if not YTDLP_AVAILABLE:
        return DownloadResult(
            success=False,
            error_message="yt-dlp is not installed. Install with: pip install yt-dlp"
        )

    if not MUTAGEN_AVAILABLE or MP4 is None or MP4Cover is None:
        return DownloadResult(
            success=False,
            error_message="mutagen is not installed. Install with: pip install mutagen"
        )

    await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)

    temp_template = os.path.join(output_dir, 'temp_%(id)s.%(ext)s')

    download_opts: Dict[str, Any] = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': temp_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '320',
        }],
        'nocheckcertificate': True,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
    }

    if ydl_opts:
        for key in ['cookiefile', 'cookiesfrombrowser', 'extractor_args']:
            if key in ydl_opts:
                download_opts[key] = ydl_opts[key]

    if proxy:
        download_opts['proxy'] = proxy

    video_id: Optional[str] = None

    try:
        logger.info(f"[Download] Starting M4A download: {url}")

        def do_download() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, download_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=True)  # type: ignore[return-value]

        info = await asyncio.to_thread(do_download)

        if not info:
            return DownloadResult(
                success=False,
                error_message="Failed to extract video information."
            )

        video_id_str: str = info.get('id') or 'unknown'
        video_id = video_id_str
        yt_title = info.get('title', 'Unknown Title')
        yt_artist = info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown Artist')
        yt_album = info.get('album')
        yt_duration = info.get('duration', 0)
        yt_year = None
        if info.get('upload_date'):
            yt_year = info['upload_date'][:4]

        final_title = custom_title or yt_title
        final_artist = custom_artist or yt_artist
        final_album = custom_album or yt_album
        final_year = custom_year or yt_year

        # Post-download: find temp file, rename, embed metadata, cleanup.
        # All of these are blocking I/O (filesystem + mutagen), so bundle
        # them into a sync helper and run in a thread.
        def _post_download_process() -> DownloadResult:
            """Sync helper for post-download file operations.

            Handles temp file discovery, rename, MP4 tag embedding, and
            cleanup. Runs via asyncio.to_thread to avoid blocking the
            event loop.
            """
            # Find the downloaded M4A file
            temp_m4a_path = ytdlp_temp_finder(output_dir, video_id_str, '.m4a')
            if not temp_m4a_path:
                return DownloadResult(
                    success=False,
                    error_message=f"Downloaded file not found. Expected: temp_{video_id_str}.m4a"
                )

            # Determine final path
            if target_filename:
                final_path = os.path.join(output_dir, target_filename)
            else:
                safe_artist = sanitize_filename(final_artist, 60)
                safe_title = sanitize_filename(final_title, 120)
                final_filename = f"{safe_artist} - {safe_title}.m4a"
                final_path = os.path.join(output_dir, final_filename)

            # Rename temp file to final path
            if not ytdlp_move_temp_file(
                temp_m4a_path,
                final_path,
                overwrite=True,
                logger=logger,
            ):
                return DownloadResult(
                    success=False,
                    error_message=f"Failed to move temp file for {video_id_str}"
                )
            logger.info(f"[Download] M4A saved as: {os.path.basename(final_path)}")

            # Embed metadata via MP4 atoms
            _thumbnail_embedded = False
            try:
                assert MP4 is not None and MP4Cover is not None  # Guarded by early return above
                audio = MP4(final_path)
                if audio.tags is None:
                    audio.add_tags()

                assert audio.tags is not None  # Guaranteed by add_tags() above
                tags = audio.tags

                # Text atoms
                tags['\xa9nam'] = [final_title]
                tags['\xa9ART'] = [final_artist]

                if custom_album_artist:
                    tags['aART'] = [custom_album_artist]

                if final_album:
                    tags['\xa9alb'] = [final_album]

                if final_year:
                    tags['\xa9day'] = [final_year]

                if custom_genre:
                    tags['\xa9gen'] = [custom_genre]

                if custom_track_num is not None:
                    tags['trkn'] = [custom_track_num]

                if custom_comment:
                    tags['\xa9cmt'] = [custom_comment]

                # Explicit flag (rtng atom: 0=clean, 1=explicit)
                if is_explicit is not None:
                    tags['rtng'] = [1 if is_explicit else 0]

                # Cover art (PNG)
                if thumbnail_bytes:
                    tags['covr'] = [
                        MP4Cover(thumbnail_bytes, imageformat=MP4Cover.FORMAT_PNG)
                    ]
                    _thumbnail_embedded = True
                    logger.debug(f"[Download] Embedded {len(thumbnail_bytes)} bytes PNG cover art")

                audio.save()
                logger.info("[Download] M4A metadata embedded successfully")

            except Exception as e:
                logger.warning(f"[Download] Failed to embed some M4A metadata: {e}")

            # Cleanup temp files
            ytdlp_cleanup_temp_files(output_dir, video_id_str, '.m4a')

            return DownloadResult(
                success=True,
                file_path=final_path,
                title=final_title,
                artist=final_artist,
                album=final_album,
                duration=yt_duration,
                thumbnail_embedded=_thumbnail_embedded
            )

        return await asyncio.to_thread(_post_download_process)

    except Exception as e:
        logger.error(f"[Download] M4A download error: {e}", exc_info=True)

        try:
            if os.path.isdir(output_dir) and video_id:
                await asyncio.to_thread(ytdlp_cleanup_temp_files, output_dir, video_id, '.m4a')
        except OSError as cleanup_err:
            logger.debug(f"Error cleanup failed (masking original error): {cleanup_err}")

        return DownloadResult(success=False, error_message=format_youtube_error(e))

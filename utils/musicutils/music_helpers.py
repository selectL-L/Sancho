"""yt-dlp wrappers, thumbnail processing, FFmpeg utilities, and MP3 downloads.

This module contains all the core music functionality that doesn't involve
state management or authentication. For auth-aware operations, see music_auth.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Optional imports for runtime
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore[assignment]
    YTDLP_AVAILABLE = False

try:
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK, TYER, TCON, COMM  # type: ignore[attr-defined]
    from mutagen.mp3 import MP3
    from mutagen._util import MutagenError  # type: ignore[attr-defined]
    MUTAGEN_AVAILABLE = True
except ImportError:
    APIC = ID3 = TALB = TIT2 = TPE1 = TRCK = TYER = TCON = COMM = None  # type: ignore[misc, assignment]
    MP3 = None  # type: ignore[misc, assignment]
    MutagenError = Exception  # type: ignore[misc, assignment]
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
from .music_data import AudioUrlResult, DownloadResult, Track


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
    'no_warnings': True,
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


async def _probe_thumbnail_dimensions(url: str, logger: Any) -> Optional[tuple[int, int]]:
    """Uses ffprobe to get actual dimensions of a thumbnail URL.

    Args:
        url: The thumbnail URL to probe.
        logger: Logger for debug output.

    Returns:
        Tuple of (width, height) or None if probe fails.
    """
    ffmpeg_path = get_ffmpeg_path()
    # ffprobe is in the same directory as ffmpeg
    if ffmpeg_path.endswith('.exe'):
        ffprobe_path = ffmpeg_path.replace('ffmpeg.exe', 'ffprobe.exe')
    else:
        ffprobe_path = ffmpeg_path.replace('ffmpeg', 'ffprobe')

    cmd = [
        ffprobe_path,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=p=0',
        url
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

        output = stdout.decode().strip()
        if ',' in output:
            w, h = output.split(',')
            return int(w), int(h)
    except asyncio.TimeoutError:
        logger.debug(f"[Thumbnail] ffprobe timeout for {url[:60]}...")
    except Exception as e:
        logger.debug(f"[Thumbnail] ffprobe failed: {e}")

    return None


def extract_mp3_thumbnail(mp3_path: str, logger: Any) -> Optional[bytes]:
    """Extracts embedded cover art from an MP3 file.

    Reads the APIC (Attached Picture) frame from the MP3's ID3 tags.
    This is used to retrieve thumbnails from cached MP3 files without
    hitting YouTube.

    Args:
        mp3_path: Path to the MP3 file.
        logger: Logger for debug output.

    Returns:
        Image bytes if found, None otherwise.
    """
    if not MUTAGEN_AVAILABLE or not os.path.exists(mp3_path):
        return None

    try:
        audio = MP3(mp3_path, ID3=ID3)  # type: ignore[misc]
        if audio.tags is None:
            logger.debug(f"[Thumbnail] No ID3 tags in {os.path.basename(mp3_path)}")
            return None

        # Look for APIC frames (cover art)
        for key in audio.tags.keys():
            if key.startswith('APIC'):
                apic = audio.tags[key]
                if apic.data:
                    logger.debug(f"[Thumbnail] Extracted {len(apic.data)} bytes from MP3 ({apic.mime})")
                    return apic.data

        logger.debug(f"[Thumbnail] No APIC frame in {os.path.basename(mp3_path)}")
    except Exception as e:
        logger.debug(f"[Thumbnail] Failed to extract from MP3: {e}")

    return None


async def crop_thumbnail_to_square(url: str, logger: Any) -> Optional[bytes]:
    """Crops a thumbnail to a square by extracting the center portion.

    YouTube Music "Art Track" videos have album art centered in a 16:9 frame.
    This function extracts the center square, which contains the album art.

    Args:
        url: The thumbnail URL to crop.
        logger: Logger for debug output.

    Returns:
        JPEG bytes of the cropped square image, or None if cropping fails.
    """
    ffmpeg_path = get_ffmpeg_path()

    # crop filter: crop=out_w:out_h:x:y
    # For center square: crop=min(iw,ih):min(iw,ih):(iw-min(iw,ih))/2:(ih-min(iw,ih))/2
    # Simplified: crop=ih:ih:(iw-ih)/2:0 for landscape (width > height)
    crop_filter = "crop='min(iw,ih):min(iw,ih):(iw-min(iw,ih))/2:(ih-min(iw,ih))/2'"

    cmd = [
        ffmpeg_path,
        '-y',  # Overwrite output
        '-i', url,
        '-vf', crop_filter,
        '-frames:v', '1',  # Only one frame (it's an image)
        '-c:v', 'mjpeg',  # Encode as JPEG (converts webp/png to jpeg)
        '-f', 'image2',  # Single image output format
        '-q:v', '2',  # High quality (1-31, lower = better)
        'pipe:1'  # Output to stdout
    ]

    logger.debug(f"[Thumbnail] Cropping to square: {url[:80]}...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

        if proc.returncode == 0 and stdout:
            logger.debug(f"[Thumbnail] Cropped successfully, {len(stdout)} bytes")
            return stdout
        else:
            logger.debug(f"[Thumbnail] Crop failed: {stderr.decode()[:200] if stderr else 'no stderr'}")
    except asyncio.TimeoutError:
        logger.debug(f"[Thumbnail] Crop timeout for {url[:60]}...")
    except Exception as e:
        logger.debug(f"[Thumbnail] Crop failed: {e}")

    return None


async def _extract_best_thumbnail(info: Dict[str, Any], logger: Any) -> tuple[Optional[str], bool]:
    """Extracts the best thumbnail URL from yt-dlp info, preferring square images.

    Uses ffprobe to determine dimensions of thumbnails that don't have them.
    Filters out thumbnails under 480px and prefers square album art.

    Args:
        info: The yt-dlp extraction info dict.
        logger: Logger for debug output.

    Returns:
        Tuple of (thumbnail_url, needs_crop). If needs_crop is True, the caller
        should use crop_thumbnail_to_square() to extract the center square.
    """
    MIN_SIZE = 480  # Minimum acceptable dimension

    thumbnails = info.get('thumbnails', [])

    logger.debug(f"[Thumbnail] Found {len(thumbnails)} raw thumbnails from yt-dlp")

    if not thumbnails:
        fallback = info.get('thumbnail')
        logger.debug(f"[Thumbnail] No thumbnails list, using fallback: {fallback}")
        return fallback, True  # Assume fallback needs crop

    # Log all thumbnails and probe those missing dimensions
    unknown_dims = []
    for i, t in enumerate(thumbnails):
        w, h = t.get('width'), t.get('height')
        if w and h:
            url_preview = t.get('url', 'no-url')[:80] + '...' if len(t.get('url', '')) > 80 else t.get('url', 'no-url')
            logger.debug(f"[Thumbnail] [{i}] {w}x{h} - {url_preview}")
        else:
            unknown_dims.append((i, t))

    # Probe unknown dimensions with ffprobe (limit to avoid slowdown)
    if unknown_dims:
        logger.debug(f"[Thumbnail] Probing {len(unknown_dims)} thumbnails with unknown dimensions...")
        for i, t in unknown_dims[:5]:  # Probe max 5 to avoid delays
            url = t.get('url')
            if url:
                dims = await _probe_thumbnail_dimensions(url, logger)
                if dims:
                    t['width'], t['height'] = dims
                    t['_probed'] = True
                    logger.debug(f"[Thumbnail] [{i}] {dims[0]}x{dims[1]} (probed) - {url[:80]}...")
                else:
                    logger.debug(f"[Thumbnail] [{i}] ?x? (probe failed) - {url[:80]}...")

    # Filter to only thumbnails with known dimensions >= MIN_SIZE
    usable = [
        t for t in thumbnails
        if t.get('width') and t.get('height')
        and t.get('width') >= MIN_SIZE and t.get('height') >= MIN_SIZE
    ]

    logger.debug(f"[Thumbnail] {len(usable)} thumbnails pass {MIN_SIZE}px minimum filter")

    if not usable:
        # Nothing meets minimum size - fall back to largest available
        with_dims = [t for t in thumbnails if t.get('width') and t.get('height')]
        if with_dims:
            best = max(with_dims, key=lambda t: t.get('width', 0) * t.get('height', 0))
            logger.debug(f"[Thumbnail] No thumbnails >= {MIN_SIZE}px, using largest: {best.get('width')}x{best.get('height')}")
            is_square = best.get('width') == best.get('height')
            return best.get('url'), not is_square
        # No dimensions at all - return any URL
        for t in thumbnails:
            if t.get('url'):
                logger.debug("[Thumbnail] No dimension info available, using first URL")
                return t.get('url'), True  # Assume needs crop
        return info.get('thumbnail'), True

    # Try to find square thumbnails (width == height) - these are album art
    square_thumbnails = [t for t in usable if t.get('width') == t.get('height')]

    if square_thumbnails:
        best = max(square_thumbnails, key=lambda t: t.get('width', 0))
        logger.info(f"[Thumbnail] Selected square thumbnail: {best.get('width')}x{best.get('height')}")
        return best.get('url'), False  # Already square, no crop needed

    # No square thumbnail - return the largest one for cropping
    best = max(usable, key=lambda t: t.get('width', 0) * t.get('height', 0))
    logger.info(f"[Thumbnail] No square thumbnail available, cropping largest: {best.get('width')}x{best.get('height')}")
    return best.get('url'), True  # Needs cropping


async def get_best_thumbnail_bytes(
    track: 'Track',
    logger: Any,
    yt_info: Optional[Dict[str, Any]] = None,
    cached_mp3_path: Optional[str] = None
) -> Optional[bytes]:
    """Gets the best thumbnail bytes for a track using unified logic.

    Priority order:
    1. Extract from cached MP3 file (already processed, best quality)
    2. Use _extract_best_thumbnail to find best URL, then crop/convert if needed
    3. Fall back to basic thumbnail URL with cropping

    This is THE ONLY function that should be used to get thumbnail bytes.
    All other thumbnail handling should go through this function.

    Args:
        track: The Track object.
        logger: Logger for debug output.
        yt_info: Optional yt-dlp info dict (avoids re-fetching if already available).
        cached_mp3_path: Optional path to cached MP3 file (for embedded thumbnail).

    Returns:
        JPEG image bytes, or None if no thumbnail available.
    """
    # Priority 1: Extract from cached MP3 (already has processed thumbnail)
    if cached_mp3_path and os.path.exists(cached_mp3_path):
        logger.debug(f"[Thumbnail] Checking cached MP3: {os.path.basename(cached_mp3_path)}")
        thumbnail_data = extract_mp3_thumbnail(cached_mp3_path, logger)
        if thumbnail_data:
            logger.info("[Thumbnail] Using embedded thumbnail from cached MP3")
            return thumbnail_data

    # Priority 2: Use _extract_best_thumbnail if we have yt_info
    if yt_info:
        logger.debug("[Thumbnail] Using yt-dlp info for thumbnail selection")
        thumbnail_url, needs_crop = await _extract_best_thumbnail(yt_info, logger)
        if thumbnail_url:
            if needs_crop:
                logger.info(f"[Thumbnail] Cropping non square thumbnail: {thumbnail_url[:60]}...")
                return await crop_thumbnail_to_square(thumbnail_url, logger)
            else:
                # Square thumbnail - just download and convert to JPEG
                logger.info(f"[Thumbnail] Using square thumbnail: {thumbnail_url[:60]}...")
                return await crop_thumbnail_to_square(thumbnail_url, logger)  # Still use this for JPEG conversion

    # Priority 3: Use track's cached thumbnail URL (from flat extraction)
    if track.thumbnail:
        logger.info(f"[Thumbnail] Using track's cached thumbnail URL: {track.thumbnail[:60]}...")
        # Always crop/convert YouTube thumbnails
        return await crop_thumbnail_to_square(track.thumbnail, logger)

    # Priority 4: Construct URL from video ID as last resort
    if track.video_id:
        fallback_url = f"https://img.youtube.com/vi/{track.video_id}/hqdefault.jpg"
        logger.info(f"[Thumbnail] Using constructed fallback URL: {fallback_url}")
        return await crop_thumbnail_to_square(fallback_url, logger)

    logger.info("[Thumbnail] No thumbnail source available")
    return None


# ==========================================================================
# YT-DLP WRAPPER FUNCTIONS
# ==========================================================================


async def get_audio_url(
    track: 'Track',
    logger: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> AudioUrlResult:
    """Gets the actual streamable audio URL for a track.

    NOTE: This is a low-level function. For production use with retry logic
    and auth handling, use AudioFetcher from music_auth module instead.

    Args:
        track: The track to get the audio URL for.
        logger: Logger instance for debug/error messages.
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
                return ydl.extract_info(track.url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            logger.warning(f"No info returned for {track.title}")
            return AudioUrlResult(is_unavailable=True, error="No info returned")

        # Extract best thumbnail - prefer square (for album art)
        thumbnail_url, needs_crop = await _extract_best_thumbnail(info, logger)

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
            return AudioUrlResult(
                url=best.get('url'),
                thumbnail=thumbnail_url,
                thumbnail_needs_crop=needs_crop,
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
                thumbnail_needs_crop=needs_crop,
                http_headers=fmt_headers or None
            )

        # Priority 3: Direct URL fallback (rare, usually livestreams or direct file links)
        if info.get('url'):
            logger.info(f"[Audio] Using direct URL fallback for {track.title}")
            return AudioUrlResult(
                url=info.get('url'),
                thumbnail=thumbnail_url,
                thumbnail_needs_crop=needs_crop,
                http_headers=http_headers or None
            )

        # No usable format found
        logger.warning(f"No playable format found for {track.title}")
        return AudioUrlResult(
            thumbnail=thumbnail_url,
            thumbnail_needs_crop=needs_crop,
            error="No playable format found"
        )

    except Exception as e:
        unavailable = is_video_unavailable(e)

        if unavailable:
            logger.warning(f"Video unavailable (will be removed): {track.title} - {e}")
        else:
            logger.error(f"Error getting audio URL for {track.title}: {e}")

        return AudioUrlResult(is_unavailable=unavailable, error=str(e))


async def search_youtube(
    query: str,
    max_results: int,
    logger: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> List['Track']:
    """Searches YouTube for tracks matching the query.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return.
        logger: Logger instance for debug messages.
        ydl_opts: Optional yt-dlp options dict. If None, uses YTDLP_OPTIONS.

    Returns:
        A list of Track objects representing search results.
    """
    if not yt_dlp:
        logger.debug("[Search] yt-dlp not available")
        return []

    logger.debug(f"[Search] Starting search for: '{query}' (max_results={max_results})")

    try:
        if ydl_opts is None:
            ydl_opts = {**YTDLP_OPTIONS}

        ydl_opts = {
            **ydl_opts,
            'extract_flat': 'in_playlist',  # Only flatten playlist entries, not search
            'noplaylist': True,  # We want individual videos from search
        }

        # Prefix with ytsearch to explicitly trigger YouTube search
        search_query = f"ytsearch{max_results}:{query}"
        logger.debug(f"[Search] Full search query: '{search_query}'")

        def search() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(search_query, download=False)  # type: ignore

        info = await asyncio.to_thread(search)

        if not info:
            logger.debug("[Search] yt-dlp returned None/empty info")
            return []

        logger.debug(f"[Search] yt-dlp returned info with keys: {list(info.keys())}")
        logger.debug(f"[Search] extractor: {info.get('extractor', 'N/A')}, _type: {info.get('_type', 'N/A')}")

        tracks: List[Track] = []
        entries = info.get('entries', [])

        # If no entries but we have direct video info, treat as single result
        if not entries and info.get('id'):
            logger.debug("[Search] No entries but found direct video, using as single result")
            entries = [info]

        logger.debug(f"[Search] Found {len(entries) if entries else 0} entries")

        for i, entry in enumerate(entries):
            if not entry:
                logger.debug(f"[Search] Entry {i} is None/empty, skipping")
                continue

            logger.debug(f"[Search] Entry {i}: id={entry.get('id')}, title={entry.get('title')}, duration={entry.get('duration')}")

            track = Track(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                url=entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                duration=int(entry.get('duration', 180) or 180),
                thumbnail=entry.get('thumbnail'),
                user_added=True,  # Search results are always user-added
                video_id=entry.get('id')  # Extract video ID for residential proxy fallback
            )
            tracks.append(track)

        logger.info(f"[Search] Returning {len(tracks)} tracks for '{query}'")
        return tracks

    except Exception as e:
        logger.error(f"Error searching YouTube: {e}", exc_info=True)
        return []


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
                return ydl.extract_info(url, download=False)  # type: ignore

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
                return ydl.extract_info(playlist_url, download=False)  # type: ignore

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
# MP3 DOWNLOAD WITH METADATA
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


async def download_track_as_mp3(
    url: str,
    output_dir: str,
    logger: Any,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    custom_album: Optional[str] = None,
    custom_genre: Optional[str] = None,
    custom_year: Optional[str] = None,
    custom_track_num: Optional[str] = None,
    custom_comment: Optional[str] = None,
    embed_thumbnail: bool = True,
    proxy: Optional[str] = None,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> DownloadResult:
    """Downloads a track from YouTube as MP3 with full metadata.

    Downloads audio from a YouTube URL, converts to MP3, and embeds
    ID3 metadata including cover art. Metadata can be customized or
    auto-populated from YouTube.

    Args:
        url: YouTube URL to download.
        output_dir: Directory to save the MP3 file.
        logger: Logger instance for messages.
        custom_title: Override the track title (None = use YouTube title).
        custom_artist: Override the artist (None = use uploader/channel).
        custom_album: Album name to embed (None = use YouTube album if available).
        custom_genre: Genre tag to embed.
        custom_year: Year tag to embed (None = auto-detect from upload date).
        custom_track_num: Track number tag (e.g., "1" or "1/12").
        custom_comment: Comment tag to embed.
        embed_thumbnail: Whether to embed the thumbnail as cover art.
        proxy: Optional proxy URL for the download (e.g., residential proxy).
        ydl_opts: Optional base yt-dlp options dict.

    Returns:
        DownloadResult with success status, file path, and metadata.
    """
    if not YTDLP_AVAILABLE:
        return DownloadResult(
            success=False,
            error_message="yt-dlp is not installed. Install with: pip install yt-dlp"
        )

    if not MUTAGEN_AVAILABLE:
        return DownloadResult(
            success=False,
            error_message="mutagen is not installed. Install with: pip install mutagen"
        )

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Temporary file template - we'll rename after getting metadata
    temp_template = os.path.join(output_dir, 'temp_%(id)s.%(ext)s')

    download_opts: Dict[str, Any] = {
        'format': 'bestaudio/best',
        'outtmpl': temp_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'writethumbnail': embed_thumbnail,  # Download thumbnail for manual embedding
        'nocheckcertificate': True,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
    }

    # Merge base options if provided
    if ydl_opts:
        # Copy auth-related options
        for key in ['cookiefile', 'cookiesfrombrowser', 'extractor_args']:
            if key in ydl_opts:
                download_opts[key] = ydl_opts[key]

    if proxy:
        download_opts['proxy'] = proxy

    # Track video_id for cleanup - we may not get it if download fails early
    video_id: Optional[str] = None

    try:
        logger.info(f"[Download] Starting download: {url}")

        def do_download() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, download_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=True)  # type: ignore

        info = await asyncio.to_thread(do_download)

        if not info:
            return DownloadResult(
                success=False,
                error_message="Failed to extract video information."
            )

        # Extract metadata from yt-dlp info
        video_id = info.get('id', 'unknown')
        yt_title = info.get('title', 'Unknown Title')
        yt_artist = info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown Artist')
        yt_album = info.get('album')
        yt_duration = info.get('duration', 0)
        yt_year = None
        if info.get('upload_date'):
            yt_year = info['upload_date'][:4]  # YYYYMMDD -> YYYY

        # Apply custom metadata or use YouTube defaults
        final_title = custom_title or yt_title
        final_artist = custom_artist or yt_artist
        final_album = custom_album or yt_album
        final_year = custom_year or yt_year

        # Find the downloaded MP3 file
        temp_mp3_path = os.path.join(output_dir, f'temp_{video_id}.mp3')

        if not os.path.exists(temp_mp3_path):
            # Sometimes yt-dlp uses different naming
            for f in os.listdir(output_dir):
                if f.startswith(f'temp_{video_id}') and f.endswith('.mp3'):
                    temp_mp3_path = os.path.join(output_dir, f)
                    break

        if not os.path.exists(temp_mp3_path):
            return DownloadResult(
                success=False,
                error_message=f"Downloaded file not found. Expected: temp_{video_id}.mp3"
            )

        # Generate final filename
        safe_artist = sanitize_filename(final_artist, 60)
        safe_title = sanitize_filename(final_title, 120)
        final_filename = f"{safe_artist} - {safe_title}.mp3"
        final_path = os.path.join(output_dir, final_filename)

        # Handle filename collision
        counter = 1
        while os.path.exists(final_path):
            final_filename = f"{safe_artist} - {safe_title} ({counter}).mp3"
            final_path = os.path.join(output_dir, final_filename)
            counter += 1

        # Rename temp file to final name
        os.rename(temp_mp3_path, final_path)
        logger.info(f"[Download] MP3 saved as: {final_filename}")

        # Embed metadata using mutagen
        thumbnail_embedded = False
        try:
            audio = MP3(final_path, ID3=ID3)  # type: ignore[misc]

            # Create ID3 tag if it doesn't exist
            try:
                audio.add_tags()
            except MutagenError:
                pass  # Tags already exist - expected

            # Set metadata tags
            audio.tags.add(TIT2(encoding=3, text=final_title))  # type: ignore[misc]  # Title
            audio.tags.add(TPE1(encoding=3, text=final_artist))  # type: ignore[misc]  # Artist

            if final_album:
                audio.tags.add(TALB(encoding=3, text=final_album))  # type: ignore[misc]  # Album

            if final_year:
                audio.tags.add(TYER(encoding=3, text=final_year))  # type: ignore[misc]  # Year

            if custom_genre:
                audio.tags.add(TCON(encoding=3, text=custom_genre))  # type: ignore[misc]  # Genre

            if custom_track_num:
                audio.tags.add(TRCK(encoding=3, text=custom_track_num))  # type: ignore[misc]  # Track number

            if custom_comment:
                audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=custom_comment))  # type: ignore[misc]  # Comment

            # Embed thumbnail as cover art
            if embed_thumbnail:
                assert video_id is not None  # Guaranteed by info.get('id', 'unknown') above
                thumbnail_embedded = await _embed_thumbnail_in_mp3(
                    audio, info, output_dir, video_id, logger
                )

            audio.save()
            logger.info("[Download] Metadata embedded successfully")

        except Exception as e:
            logger.warning(f"[Download] Failed to embed some metadata: {e}")

        # Cleanup thumbnail files
        for f in os.listdir(output_dir):
            if f.startswith(f'temp_{video_id}') and not f.endswith('.mp3'):
                try:
                    os.remove(os.path.join(output_dir, f))
                except OSError as e:
                    logger.debug(f"Failed to cleanup temp file {f}: {e}")

        return DownloadResult(
            success=True,
            file_path=final_path,
            title=final_title,
            artist=final_artist,
            album=final_album,
            duration=yt_duration,
            thumbnail_embedded=thumbnail_embedded
        )

    except Exception as e:
        logger.error(f"[Download] Error: {e}", exc_info=True)

        # Cleanup temp files for THIS download only
        try:
            if os.path.isdir(output_dir) and video_id:
                for f in os.listdir(output_dir):
                    if f.startswith(f'temp_{video_id}'):
                        try:
                            os.remove(os.path.join(output_dir, f))
                        except OSError as cleanup_err:
                            logger.debug(f"Failed to cleanup temp file {f} after error: {cleanup_err}")
        except OSError as cleanup_err:
            logger.debug(f"Error cleanup failed (masking original error): {cleanup_err}")

        return DownloadResult(success=False, error_message=format_youtube_error(e))


async def _embed_thumbnail_in_mp3(
    audio: Any,
    info: Dict[str, Any],
    output_dir: str,
    video_id: str,
    logger: Any
) -> bool:
    """Embeds thumbnail as cover art in an MP3 file.

    Uses _extract_best_thumbnail() for proper thumbnail selection (prefers square),
    then crop_thumbnail_to_square() for processing. This ensures we use the same
    logic as the now playing widget.

    Args:
        audio: Mutagen MP3 object with ID3 tags.
        info: yt-dlp extraction info dict.
        output_dir: Directory where temp files are stored.
        video_id: YouTube video ID.
        logger: Logger for debug messages.

    Returns:
        True if thumbnail was embedded, False otherwise.
    """
    thumbnail_data = None

    # Use _extract_best_thumbnail for proper thumbnail selection
    # This prefers square album art over 16:9 video thumbnails
    try:
        thumbnail_url, needs_crop = await _extract_best_thumbnail(info, logger)
        if thumbnail_url:
            # Always run through crop_thumbnail_to_square for JPEG conversion
            # (it handles both cropping and format conversion)
            thumbnail_data = await crop_thumbnail_to_square(thumbnail_url, logger)
            if thumbnail_data:
                logger.debug(f"[Download] Thumbnail: {thumbnail_url[:50]}... (crop={needs_crop})")
    except Exception as e:
        logger.debug(f"[Download] Failed to process thumbnail: {e}")

    if thumbnail_data:
        try:
            audio.tags.add(APIC(  # type: ignore[misc]
                encoding=3,
                mime='image/jpeg',  # crop_thumbnail_to_square always outputs JPEG
                type=3,  # Front cover
                desc='Cover',
                data=thumbnail_data
            ))
            return True
        except Exception as e:
            logger.warning(f"[Download] Failed to embed thumbnail: {e}")

    return False


async def get_track_info_for_download(
    url: str,
    logger: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Gets track metadata without downloading, for preview purposes.

    Useful for showing the user what will be downloaded before committing.

    Args:
        url: YouTube URL to inspect.
        logger: Logger instance.
        ydl_opts: Optional yt-dlp options dict.

    Returns:
        Dict with title, artist, album, duration, thumbnail, or None on failure.
    """
    if not YTDLP_AVAILABLE:
        return None

    try:
        if ydl_opts is None:
            ydl_opts = {**YTDLP_OPTIONS}

        ydl_opts = {
            **ydl_opts,
            'extract_flat': False,
            'skip_download': True,
        }

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            return None

        return {
            'title': info.get('title', 'Unknown Title'),
            'artist': info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown Artist'),
            'album': info.get('album'),
            'duration': info.get('duration', 0),
            'thumbnail': info.get('thumbnail'),
            'upload_date': info.get('upload_date'),
            'url': url,
        }

    except Exception as e:
        logger.error(f"[Download] Error getting track info: {e}")
        return None

"""Unified search and metadata provider for music playback.

This module consolidates all search functionality (YTM + YouTube) and metadata
handling (thumbnails, track info) into a single coherent API.

Architecture:
    - SearchResult: Internal dataclass for search/selection flow
    - Query Mode: User types search query → parallel YTM + yt-dlp search
    - URL Mode: User provides URL → check if ATV, find alternatives
    - Thumbnails: Resize YTM square thumbnails, crop YouTube 16:9 thumbnails

The goal is good metadata (especially thumbnails). yt-dlp handles streaming
regardless of source.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

# Optional imports
try:
    from ytmusicapi import YTMusic
    YTMUSIC_AVAILABLE = True
except ImportError:
    YTMusic = None  # type: ignore[misc, assignment]
    YTMUSIC_AVAILABLE = False

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore[assignment]
    YTDLP_AVAILABLE = False

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

if TYPE_CHECKING:
    from .music_data import Track

from .music_data import Track

logger = logging.getLogger(__name__)

# ==========================================================================
# YTMUSIC SINGLETON
# ==========================================================================

# Module-level singleton (initialized on first use)
if YTMUSIC_AVAILABLE:
    from ytmusicapi import YTMusic as _YTMusicClass
    _ytm: _YTMusicClass | None = None
else:
    _ytm = None  # type: ignore[assignment]


def _get_ytm():
    """Get or create the YTMusic singleton instance.

    Returns:
        YTMusic instance if available, None otherwise.
    """
    global _ytm
    if not YTMUSIC_AVAILABLE:
        return None
    if _ytm is None:
        try:
            from ytmusicapi import YTMusic
            _ytm = YTMusic()
        except Exception as e:
            logger.warning(f"Failed to initialize YTMusic: {e}")
            return None
    return _ytm


# ==========================================================================
# SEARCH RESULT DATACLASS
# ==========================================================================


@dataclass
class SearchResult:
    """Internal representation of a search result during selection flow.

    This is separate from Track because it contains search-specific metadata
    (artist_id, video_type, source) that Track doesn't need for playback.
    """
    video_id: str
    title: str
    artist: str
    artist_id: Optional[str] = None  # For artist ID matching (URL mode)
    album: Optional[str] = None  # From YTM search only
    duration_seconds: Optional[int] = None
    thumbnail_url: Optional[str] = None
    thumbnail_is_square: bool = False  # True for ATVs (lh3 thumbnails)
    source: str = 'youtube'  # 'ytm_song', 'ytm_video', 'youtube'
    video_type: Optional[str] = None  # MUSIC_VIDEO_TYPE_ATV, etc.
    is_explicit: Optional[bool] = None  # True if explicit, False if clean, None if unknown

    def to_track(self) -> Track:
        """Convert to Track for playback."""
        return Track(
            title=self.title,
            artist=self.artist,
            url=f"https://www.youtube.com/watch?v={self.video_id}",
            duration=self.duration_seconds or 0,
            thumbnail=self.thumbnail_url,
            thumbnail_is_square=self.thumbnail_is_square,
            video_id=self.video_id,
            album=self.album,
            source=self.source,
            is_explicit=self.is_explicit,
        )


# ==========================================================================
# VIDEO ID EXTRACTION
# ==========================================================================


def extract_video_id(url: str) -> Optional[str]:
    """Extracts the YouTube video ID from a URL.

    Handles various YouTube URL formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://music.youtube.com/watch?v=VIDEO_ID
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


# Standard thumbnail width - used for both square (720x720) and 16:9 (720x405) thumbnails
THUMBNAIL_WIDTH = 720


def resize_ytm_thumbnail(url: str, size: int = THUMBNAIL_WIDTH) -> str:
    """Resize a YouTube Music thumbnail URL to a specific size.

    YTM thumbnails from lh3.googleusercontent.com support dynamic resizing
    via URL parameters.

    Args:
        url: The original thumbnail URL.
        size: Desired width and height (square). Defaults to THUMBNAIL_WIDTH (720).

    Returns:
        Resized URL, or original if not an lh3 URL.
    """
    if 'lh3.googleusercontent.com' not in url:
        return url

    # Strip existing size params and add new ones
    base_url = url.split('=')[0]
    return f"{base_url}=w{size}-h{size}"


def get_best_thumbnail_url(result: SearchResult, size: int = THUMBNAIL_WIDTH) -> Optional[str]:
    """Get the best thumbnail URL for a search result.

    Args:
        result: The search result.
        size: Desired size for square thumbnails. Defaults to THUMBNAIL_WIDTH (720).

    Returns:
        Optimized thumbnail URL.
    """
    if not result.thumbnail_url:
        # Fallback to constructed YouTube URL - sddefault.jpg is 640x480, maxresdefault is 1280x720
        if result.video_id:
            return f"https://img.youtube.com/vi/{result.video_id}/sddefault.jpg"
        return None

    if result.thumbnail_is_square:
        return resize_ytm_thumbnail(result.thumbnail_url, size)

    return result.thumbnail_url


async def _probe_thumbnail_dimensions(url: str) -> Optional[tuple[int, int]]:
    """Uses ffprobe to get actual dimensions of a thumbnail URL.

    Args:
        url: The thumbnail URL to probe.

    Returns:
        Tuple of (width, height) or None if probe fails.
    """
    from .music_helpers import get_ffmpeg_path

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


async def extract_best_thumbnail_from_info(info: Dict[str, Any]) -> tuple[Optional[str], bool]:
    """Extracts the best thumbnail URL from yt-dlp info, preferring square images.

    Uses ffprobe to determine dimensions of thumbnails that don't have them.
    Filters out thumbnails under 480px and prefers square album art.

    Args:
        info: The yt-dlp extraction info dict.

    Returns:
        Tuple of (thumbnail_url, is_square). is_square indicates if the
        thumbnail is already square (no cropping needed).
    """
    MIN_SIZE = 480  # Minimum acceptable dimension

    thumbnails = info.get('thumbnails', [])

    logger.debug(f"[Thumbnail] Found {len(thumbnails)} raw thumbnails from yt-dlp")

    if not thumbnails:
        fallback = info.get('thumbnail')
        logger.debug(f"[Thumbnail] No thumbnails list, using fallback: {fallback}")
        return fallback, False  # Assume fallback is not square

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
                dims = await _probe_thumbnail_dimensions(url)
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
            return best.get('url'), is_square
        # No dimensions at all - return any URL
        for t in thumbnails:
            if t.get('url'):
                logger.debug("[Thumbnail] No dimension info available, using first URL")
                return t.get('url'), False  # Assume not square
        return info.get('thumbnail'), False

    # Try to find square thumbnails (width == height) - these are album art
    square_thumbnails = [t for t in usable if t.get('width') == t.get('height')]

    if square_thumbnails:
        best = max(square_thumbnails, key=lambda t: t.get('width', 0))
        logger.info(f"[Thumbnail] Selected square thumbnail: {best.get('width')}x{best.get('height')}")
        return best.get('url'), True  # Already square

    # No square thumbnail - return the largest one
    best = max(usable, key=lambda t: t.get('width', 0) * t.get('height', 0))
    logger.info(f"[Thumbnail] No square thumbnail available, using largest: {best.get('width')}x{best.get('height')}")
    return best.get('url'), False  # Not square


async def fetch_thumbnail_bytes(url: str) -> Optional[bytes]:
    """Fetch thumbnail bytes from a URL.

    Simple HTTP fetch with no processing.

    Args:
        url: The thumbnail URL.

    Returns:
        Image bytes, or None if fetch fails.
    """
    if not AIOHTTP_AVAILABLE or not url:
        return None

    try:
        async with aiohttp.ClientSession() as session:  # type: ignore[union-attr]
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:  # type: ignore[union-attr]
                if resp.status == 200:
                    data = await resp.read()
                    logger.debug(f"[Thumbnail] Fetched {len(data)} bytes from {url[:60]}...")
                    return data
                else:
                    logger.debug(f"[Thumbnail] HTTP {resp.status} for {url[:60]}...")
    except asyncio.TimeoutError:
        logger.debug(f"[Thumbnail] Fetch timeout for {url[:60]}...")
    except Exception as e:
        logger.debug(f"[Thumbnail] Fetch failed: {e}")

    return None


async def resize_thumbnail_bytes(data: bytes, width: int = THUMBNAIL_WIDTH) -> Optional[bytes]:
    """Resize thumbnail image to specified width using FFmpeg.

    Maintains aspect ratio - height is calculated automatically.
    For 16:9 source at 720px width, output will be 720x405.

    Args:
        data: Raw image bytes.
        width: Target width in pixels. Defaults to THUMBNAIL_WIDTH (720).

    Returns:
        Resized image bytes (JPEG), or original data if resize fails.
    """
    from .music_helpers import get_ffmpeg_path

    ffmpeg_path = get_ffmpeg_path()

    # FFmpeg command: read from stdin, scale to width (height auto), output JPEG to stdout
    cmd = [
        ffmpeg_path,
        '-hide_banner',
        '-loglevel', 'error',
        '-i', 'pipe:0',           # Read from stdin
        '-vf', f'scale={width}:-1',  # Scale to width, auto height
        '-f', 'image2',           # Output format
        '-c:v', 'mjpeg',          # JPEG codec
        '-q:v', '2',              # High quality (1-31, lower is better)
        'pipe:1'                  # Write to stdout
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=data),
            timeout=10.0
        )

        if proc.returncode == 0 and stdout:
            logger.debug(f"[Thumbnail] Resized {len(data)} -> {len(stdout)} bytes (width={width})")
            return stdout
        else:
            if stderr:
                logger.debug(f"[Thumbnail] FFmpeg resize failed: {stderr.decode()[:100]}")
            return data  # Return original on failure

    except asyncio.TimeoutError:
        logger.debug("[Thumbnail] FFmpeg resize timeout")
        return data
    except Exception as e:
        logger.debug(f"[Thumbnail] FFmpeg resize error: {e}")
        return data


async def fetch_and_resize_thumbnail(url: str, width: int = THUMBNAIL_WIDTH) -> Optional[bytes]:
    """Fetch thumbnail and resize to specified width.

    Args:
        url: The thumbnail URL.
        width: Target width in pixels. Defaults to THUMBNAIL_WIDTH (720).

    Returns:
        Resized image bytes, or None if fetch fails.
    """
    data = await fetch_thumbnail_bytes(url)
    if data:
        return await resize_thumbnail_bytes(data, width)
    return None


async def get_thumbnail_bytes(track: 'Track', cache_manager: Optional[Any] = None) -> Optional[bytes]:
    """Get thumbnail bytes for a Track.

    This is THE unified thumbnail function for playback/display.

    Priority:
    1. Extract from cached MP3 (already embedded)
    2. Fetch from track.thumbnail URL
    3. Fallback to constructed URL from video_id

    Args:
        track: The Track object.
        cache_manager: Optional MusicCacheManager for cached file lookup.

    Returns:
        Image bytes, or None if unavailable.
    """
    import os
    from .music_helpers import extract_mp3_thumbnail

    # Priority 1: Extract from cached MP3
    cached_mp3_path = None
    if cache_manager and track.video_id:
        cached_mp3_path = cache_manager.get_any_local_path(track.video_id)

    if cached_mp3_path and os.path.exists(cached_mp3_path):
        logger.debug(f"[Thumbnail] Checking cached MP3: {os.path.basename(cached_mp3_path)}")
        thumbnail_data = extract_mp3_thumbnail(cached_mp3_path)
        if thumbnail_data:
            logger.info(f"[Thumbnail] Using embedded MP3 thumbnail for {track.video_id}")
            return thumbnail_data

    # Priority 2: Use track's thumbnail URL (square only)
    # YTM 16:9 URLs have ?sqp= params that return pre-cropped images - skip them
    if track.thumbnail and track.thumbnail_is_square:
        logger.info(f"[Thumbnail] Using square thumbnail for {track.video_id}")
        logger.debug(
            f"[Thumbnail] URL: {track.thumbnail[:60]}..."
        )
        return await fetch_thumbnail_bytes(track.thumbnail)

    # Priority 3: Construct clean YouTube URL for 16:9 thumbnails
    # maxresdefault.jpg gives us clean 1280x720, then we resize to 720x405
    if track.video_id:
        # Log why we're skipping track.thumbnail if it exists
        if track.thumbnail:
            thumb_host = track.thumbnail.split('/')[2] if '/' in track.thumbnail else 'unknown'
            logger.debug(
                f"[Thumbnail] Skipping track.thumbnail (square=False, host={thumb_host})"
            )
        fallback_url = f"https://img.youtube.com/vi/{track.video_id}/maxresdefault.jpg"
        logger.info(f"[Thumbnail] Using 16:9 YouTube thumbnail for {track.video_id}")
        logger.debug(f"[Thumbnail] URL: {fallback_url}")
        return await fetch_and_resize_thumbnail(fallback_url)

    logger.debug("[Thumbnail] No thumbnail source available")
    return None


# ==========================================================================
# YTM SEARCH FUNCTIONS
# ==========================================================================


def _parse_ytm_result(item: Dict[str, Any]) -> Optional[SearchResult]:
    """Parse a YTM search result item into a SearchResult.

    Args:
        item: Raw result from ytm.search().

    Returns:
        SearchResult or None if not playable.
    """
    result_type = item.get('resultType')

    # Skip non-playable types
    if result_type in ('artist', 'album', 'playlist', 'podcast'):
        return None

    # Skip podcast episodes
    video_type = item.get('videoType', '')
    if video_type == 'MUSIC_VIDEO_TYPE_PODCAST_EPISODE':
        return None

    video_id = item.get('videoId')
    if not video_id:
        return None

    # Determine source based on result type and video type
    is_atv = video_type == 'MUSIC_VIDEO_TYPE_ATV'
    source = 'ytm_song' if is_atv else 'ytm_video'

    # Extract artist info
    artists = item.get('artists', [])
    artist_name = artists[0].get('name', 'Unknown') if artists else item.get('author', 'Unknown')
    artist_id = artists[0].get('id') if artists else None

    # Extract album
    album_info = item.get('album', {})
    album_name = album_info.get('name') if isinstance(album_info, dict) else None

    # Extract thumbnail
    thumbnails = item.get('thumbnails', [])
    thumb_url = None
    thumb_is_square = False
    if thumbnails:
        largest = thumbnails[-1]
        thumb_is_square = largest.get('width') == largest.get('height')
        raw_url = largest.get('url')
        # Resize YTM lh3 URLs to 720px (they come as 120x120 by default)
        if raw_url and thumb_is_square:
            thumb_url = resize_ytm_thumbnail(raw_url, THUMBNAIL_WIDTH)
        else:
            thumb_url = raw_url

    return SearchResult(
        video_id=video_id,
        title=item.get('title', 'Unknown'),
        artist=artist_name,
        artist_id=artist_id,
        album=album_name,
        duration_seconds=item.get('duration_seconds'),  # May be None, filled by search_ytm
        thumbnail_url=thumb_url,
        thumbnail_is_square=thumb_is_square,
        source=source,
        video_type=video_type,
    )


async def search_ytm(query: str, limit: int = 10) -> List[SearchResult]:
    """Search YouTube Music for tracks.

    Performs unfiltered search (songs + videos) and filtered songs search in
    parallel to get both mixed results AND album info for ATVs.

    Args:
        query: Search query string.
        limit: Maximum results to return.

    Returns:
        List of SearchResult objects (songs and videos mixed, with album info).
    """
    ytm = _get_ytm()
    if not ytm:
        logger.debug("[YTM Search] YTMusic not available")
        return []

    try:
        def do_unfiltered_search() -> List[Dict[str, Any]]:
            return ytm.search(query, limit=limit)  # type: ignore[union-attr]

        def do_songs_search() -> List[Dict[str, Any]]:
            return ytm.search(query, filter="songs", limit=limit)  # type: ignore[union-attr]

        # Run both searches in parallel
        unfiltered_task = asyncio.to_thread(do_unfiltered_search)
        songs_task = asyncio.to_thread(do_songs_search)

        unfiltered_results, songs_results = await asyncio.wait_for(
            asyncio.gather(unfiltered_task, songs_task),
            timeout=15.0
        )

        # Build album and explicit maps from filtered songs search
        album_map: Dict[str, Optional[str]] = {}
        explicit_map: Dict[str, Optional[bool]] = {}
        for item in songs_results:
            video_id = item.get('videoId')
            if video_id:
                album_info = item.get('album')
                if album_info:
                    album_map[video_id] = album_info.get('name')
                # isExplicit is True/False/None in filtered search results
                explicit_map[video_id] = item.get('isExplicit')

        # Parse unfiltered results (has both songs and videos)
        parsed: List[SearchResult] = []
        for item in unfiltered_results:
            result = _parse_ytm_result(item)
            if result:
                # Attach album and explicit info from filtered search if available
                if result.video_id in album_map:
                    result.album = album_map[result.video_id]
                if result.video_id in explicit_map:
                    result.is_explicit = explicit_map[result.video_id]
                parsed.append(result)

        # Fetch duration for results that don't have it (songs often missing duration in search)
        for result in parsed:
            if result.duration_seconds is None and result.video_id:
                metadata = await get_ytm_metadata(result.video_id)
                if metadata:
                    video_details = metadata.get('videoDetails', {})
                    length = video_details.get('lengthSeconds')
                    if length:
                        result.duration_seconds = int(length)

        # Detailed logging of YTM results
        logger.debug(f"[YTM Search] Found {len(parsed)} results for '{query}':")
        for i, r in enumerate(parsed):
            thumb_host = r.thumbnail_url.split('/')[2] if r.thumbnail_url else 'none'
            logger.debug(
                f"  [{i}] {r.source} | {r.video_id} | '{r.title[:40]}' | "
                f"square={r.thumbnail_is_square} | thumb_host={thumb_host}"
            )

        # Summary at INFO level
        songs = sum(1 for r in parsed if r.source == 'ytm_song')
        videos = sum(1 for r in parsed if r.source == 'ytm_video')
        logger.info(f"[YTM Search] '{query}' -> {songs} songs, {videos} videos")
        return parsed

    except Exception as e:
        logger.warning(f"[YTM Search] Error searching: {e}")
        return []


async def get_ytm_metadata(video_id: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a video from YouTube Music.

    Args:
        video_id: YouTube video ID.

    Returns:
        Raw metadata dict, or None if unavailable.
    """
    ytm = _get_ytm()
    if not ytm:
        return None

    try:
        def do_get() -> Dict[str, Any]:
            return ytm.get_song(video_id)  # type: ignore[union-attr]

        result = await asyncio.wait_for(
            asyncio.to_thread(do_get),
            timeout=15.0
        )

        # Check if valid (invalid IDs return empty videoDetails)
        video_details = result.get('videoDetails', {})
        if not video_details.get('title'):
            logger.debug(f"[YTM Metadata] No valid data for {video_id}")
            return None

        return result

    except Exception as e:
        logger.warning(f"[YTM Metadata] Error getting metadata for {video_id}: {e}")
        return None


def is_atv(metadata: Dict[str, Any]) -> bool:
    """Check if metadata represents an Audio Track Version.

    Args:
        metadata: Result from get_ytm_metadata().

    Returns:
        True if this is an ATV (clean metadata, square thumbnail).
    """
    video_details = metadata.get('videoDetails', {})
    video_type = video_details.get('musicVideoType', '')
    return video_type == 'MUSIC_VIDEO_TYPE_ATV'


# ==========================================================================
# YOUTUBE (YT-DLP) SEARCH FUNCTIONS
# ==========================================================================


# yt-dlp options for searching
YTDLP_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'extract_flat': 'in_playlist',
    'noplaylist': True,
}


async def search_youtube(query: str, limit: int = 6) -> List[SearchResult]:
    """Search YouTube using yt-dlp.

    Args:
        query: Search query string.
        limit: Maximum results to return.

    Returns:
        List of SearchResult objects.
    """
    if not yt_dlp:
        logger.debug("[YT Search] yt-dlp not available")
        return []

    try:
        search_query = f"ytsearch{limit}:{query}"

        def do_search() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, YTDLP_SEARCH_OPTIONS)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(search_query, download=False)  # type: ignore

        info = await asyncio.wait_for(
            asyncio.to_thread(do_search),
            timeout=15.0
        )

        if not info:
            return []

        results: List[SearchResult] = []
        entries = info.get('entries', [])

        for entry in entries:
            if not entry:
                continue

            results.append(SearchResult(
                video_id=entry.get('id', ''),
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown')),
                duration_seconds=int(entry.get('duration', 0) or 0),
                thumbnail_url=entry.get('thumbnail'),
                thumbnail_is_square=False,  # YouTube thumbnails are 16:9
                source='youtube',
            ))

        # Detailed logging of YouTube results
        logger.debug(f"[YT Search] Found {len(results)} results for '{query}':")
        for i, r in enumerate(results):
            logger.debug(
                f"  [{i}] {r.source} | {r.video_id} | '{r.title[:40]}' | "
                f"square={r.thumbnail_is_square}"
            )

        # Summary at INFO level
        logger.info(f"[YT Search] '{query}' -> {len(results)} results")
        return results

    except Exception as e:
        logger.warning(f"[YT Search] Error searching: {e}")
        return []


# ==========================================================================
# DEDUPLICATION & SCORING
# ==========================================================================


def dedupe_results(
    ytm_results: List[SearchResult],
    yt_results: List[SearchResult],
    exclude_id: Optional[str] = None
) -> Tuple[List[SearchResult], List[SearchResult]]:
    """Deduplicate results, preferring YTM versions.

    Args:
        ytm_results: Results from YTM search.
        yt_results: Results from yt-dlp search.
        exclude_id: Optional video ID to exclude (user's original URL).

    Returns:
        Tuple of (ytm_results, yt_results) with duplicates removed from yt_results.
    """
    ytm_ids = {r.video_id for r in ytm_results}

    # Filter yt_results to remove duplicates and excluded ID
    filtered_yt = []
    deduped_ids = []
    for r in yt_results:
        if r.video_id in ytm_ids:
            deduped_ids.append(r.video_id)
        elif r.video_id == exclude_id:
            pass  # Excluded
        else:
            filtered_yt.append(r)

    # YTM results are NOT filtered by exclude_id - the same video might be
    # the ATV version with better metadata (slot 0 = original, slot 1 = ATV)

    # Log dedupe results
    if deduped_ids:
        logger.debug(f"[Dedupe] Removed {len(deduped_ids)} YT results (dupes of YTM): {deduped_ids}")
    logger.debug(
        f"[Dedupe] Final counts: YTM={len(ytm_results)}, YT={len(filtered_yt)} "
        f"(excluded_id={exclude_id})"
    )

    return ytm_results, filtered_yt


def text_similarity(a: str, b: str) -> float:
    """Calculate text similarity ratio.

    Args:
        a: First string.
        b: Second string.

    Returns:
        Similarity ratio 0.0-1.0.
    """
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ==========================================================================
# CONFIDENCE FILTERING
# ==========================================================================


def _normalize_for_comparison(text: str) -> set[str]:
    """Normalize text for word overlap comparison."""
    # Remove common punctuation and split
    # Fullwidth brackets (Japanese) + standard punctuation
    cleaned = re.sub(r'[\u3010\u3011\[\]()\uff08\uff09\u300c\u300d\u300e\u300f\-_/\\|]', ' ', text)
    words = cleaned.lower().split()
    # Filter out very short words and common stopwords
    stopwords = {'the', 'a', 'an', 'and', 'or', 'of', 'in', 'to', 'for', 'by', 'feat', 'ft'}
    return {w for w in words if len(w) > 1 and w not in stopwords}


def is_relevant(query: str, result: SearchResult, strict: bool = False) -> bool:
    """Check if a result is relevant to the query.

    Uses character-level similarity and substring matching to handle:
    - CJK titles without spaces
    - Packed titles like "Song【Official】MV"
    - Mixed language content

    Args:
        query: Original search query.
        result: Search result to check.
        strict: If True, use tighter thresholds (for URL mode where query is
            a video title). If False, use looser thresholds (for human queries).

    Returns:
        True if result seems relevant.
    """
    q = query.lower()
    combined = f"{result.title} {result.artist}".lower()

    # Garbage filtering thresholds - very low, only catches truly unrelated results
    # Strict mode (URL) is slightly tighter since titles are cleaner than human queries
    char_sim_threshold = 0.20 if strict else 0.15
    short_query_hit_ratio = 0.15 if strict else 0.10
    long_query_hit_ratio = 0.20 if strict else 0.15

    # 1. Character-level similarity (handles CJK, packed titles)
    char_sim = SequenceMatcher(None, q, combined).ratio()
    if char_sim > char_sim_threshold:
        return True

    # 2. Substring matching - check if query segments appear anywhere
    query_words = _normalize_for_comparison(query)
    if query_words:
        substring_hits = sum(1 for w in query_words if w in combined)
        hit_ratio = substring_hits / len(query_words)

        # Scale requirement with query length
        if len(query_words) <= 3:
            return hit_ratio >= short_query_hit_ratio
        else:
            return hit_ratio >= long_query_hit_ratio

    return False


# ==========================================================================
# JAPANESE/CHINESE NAME EXTRACTION
# ==========================================================================


def has_cjk(text: str) -> bool:
    """Check if text contains CJK (Chinese/Japanese/Korean) characters."""
    return bool(re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text))


# Tags to skip when extracting artist names
_SKIP_TAGS = {
    'ホロライブ', 'hololive', 'にじさんじ', 'nijisanji',
    '歌ってみた', 'cover', 'original', 'オリジナル',
    '実況', 'バーチャル', 'vtuber', 'virtual',
    'music', 'mv', 'pv', 'lyric', 'lyrics',
}


def extract_jp_names(tags: List[str]) -> List[str]:
    """Extract likely Japanese artist names from video tags.

    Args:
        tags: List of video tags.

    Returns:
        List of tags that look like Japanese artist names.
    """
    names: List[str] = []
    for tag in tags:
        # Must contain CJK characters
        if not has_cjk(tag):
            continue
        # Length filter (artist names are typically 2-20 chars)
        if not (2 <= len(tag) <= 20):
            continue
        # Skip common non-name tags
        if any(skip.lower() in tag.lower() for skip in _SKIP_TAGS):
            continue
        names.append(tag)
    return names


# ==========================================================================
# UNIFIED SEARCH API
# ==========================================================================


async def search_query_mode(query: str) -> Tuple[List[SearchResult], List[SearchResult]]:
    """Search for a user query (Query Mode).

    Searches both YTM and YouTube in parallel, dedupes, and returns results
    split into songs (ATVs) and videos.

    Args:
        query: User's search query.

    Returns:
        Tuple of (songs, videos) - up to 3 of each.
    """
    # Parallel search
    ytm_task = search_ytm(query, limit=10)
    yt_task = search_youtube(query, limit=6)

    ytm_results, yt_results = await asyncio.gather(
        ytm_task, yt_task, return_exceptions=True
    )

    # Handle exceptions
    if isinstance(ytm_results, Exception):
        logger.warning(f"[Query Mode] YTM search failed: {ytm_results}")
        ytm_results = []
    if isinstance(yt_results, Exception):
        logger.warning(f"[Query Mode] YT search failed: {yt_results}")
        yt_results = []

    # Dedupe (prefer YTM)
    ytm_results, yt_results = dedupe_results(
        cast(List[SearchResult], ytm_results),
        cast(List[SearchResult], yt_results)
    )

    # Filter irrelevant results
    ytm_results = [r for r in ytm_results if is_relevant(query, r)]
    yt_results = [r for r in yt_results if is_relevant(query, r)]

    # Split YTM into songs (ATVs) and videos
    songs = [r for r in ytm_results if r.source == 'ytm_song']
    ytm_videos = [r for r in ytm_results if r.source == 'ytm_video']
    videos = ytm_videos + yt_results

    # Log final composition
    logger.debug(
        f"[Query Mode] Final split for '{query}': "
        f"{len(songs)} songs (YTM ATVs), {len(ytm_videos)} YTM videos, {len(yt_results)} YT videos"
    )
    logger.debug("[Query Mode] Songs (will be slots 1-3):")
    for i, r in enumerate(songs[:3]):
        logger.debug(f"  [{i+1}] {r.video_id} | '{r.title[:40]}' | {r.source}")
    logger.debug("[Query Mode] Videos (will be slots 4-6):")
    for i, r in enumerate(videos[:3]):
        src_note = '(from YTM)' if r.source == 'ytm_video' else '(from YT)'
        logger.debug(f"  [{i+4}] {r.video_id} | '{r.title[:40]}' | {r.source} {src_note}")

    # INFO summary
    logger.info(
        f"[Search] '{query}' -> presenting {len(songs[:3])} songs + {len(videos[:3])} videos"
    )

    # Take top 3 of each
    return songs[:3], videos[:3]


async def search_url_mode(
    video_id: str
) -> Tuple[Optional[SearchResult], List[SearchResult], List[SearchResult], Optional[str]]:
    """Search for alternatives to a user-provided URL (URL Mode).

    If the URL is already an ATV, returns (None, [], [], None) - just play it.
    Otherwise, searches for matching ATVs and related videos.

    Args:
        video_id: YouTube video ID from user's URL.

    Returns:
        Tuple of (original, songs, videos, recommended_id):
        - original: SearchResult for user's URL (None if ATV - just play it)
        - songs: Up to 3 ATVs (first is best match)
        - videos: Up to 3 related videos
        - recommended_id: Video ID of recommended song if confidence > 0.5, else None

    """
    # Get metadata for the original video
    metadata = await get_ytm_metadata(video_id)

    if not metadata:
        # Couldn't get metadata - return original only, no alternatives
        logger.debug(f"[URL Mode] No metadata for {video_id}, returning original only")
        original_fallback = SearchResult(
            video_id=video_id,
            title="Unknown",
            artist="Unknown",
            source='youtube',
        )
        return original_fallback, [], [], None

    video_details = metadata.get('videoDetails', {})

    # Check if already an ATV - if so, just play it
    if is_atv(metadata):
        logger.info(f"[URL Mode] {video_id} is already an ATV, playing directly")
        return None, [], [], None  # None signals "just play it"

    # Extract metadata for searching
    title = video_details.get('title', 'Unknown')
    author = video_details.get('author', 'Unknown')
    duration = int(video_details.get('lengthSeconds', 0) or 0)

    # Extract tags for Japanese name extraction
    microformat = metadata.get('microformat', {}).get('microformatDataRenderer', {})
    tags = microformat.get('tags', [])
    jp_names = extract_jp_names(tags)

    # Build original SearchResult
    thumbnails = video_details.get('thumbnail', {}).get('thumbnails', [])
    thumb_url = thumbnails[-1].get('url') if thumbnails else None

    original = SearchResult(
        video_id=video_id,
        title=title,
        artist=author,
        duration_seconds=duration,
        thumbnail_url=thumb_url,
        thumbnail_is_square=False,
        source='youtube',
    )

    # Multi-query search for YTM
    # Primary query always first, then Japanese name variants if available
    queries = [f"{title} {author}"]
    for jp_name in jp_names[:2]:  # Limit to 2 Japanese queries
        queries.append(f"{title} {jp_name}")

    # Search YTM with all queries, collecting results
    # NOTE: We do NOT exclude the original video_id from YTM results.
    # If the original appears as an ATV, that's valuable - user can choose between
    # their URL (slot 0, YouTube metadata) or the YTM version (slot 1, clean metadata).
    # Even if same video_id, the metadata quality differs.
    all_ytm_results: List[SearchResult] = []
    seen_ids: set[str] = set()
    original_artist_id: Optional[str] = None  # Artist ID from original video in YTM
    original_found_as_atv = False

    for query in queries:
        results = await search_ytm(query, limit=5)
        for r in results:
            # Capture artist_id from the ORIGINAL video if it appears in YTM
            # This lets us match the MV's artist to potential ATVs
            if r.video_id == video_id and r.artist_id and not original_artist_id:
                original_artist_id = r.artist_id
                logger.debug(f"[URL Mode] Found original {video_id} in YTM with artist_id={r.artist_id}")

            if r.video_id not in seen_ids:
                seen_ids.add(r.video_id)
                all_ytm_results.append(r)
                # Track if original appears as ATV for recommendation
                if r.video_id == video_id and r.source == 'ytm_song':
                    original_found_as_atv = True
                    logger.info(f"[URL Mode] Original {video_id} found as ATV in YTM results")

    # Search YouTube (title only, as discussed)
    # DO exclude original from YouTube results - same source, no benefit showing twice
    yt_results = await search_youtube(title, limit=6)
    yt_results = [r for r in yt_results if r.video_id not in seen_ids and r.video_id != video_id]

    # Split YTM results into songs (ATVs) and videos
    # Trust YTM's ranking - don't re-sort, just take in order
    songs = [r for r in all_ytm_results if r.source == 'ytm_song']
    videos = [r for r in all_ytm_results if r.source == 'ytm_video'] + yt_results

    # Garbage filter - only filter videos, NOT songs
    # YTM ATVs are curated catalog entries - if YTM returned them, they're relevant
    # Videos (YTM videos + YouTube) can be noisy user uploads, so filter those
    # Note: This fixes JP↔EN title mismatch where correct ATV has Japanese title
    videos = [r for r in videos if is_relevant(title, r, strict=True)]

    # Determine recommended_id - priority order:
    # 1. Original video_id found as ATV in search → 100% match, it IS the ATV
    # 2. Semantic matching for top ATV (conservative to avoid false positives on covers)
    recommended_id: Optional[str] = None

    if original_found_as_atv:
        # The user's URL is literally the ATV version - can't get more certain than this
        recommended_id = video_id
        logger.info(f"[URL Mode] Original {video_id} IS the ATV - 100% match")
    elif songs:
        top_song = songs[0]

        # Matching using DETERMINISTIC signals:
        # - Artist ID match: original MV's artist_id == top song's artist_id
        # - Title similarity >= 0.85 (allows for feat. additions, language variants)
        # Note: We can't get the MV→ATV link from YouTube's API, so we rely on text matching
        artist_id_matches = (
            original_artist_id is not None
            and top_song.artist_id is not None
            and original_artist_id == top_song.artist_id
        )
        title_sim = text_similarity(title, top_song.title)

        if artist_id_matches and title_sim >= 0.85:
            recommended_id = top_song.video_id
            logger.info(
                f"[URL Mode] Recommending {recommended_id} | "
                f"artist_id=✓ ({original_artist_id}), title_sim={title_sim:.2f}"
            )
        else:
            logger.debug(
                f"[URL Mode] No recommendation - artist_id_match={artist_id_matches} "
                f"(orig={original_artist_id}, song={top_song.artist_id}), "
                f"title_sim={title_sim:.2f}"
            )

    # Take top 3 songs (YTM's ranking, filtered for relevance)
    final_songs = songs[:3]

    logger.debug(f"[URL Mode] Found {len(final_songs)} songs, {len(videos[:3])} videos for {video_id}")

    return original, final_songs, videos[:3], recommended_id


# ==========================================================================
# LEGACY COMPATIBILITY - will be removed after migration
# ==========================================================================


async def search_youtube_legacy(
    query: str,
    max_results: int,
    logger_instance: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> List['Track']:
    """Legacy wrapper for old search_youtube signature.

    This maintains compatibility with existing code during migration.
    Will be removed once all callers are updated.
    """
    results = await search_youtube(query, limit=max_results)
    return [r.to_track() for r in results]

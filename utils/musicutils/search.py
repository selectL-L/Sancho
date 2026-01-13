"""Unified search and metadata provider for music playback.

This module provides search functionality across YTM and YouTube with:
- Multi-query search with Japanese name extraction (for cross-language matching)
- Language-aware garbage filtering (trusts YTM for cross-script results)
- Confidence-based star scoring (not ID-gated)
- Version labels and view counts for display

Philosophy: "Right enough" over "perfectly accurate" — present good options,
recommend one with a star, and trust the user to choose.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
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
# CONSTANTS
# ==========================================================================

# Video type constants from YTM
MUSIC_VIDEO_TYPE_ATV = "MUSIC_VIDEO_TYPE_ATV"
MUSIC_VIDEO_TYPE_OMV = "MUSIC_VIDEO_TYPE_OMV"
MUSIC_VIDEO_TYPE_UGC = "MUSIC_VIDEO_TYPE_UGC"
MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE = "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC"

# Version label mapping
VERSION_LABELS = {
    MUSIC_VIDEO_TYPE_ATV: "Official Audio",
    MUSIC_VIDEO_TYPE_OMV: "Music Video",
    MUSIC_VIDEO_TYPE_UGC: "Cover",
    MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE: "Official Source",
}

# Scoring thresholds
STAR_THRESHOLD = 0.6  # Minimum combined score to award star
GARBAGE_SIMILARITY_THRESHOLD = 0.3  # Below this = garbage (same-script only)

# Thumbnail constants
THUMBNAIL_WIDTH = 720  # Target width for resized thumbnails


# ==========================================================================
# YTMUSIC SINGLETON
# ==========================================================================

_ytm: Optional[YTMusic] = None  # type: ignore[assignment]


def _get_ytm() -> Optional[YTMusic]:  # type: ignore[return]
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

    Contains search-specific metadata that Track doesn't need for playback,
    plus display fields (version_label, view_count) for UI presentation.
    """
    video_id: str
    title: str
    artist: str
    artist_id: Optional[str] = None
    album: Optional[str] = None
    duration_seconds: Optional[int] = None
    thumbnail_url: Optional[str] = None
    thumbnail_is_square: bool = False
    source: str = 'youtube'  # 'ytm_song', 'ytm_video', 'youtube'
    video_type: Optional[str] = None
    is_explicit: Optional[bool] = None

    # NEW: Display fields
    version_label: str = "Video"
    view_count: Optional[int] = None

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
            version_label=self.version_label,
            view_count=self.view_count,
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

    Args:
        url: YouTube video URL.

    Returns:
        11-character video ID, or None if not found.
    """
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ==========================================================================
# HELPER FUNCTIONS
# ==========================================================================


def determine_version_label(video_type: Optional[str], source: str) -> str:
    """Map video type to human-readable label.

    Args:
        video_type: YTM video type constant (e.g., MUSIC_VIDEO_TYPE_ATV)
        source: Source identifier ('ytm_song', 'ytm_video', 'youtube')

    Returns:
        Human-readable label for display.
    """
    if video_type and video_type in VERSION_LABELS:
        return VERSION_LABELS[video_type]
    return "Video"


def has_cjk(text: str) -> bool:
    """Check if text contains CJK (Chinese/Japanese/Korean) characters.

    Args:
        text: String to check.

    Returns:
        True if any CJK characters are present.
    """
    for char in text:
        if '\u4e00' <= char <= '\u9fff':  # CJK Unified Ideographs
            return True
        if '\u3040' <= char <= '\u309f':  # Hiragana
            return True
        if '\u30a0' <= char <= '\u30ff':  # Katakana
            return True
        if '\uac00' <= char <= '\ud7af':  # Korean Hangul
            return True
    return False


def normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, strip, normalize unicode).

    Args:
        text: Text to normalize.

    Returns:
        Normalized text.
    """
    text = unicodedata.normalize('NFKC', text)
    text = text.lower().strip()
    # Remove common suffixes that vary between versions
    text = re.sub(r'\s*[\(\[](official\s*)?(music\s*)?(video|audio|mv|lyric|lyrics|visualizer)[\)\]]', '', text, flags=re.IGNORECASE)
    return text


def text_similarity(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings.

    Args:
        a: First string.
        b: Second string.

    Returns:
        Similarity ratio between 0.0 and 1.0.
    """
    if not a or not b:
        return 0.0
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def text_contains(haystack: str, needle: str) -> bool:
    """Check if one text contains the other (normalized).

    Args:
        haystack: Text to search in.
        needle: Text to search for.

    Returns:
        True if needle is contained in haystack.
    """
    return normalize_text(needle) in normalize_text(haystack)


def format_view_count(count: Optional[int]) -> str:
    """Format view count for display (e.g., 1.2M views).

    Args:
        count: Raw view count.

    Returns:
        Formatted string, or empty string if None.
    """
    if count is None:
        return ""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B views"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M views"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K views"
    return f"{count} views"


# ==========================================================================
# LANGUAGE-AWARE GARBAGE FILTER
# ==========================================================================


def is_relevant(query: str, result: SearchResult) -> bool:
    """Determine if a search result is relevant to the query.

    Uses language-aware filtering:
    - Cross-script (Latin query, CJK result or vice versa): Trust YTM's judgment
    - Same-script: Apply similarity/substring checks to catch garbage

    Args:
        query: Original search query.
        result: Search result to evaluate.

    Returns:
        True if result should be kept, False if garbage.
    """
    query_has_cjk = has_cjk(query)
    title_has_cjk = has_cjk(result.title)

    # Cross-script: trust YTM, they wouldn't return unrelated cross-language results
    if query_has_cjk != title_has_cjk:
        logger.debug(f"Cross-script match, trusting YTM: {result.title}")
        return True

    # Same-script: apply similarity checks
    combined = f"{result.title} {result.artist}"

    # Check if query words appear in result
    query_words = set(normalize_text(query).split())
    result_words = set(normalize_text(combined).split())

    # If any query word appears in result, it's relevant
    if query_words & result_words:
        return True

    # Containment check
    if text_contains(combined, query) or text_contains(query, result.title):
        return True

    # Similarity fallback
    title_sim = text_similarity(query, result.title)
    combined_sim = text_similarity(query, combined)

    if title_sim >= GARBAGE_SIMILARITY_THRESHOLD or combined_sim >= GARBAGE_SIMILARITY_THRESHOLD:
        return True

    logger.debug(f"Filtered as garbage: {result.title} (sim={title_sim:.2f})")
    return False


# ==========================================================================
# CONFIDENCE-BASED STAR SCORING
# ==========================================================================


@dataclass
class OriginalMetadata:
    """Metadata from the original video for comparison during star scoring."""
    title: str
    artist: str
    artist_id: Optional[str] = None


def calculate_artist_confidence(original: OriginalMetadata, candidate: SearchResult) -> float:
    """Calculate confidence that candidate is by the same artist as original.

    Artist ID match is a BONUS, not a gate. Text similarity is the base.

    Args:
        original: Metadata from original video.
        candidate: Search result candidate.

    Returns:
        Confidence score between 0.0 and 1.0.
    """
    # Base: text similarity
    base_sim = text_similarity(original.artist, candidate.artist)

    # ID match bonus: if both have IDs and they match, boost to at least 0.85
    if original.artist_id and candidate.artist_id:
        if original.artist_id == candidate.artist_id:
            return max(base_sim, 0.85)

    return base_sim


def calculate_title_match(orig_title: str, cand_title: str) -> float:
    """Calculate how well candidate title matches original.

    Containment-first (if one contains the other, high match).
    Similarity-fallback.

    Args:
        orig_title: Original video title.
        cand_title: Candidate title.

    Returns:
        Match score between 0.0 and 1.0.
    """
    orig_norm = normalize_text(orig_title)
    cand_norm = normalize_text(cand_title)

    # Containment: strong signal
    if orig_norm in cand_norm or cand_norm in orig_norm:
        return 0.9

    # Similarity fallback
    return text_similarity(orig_title, cand_title)


def score_candidate(original: OriginalMetadata, candidate: SearchResult) -> float:
    """Score a candidate for star assignment.

    Sliding scale: higher artist confidence → lower title threshold required.
    Perfect title match always passes regardless of artist.

    Thresholds (approximate):
        - Artist 100% → Title 25%
        - Artist 75%  → Title 45%
        - Artist 50%  → Title 65%

    Args:
        original: Metadata from original video.
        candidate: Search result candidate.

    Returns:
        Combined score if candidate passes, 0.0 if fails.
    """
    artist_conf = calculate_artist_confidence(original, candidate)
    title_match = calculate_title_match(original.title, candidate.title)

    logger.info(
        f"Scoring candidate: '{candidate.title}' by '{candidate.artist}' | "
        f"artist_conf={artist_conf:.2f}, title_match={title_match:.2f}"
    )

    # Perfect title match always passes (regardless of artist)
    if title_match >= 0.95:
        score = (artist_conf * 0.4) + (title_match * 0.6)
        logger.info(f"  → AUTO-PASS (perfect title match): score={score:.2f}")
        return score

    # Sliding threshold: title_threshold = 1.05 - (0.8 * artist_conf)
    # Artist 1.0 → title needs 0.25
    # Artist 0.75 → title needs 0.45
    # Artist 0.5 → title needs 0.65
    title_threshold = 1.05 - (artist_conf * 0.8)

    if title_match < title_threshold:
        logger.info(
            f"  → FAILED: title_match {title_match:.2f} < threshold {title_threshold:.2f} "
            f"(required for artist_conf={artist_conf:.2f})"
        )
        return 0.0

    # Combined score: weighted average
    score = (artist_conf * 0.4) + (title_match * 0.6)
    logger.info(f"  → PASSED: score={score:.2f} (threshold was {title_threshold:.2f})")
    return score


def find_star(original: OriginalMetadata, candidates: List[SearchResult]) -> Optional[SearchResult]:
    """Find the best ATV candidate to star.

    Walks candidates in order, scores each ATV, returns first above threshold.

    Args:
        original: Metadata from original video.
        candidates: List of search result candidates.

    Returns:
        Best candidate above threshold, or None.
    """
    best_candidate: Optional[SearchResult] = None
    best_score = 0.0

    for candidate in candidates:
        # Only consider ATVs for starring
        if candidate.video_type != MUSIC_VIDEO_TYPE_ATV:
            continue

        score = score_candidate(original, candidate)
        if score > best_score and score >= STAR_THRESHOLD:
            best_score = score
            best_candidate = candidate

    if best_candidate:
        logger.info(f"Star assigned to: {best_candidate.title} (score={best_score:.2f})")

    return best_candidate


# ==========================================================================
# JAPANESE NAME EXTRACTION
# ==========================================================================

# Tags to skip when extracting artist names
_SKIP_TAGS = {
    'ホロライブ', 'hololive', 'にじさんじ', 'nijisanji',
    '歌ってみた', 'cover', 'original', 'オリジナル',
    '実況', 'バーチャル', 'vtuber', 'virtual',
    'music', 'mv', 'pv', 'lyric', 'lyrics',
}


def extract_jp_names(tags: List[str]) -> List[str]:
    """Extract likely Japanese artist names from video tags.

    This is the crown jewel for cross-language matching. Video tags contain
    both romanized and Japanese names (e.g., 'Murasaki Shion' and '紫咲シオン').
    By searching with both, we find ATVs regardless of how YTM indexed them.

    Args:
        tags: List of video tags.

    Returns:
        List of tags that look like Japanese artist names.
    """
    names: List[str] = []
    for tag in tags:
        if not has_cjk(tag):
            continue
        if not (2 <= len(tag) <= 20):
            continue
        if any(skip.lower() in tag.lower() for skip in _SKIP_TAGS):
            continue
        names.append(tag)
    return names


# ==========================================================================
# YTM RESULT PARSING
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

    video_type = item.get('videoType', '')
    if video_type == 'MUSIC_VIDEO_TYPE_PODCAST_EPISODE':
        return None

    video_id = item.get('videoId')
    if not video_id:
        return None

    # Determine source and version label
    is_atv = video_type == MUSIC_VIDEO_TYPE_ATV
    source = 'ytm_song' if is_atv else 'ytm_video'
    version_label = determine_version_label(video_type, source)

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
        duration_seconds=item.get('duration_seconds'),
        thumbnail_url=thumb_url,
        thumbnail_is_square=thumb_is_square,
        source=source,
        video_type=video_type,
        version_label=version_label,
    )


# ==========================================================================
# YTM SEARCH & METADATA
# ==========================================================================


async def get_ytm_metadata(video_id: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a video from YouTube Music.

    Args:
        video_id: YouTube video ID.

    Returns:
        Raw metadata dict from get_song(), or None if unavailable.
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
        True if this is an ATV.
    """
    video_details = metadata.get('videoDetails', {})
    video_type = video_details.get('musicVideoType', '')
    return video_type == MUSIC_VIDEO_TYPE_ATV


def extract_view_count(metadata: Dict[str, Any]) -> Optional[int]:
    """Extract view count from YTM metadata.

    Args:
        metadata: Result from get_ytm_metadata().

    Returns:
        View count as integer, or None if unavailable.
    """
    video_details = metadata.get('videoDetails', {})
    view_count = video_details.get('viewCount')
    if view_count:
        try:
            return int(view_count)
        except (ValueError, TypeError):
            pass
    return None


async def search_ytm(query: str, limit: int = 10) -> List[SearchResult]:
    """Search YouTube Music for tracks.

    Performs unfiltered search (songs + videos) and filtered songs search
    in parallel to get both mixed results AND album info for ATVs.

    Args:
        query: Search query string.
        limit: Maximum results to return.

    Returns:
        List of SearchResult objects (songs and videos mixed).
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

        # Build maps from filtered songs search (has album + explicit info)
        album_map: Dict[str, Optional[str]] = {}
        explicit_map: Dict[str, Optional[bool]] = {}
        for item in songs_results:
            video_id = item.get('videoId')
            if video_id:
                album_info = item.get('album')
                if album_info:
                    album_map[video_id] = album_info.get('name')
                explicit_map[video_id] = item.get('isExplicit')

        # Parse unfiltered results
        parsed: List[SearchResult] = []
        for item in unfiltered_results:
            result = _parse_ytm_result(item)
            if result:
                # Attach album and explicit info from filtered search
                if result.video_id in album_map:
                    result.album = album_map[result.video_id]
                if result.video_id in explicit_map:
                    result.is_explicit = explicit_map[result.video_id]
                parsed.append(result)

        # Fetch metadata for results missing duration or view_count
        # Collect all results that need metadata fetching
        results_needing_metadata = [
            r for r in parsed
            if r.video_id and (r.duration_seconds is None or r.view_count is None)
        ]

        if results_needing_metadata:
            # Fetch metadata in parallel for efficiency
            async def fetch_and_update(result: SearchResult) -> None:
                metadata = await get_ytm_metadata(result.video_id)
                if metadata:
                    video_details = metadata.get('videoDetails', {})
                    if result.duration_seconds is None:
                        length = video_details.get('lengthSeconds')
                        if length:
                            result.duration_seconds = int(length)
                    if result.view_count is None:
                        result.view_count = extract_view_count(metadata)

            await asyncio.gather(*[fetch_and_update(r) for r in results_needing_metadata])

        # Log results
        songs = sum(1 for r in parsed if r.source == 'ytm_song')
        videos = sum(1 for r in parsed if r.source == 'ytm_video')
        logger.info(f"[YTM Search] '{query}' -> {songs} songs, {videos} videos")

        return parsed

    except Exception as e:
        logger.warning(f"[YTM Search] Error searching: {e}")
        return []


# ==========================================================================
# YOUTUBE (YT-DLP) SEARCH
# ==========================================================================

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
                return ydl.extract_info(search_query, download=False)  # type: ignore[return-value]

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

            view_count = entry.get('view_count')
            results.append(SearchResult(
                video_id=entry.get('id', ''),
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown')),
                duration_seconds=int(entry.get('duration', 0) or 0),
                thumbnail_url=entry.get('thumbnail'),
                thumbnail_is_square=False,
                source='youtube',
                version_label='Video',
                view_count=int(view_count) if view_count else None,
            ))

        logger.info(f"[YT Search] '{query}' -> {len(results)} results")
        return results

    except Exception as e:
        logger.warning(f"[YT Search] Error searching: {e}")
        return []


# ==========================================================================
# DEDUPLICATION
# ==========================================================================


def dedupe_results(
    ytm_results: List[SearchResult],
    yt_results: List[SearchResult],
    exclude_id: Optional[str] = None
) -> Tuple[List[SearchResult], List[SearchResult]]:
    """Deduplicate results, preferring YTM versions.

    Same video ID = same video. Prefer YTM metadata (better quality).

    Args:
        ytm_results: Results from YTM search.
        yt_results: Results from yt-dlp search.
        exclude_id: Optional video ID to exclude from yt_results.

    Returns:
        Tuple of (ytm_results, filtered_yt_results).
    """
    ytm_ids = {r.video_id for r in ytm_results}

    filtered_yt = []
    for r in yt_results:
        if r.video_id in ytm_ids:
            continue  # Duplicate of YTM result
        if r.video_id == exclude_id:
            continue
        filtered_yt.append(r)

    logger.debug(
        f"[Dedupe] YTM={len(ytm_results)}, YT={len(filtered_yt)} (excluded={exclude_id})"
    )

    return ytm_results, filtered_yt


# ==========================================================================
# UNIFIED SEARCH API
# ==========================================================================


async def search_query_mode(query: str) -> Tuple[List[SearchResult], List[SearchResult], Optional[str]]:
    """Search for a user query (Query Mode).

    Searches both YTM and YouTube in parallel, deduplicates, applies
    language-aware garbage filtering, and returns results split into
    songs (ATVs) and videos.

    Args:
        query: User's search query.

    Returns:
        Tuple of (songs, videos, recommended_id):
        - songs: Up to 3 ATVs
        - videos: Up to 3 videos
        - recommended_id: First non-garbage ATV's video_id, or None
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

    # Language-aware garbage filter
    ytm_results = [r for r in ytm_results if is_relevant(query, r)]
    yt_results = [r for r in yt_results if is_relevant(query, r)]

    # Split into songs (ATVs) and videos
    songs = [r for r in ytm_results if r.source == 'ytm_song']
    ytm_videos = [r for r in ytm_results if r.source == 'ytm_video']
    videos = ytm_videos + yt_results

    # First ATV gets recommended (query mode has no original to compare against)
    recommended_id = songs[0].video_id if songs else None

    logger.info(
        f"[Query Mode] '{query}' -> {len(songs[:3])} songs + {len(videos[:3])} videos"
    )

    return songs[:3], videos[:3], recommended_id


async def search_url_mode(
    video_id: str
) -> Tuple[Optional[SearchResult], List[SearchResult], List[SearchResult], Optional[str]]:
    """Search for alternatives to a user-provided URL (URL Mode).

    If the URL is already an ATV, returns (None, [], [], None) - just play it.
    Otherwise, searches for matching ATVs and related videos using multi-query
    with Japanese name extraction.

    Args:
        video_id: YouTube video ID from user's URL.

    Returns:
        Tuple of (original, songs, videos, recommended_id):
        - original: SearchResult for user's URL (None if already ATV)
        - songs: Up to 3 ATVs
        - videos: Up to 3 related videos
        - recommended_id: Video ID of recommended song, or None
    """
    # Get metadata for the original video
    metadata = await get_ytm_metadata(video_id)

    if not metadata:
        logger.debug(f"[URL Mode] No metadata for {video_id}, returning original only")
        original_fallback = SearchResult(
            video_id=video_id,
            title="Unknown",
            artist="Unknown",
            source='youtube',
            version_label='Video',
        )
        return original_fallback, [], [], None

    video_details = metadata.get('videoDetails', {})

    # Check if already an ATV - if so, just play it
    if is_atv(metadata):
        logger.info(f"[URL Mode] {video_id} is already an ATV, playing directly")
        return None, [], [], None

    # Extract metadata for searching
    title = video_details.get('title', 'Unknown')
    author = video_details.get('author', 'Unknown')
    duration = int(video_details.get('lengthSeconds', 0) or 0)
    view_count = extract_view_count(metadata)

    # Extract tags for Japanese name extraction
    microformat = metadata.get('microformat', {}).get('microformatDataRenderer', {})
    tags = microformat.get('tags', [])
    jp_names = extract_jp_names(tags)

    # Build original SearchResult
    thumbnails = video_details.get('thumbnail', {}).get('thumbnails', [])
    thumb_url = thumbnails[-1].get('url') if thumbnails else None

    # Try to get artist_id from YTM if available
    original_artist_id: Optional[str] = None

    original = SearchResult(
        video_id=video_id,
        title=title,
        artist=author,
        artist_id=original_artist_id,
        duration_seconds=duration,
        thumbnail_url=thumb_url,
        thumbnail_is_square=False,
        source='youtube',
        version_label='Video',
        view_count=view_count,
    )

    # Multi-query search: primary query + Japanese name variants
    queries = [f"{title} {author}"]
    for jp_name in jp_names[:2]:
        queries.append(f"{title} {jp_name}")

    # Search YTM with all queries, collecting unique results
    all_ytm_results: List[SearchResult] = []
    seen_ids: set[str] = set()
    original_found_as_atv = False

    for query in queries:
        results = await search_ytm(query, limit=5)
        for r in results:
            # Capture artist_id from original if it appears in YTM
            if r.video_id == video_id and r.artist_id and not original_artist_id:
                original_artist_id = r.artist_id
                original.artist_id = original_artist_id
                logger.debug(f"[URL Mode] Found original in YTM with artist_id={r.artist_id}")

            if r.video_id not in seen_ids:
                seen_ids.add(r.video_id)
                all_ytm_results.append(r)
                if r.video_id == video_id and r.source == 'ytm_song':
                    original_found_as_atv = True
                    logger.info(f"[URL Mode] Original {video_id} found as ATV in YTM")

    # Search YouTube (title only), excluding IDs already seen
    yt_results = await search_youtube(title, limit=6)
    yt_results = [r for r in yt_results if r.video_id not in seen_ids and r.video_id != video_id]

    # Split into songs and videos
    songs = [r for r in all_ytm_results if r.source == 'ytm_song']
    videos = [r for r in all_ytm_results if r.source == 'ytm_video'] + yt_results

    # Garbage filter videos only - YTM ATVs are curated, trust them
    videos = [r for r in videos if is_relevant(title, r)]

    # Determine recommended_id using confidence-based scoring
    recommended_id: Optional[str] = None
    star: Optional[SearchResult] = None

    if original_found_as_atv:
        # The user's URL IS the ATV - perfect match
        recommended_id = video_id
        logger.info(f"[URL Mode] Original {video_id} IS the ATV - 100% match")
    elif songs:
        # Use confidence-based star scoring
        original_meta = OriginalMetadata(
            title=title,
            artist=author,
            artist_id=original_artist_id,
        )
        star = find_star(original_meta, songs)
        if star:
            recommended_id = star.video_id

    # Select top 3 songs, but ensure starred track is included if found
    top_songs = songs[:3]
    if star and star not in top_songs:
        # Replace last slot with starred track so it's visible
        logger.info(f"[URL Mode] Moving starred track '{star.title}' into visible results")
        top_songs = songs[:2] + [star]

    # Select top 3 videos
    top_videos = videos[:3]

    logger.info(f"[URL Mode] {video_id} -> {len(top_songs)} songs, {len(top_videos)} videos")

    return original, top_songs, top_videos, recommended_id


# ==========================================================================
# THUMBNAIL FUNCTIONS
# ==========================================================================


def resize_ytm_thumbnail(url: str, size: int = THUMBNAIL_WIDTH) -> str:
    """Resize a YTM lh3.googleusercontent.com URL by modifying the size parameter.

    Args:
        url: YTM thumbnail URL.
        size: Target dimension in pixels.

    Returns:
        Modified URL with new size.
    """
    # YTM lh3 URLs have format: ...=w60-h60-... or ...=s120-...
    # Replace size parameters with our target
    url = re.sub(r'=w\d+-h\d+', f'=w{size}-h{size}', url)
    url = re.sub(r'=s\d+', f'=s{size}', url)
    return url


async def fetch_thumbnail_bytes(url: str) -> Optional[bytes]:
    """Fetch thumbnail bytes from a URL.

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

    Args:
        data: Raw image bytes.
        width: Target width in pixels.

    Returns:
        Resized image bytes (JPEG), or original data if resize fails.
    """
    from .music_helpers import get_ffmpeg_path

    ffmpeg_path = get_ffmpeg_path()

    cmd = [
        ffmpeg_path,
        '-hide_banner',
        '-loglevel', 'error',
        '-i', 'pipe:0',
        '-vf', f'scale={width}:-1',
        '-f', 'image2',
        '-c:v', 'mjpeg',
        '-q:v', '2',
        'pipe:1'
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
            return data

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
        width: Target width in pixels.

    Returns:
        Resized image bytes, or None if fetch fails.
    """
    data = await fetch_thumbnail_bytes(url)
    if data:
        return await resize_thumbnail_bytes(data, width)
    return None


async def get_thumbnail_bytes(track: 'Track', cache_manager: Optional[Any] = None) -> Optional[bytes]:
    """Get thumbnail bytes for a Track.

    Priority:
    1. Extract from cached MP3 (already embedded)
    2. Fetch from track.thumbnail URL (if square)
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
    if track.thumbnail and track.thumbnail_is_square:
        logger.info(f"[Thumbnail] Using square thumbnail for {track.video_id}")
        return await fetch_thumbnail_bytes(track.thumbnail)

    # Priority 3: Construct clean YouTube URL for 16:9 thumbnails
    if track.video_id:
        fallback_url = f"https://img.youtube.com/vi/{track.video_id}/maxresdefault.jpg"
        logger.info(f"[Thumbnail] Using 16:9 YouTube thumbnail for {track.video_id}")
        return await fetch_and_resize_thumbnail(fallback_url)

    logger.debug("[Thumbnail] No thumbnail source available")
    return None


def get_thumbnail_url(result: SearchResult, size: int = THUMBNAIL_WIDTH) -> Optional[str]:
    """Get the best thumbnail URL for a search result.

    Args:
        result: SearchResult object.
        size: Target size for square thumbnails.

    Returns:
        Thumbnail URL or None.
    """
    if not result.thumbnail_url:
        if result.video_id:
            return f"https://img.youtube.com/vi/{result.video_id}/sddefault.jpg"
        return None

    if result.thumbnail_is_square:
        return resize_ytm_thumbnail(result.thumbnail_url, size)

    return result.thumbnail_url


async def _probe_thumbnail_dimensions(url: str) -> Optional[Tuple[int, int]]:
    """Uses ffprobe to get actual dimensions of a thumbnail URL.

    Args:
        url: The thumbnail URL to probe.

    Returns:
        Tuple of (width, height) or None if probe fails.
    """
    from .music_helpers import get_ffmpeg_path

    ffmpeg_path = get_ffmpeg_path()
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


async def extract_best_thumbnail_from_info(info: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Extracts the best thumbnail URL from yt-dlp info, preferring square images.

    Uses ffprobe to determine dimensions of thumbnails that don't have them.

    Args:
        info: The yt-dlp extraction info dict.

    Returns:
        Tuple of (thumbnail_url, is_square).
    """
    MIN_SIZE = 480

    thumbnails = info.get('thumbnails', [])

    if not thumbnails:
        fallback = info.get('thumbnail')
        return fallback, False

    # Probe thumbnails missing dimensions
    unknown_dims = [(i, t) for i, t in enumerate(thumbnails) if not (t.get('width') and t.get('height'))]
    for _i, t in unknown_dims[:5]:
        url = t.get('url')
        if url:
            dims = await _probe_thumbnail_dimensions(url)
            if dims:
                t['width'], t['height'] = dims

    # Filter to usable thumbnails
    usable = [
        t for t in thumbnails
        if t.get('width') and t.get('height')
        and t.get('width') >= MIN_SIZE and t.get('height') >= MIN_SIZE
    ]

    if not usable:
        with_dims = [t for t in thumbnails if t.get('width') and t.get('height')]
        if with_dims:
            best = max(with_dims, key=lambda t: t.get('width', 0) * t.get('height', 0))
            is_square = best.get('width') == best.get('height')
            return best.get('url'), is_square
        for t in thumbnails:
            if t.get('url'):
                return t.get('url'), False
        return info.get('thumbnail'), False

    # Prefer square thumbnails (album art)
    square_thumbnails = [t for t in usable if t.get('width') == t.get('height')]
    if square_thumbnails:
        best = max(square_thumbnails, key=lambda t: t.get('width', 0))
        logger.info(f"[Thumbnail] Selected square: {best.get('width')}x{best.get('height')}")
        return best.get('url'), True

    # No square - return largest
    best = max(usable, key=lambda t: t.get('width', 0) * t.get('height', 0))
    logger.info(f"[Thumbnail] Using largest: {best.get('width')}x{best.get('height')}")
    return best.get('url'), False


async def search_youtube_legacy(
    query: str,
    max_results: int,
    logger_instance: Any,
    ydl_opts: Optional[Dict[str, Any]] = None
) -> List['Track']:
    """Legacy wrapper for old search_youtube signature.

    Maintains compatibility with existing code during migration.
    """
    results = await search_youtube(query, limit=max_results)
    return [r.to_track() for r in results]

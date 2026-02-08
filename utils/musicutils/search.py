"""Unified search and metadata provider for music playback.

This module provides search functionality across YTM and YouTube with:
- Multi-query search with Japanese name extraction (for cross-language matching)
- Language-aware garbage filtering (trusts YTM for cross-script results)
- Confidence-based star scoring (not ID-gated)
- Version labels and view counts for display

Philosophy: "Right enough" over "perfectly accurate" — present good options,
recommend one with a star, and trust the user to choose.

Glossary (YTM video types):
    ATV  — Audio Track Version. Audio-only track in YTM's catalog with square
           album art. These are the "songs" YTM serves — highest quality metadata.
    OMV  — Official Music Video. The artist's canonical music video.
    UGC  — User-Generated Content. Covers, fan videos, unofficial uploads.

    "Star" refers to the recommended track indicator shown in the selection UI.
    When a user provides a URL, the module scores ATV candidates against the
    original and marks the best match with a star so the user can one-click it.

Architecture: Top-to-bottom data flow
─────────────────────────────────────
1. ENTRY POINTS      - What external code calls
2. ORCHESTRATION     - High-level flow coordination
3. API LAYER         - External service interaction
4. RESULT PROCESSING - Parsing, filtering, scoring
5. TEXT ANALYSIS      - String comparison, relevance
6. CJK SUPPORT       - Transliteration, script detection
7. PURE UTILITIES    - Stateless helpers
8. THUMBNAILS        - Separate concern
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
    YTMusic = None  # type: ignore[assignment,misc]
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


# =============================================================================
# IMPORTS & CONSTANTS
# =============================================================================

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
# Cross-script results get a slightly lower threshold because transliteration
# may not perfectly capture all representations.
CROSS_SCRIPT_GARBAGE_THRESHOLD = 0.25

# Thumbnail constants
THUMBNAIL_WIDTH = 720  # Target width for resized thumbnails


# =============================================================================
# CJK TRANSLITERATION LIBRARIES
# =============================================================================
# These libraries convert CJK text to romanized forms for cross-script matching.
# All conversions are rule-based dictionary lookups - same input always produces
# same output. This is NOT machine learning, just linguistic rules.
#
# WHY THIS MATTERS:
# When a user searches "Murasaki Shion", YTM might return results titled "紫咲シオン".
# Without transliteration, we can't verify if "紫咲シオン" matches "Murasaki Shion"
# because they share zero characters. With transliteration, we can convert
# "紫咲シオン" → "murasaki shion" and detect the match.
#
# CURRENT LIMITATION:
# Transliteration helps with VERIFICATION but not DISCOVERY. If YTM doesn't
# return the Japanese result in the first place, transliteration can't help.
# That's why we still need the multi-query approach (searching with both
# "Murasaki Shion" AND "紫咲シオン" extracted from video tags).

# Japanese: pykakasi (Hepburn romanization)
try:
    import pykakasi
    _kakasi = pykakasi.kakasi()
    PYKAKASI_AVAILABLE = True
except ImportError:
    _kakasi = None
    PYKAKASI_AVAILABLE = False
    logger.info("[CJK Libraries] pykakasi not available")

# Chinese: pypinyin (Pinyin romanization)
try:
    from pypinyin import lazy_pinyin
    PYPINYIN_AVAILABLE = True
except ImportError:
    lazy_pinyin = None  # type: ignore[assignment]
    PYPINYIN_AVAILABLE = False
    logger.info("[CJK Libraries] pypinyin not available")

# Korean: korean-romanizer (Revised Romanization of Korean)
try:
    from korean_romanizer.romanizer import Romanizer
    KOREAN_ROMANIZER_AVAILABLE = True
except ImportError:
    Romanizer = None  # type: ignore[assignment,misc]
    KOREAN_ROMANIZER_AVAILABLE = False
    logger.info("[CJK Libraries] korean_romanizer not available")


# =============================================================================
# DATA TYPES
# =============================================================================


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


@dataclass
class OriginalMetadata:
    """Metadata from the original video for comparison during star scoring."""
    title: str
    artist: str
    artist_id: Optional[str] = None


@dataclass
class MetadataExtraction:
    """Output from _build_original_metadata — everything needed for search.

    Transport container: downstream functions unpack only the fields they need.
    Named fields prevent positional swap-bugs (multiple str fields).

    Attributes:
        original: SearchResult built from the video's metadata.
        base_query: Pre-computed search query (mf_title or vd_title depending
            on video type and mismatch status). Author is NOT included.
        vd_title: Raw videoDetails title, always the clean song name.
            Used for CJK query pairing (CJK metadata is reliably mapped).
        author: Channel/artist name for display and star scoring only.
        jp_names: CJK artist name variants extracted from video tags.
        mismatch_detected: True if videoDetails title diverged from raw YouTube title.
        video_type: musicVideoType from videoDetails (ATV/OMV/UGC/etc.).
    """
    original: SearchResult
    base_query: str
    vd_title: str
    author: str
    jp_names: List[str]
    mismatch_detected: bool
    video_type: str


# =============================================================================
# SECTION 1: ENTRY POINTS
# =============================================================================
# The only functions external code should call.
# These read like a table of contents for what the module does.


async def search_url_mode(
    url_or_id: str
) -> Tuple[SearchResult, List[SearchResult], List[SearchResult], Optional[str]]:
    """Search for alternatives to a user-provided URL (URL Mode).

    If the URL is already an ATV, returns it directly with proper metadata and
    empty alternatives (no selection UI needed). Otherwise, searches for matching
    ATVs and related videos using multi-query with Japanese name extraction.

    Args:
        url_or_id: YouTube video URL or video ID.

    Returns:
        Tuple of (original, songs, videos, recommended_id):
        - original: SearchResult for user's URL (always present)
        - songs: Up to 3 ATVs (empty if original is already an ATV)
        - videos: Up to 3 related videos (empty if original is already an ATV)
        - recommended_id: Video ID of recommended song (original's ID if ATV)

    Raises:
        ValueError: If video ID cannot be extracted from url_or_id.
    """
    # Normalize input: extract video ID if a URL was passed
    video_id = extract_video_id(url_or_id)
    if not video_id:
        raise ValueError(f"Could not extract video ID from: {url_or_id}")

    # Phase 1: Fetch and classify the original video
    metadata, already_atv = await _fetch_and_validate_original(video_id)

    if metadata is None:
        logger.debug(f"[URL Mode] No metadata for {video_id}, returning original only")
        original_fallback = SearchResult(
            video_id=video_id,
            title="Unknown",
            artist="Unknown",
            source='youtube',
            version_label='Video',
        )
        return original_fallback, [], [], None

    # Phase 2: If already an ATV, return it directly
    if already_atv:
        atv_result = await _build_atv_result(video_id, metadata)
        return atv_result, [], [], video_id

    # Phase 3: Build original SearchResult and extract search parameters
    ext = _build_original_metadata(video_id, metadata)

    # Phase 4: Search for alternatives
    songs, videos, original_found_as_atv = await _search_alternatives(
        ext.base_query, ext.vd_title, ext.jp_names, video_id, ext.original
    )

    # Phase 5: Pick recommendation
    recommended_id, star = _select_recommendation(
        ext.original, songs, original_found_as_atv, ext.vd_title, ext.author
    )

    # Select top 3 songs, but ensure starred track is included if found
    top_songs = songs[:3]
    if star and star not in top_songs:
        # Replace last slot with starred track so it's visible
        logger.info(f"[URL Mode] Moving starred track '{star.title}' into visible results")
        top_songs = songs[:2] + [star]

    # Select top 3 videos
    top_videos = videos[:3]

    # Lightweight backfill: only enrich the final results shown to the user
    await _backfill_missing_metadata(top_songs + top_videos)

    logger.info(f"[URL Mode] {video_id} -> {len(top_songs)} songs, {len(top_videos)} videos")

    return ext.original, top_songs, top_videos, recommended_id


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

    top_songs = songs[:3]
    top_videos = videos[:3]

    # Lightweight backfill: only enrich the final results shown to the user
    await _backfill_missing_metadata(top_songs + top_videos)

    logger.info(
        f"[Query Mode] '{query}' -> {len(top_songs)} songs + {len(top_videos)} videos"
    )

    return top_songs, top_videos, recommended_id


# =============================================================================
# SECTION 2: ORCHESTRATION HELPERS
# =============================================================================
# Break down the complex flows in entry points into named steps.
# These are "private" to the module — called only by entry points.


async def _backfill_missing_metadata(results: List[SearchResult]) -> None:
    """Backfill duration and view_count on final results shown to the user.

    Called after truncation to [:3] so we only fetch metadata for results
    that will actually appear in the embed. Songs filter already provides
    these fields for most ATVs, so this typically fires 0-3 calls for
    video-type results only.

    Args:
        results: The final list of SearchResults to enrich (mutated in place).
    """
    needs_backfill = [
        r for r in results
        if r.video_id and (r.duration_seconds is None or r.view_count is None)
    ]

    if not needs_backfill:
        return

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

    await asyncio.gather(*[fetch_and_update(r) for r in needs_backfill])
    logger.debug(f"[Backfill] Enriched {len(needs_backfill)} results with get_song()")


async def _fetch_and_validate_original(video_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Fetch metadata for original video, determine if ATV.

    Args:
        video_id: YouTube video ID.

    Returns:
        Tuple of (metadata, is_atv). metadata is None if unavailable.
    """
    metadata = await get_ytm_metadata(video_id)
    if not metadata:
        return None, False
    return metadata, is_atv(metadata)


async def _build_atv_result(video_id: str, metadata: Dict[str, Any]) -> SearchResult:
    """Construct SearchResult for a confirmed ATV.

    Tries YTM search first (for full metadata with album info), falls back
    to building from get_song() metadata.

    Args:
        video_id: YouTube video ID.
        metadata: Raw metadata from get_ytm_metadata().

    Returns:
        SearchResult with ATV metadata.
    """
    logger.info(f"[URL Mode] {video_id} is already an ATV, fetching full metadata from YTM")

    # Search YTM by video ID - this gives us the complete SearchResult with album info
    ytm_results = await search_ytm(video_id, limit=1)
    for result in ytm_results:
        if result.video_id == video_id:
            logger.debug(f"[URL Mode] Found ATV in YTM search: album='{result.album}'")
            return result

    # Fallback: build from get_song() metadata if YTM search didn't find it
    # (This can happen if the ATV is region-locked or very new)
    logger.debug("[URL Mode] ATV not found in YTM search, using get_song() metadata")
    video_details = metadata.get('videoDetails', {})

    # For ATVs, videoDetails.author IS the clean artist name (not channel)
    atv_title = video_details.get('title', 'Unknown')
    atv_artist = video_details.get('author', 'Unknown')
    atv_duration = int(video_details.get('lengthSeconds', 0) or 0)
    atv_view_count = extract_view_count(metadata)

    # ATVs have square thumbnails
    thumbnails = video_details.get('thumbnail', {}).get('thumbnails', [])
    atv_thumb_url = None
    if thumbnails:
        # Get largest thumbnail and resize
        raw_url = thumbnails[-1].get('url')
        if raw_url:
            atv_thumb_url = resize_ytm_thumbnail(raw_url, THUMBNAIL_WIDTH)

    return SearchResult(
        video_id=video_id,
        title=atv_title,
        artist=atv_artist,
        duration_seconds=atv_duration,
        thumbnail_url=atv_thumb_url,
        thumbnail_is_square=True,  # ATVs always have square art
        source='ytm_song',
        video_type=MUSIC_VIDEO_TYPE_ATV,
        version_label='Official Audio',
        view_count=atv_view_count,
    )


def _build_original_metadata(
    video_id: str,
    metadata: Dict[str, Any],
) -> MetadataExtraction:
    """Extract search parameters and build original SearchResult from metadata.

    Handles catalog mismatch detection, video-type-aware query strategy,
    and Japanese name extraction from tags.

    Query strategy (base_query selection):
        - Mismatch detected → microformat title (catalog pointed to wrong song)
        - UGC → vd_title (author is cover channel, not the artist)
        - OMV → microformat title if it differs from vd_title (contains artist
          name in YouTube's raw format, e.g. "Artist - Song"), else vd_title
        - Other (ATV, etc.) → vd_title (trust videoDetails)

    Author is excluded from queries entirely — it's unreliable for UGCs
    (cover channels) and OMVs (VEVO/Topic channels). Kept for display
    and star scoring only.

    Args:
        video_id: YouTube video ID.
        metadata: Raw metadata from get_ytm_metadata().

    Returns:
        MetadataExtraction with all fields populated.
    """
    video_details = metadata.get('videoDetails', {})

    # Start with videoDetails (structured metadata)
    vd_title = video_details.get('title', 'Unknown')
    vd_author = video_details.get('author', 'Unknown')
    video_type = video_details.get('musicVideoType', '')
    duration = int(video_details.get('lengthSeconds', 0) or 0)
    view_count = extract_view_count(metadata)

    # Extract microformat (raw YouTube title) for comparison
    microformat = metadata.get('microformat', {}).get('microformatDataRenderer', {})
    mf_title_raw = microformat.get('title', '')
    mf_title = clean_microformat_title(mf_title_raw) if mf_title_raw else ''

    # Check for YTM catalog mismatch (videoDetails points to wrong song).
    # This happens when YTM's catalog maps the wrong song to a video ID.
    mismatch_detected = bool(mf_title) and has_ytm_catalog_mismatch(vd_title, mf_title)
    if mismatch_detected:
        logger.info(f"[URL Mode] Using microformat title due to catalog mismatch: '{mf_title}'")

    # Determine base query by video type and mismatch status.
    # Author is intentionally excluded — unreliable for UGCs (cover channels)
    # and OMVs (VEVO/Topic channels). Microformat title for OMVs already
    # contains the artist name (e.g. "Pop Smoke - Dior (Official Audio)").
    if mismatch_detected:
        base_query = mf_title
    elif video_type == MUSIC_VIDEO_TYPE_UGC:
        base_query = vd_title
    elif video_type == MUSIC_VIDEO_TYPE_OMV:
        if mf_title and mf_title != vd_title:
            base_query = mf_title
        else:
            base_query = vd_title
    else:
        base_query = vd_title

    logger.debug(
        f"[URL Mode] Query strategy: video_type={video_type}, "
        f"mismatch={mismatch_detected}, base_query='{base_query}'"
    )

    # Extract tags for Japanese name extraction
    tags = microformat.get('tags', [])
    jp_names = extract_jp_names(tags, video_title=vd_title, author=vd_author)

    # Build original SearchResult
    thumbnails = video_details.get('thumbnail', {}).get('thumbnails', [])
    thumb_url = thumbnails[-1].get('url') if thumbnails else None

    original = SearchResult(
        video_id=video_id,
        title=vd_title,
        artist=vd_author,
        artist_id=None,
        duration_seconds=duration,
        thumbnail_url=thumb_url,
        thumbnail_is_square=False,
        source='youtube',
        version_label='Video',
        view_count=view_count,
    )

    return MetadataExtraction(
        original=original,
        base_query=base_query,
        vd_title=vd_title,
        author=vd_author,
        jp_names=jp_names,
        mismatch_detected=mismatch_detected,
        video_type=video_type,
    )


async def _search_alternatives(
    base_query: str,
    vd_title: str,
    jp_names: List[str],
    video_id: str,
    original: SearchResult,
) -> Tuple[List[SearchResult], List[SearchResult], bool]:
    """Run multi-query search across YTM and YouTube.

    Query strategy is fully resolved upstream in _build_original_metadata().
    This function just executes the queries — no branching on video type
    or mismatch status.

    Args:
        base_query: Pre-computed search query (title only, no author).
        vd_title: Raw videoDetails title for CJK query pairing and filtering.
        jp_names: Japanese name variants extracted from tags.
        video_id: Original video ID to exclude from results.
        original: The original SearchResult (mutated to attach artist_id if found).

    Returns:
        Tuple of (songs, videos, original_found_as_atv).
    """
    # Build query list. base_query handles the primary search.
    # CJK queries always pair vd_title (the clean song name) with Japanese
    # name variants — CJK metadata on YTM is reliably mapped, so vd_title
    # is always safe for this pairing.
    queries = [base_query]
    for jp_name in jp_names[:2]:
        queries.append(f"{vd_title} {jp_name}")

    # Search YTM with all queries, collecting unique results
    all_ytm_results: List[SearchResult] = []
    seen_ids: set[str] = set()
    original_found_as_atv = False
    original_artist_id: Optional[str] = None

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

    # Search YouTube (vd_title only), excluding IDs already seen
    yt_results = await search_youtube(vd_title, limit=6)
    yt_results = [r for r in yt_results if r.video_id not in seen_ids and r.video_id != video_id]

    # Split into songs and videos
    songs = [r for r in all_ytm_results if r.source == 'ytm_song']
    videos = [r for r in all_ytm_results if r.source == 'ytm_video'] + yt_results

    # Garbage filter videos only - YTM ATVs are curated, trust them
    videos = [r for r in videos if is_relevant(vd_title, r)]

    return songs, videos, original_found_as_atv


def _select_recommendation(
    original: SearchResult,
    songs: List[SearchResult],
    original_found_as_atv: bool,
    title: str,
    author: str,
) -> Tuple[Optional[str], Optional[SearchResult]]:
    """Apply star scoring to pick recommended track.

    Args:
        original: The original SearchResult.
        songs: List of ATV candidates.
        original_found_as_atv: Whether the original was found as an ATV in search.
        title: Video title used for scoring.
        author: Video author used for scoring.

    Returns:
        Tuple of (recommended_id, star_result). star_result is None if
        recommendation is the original itself or no star was found.
    """
    video_id = original.video_id

    if original_found_as_atv:
        # The user's URL IS the ATV - perfect match
        logger.info(f"[URL Mode] Original {video_id} IS the ATV - 100% match")
        return video_id, None

    if not songs:
        return None, None

    # Use confidence-based star scoring
    original_meta = OriginalMetadata(
        title=title,
        artist=author,
        artist_id=original.artist_id,
    )
    star = find_star(original_meta, songs)
    if star:
        return star.video_id, star

    return None, None


# =============================================================================
# SECTION 3: API LAYER
# =============================================================================
# Direct interaction with external services.
# These return raw or lightly-processed data.

_ytm: Optional[YTMusic] = None  # type: ignore[type-arg]


def _get_ytm() -> Optional[YTMusic]:  # type: ignore[type-arg]
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

        # Log results
        songs = sum(1 for r in parsed if r.source == 'ytm_song')
        videos = sum(1 for r in parsed if r.source == 'ytm_video')
        logger.info(f"[YTM Search] '{query}' -> {songs} songs, {videos} videos")

        return parsed

    except Exception as e:
        logger.warning(f"[YTM Search] Error searching: {e}")
        return []


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


# =============================================================================
# SECTION 4: RESULT PROCESSING
# =============================================================================
# Transform API responses into SearchResults, filter, dedupe, score.


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
    is_atv_result = video_type == MUSIC_VIDEO_TYPE_ATV
    source = 'ytm_song' if is_atv_result else 'ytm_video'
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


def is_relevant(query: str, result: SearchResult) -> bool:
    """Determine if a search result is relevant to the query.

    Uses language-aware filtering with transliteration support:
    - Expands CJK text to romanized forms for cross-script verification
    - Cross-script matches: attempt verification, fall back to trusting YTM
    - Same-script matches: use similarity/containment checks

    DESIGN PHILOSOPHY (Data Gathering Phase):
    We currently TRUST YTM for cross-script results while LOGGING what we see.
    YTM is more selective about returning cross-language results, so their
    cross-script results are generally higher quality. However, we want to
    gather data on:
    1. How often transliteration verification succeeds vs fails
    2. What similarity scores cross-script results typically get
    3. Whether blind trust ever lets through garbage

    This logging will help us decide if/when to tighten cross-script filtering.

    Args:
        query: Original search query.
        result: Search result to evaluate.

    Returns:
        True if result should be kept, False if garbage.
    """
    query_has_cjk = has_cjk(query)
    title_has_cjk = has_cjk(result.title)
    is_cross_script = query_has_cjk != title_has_cjk

    combined = f"{result.title} {result.artist}"

    # Extract words from ALL representations (original + transliterated)
    # This is the key to cross-script verification: "紫咲シオン" expands to
    # include "murasaki", "shion" which can match query "Murasaki Shion"
    query_words = extract_words_expanded(query)
    result_words = extract_words_expanded(combined)

    # Word overlap in ANY representation = definitely relevant
    word_overlap = query_words & result_words
    if word_overlap:
        if is_cross_script:
            logger.info(
                f"[Cross-Script Verified] query='{query}' | "
                f"result='{result.title}' by '{result.artist}' | "
                f"overlapping_words={word_overlap}"
            )
        return True

    # Containment check across all representations
    # Check if any representation of query is contained in any representation of result
    for q_repr in expand_text(query):
        q_norm = normalize_text(q_repr)
        for r_repr in expand_text(combined):
            r_norm = normalize_text(r_repr)
            if q_norm in r_norm or r_norm in q_norm:
                if is_cross_script:
                    logger.info(
                        f"[Cross-Script Containment] query='{query}' ({q_repr}) | "
                        f"result='{result.title}' ({r_repr})"
                    )
                return True

    # Similarity fallback (on original text - transliteration handled above)
    title_sim = text_similarity(query, result.title)
    combined_sim = text_similarity(query, combined)
    best_sim = max(title_sim, combined_sim)

    # Use script-appropriate threshold
    # Cross-script gets lower threshold because YTM is more selective
    threshold = CROSS_SCRIPT_GARBAGE_THRESHOLD if is_cross_script else GARBAGE_SIMILARITY_THRESHOLD

    # Log borderline cases for threshold tuning
    if threshold - 0.1 <= best_sim < threshold + 0.1:
        logger.info(
            f"[Garbage Filter Borderline] query='{query}' | "
            f"result='{result.title}' by '{result.artist}' | "
            f"title_sim={title_sim:.2f}, combined_sim={combined_sim:.2f} | "
            f"threshold={threshold} ({'cross-script' if is_cross_script else 'same-script'}) | "
            f"{'KEPT' if best_sim >= threshold else 'FILTERED'}"
        )

    if best_sim >= threshold:
        return True

    logger.info(f"Filtered as garbage: {result.title} (sim={best_sim:.2f})")
    return False


def find_star(original: OriginalMetadata, candidates: List[SearchResult]) -> Optional[SearchResult]:
    """Find the best ATV candidate to recommend ("star").

    The "star" is the recommended track shown in the selection UI — the one
    the user can accept with a single click. This function scores each ATV
    candidate against the original video's metadata and picks the best match
    above STAR_THRESHOLD.

    If multiple candidates tie (same score), uses title length ratio as
    tiebreaker to prefer exact matches over variants like "(Instrumental)"
    or "(Remix)".

    Args:
        original: Metadata from original video.
        candidates: List of search result candidates.

    Returns:
        Best candidate above threshold, or None.
    """
    # Collect all passing candidates with their scores
    passing: List[tuple[SearchResult, float]] = []

    for candidate in candidates:
        # Only consider ATVs for starring
        if candidate.video_type != MUSIC_VIDEO_TYPE_ATV:
            continue

        score = score_candidate(original, candidate)
        if score >= STAR_THRESHOLD:
            passing.append((candidate, score))

    if not passing:
        return None

    # Find the best score
    best_score = max(score for _, score in passing)

    # Get all candidates with the best score (ties)
    tied = [(cand, score) for cand, score in passing if score == best_score]

    if len(tied) == 1:
        # No tie, just return the winner
        best_candidate = tied[0][0]
    else:
        # Multiple candidates tied—use length ratio as tiebreaker
        # Prefer the candidate whose title is closest in length to original
        best_candidate = max(
            tied,
            key=lambda x: _title_length_ratio(original.title, x[0].title)
        )[0]
        logger.debug(
            f"[Star Scoring] Tiebreaker: {len(tied)} candidates tied at {best_score:.2f}, "
            f"selected '{best_candidate.title}' by length ratio"
        )

    logger.info(f"Star assigned to: {best_candidate.title} (score={best_score:.2f})")
    return best_candidate


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

    logger.debug(
        f"[Star Scoring] Candidate: '{candidate.title}' by '{candidate.artist}' | "
        f"artist_conf={artist_conf:.2f}, title_match={title_match:.2f}"
    )

    # Perfect title match always passes (regardless of artist)
    if title_match >= 0.95:
        score = (artist_conf * 0.4) + (title_match * 0.6)
        logger.debug(f"[Star Scoring]   → AUTO-PASS (perfect title): score={score:.2f}")
        return score

    # Sliding threshold: title_threshold = 1.05 - (0.8 * artist_conf)
    # Artist 1.0 → title needs 0.25
    # Artist 0.75 → title needs 0.45
    # Artist 0.5 → title needs 0.65
    title_threshold = 1.05 - (artist_conf * 0.8)

    if title_match < title_threshold:
        logger.debug(
            f"[Star Scoring]   → FAILED: title_match {title_match:.2f} < threshold {title_threshold:.2f} "
            f"(required for artist_conf={artist_conf:.2f})"
        )
        return 0.0

    # Combined score: weighted average
    score = (artist_conf * 0.4) + (title_match * 0.6)
    logger.debug(f"[Star Scoring]   → PASSED: score={score:.2f} (threshold was {title_threshold:.2f})")

    # Log edge cases for threshold validation
    if score < STAR_THRESHOLD and score >= 0.5:
        logger.debug(
            f"[Star Scoring] Near-miss: '{candidate.title}' score={score:.2f} < threshold={STAR_THRESHOLD}"
        )
    elif score >= STAR_THRESHOLD and score < 0.7:
        logger.debug(
            f"[Star Scoring] Near-hit: '{candidate.title}' score={score:.2f} (barely passed)"
        )

    return score


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

    Uses transliteration expansion for cross-script matching.
    Containment-first (if one contains the other, high match).
    Word overlap ratio as secondary signal.
    Similarity-fallback.

    Args:
        orig_title: Original video title.
        cand_title: Candidate title.

    Returns:
        Match score between 0.0 and 1.0.
    """
    # Expand both titles to all representations (original + transliterated)
    orig_representations = expand_text(orig_title)
    cand_representations = expand_text(cand_title)

    best_match = 0.0

    # Check all combinations of representations
    for orig_repr in orig_representations:
        orig_norm = normalize_text(orig_repr)

        for cand_repr in cand_representations:
            cand_norm = normalize_text(cand_repr)

            # Containment: strong signal
            if orig_norm in cand_norm or cand_norm in orig_norm:
                best_match = max(best_match, 0.9)
                continue

            # Word overlap ratio
            orig_words = set(orig_norm.split())
            cand_words = set(cand_norm.split())

            if orig_words and cand_words:
                overlap = len(orig_words & cand_words)
                total = max(len(orig_words), len(cand_words))
                ratio = overlap / total
                best_match = max(best_match, ratio)

            # Similarity as final fallback
            sim = text_similarity(orig_repr, cand_repr)
            best_match = max(best_match, sim)

    return best_match


def _title_length_ratio(orig_title: str, cand_title: str) -> float:
    """Calculate length ratio for containment tiebreaking.

    When multiple candidates pass containment check, prefer the one
    closest in length to the original. This prevents "(Instrumental)"
    or "(Slowed)" versions from winning over the exact match.

    Args:
        orig_title: Original video title.
        cand_title: Candidate title.

    Returns:
        Ratio between 0.0 and 1.0 (1.0 = same length).
    """
    orig_norm = normalize_text(orig_title)
    cand_norm = normalize_text(cand_title)

    shorter = min(len(orig_norm), len(cand_norm))
    longer = max(len(orig_norm), len(cand_norm))

    return shorter / longer if longer > 0 else 1.0


# =============================================================================
# SECTION 5: TEXT ANALYSIS
# =============================================================================
# String comparison, similarity, relevance checking.
# No API calls, no SearchResult knowledge — just text in, scores out.


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


def normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, strip, normalize unicode).

    Args:
        text: Text to normalize.

    Returns:
        Normalized text.
    """
    text = unicodedata.normalize('NFKC', text)
    text = text.lower().strip()
    return text


def extract_words(text: str) -> set[str]:
    """Extract words from text for comparison.

    Simple word extraction for detecting content mismatches.

    Args:
        text: Text to extract words from.

    Returns:
        Set of lowercase words (2+ characters).
    """
    words = set()
    for word in text.lower().split():
        # Strip punctuation from edges (including CJK brackets)
        cleaned = word.strip('()[]【】「」『』〔〕.,!?&-')  # noqa: RUF001
        if len(cleaned) >= 2:
            words.add(cleaned)
    return words


def extract_words_expanded(text: str) -> set[str]:
    """Extract words from ALL script representations of text.

    Combines extract_words() across all transliterated forms. This enables
    cross-script word matching: "紫咲シオン" expands to include "murasaki"
    and "shion", which can then match a query for "Murasaki Shion".

    Args:
        text: Text to extract words from.

    Returns:
        Set of lowercase words (2+ characters) from all representations.
    """
    words: set[str] = set()
    for representation in expand_text(text):
        words.update(extract_words(representation))
    return words


# =============================================================================
# SECTION 6: CJK SUPPORT
# =============================================================================
# Japanese/Chinese/Korean detection and transliteration.
# Also tag extraction for cross-language matching.

# --- Script Detection ---


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


def has_japanese(text: str) -> bool:
    """Check if text contains Japanese-specific characters (hiragana/katakana).

    Kanji (CJK ideographs) are shared with Chinese, so we specifically check
    for hiragana and katakana which are unique to Japanese.

    Args:
        text: Text to check.

    Returns:
        True if text contains hiragana or katakana.
    """
    for char in text:
        # Hiragana: U+3040 to U+309F
        if '\u3040' <= char <= '\u309f':
            return True
        # Katakana: U+30A0 to U+30FF
        if '\u30a0' <= char <= '\u30ff':
            return True
    return False


def has_chinese(text: str) -> bool:
    """Check if text contains Chinese characters without Japanese kana.

    CJK ideographs (kanji/hanzi) are shared between Japanese and Chinese.
    We assume text is Chinese if it has CJK ideographs but NO hiragana/katakana.
    This is imperfect but works for most real-world cases.

    Args:
        text: Text to check.

    Returns:
        True if text appears to be Chinese (has hanzi, no kana).
    """
    has_hanzi = any('\u4e00' <= char <= '\u9fff' for char in text)
    return has_hanzi and not has_japanese(text)


def has_korean(text: str) -> bool:
    """Check if text contains Korean Hangul characters.

    Hangul syllables occupy a distinct Unicode block, making detection
    unambiguous unlike the shared CJK ideographs.

    Args:
        text: Text to check.

    Returns:
        True if text contains Hangul.
    """
    # Hangul Syllables: U+AC00 to U+D7AF
    return any('\uac00' <= char <= '\ud7af' for char in text)


# --- Transliteration ---


def transliterate_japanese(text: str) -> Optional[str]:
    """Convert Japanese text to Hepburn romanization.

    Uses pykakasi for rule-based conversion. This is deterministic:
    same input always produces same output.

    Args:
        text: Text potentially containing Japanese characters.

    Returns:
        Romanized text, or None if pykakasi unavailable or conversion unchanged.
    """
    if not PYKAKASI_AVAILABLE or not _kakasi:
        return None
    if not has_cjk(text):
        return None

    try:
        result = _kakasi.convert(text)
        # pykakasi returns list of dicts with 'hepburn' key for romanization
        romaji = ' '.join(item['hepburn'] for item in result)
        # Clean up: collapse multiple spaces, strip, lowercase
        romaji = ' '.join(romaji.split()).strip().lower()
        # Only return if we actually converted something
        return romaji if romaji and romaji != text.lower() else None
    except Exception as e:
        logger.debug(f"[Transliterate] Japanese conversion failed: {e}")
        return None


def transliterate_chinese(text: str) -> Optional[str]:
    """Convert Chinese text to Pinyin romanization.

    Uses pypinyin's lazy_pinyin for simple conversion without tone marks.

    Args:
        text: Text potentially containing Chinese characters.

    Returns:
        Pinyin text, or None if pypinyin unavailable or conversion unchanged.
    """
    if not PYPINYIN_AVAILABLE or not lazy_pinyin:
        return None
    if not has_chinese(text):
        return None

    try:
        # lazy_pinyin returns list of pinyin strings
        pinyin_list = lazy_pinyin(text)
        pinyin = ' '.join(pinyin_list)
        pinyin = ' '.join(pinyin.split()).strip().lower()
        return pinyin if pinyin and pinyin != text.lower() else None
    except Exception as e:
        logger.debug(f"[Transliterate] Chinese conversion failed: {e}")
        return None


def transliterate_korean(text: str) -> Optional[str]:
    """Convert Korean text to romanization.

    Uses korean-romanizer which follows the Revised Romanization of Korean,
    the official romanization system used by the Republic of Korea.

    Args:
        text: Text potentially containing Korean characters.

    Returns:
        Romanized text, or None if korean-romanizer unavailable or conversion unchanged.
    """
    if not KOREAN_ROMANIZER_AVAILABLE or not Romanizer:
        return None
    if not has_korean(text):
        return None

    try:
        romanizer = Romanizer(text)
        romanized = romanizer.romanize()
        romanized = ' '.join(romanized.split()).strip().lower()
        return romanized if romanized and romanized != text.lower() else None
    except Exception as e:
        logger.debug(f"[Transliterate] Korean conversion failed: {e}")
        return None


def expand_text(text: str) -> set[str]:
    """Expand text to all script representations.

    Returns the original text plus any romanized versions. This is additive:
    the original is ALWAYS included. Deterministic: same input → same output.

    Args:
        text: Text to expand.

    Returns:
        Set containing original text plus any romanized versions.
    """
    representations = {text.lower()}

    # Try each transliteration in order of likely relevance
    # Japanese is most common for our use case (VTuber music)
    romaji = transliterate_japanese(text)
    if romaji:
        representations.add(romaji)

    # Chinese - Bilibili VTubers, C-pop covers
    pinyin = transliterate_chinese(text)
    if pinyin:
        representations.add(pinyin)

    # Korean - K-pop is everywhere
    korean_roman = transliterate_korean(text)
    if korean_roman:
        representations.add(korean_roman)

    return representations


# --- Tag Confidence & Extraction ---

# =============================================================================
# CJK TAG CONFIDENCE SCORING
# =============================================================================
#
# This system scores tags by likelihood of being a useful artist name.
# It replaces the binary _SKIP_TAGS approach with nuanced confidence scoring.
#
# The confidence system IS ACTIVE - it determines which tags are returned.
#
# TRANSLITERATION CROSS-CHECK: DATA GATHERING ONLY
# -------------------------------------------------
# The one exception is the transliteration cross-check in calculate_tag_confidence().
# This uses pykakasi/pypinyin/korean-romanizer to romanize CJK tags and compare
# against the author field. This feature is LOGGED but does NOT affect confidence
# scores yet because:
# - The transliteration libraries are untested in production
# - Kanji readings can be ambiguous (稲葉曇 → "inaba don" vs "inabakumori")
# - We need real-world data to validate the approach
#
# Once logs show transliteration matching reliably identifies artists,
# we can wire it up to boost confidence scores.

# HARD SKIP LISTS - Tags that are NEVER artist names (confidence = 0.0)

_HARD_SKIP_EXACT = {
    # Agencies - organization names, never individual artists
    'ホロライブ', 'hololive', 'holostars', 'ホロスターズ',
    'にじさんじ', 'nijisanji', 'anycolor',
    'vspo', 'ぶいすぽ', 'vshojo', 'phase connect',
    'idol corp', '774inc', 'brave group',
    'cover corp', 'カバー株式会社',

    # Format markers - describe video type, not artist
    'mv', 'pv', 'music video', 'ミュージックビデオ',
    'lyric', 'lyrics', '歌詞',
    'shorts', '#shorts',

    # Platform noise
    'youtube', 'youtube music', 'spotify',
}

# Containment check - if these appear ANYWHERE in the tag, hard skip
_HARD_SKIP_CONTAINS = {
    'hololive', 'nijisanji', 'にじさんじ', 'ホロライブ',
}

# SOFT SKIP LIST - Reduces confidence but doesn't zero out
# These COULD be part of a legitimate compound tag (e.g., "Official髭男dism")
# so we penalize rather than reject outright.

_SOFT_SKIP = {
    # Content type descriptors
    '歌ってみた', 'cover', 'カバー', 'covered',
    'original', 'オリジナル', 'オリジナル曲',
    'コラボ', 'collab', 'collaboration',
    '公式', 'official',

    # Event markers
    '周年', 'anniversary',
    '誕生日', 'birthday',
    'デビュー', 'debut',
    '卒業', 'graduation',
    '記念', 'commemoration',

    # Role descriptors (but NOT vocaloid producer - those are real names)
    '歌い手', 'utaite',
    'vシンガー', 'vsinger',

    # Genre markers
    '東方', 'touhou',
    'jpop', 'j-pop', 'kpop', 'k-pop',
}


def calculate_tag_confidence(
    tag: str,
    video_title: Optional[str] = None,
    author: Optional[str] = None,
) -> float:
    """Score how likely a tag is to be a useful CJK artist name.

    Args:
        tag: The tag to score.
        video_title: Optional video title (used to detect song name tags).
        author: Optional author/channel name (used for transliteration cross-check logging).

    Returns:
        Confidence score from 0.0 to 1.0:
        - 0.0: Definitely not an artist name (hard skip)
        - 0.1-0.3: Unlikely to be useful
        - 0.4-0.6: Uncertain
        - 0.7-0.9: Likely a good artist name
        - 1.0: Reserved for exact matches to known artists (future use)
    """
    tag_lower = tag.lower().strip()

    if not tag_lower:
        return 0.0

    # -------------------------------------------------------------------------
    # HARD SKIP CHECK
    # -------------------------------------------------------------------------

    if tag_lower in _HARD_SKIP_EXACT:
        return 0.0

    if any(skip in tag_lower for skip in _HARD_SKIP_CONTAINS):
        return 0.0

    # -------------------------------------------------------------------------
    # VIDEO TITLE SIMILARITY CHECK
    # -------------------------------------------------------------------------
    # Penalize tags that look like the song title rather than artist name

    title_penalty = 1.0
    if video_title:
        similarity = _title_similarity(tag, video_title)

        if similarity > 0.8:
            # Tag is almost identical to title - almost certainly the song name
            return 0.1
        elif similarity > 0.5:
            # Suspicious overlap - might be song name, reduce confidence
            title_penalty = 0.6

    # -------------------------------------------------------------------------
    # SCRIPT COMPOSITION SCORING
    # -------------------------------------------------------------------------
    # Pure CJK tags are more likely to be Japanese/Chinese artist names

    cjk_ratio = _calculate_script_ratio(tag)

    if cjk_ratio == 1.0:
        # Pure CJK - highest base confidence
        confidence = 0.85
    elif cjk_ratio >= 0.7:
        # Mostly CJK with some Latin (like "みきとP", "DECO*27")
        confidence = 0.75
    elif cjk_ratio >= 0.4:
        # Mixed - uncertain but possible
        confidence = 0.55
    elif cjk_ratio > 0:
        # Mostly Latin with some CJK - probably not what we want
        confidence = 0.3
    else:
        # Pure Latin - not relevant for CJK name extraction
        return 0.0

    # -------------------------------------------------------------------------
    # SPECIAL PATTERN DETECTION
    # -------------------------------------------------------------------------

    # VocaloidP pattern gets a boost - these are almost always artist names
    if _is_vocaloid_p_pattern(tag):
        confidence = max(confidence, 0.8)

    # -------------------------------------------------------------------------
    # AUTHOR TRANSLITERATION CROSS-CHECK (DATA GATHERING)
    # -------------------------------------------------------------------------
    # Log transliteration matches for data gathering - does NOT affect score yet.
    # Once logs show this reliably identifies artists, we can wire it up to boost.
    #
    # WHY NOT WIRED UP: Kanji readings can be ambiguous (稲葉曇 → "inaba don"
    # vs "inabakumori"). Need real-world data to validate before trusting.

    if author and has_cjk(tag):
        # Try all applicable transliterations
        romanizations: List[Tuple[str, str]] = []  # (library_name, result)

        if has_japanese(tag):
            jp_romaji = transliterate_japanese(tag)
            if jp_romaji:
                romanizations.append(('pykakasi', jp_romaji))

        if has_chinese(tag):
            cn_pinyin = transliterate_chinese(tag)
            if cn_pinyin:
                romanizations.append(('pypinyin', cn_pinyin))

        if has_korean(tag):
            kr_roman = transliterate_korean(tag)
            if kr_roman:
                romanizations.append(('korean-romanizer', kr_roman))

        # Check for matches against author
        author_lower = author.lower().replace(' ', '')
        for lib_name, romanized in romanizations:
            romanized_normalized = romanized.lower().replace(' ', '')
            match_type: Optional[str] = None

            if romanized_normalized == author_lower:
                match_type = 'exact'
            elif romanized_normalized in author_lower:
                match_type = 'tag_in_author'
            elif author_lower in romanized_normalized:
                match_type = 'author_in_tag'

            if match_type:
                logger.info(
                    f"[Transliteration Cross-Check] MATCH | "
                    f"tag='{tag}' | romanized='{romanized}' | author='{author}' | "
                    f"match_type={match_type} | library={lib_name} | "
                    f"current_confidence={confidence:.2f}"
                )
                # FUTURE: Uncomment to boost confidence when validated
                # confidence = max(confidence, 0.9)
            else:
                logger.debug(
                    f"[Transliteration Cross-Check] no_match | "
                    f"tag='{tag}' | romanized='{romanized}' | author='{author}' | "
                    f"library={lib_name}"
                )

    # -------------------------------------------------------------------------
    # LENGTH PENALTIES
    # -------------------------------------------------------------------------

    tag_len = len(tag)

    if tag_len < 2:
        # Single character - very unlikely to be a useful artist name
        confidence *= 0.3
    elif tag_len > 25:
        # Very long - probably a phrase or description, not a name
        confidence *= 0.4
    elif tag_len > 20:
        # Long - suspicious
        confidence *= 0.5
    elif tag_len > 15:
        # Slightly long - mild penalty
        confidence *= 0.8

    # -------------------------------------------------------------------------
    # SOFT SKIP PENALTIES
    # -------------------------------------------------------------------------

    if any(skip in tag_lower for skip in _SOFT_SKIP):
        confidence *= 0.5

    # -------------------------------------------------------------------------
    # APPLY TITLE PENALTY
    # -------------------------------------------------------------------------

    confidence *= title_penalty

    return round(confidence, 3)


def extract_cjk_artist_names(
    tags: List[str],
    video_title: Optional[str] = None,
    author: Optional[str] = None,
    min_confidence: float = 0.4,
    max_results: int = 3,
) -> List[Tuple[str, float]]:
    """Extract likely CJK artist names from video tags, ranked by confidence.

    Args:
        tags: List of video tags to analyze.
        video_title: Video title (used to filter out song name tags).
        author: Author/channel name (used for transliteration cross-check logging).
        min_confidence: Minimum confidence threshold to include a tag.
        max_results: Maximum number of tags to return.

    Returns:
        List of (tag, confidence) tuples, sorted by confidence descending.
    """
    scored: List[Tuple[str, float]] = []

    for tag in tags:
        tag = tag.strip()

        # Must have CJK to be relevant for this extractor
        if not has_cjk(tag):
            continue

        confidence = calculate_tag_confidence(tag, video_title, author)

        if confidence >= min_confidence:
            scored.append((tag, confidence))

    # Sort by confidence descending, take top N
    scored.sort(key=lambda x: x[1], reverse=True)

    return scored[:max_results]


def extract_jp_names(
    tags: List[str],
    video_title: Optional[str] = None,
    author: Optional[str] = None,
) -> List[str]:
    """Extract likely Japanese artist names from video tags.

    This is the crown jewel for cross-language matching. Video tags contain
    both romanized and Japanese names (e.g., 'Murasaki Shion' and '紫咲シオン').
    By searching with both, we find ATVs regardless of how YTM indexed them.

    Uses confidence-based scoring to filter tags. Tags are scored 0.0-1.0 based
    on likelihood of being an actual artist name vs noise (agencies, song titles,
    format markers, etc.).

    Args:
        tags: List of video tags.
        video_title: Video title (used to penalize tags that match the song name).
        author: Author/channel name (used for transliteration cross-check logging).

    Returns:
        List of tags that look like CJK artist names, sorted by confidence.
    """
    if not tags:
        return []

    # Use confidence-based extraction
    ranked = extract_cjk_artist_names(
        tags,
        video_title=video_title,
        author=author,
        min_confidence=0.4,
        max_results=3,
    )

    # Log for observability
    if ranked:
        logger.debug(
            f"[Tag Extraction] Confidence-based: {[(t, f'{c:.2f}') for t, c in ranked]}"
        )

    # Return just the tag names (strip confidence scores)
    return [tag for tag, _conf in ranked]


def _calculate_script_ratio(tag: str) -> float:
    """Calculate the ratio of CJK characters to total alphabetic characters.

    Used to determine if a tag is primarily CJK (likely Japanese/Chinese name)
    vs primarily Latin (less useful for CJK name extraction).

    Args:
        tag: The tag to analyze.

    Returns:
        Float from 0.0 (no CJK) to 1.0 (pure CJK).
        Returns 0.0 if no alphabetic characters present.
    """
    cjk_count = sum(1 for c in tag if has_cjk(c))
    latin_count = sum(1 for c in tag if c.isalpha() and ord(c) < 0x300)
    total = cjk_count + latin_count

    if total == 0:
        return 0.0

    return cjk_count / total


def _is_vocaloid_p_pattern(tag: str) -> bool:
    """Detect Vocaloid producer naming pattern: CJK characters + P suffix.

    Examples: みきとP, ハチP, ピノキオピー, syudouP

    These are almost always real artist names and should get a confidence boost.

    Args:
        tag: The tag to check.

    Returns:
        True if tag matches the VocaloidP pattern.
    """
    # Ends with P or fullwidth P (U+FF30)
    if not tag.endswith('P') and not tag.endswith('Ｐ'):  # noqa: RUF001
        return False

    # Has CJK content before the P
    prefix = tag[:-1]
    return len(prefix) >= 2 and has_cjk(prefix)


def _title_similarity(tag: str, title: str) -> float:
    """Calculate similarity between tag and video title.

    Used to detect when a tag IS the song title (which we want to filter out).
    Intentionally simple word overlap - we just want to catch obvious cases.

    Args:
        tag: The tag to check.
        title: The video title.

    Returns:
        Float from 0.0 (no overlap) to 1.0 (tag words all appear in title).
    """
    def normalize(s: str) -> set:
        s = s.lower()
        # Remove common title noise/brackets (fullwidth chars intentional)
        for noise in ['【', '】', '「', '」', '[', ']', '/', '-', '|', '(', ')', '（', '）']:  # noqa: RUF001
            s = s.replace(noise, ' ')
        return {w for w in s.split() if len(w) >= 2}

    tag_words = normalize(tag)
    title_words = normalize(title)

    if not tag_words or not title_words:
        return 0.0

    overlap = len(tag_words & title_words)

    # Ratio of tag words that appear in title
    return overlap / len(tag_words)


# =============================================================================
# SECTION 7: PURE UTILITIES
# =============================================================================
# Stateless helpers. No module state, no complex dependencies.


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


def clean_microformat_title(title: str) -> str:
    """Strip YouTube suffixes from microformat title.

    Args:
        title: Raw microformat title.

    Returns:
        Cleaned title without YouTube suffixes.
    """
    for suffix in (' - YouTube Music', ' - YouTube'):
        if title.endswith(suffix):
            return title[:-len(suffix)]
    return title


# Keywords that indicate YTM mapped a remix/alternate version instead of the original.
# If these appear in videoDetails but NOT in microformat, it's a catalog mismatch.
CATALOG_MISMATCH_KEYWORDS = frozenset({
    'slowed', 'reverb', 'remix', 'nightcore', 'sped', 'speedup',
    'speed', 'bass', 'boosted', 'bassboosted', '8d', 'audio',
    'lofi', 'lo-fi', 'acoustic', 'instrumental', 'karaoke',
    'cover', 'live', 'concert', 'extended', 'edit', 'mashup',
})


def has_ytm_catalog_mismatch(vd_title: str, mf_title: str) -> bool:
    """Detect if YTM videoDetails points to a wrong version (catalog mismatch).

    YTM's catalog sometimes maps the wrong song variant to a video ID. For example,
    the original song's ID might return metadata for a "Slowed + Reverb" version.

    We detect this by checking if videoDetails contains specific remix/version
    keywords that don't appear in the microformat (raw YouTube) title. Only these
    keywords trigger a mismatch—author differences are ignored (channel name vs
    artist name is expected and doesn't pollute search results significantly).

    Args:
        vd_title: Title from videoDetails.
        mf_title: Cleaned title from microformat (raw YouTube title).

    Returns:
        True if mismatch detected (use microformat instead), False otherwise.
    """
    # Compare only titles, not author—author differences are expected
    # (channel name vs artist name is normal, not a mismatch)
    vd_words = extract_words(vd_title)
    mf_words = extract_words(mf_title)

    # Words in videoDetails but NOT in microformat
    extra_in_vd = vd_words - mf_words

    # Check if any are catalog mismatch keywords
    mismatch_words = extra_in_vd & CATALOG_MISMATCH_KEYWORDS

    if mismatch_words:
        logger.warning(
            f"[YTM Metadata] Catalog mismatch detected: "
            f"videoDetails='{vd_title}' has version keywords {mismatch_words} "
            f"not in microformat='{mf_title}'"
        )
        return True

    return False


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


# =============================================================================
# SECTION 8: THUMBNAILS
# =============================================================================
# Completely separate concern. Could be its own file.
# Grouped at bottom because it's orthogonal to search flow.


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

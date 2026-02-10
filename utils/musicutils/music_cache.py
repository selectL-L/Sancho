"""Music cache management for ambient playlists.

Provides proactive caching system that:
- Refreshes ALL playlists from ambience.toml on startup and every 24 hours
- Resolves per-track metadata via YTM + yt-dlp with provenance tracking
- Downloads tracks as M4A with embedded metadata and thumbnails
- Manages orphaned tracks with 90-day TTL
- Handles residential proxy fallback for blocked tracks

File Structure:
    cache/music/
        ambient.json       # Unified index: playlists + tracks + provenance
        tracks/            # Flat folder of human-readable M4A files
            Artist - Title [video_id].m4a
        orphaned/          # 90-day holding area for removed tracks
            Artist - Title [video_id].m4a
        residential/       # Permanent cache for proxy-downloaded tracks
            <video_id>.mp3
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .music_data import Track

from .music_data import Track
from .music_helpers import (
    MUTAGEN_AVAILABLE,
    YTDLP_AVAILABLE,
    download_track_as_m4a,
    fetch_playlist_metadata,
    generate_ambient_filename,
    get_residential_proxy_url,
)
from .music_auth import get_ytdlp_options

# Try to import yt_dlp for type checking the cast
try:
    import yt_dlp
except ImportError:
    yt_dlp = None  # type: ignore[assignment]


class MusicCacheManager:
    """Proactive music cache manager for ambient playlists.

    This class handles:
    - Automatic refresh of all playlists from ambience.toml
    - Per-track metadata resolution via YTM + yt-dlp with provenance
    - Background downloading of tracks as M4A with embedded metadata
    - Orphan management with 90-day TTL
    - Cache miss handling with immediate refresh

    File schema:
        ambient.json: Unified index with playlists, tracks, and provenance
    """

    AMBIENT_SCHEMA_VERSION = 2
    ORPHAN_TTL_DAYS = 90
    REFRESH_INTERVAL_HOURS = 24
    RESOLVE_DELAY_SECONDS = 0.75  # Rate-limit delay between YTM lookups

    def __init__(
        self,
        cache_root: str,
        logger: logging.Logger,
        on_track_unavailable: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ):
        """Initialize the music cache manager.

        Args:
            cache_root: Path to cache/music/ directory.
            logger: Logger instance for messages.
            on_track_unavailable: Async callback fired when a track is marked
                unavailable after all download methods fail. Signature:
                async def callback(title: str, artist: str, url: str) -> None
        """
        self.cache_root = cache_root
        self.logger = logger
        self._on_track_unavailable = on_track_unavailable

        # Paths
        self.tracks_path = os.path.join(cache_root, "tracks")
        self.orphaned_path = os.path.join(cache_root, "orphaned")
        self.residential_path = os.path.join(cache_root, "residential")
        self.ambient_file = os.path.join(cache_root, "ambient.json")

        # In-memory cache (lazy-loaded)
        self._ambient_cache: Optional[Dict[str, Any]] = None

        # Guards both in-memory mutation AND disk writes.
        # Must NOT be held during network calls.
        self._ambient_lock = asyncio.Lock()

        # Background tasks
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._download_task: Optional[asyncio.Task[None]] = None
        self._download_queue: asyncio.Queue[str] = asyncio.Queue()  # video_id strings
        self._missing_invalidation_tasks: Set[asyncio.Task[None]] = set()

        # In-memory thumbnail cache: video_id -> PNG bytes
        # Populated during resolution, consumed during download
        self._thumbnail_cache: Dict[str, bytes] = {}

        # Flag to signal download worker to stop
        self._shutdown = False

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize the cache manager. Call once on bot ready.

        Creates necessary directories and loads cached data.
        Does NOT hit YouTube - that happens in refresh_all_playlists().
        """
        self._ensure_directories()
        self._load_ambient()
        self.logger.info("[CacheManager] Initialized")

    def _ensure_directories(self) -> None:
        """Creates cache directories if they don't exist."""
        os.makedirs(self.cache_root, exist_ok=True)
        os.makedirs(self.tracks_path, exist_ok=True)
        os.makedirs(self.orphaned_path, exist_ok=True)
        os.makedirs(self.residential_path, exist_ok=True)

    # =========================================================================
    # AMBIENT.JSON - Unified Index
    # =========================================================================

    def _load_ambient(self) -> Dict[str, Any]:
        """Loads ambient.json or returns empty structure.

        Returns:
            The ambient cache dict.
        """
        if self._ambient_cache is not None:
            return self._ambient_cache

        if os.path.exists(self.ambient_file):
            try:
                with open(self.ambient_file, 'r', encoding='utf-8') as f:
                    self._ambient_cache = json.load(f)
                    if self._ambient_cache is not None:
                        return self._ambient_cache
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"[CacheManager] ambient.json corrupted: {e}")

        self._ambient_cache = {
            'version': self.AMBIENT_SCHEMA_VERSION,
            'last_refresh': 0,
            'playlists': {},
            'tracks': {},
        }
        return self._ambient_cache

    async def _save_ambient(self) -> None:
        """Atomic write of ambient.json to disk.

        Must be called while holding _ambient_lock.
        """
        if self._ambient_cache is None:
            return
        await asyncio.to_thread(self._write_ambient_to_disk)

    def _write_ambient_to_disk(self) -> None:
        """Sync helper for _save_ambient. Writes ambient.json atomically.

        Note: Only call from _save_ambient (via to_thread) to ensure lock protection.
        """
        if self._ambient_cache is None:
            return
        try:
            temp_file = self.ambient_file + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._ambient_cache, f, indent=2, ensure_ascii=False)
            shutil.move(temp_file, self.ambient_file)
        except IOError as e:
            self.logger.error(f"[CacheManager] Failed to save ambient.json: {e}")

    # =========================================================================
    # PLAYLIST MANAGEMENT
    # =========================================================================

    def get_all_playlist_urls(self) -> List[str]:
        """Extracts all unique playlist URLs from ambience.toml.

        Returns:
            List of unique playlist URLs.
        """
        from utils.ambience import _load_toml

        toml_data = _load_toml()
        playlists_section = toml_data.get('playlists', {})

        urls: Set[str] = set()
        for key, value in playlists_section.items():
            if key == 'descriptions':
                continue  # Skip the descriptions sub-table
            if isinstance(value, list):
                urls.update(value)

        return list(urls)

    async def refresh_all_playlists(self) -> Tuple[Dict[str, List[Track]], Dict[str, str]]:
        """Fetches ALL playlist URLs from ambience.toml and updates cache.

        For each playlist, fetches video IDs via yt-dlp, then resolves
        per-track metadata via YTM + yt-dlp with provenance tracking.

        Returns:
            Tuple of (results dict mapping playlist URLs to track lists,
            old_membership dict mapping video_id to playlist_url from
            before the refresh).
        """
        urls = self.get_all_playlist_urls()
        if not urls:
            self.logger.warning("[CacheManager] No playlist URLs found in ambience.toml")
            return {}, {}

        self.logger.info(f"[CacheManager] Refreshing {len(urls)} playlists from YouTube...")

        results: Dict[str, List[Track]] = {}
        ambient = self._load_ambient()

        # Snapshot current membership before overwriting (Fix 1)
        old_membership: Dict[str, str] = {}  # video_id -> playlist_url
        for url, playlist_data in ambient.get('playlists', {}).items():
            for vid in playlist_data.get('track_ids', []):
                old_membership[vid] = url  # Last-write-wins is fine

        # Phase 1: Fetch playlist structure via yt-dlp (video IDs + basic metadata)
        all_new_video_ids: Set[str] = set()
        for url in urls:
            try:
                tracks = await fetch_playlist_metadata(url, self.logger)
                if tracks:
                    results[url] = tracks
                    track_ids = [t.video_id for t in tracks if t.video_id]

                    # Update playlists section
                    ambient['playlists'][url] = {
                        'display_name': f"Playlist ({len(tracks)} tracks)",
                        'track_ids': track_ids,
                    }

                    # Identify video IDs not yet in tracks section
                    for vid in track_ids:
                        if vid not in ambient.get('tracks', {}):
                            all_new_video_ids.add(vid)

                    self.logger.debug(f"[CacheManager] Fetched {len(tracks)} tracks from {url[:50]}...")
                else:
                    self.logger.warning(f"[CacheManager] No tracks from {url[:50]}...")
            except Exception as e:
                self.logger.error(f"[CacheManager] Failed to fetch {url[:50]}: {e}")

        # Phase 2: Resolve metadata for new tracks
        if all_new_video_ids:
            self.logger.info(f"[CacheManager] Resolving metadata for {len(all_new_video_ids)} new tracks...")
            for video_id in all_new_video_ids:
                try:
                    entry = await self._resolve_track_metadata(
                        video_id, f"https://www.youtube.com/watch?v={video_id}"
                    )
                    async with self._ambient_lock:
                        ambient['tracks'][video_id] = entry
                except Exception as e:
                    self.logger.error(f"[CacheManager] Failed to resolve {video_id}: {e}")

                # Rate-limit between YTM lookups
                await asyncio.sleep(self.RESOLVE_DELAY_SECONDS)

        # Phase 3: Re-resolve existing tracks that have gaps (optional, lightweight)
        existing_with_gaps = [
            vid for vid, entry in ambient.get('tracks', {}).items()
            if vid not in all_new_video_ids and entry.get('title') == 'Unknown'
        ]
        if existing_with_gaps:
            self.logger.info(f"[CacheManager] Re-resolving {len(existing_with_gaps)} tracks with gaps...")
            for video_id in existing_with_gaps[:20]:  # Cap at 20 per refresh
                try:
                    entry = await self._resolve_track_metadata(
                        video_id, f"https://www.youtube.com/watch?v={video_id}"
                    )
                    async with self._ambient_lock:
                        ambient['tracks'][video_id] = entry
                except Exception as e:
                    self.logger.debug(f"[CacheManager] Re-resolve failed for {video_id}: {e}")
                await asyncio.sleep(self.RESOLVE_DELAY_SECONDS)

        # Phase 4: Reset unavailable flags for tracks still in playlists.
        # This gives previously-blocked tracks another download attempt each
        # refresh cycle in case restrictions have been lifted.
        current_track_ids: Set[str] = set()
        for playlist_data in ambient.get('playlists', {}).values():
            current_track_ids.update(playlist_data.get('track_ids', []))

        reset_count = 0
        for video_id in current_track_ids:
            entry = ambient.get('tracks', {}).get(video_id)
            if entry and entry.get('unavailable'):
                entry['unavailable'] = None
                reset_count += 1

        if reset_count:
            self.logger.info(f"[CacheManager] Reset {reset_count} unavailable flags for retry")

        # Save
        async with self._ambient_lock:
            ambient['last_refresh'] = time.time()
            self._ambient_cache = ambient
            await self._save_ambient()

        self.logger.info(f"[CacheManager] Refresh complete: {len(results)} playlists updated")
        return results, old_membership

    def get_cached_tracks(self, playlist_url: str) -> List[Track]:
        """Returns tracks from cache without hitting YouTube.

        Reads track_ids from ambient.json playlists section, then looks up
        each track in the tracks section and converts to Track objects.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            List of Track objects, or empty list if not cached.
        """
        ambient = self._load_ambient()
        playlist_data = ambient.get('playlists', {}).get(playlist_url)

        if not playlist_data:
            return []

        tracks_section = ambient.get('tracks', {})
        tracks: List[Track] = []

        for video_id in playlist_data.get('track_ids', []):
            entry = tracks_section.get(video_id)
            if not entry:
                continue

            # Skip tracks marked as unavailable (age-restricted, region-locked, etc.)
            if entry.get('unavailable'):
                continue

            track = Track(
                title=entry.get('title', 'Unknown'),
                artist=entry.get('artist', 'Unknown'),
                url=f"https://www.youtube.com/watch?v={video_id}",
                duration=entry.get('duration', 0),
                thumbnail=entry.get('thumbnail_url'),
                thumbnail_is_square=entry.get('thumbnail_is_square', False),
                video_id=video_id,
                album=entry.get('album'),
                source=entry.get('source', 'youtube'),
                is_explicit=entry.get('is_explicit'),
                version_label=entry.get('version_label', 'Video'),
                view_count=entry.get('view_count'),
                video_type=entry.get('video_type'),
            )
            tracks.append(track)

        return tracks

    # =========================================================================
    # METADATA RESOLUTION
    # =========================================================================

    async def _resolve_track_metadata(self, video_id: str, url: str) -> Dict[str, Any]:
        """Resolve full metadata for a single track via YTM + yt-dlp.

        Calls _build_quick_result for YTM data, then fills gaps from yt-dlp.
        Records per-field provenance (_source suffixes).

        Args:
            video_id: YouTube video ID.
            residential_allowed: Whether to consider residential cache files.
            url: Full YouTube URL.

        Returns:
            An ambient.json track entry dict with all fields and provenance.
        """
        from .search import (
            _build_quick_result,
            fetch_and_resize_thumbnail,
            MUSIC_VIDEO_TYPE_ATV,
        )

        entry: Dict[str, Any] = {
            'video_id': video_id,
            'url': url,
        }

        # ------------------------------------------------------------------
        # Step 1: YTM lookup via _build_quick_result
        # ------------------------------------------------------------------
        ytm_data: Dict[str, Any] = {}
        try:
            result = await _build_quick_result(video_id)
            if (
                result.title == 'Unknown'
                and result.artist == 'Unknown'
                and not result.thumbnail_url
            ):
                raise ValueError("YTM metadata unavailable")
            ytm_data = {
                'title': result.title if result.title != 'Unknown' else None,
                'artist': result.artist if result.artist != 'Unknown' else None,
                'album': result.album,
                'duration': result.duration_seconds if result.duration_seconds else None,
                'is_explicit': result.is_explicit,
                'thumbnail_url': result.thumbnail_url,
                'thumbnail_is_square': result.thumbnail_is_square,
                'video_type': result.video_type,
                'source': result.source,
                'version_label': result.version_label,
                'view_count': result.view_count,
            }
        except Exception as e:
            self.logger.debug(f"[CacheManager] YTM lookup failed for {video_id}: {e}")

        # ------------------------------------------------------------------
        # Step 2: yt-dlp gap-fill for missing fields
        # Only used for title/artist/duration when YTM can't provide them.
        # ------------------------------------------------------------------
        ytdlp_data: Dict[str, Any] = {}
        needs_ytdlp = (
            ytm_data.get('title') is None
            or ytm_data.get('artist') is None
            or ytm_data.get('duration') is None
        )

        if needs_ytdlp and YTDLP_AVAILABLE:
            try:
                import yt_dlp
                from typing import cast
                ydl_opts = get_ytdlp_options({
                    'quiet': True,
                    'no_warnings': True,
                    'noplaylist': True,
                })
                info = await asyncio.to_thread(
                    lambda: yt_dlp.YoutubeDL(cast(Any, ydl_opts)).extract_info(url, download=False)  # type: ignore[union-attr]
                )
                if info:
                    ytdlp_data = {
                        'title': info.get('title'),
                        'artist': info.get('uploader') or info.get('channel'),
                        'duration': int(info.get('duration', 0) or 0),
                    }
            except Exception as e:
                self.logger.debug(f"[CacheManager] yt-dlp extract_info failed for {video_id}: {e}")

        # ------------------------------------------------------------------
        # Step 3: Per-field merge with provenance
        # ------------------------------------------------------------------
        provenance_fields = ['title', 'artist', 'album', 'duration', 'is_explicit']
        for field in provenance_fields:
            ytm_val = ytm_data.get(field)
            ytdlp_val = ytdlp_data.get(field)
            if ytm_val is not None:
                entry[field] = ytm_val
                entry[f'{field}_source'] = 'ytm'
            elif ytdlp_val is not None:
                entry[field] = ytdlp_val
                entry[f'{field}_source'] = 'ytdlp'
            else:
                entry[field] = None
                entry[f'{field}_source'] = None

        # Fallback defaults for required display fields
        if entry.get('title') is None:
            entry['title'] = 'Unknown'
        if entry.get('artist') is None:
            entry['artist'] = 'Unknown'
        if entry.get('duration') is None:
            entry['duration'] = 0

        # Non-provenance fields (don't need _source tracking)
        entry['video_type'] = ytm_data.get('video_type')
        entry['source'] = ytm_data.get('source', 'youtube')
        entry['version_label'] = ytm_data.get('version_label', 'Video')
        entry['view_count'] = ytm_data.get('view_count')

        # ------------------------------------------------------------------
        # Step 4: Thumbnail fetch and resize
        # ------------------------------------------------------------------
        # ytm_data.thumbnail_url may be either a YTM square thumbnail OR a constructed
        # 16:9 URL built from video_id. Thumbnails should always be available via YTM.
        thumb_url = ytm_data.get('thumbnail_url')
        entry['thumbnail_url'] = thumb_url
        entry['thumbnail_is_square'] = ytm_data.get('thumbnail_is_square', False)
        entry['thumbnail_source'] = 'ytm' if ytm_data.get('thumbnail_url') else None

        if thumb_url:
            try:
                thumb_bytes = await fetch_and_resize_thumbnail(thumb_url)
                if thumb_bytes:
                    self._thumbnail_cache[video_id] = thumb_bytes
            except Exception as e:
                self.logger.debug(f"[CacheManager] Thumbnail fetch failed for {video_id}: {e}")

        # ------------------------------------------------------------------
        # Step 5: Filename generation
        # ------------------------------------------------------------------
        video_type = entry.get('video_type') or ''
        artist_for_name = entry['artist'] if video_type == MUSIC_VIDEO_TYPE_ATV else None

        filename, was_modified, original = generate_ambient_filename(
            title=entry['title'],
            video_id=video_id,
            video_type=video_type,
            artist=artist_for_name,
        )
        entry['filename'] = filename
        entry['filename_modified'] = was_modified
        entry['filename_original'] = original

        # ------------------------------------------------------------------
        # Step 6: Timestamps
        # ------------------------------------------------------------------
        entry['resolved_at'] = time.time()
        entry['downloaded_at'] = None  # Set by download worker
        entry['orphan'] = None  # Set by reconciliation
        entry['unavailable'] = None  # Set by download worker on permanent failure

        self.logger.debug(
            f"[CacheManager] Resolved {video_id}: "
            f"title='{entry['title']}' artist='{entry['artist']}' "
            f"type={entry.get('video_type')}"
        )

        return entry

    # =========================================================================
    # DOWNLOAD MANAGEMENT
    # =========================================================================

    def _schedule_missing_invalidation(self, video_id: str) -> None:
        """Best-effort invalidation for a missing cached file.

        Args:
            video_id: YouTube video ID.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.debug(
                f"[CacheManager] No running loop to invalidate missing file for {video_id}"
            )
            return

        task = loop.create_task(self._invalidate_missing_download(video_id))
        self._missing_invalidation_tasks.add(task)
        task.add_done_callback(self._missing_invalidation_tasks.discard)

    async def _invalidate_missing_download(self, video_id: str) -> None:
        """Marks a missing download as not downloaded and re-queues it.

        Args:
            video_id: YouTube video ID.
        """
        async with self._ambient_lock:
            ambient = self._load_ambient()
            entry = ambient.get('tracks', {}).get(video_id)
            if not entry or entry.get('downloaded_at') is None:
                return

            entry['downloaded_at'] = None
            await self._save_ambient()

        try:
            self._download_queue.put_nowait(video_id)
        except asyncio.QueueFull:
            self.logger.warning(f"[CacheManager] Download queue full for {video_id}")

    def get_any_local_path(self, video_id: str, residential_allowed: bool = False) -> Optional[str]:
        """Finds any existing copy of a track across all locations.

        Args:
            video_id: YouTube video ID.

        Returns:
            Path to existing cached file, or None.
        """
        # Check tracks/ via ambient.json filename
        ambient = self._load_ambient()
        entry = ambient.get('tracks', {}).get(video_id)

        if entry:
            filename = entry.get('filename')
            if filename:
                file_path = os.path.join(self.tracks_path, filename)
                if os.path.exists(file_path):
                    return file_path
                if entry.get('downloaded_at') is not None:
                    self.logger.warning(
                        f"[CacheManager] Missing cached file for {video_id}: {filename}"
                    )
                    self._schedule_missing_invalidation(video_id)

                orphaned_path = os.path.join(self.orphaned_path, filename)
                if os.path.exists(orphaned_path):
                    return orphaned_path

        # Check orphaned/ by video_id as fallback
        pattern = os.path.join(self.orphaned_path, f"*[[]{video_id}[]]*")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

        if residential_allowed:
            # Check residential cache (MP3 + legacy formats)
            residential_path = self._find_residential_file(video_id)
            if residential_path:
                return residential_path

        return None

    def _find_residential_file(self, video_id: str) -> Optional[str]:
        """Find a cached residential download for a video ID.

        Args:
            video_id: YouTube video ID.

        Returns:
            Path to cached residential file if exists, None otherwise.
        """
        mp3_path = os.path.join(self.residential_path, f"{video_id}.mp3")
        if os.path.exists(mp3_path):
            return mp3_path

        for ext in ['.webm', '.opus', '.m4a', '.ogg']:
            path = os.path.join(self.residential_path, f"{video_id}{ext}")
            if os.path.exists(path):
                return path

        return None

    async def download_track(self, video_id: str) -> Optional[str]:
        """Downloads a single track to the tracks/ folder.

        Looks up metadata from ambient.json, checks for existing copies,
        downloads as M4A with embedded metadata and thumbnail. Falls back
        to residential proxy on 403/IP-block.

        Args:
            video_id: Video ID to download.

        Returns:
            Local path on success, None on failure.
        """
        ambient = self._load_ambient()
        entry = ambient.get('tracks', {}).get(video_id)

        if not entry:
            self.logger.warning(f"[CacheManager] No track entry for {video_id}")
            return None

        filename = entry.get('filename')
        if not filename:
            self.logger.warning(f"[CacheManager] No filename for {video_id}")
            return None

        target_path = os.path.join(self.tracks_path, filename)

        # Already exists?
        if os.path.exists(target_path):
            return target_path

        # Check for existing copy elsewhere (orphaned, etc.)
        existing_path = self.get_any_local_path(video_id, residential_allowed=False)
        if existing_path and existing_path != target_path:
            residential_root = os.path.abspath(self.residential_path)
            existing_abs = os.path.abspath(existing_path)
            if os.path.commonpath([existing_abs, residential_root]) == residential_root:
                self.logger.info(
                    f"[CacheManager] Skipping residential cache promotion for {video_id}"
                )
            else:
                try:
                    shutil.copy2(existing_path, target_path)
                    async with self._ambient_lock:
                        entry['downloaded_at'] = time.time()
                        await self._save_ambient()
                    self.logger.info(f"[CacheManager] Copied {video_id} from existing location")
                    return target_path
                except IOError as e:
                    self.logger.warning(f"[CacheManager] Copy failed: {e}")

        # Download fresh
        if not YTDLP_AVAILABLE or not MUTAGEN_AVAILABLE:
            return None

        url = entry.get('url', f"https://www.youtube.com/watch?v={video_id}")

        # Get thumbnail bytes (from cache or re-fetch)
        thumb_bytes = self._thumbnail_cache.pop(video_id, None)
        if thumb_bytes is None and entry.get('thumbnail_url'):
            try:
                from .search import fetch_and_resize_thumbnail
                thumb_bytes = await fetch_and_resize_thumbnail(entry['thumbnail_url'])
            except Exception as e:
                self.logger.debug(f"[CacheManager] Thumbnail re-fetch failed for {video_id}: {e}")

        try:
            result = await download_track_as_m4a(
                url=url,
                output_dir=self.tracks_path,
                logger=self.logger,
                custom_title=entry.get('title'),
                custom_artist=entry.get('artist'),
                custom_album=entry.get('album'),
                is_explicit=entry.get('is_explicit'),
                thumbnail_bytes=thumb_bytes,
                target_filename=filename,
                ydl_opts=get_ytdlp_options(),
            )

            if result.success and result.file_path:
                async with self._ambient_lock:
                    entry['downloaded_at'] = time.time()
                    await self._save_ambient()
                self.logger.info(f"[CacheManager] Downloaded: {entry.get('title')}")
                return result.file_path

            # Check for 403/IP-block
            error_msg = (result.error_message or '').lower()
            is_ip_block = any(ind in error_msg for ind in ['403', 'forbidden', 'sign in', 'age-restricted'])

            if is_ip_block:
                self.logger.info(f"[CacheManager] Direct download blocked, trying residential: {entry.get('title')}")
                residential_result = await self._download_track_via_residential(
                    video_id=video_id,
                    entry=entry,
                    target_path=target_path,
                    thumbnail_bytes=thumb_bytes,
                )
                if residential_result:
                    return residential_result

            # Both direct and residential failed (or non-IP error) — mark unavailable
            await self._mark_track_unavailable(video_id, entry, result.error_message)
            return None

        except Exception as e:
            self.logger.error(f"[CacheManager] Download error: {e}")
            return None

    async def _mark_track_unavailable(
        self,
        video_id: str,
        entry: Dict[str, Any],
        error_message: Optional[str] = None,
    ) -> None:
        """Mark a track as unavailable after all download methods fail.

        Sets the ``unavailable`` field in ambient.json so the track is
        excluded from playlist loading and future download queues.
        Fires the ``on_track_unavailable`` callback to notify the owner.

        Args:
            video_id: YouTube video ID.
            entry: Track entry dict from ambient.json (mutated in-place).
            error_message: Error string from the last download attempt.
        """
        title = entry.get('title', 'Unknown')
        artist = entry.get('artist', 'Unknown')
        url = entry.get('url', f"https://www.youtube.com/watch?v={video_id}")

        async with self._ambient_lock:
            entry['unavailable'] = {
                'marked_at': time.time(),
                'reason': error_message or 'Download failed',
            }
            await self._save_ambient()

        self.logger.warning(
            f"[CacheManager] Marked unavailable: '{title}' by {artist} ({video_id}) "
            f"- {error_message}"
        )

        # Notify owner via callback
        if self._on_track_unavailable:
            try:
                await self._on_track_unavailable(title, artist, url)
            except Exception as e:
                self.logger.debug(f"[CacheManager] Unavailable callback error: {e}")

    async def _download_track_via_residential(
        self,
        video_id: str,
        entry: Dict[str, Any],
        target_path: str,
        thumbnail_bytes: Optional[bytes] = None,
    ) -> Optional[str]:
        """Downloads a track via residential proxy with ambient-quality settings.

        Produces an M4A file identical to direct download, just routed through
        the proxy. Unlike download_residential() which serves live playback at
        192kbps MP3, this uses full 320kbps M4A with metadata.

        Args:
            video_id: YouTube video ID.
            entry: Track entry from ambient.json.
            target_path: Final destination path in tracks/.
            thumbnail_bytes: Pre-fetched PNG thumbnail bytes, or None.

        Returns:
            Path to downloaded file on success, None on failure.
        """
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            self.logger.info("[CacheManager] Residential proxy not configured, cannot retry")
            return None

        url = entry.get('url', f"https://www.youtube.com/watch?v={video_id}")
        filename = entry.get('filename')
        if not filename:
            return None

        self.logger.info(f"[CacheManager] Retrying via residential proxy: {entry.get('title')}")

        result = await download_track_as_m4a(
            url=url,
            output_dir=self.tracks_path,
            logger=self.logger,
            custom_title=entry.get('title'),
            custom_artist=entry.get('artist'),
            custom_album=entry.get('album'),
            is_explicit=entry.get('is_explicit'),
            thumbnail_bytes=thumbnail_bytes,
            target_filename=filename,
            proxy=proxy_url,
            ydl_opts=get_ytdlp_options(),
        )

        if not result.success or not result.file_path:
            error_msg = (result.error_message or '').lower()
            if '403' in error_msg:
                self.logger.warning(f"[CacheManager] 403 even via residential: {entry.get('title')}")
            else:
                self.logger.warning(f"[CacheManager] Residential download failed: {result.error_message}")
            return None

        # Log cost estimate
        import config
        file_size = os.path.getsize(result.file_path)
        cost_per_gb = getattr(config, 'RESIDENTIAL_PROXY_COST_PER_GB', 4.0)
        cost = (file_size / (1024 ** 3)) * cost_per_gb
        self.logger.info(
            f"[CacheManager] Downloaded via residential: {entry.get('title')} - "
            f"{file_size / (1024*1024):.2f} MB (~${cost:.4f})"
        )

        async with self._ambient_lock:
            entry['downloaded_at'] = time.time()
            await self._save_ambient()

        return result.file_path

    async def start_background_downloads(self) -> None:
        """Starts background download worker.

        Call after refresh_all_playlists() to download missing tracks.
        """
        if self._download_task and not self._download_task.done():
            return

        self._shutdown = False
        self._download_task = asyncio.create_task(self._download_worker())
        self.logger.info("[CacheManager] Background download worker started")

    async def queue_missing_downloads(self) -> int:
        """Queues all tracks that need downloading.

        Walks ambient.json tracks — queues any where downloaded_at is None
        or the file doesn't exist on disk.

        Returns:
            Number of tracks queued.
        """
        ambient = self._load_ambient()
        queued = 0

        for video_id, entry in ambient.get('tracks', {}).items():
            # Skip orphaned tracks
            if entry.get('orphan') is not None:
                continue

            # Skip tracks marked as unavailable
            if entry.get('unavailable'):
                continue

            filename = entry.get('filename')
            if not filename:
                continue

            file_path = os.path.join(self.tracks_path, filename)
            if not os.path.exists(file_path):
                await self._download_queue.put(video_id)
                queued += 1

        self.logger.info(f"[CacheManager] Queued {queued} tracks for download")
        return queued

    async def _download_worker(self) -> None:
        """Background worker that processes download queue."""
        while not self._shutdown:
            try:
                try:
                    video_id = await asyncio.wait_for(
                        self._download_queue.get(),
                        timeout=5.0
                    )
                except TimeoutError:
                    continue

                await self.download_track(video_id)

                # Rate limit: small delay between downloads
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[CacheManager] Download worker error: {e}")
                await asyncio.sleep(5.0)

        self.logger.info("[CacheManager] Download worker stopped")

    # =========================================================================
    # ORPHAN MANAGEMENT
    # =========================================================================

    async def reconcile_downloads(self, new_playlists: Dict[str, List[Track]],
                                    old_membership: Optional[Dict[str, str]] = None) -> None:
        """Compares ambient.json against new playlist data.

        Handles:
        - Tracks removed from all playlists -> orphan
        - Tracks that returned -> unorphan

        Args:
            new_playlists: Dict of {playlist_url: [Track, ...]} from refresh.
            old_membership: Pre-refresh map of video_id -> playlist_url.
                If None, original_playlist info won't be available.
        """
        ambient = self._load_ambient()

        # Build set of all active video IDs (union of all track_ids)
        active_ids: Set[str] = set()
        for playlist_data in ambient.get('playlists', {}).values():
            active_ids.update(playlist_data.get('track_ids', []))

        orphaned_count = 0
        unorphaned_count = 0

        for video_id, entry in list(ambient.get('tracks', {}).items()):
            is_active = video_id in active_ids
            is_orphaned = entry.get('orphan') is not None

            if not is_active and not is_orphaned:
                # Track removed from all playlists -> orphan it
                self._orphan_track(video_id, entry, old_membership)
                orphaned_count += 1
            elif is_active and is_orphaned:
                # Track returned -> unorphan it
                self._unorphan_track(video_id, entry)
                unorphaned_count += 1

        async with self._ambient_lock:
            self._ambient_cache = ambient
            await self._save_ambient()

        if orphaned_count or unorphaned_count:
            self.logger.info(
                f"[CacheManager] Reconciliation: {orphaned_count} orphaned, "
                f"{unorphaned_count} unorphaned"
            )
        else:
            self.logger.debug("[CacheManager] Reconciliation: no changes")

    def _orphan_track(self, video_id: str, entry: Dict[str, Any],
                      old_membership: Optional[Dict[str, str]] = None) -> None:
        """Moves a track from tracks/ to orphaned/.

        Args:
            video_id: YouTube video ID.
            entry: Track entry from ambient.json (modified in place).
            old_membership: Pre-refresh map of video_id -> playlist_url.
        """
        filename = entry.get('filename')
        if not filename:
            return

        source_path = os.path.join(self.tracks_path, filename)
        target_path = os.path.join(self.orphaned_path, filename)

        if os.path.exists(source_path):
            try:
                if os.path.exists(target_path):
                    os.remove(source_path)  # Already in orphaned
                else:
                    shutil.move(source_path, target_path)
                self.logger.debug(f"[CacheManager] Orphaned: {filename}")
            except IOError as e:
                self.logger.warning(f"[CacheManager] Could not orphan {video_id}: {e}")

        entry['orphan'] = {
            'orphaned_at': time.time(),
            'original_playlist': old_membership.get(video_id) if old_membership else None,
        }

    def _unorphan_track(self, video_id: str, entry: Dict[str, Any]) -> None:
        """Moves a track from orphaned/ back to tracks/.

        Args:
            video_id: YouTube video ID.
            entry: Track entry from ambient.json (modified in place).
        """
        filename = entry.get('filename')
        if not filename:
            return

        source_path = os.path.join(self.orphaned_path, filename)
        target_path = os.path.join(self.tracks_path, filename)

        if os.path.exists(source_path):
            try:
                shutil.move(source_path, target_path)
                entry['downloaded_at'] = time.time()
                self.logger.debug(f"[CacheManager] Unorphaned: {filename}")
            except IOError as e:
                self.logger.warning(f"[CacheManager] Could not unorphan {video_id}: {e}")
        else:
            # File not in orphaned either — needs re-download
            entry['downloaded_at'] = None

        entry['orphan'] = None

    async def cleanup_expired_orphans(self) -> int:
        """Deletes orphaned files older than 90 days.

        Returns:
            Number of files deleted.
        """
        ambient = self._load_ambient()
        now = time.time()
        ttl_seconds = self.ORPHAN_TTL_DAYS * 24 * 3600
        deleted = 0

        for video_id, entry in list(ambient.get('tracks', {}).items()):
            orphan_info = entry.get('orphan')
            if not orphan_info:
                continue

            orphaned_at = orphan_info.get('orphaned_at', 0)
            if now - orphaned_at > ttl_seconds:
                filename = entry.get('filename')
                if filename:
                    file_path = os.path.join(self.orphaned_path, filename)
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except IOError as e:
                        self.logger.warning(f"[CacheManager] Could not delete orphan {video_id}: {e}")

                # Remove entire track entry
                del ambient['tracks'][video_id]
                deleted += 1
                self.logger.debug(f"[CacheManager] Expired orphan deleted: {video_id}")

        if deleted > 0:
            async with self._ambient_lock:
                self._ambient_cache = ambient
                await self._save_ambient()
            self.logger.info(f"[CacheManager] Cleaned up {deleted} expired orphans")

        return deleted

    async def clear_orphaned(self) -> int:
        """Manually clears all orphaned files.

        Returns:
            Number of files deleted.
        """
        ambient = self._load_ambient()
        deleted = 0

        for video_id, entry in list(ambient.get('tracks', {}).items()):
            if entry.get('orphan') is None:
                continue

            filename = entry.get('filename')
            if filename:
                file_path = os.path.join(self.orphaned_path, filename)
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except IOError as e:
                    self.logger.warning(f"[CacheManager] Could not delete orphan {video_id}: {e}")

            del ambient['tracks'][video_id]
            deleted += 1

        async with self._ambient_lock:
            self._ambient_cache = ambient
            await self._save_ambient()

        self.logger.info(f"[CacheManager] Cleared {deleted} orphaned files")
        return deleted

    # =========================================================================
    # SCHEDULED REFRESH
    # =========================================================================

    def start_refresh_timer(self) -> None:
        """Starts 24-hour refresh timer."""
        if self._refresh_task and not self._refresh_task.done():
            return

        self._refresh_task = asyncio.create_task(self._refresh_timer_task())
        self.logger.info("[CacheManager] Refresh timer started (24h interval)")

    def cancel_refresh_timer(self) -> None:
        """Cancels the refresh timer."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_timer_task(self) -> None:
        """Background task that refreshes playlists every 24 hours."""
        while True:
            try:
                await asyncio.sleep(self.REFRESH_INTERVAL_HOURS * 3600)

                self.logger.info("[CacheManager] Scheduled refresh starting...")
                playlists, old_membership = await self.refresh_all_playlists()
                await self.reconcile_downloads(playlists, old_membership)
                await self.cleanup_expired_orphans()
                await self.queue_missing_downloads()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[CacheManager] Refresh timer error: {e}")
                await asyncio.sleep(300)  # Wait 5 min on error

    # =========================================================================
    # CACHE MISS HANDLING
    # =========================================================================

    async def handle_cache_miss(self, playlist_url: str) -> List[Track]:
        """Handles cache miss by triggering immediate refresh.

        Cancels current timer, refreshes, restarts timer.

        Args:
            playlist_url: Playlist URL that had cache miss.

        Returns:
            Fresh track list.
        """
        self.logger.info(f"[CacheManager] Cache miss for {playlist_url[:50]}...")

        # Cancel existing timer
        self.cancel_refresh_timer()

        # Refresh just this playlist
        tracks = await fetch_playlist_metadata(playlist_url, self.logger)
        if tracks:
            ambient = self._load_ambient()
            track_ids = [t.video_id for t in tracks if t.video_id]
            ambient['playlists'][playlist_url] = {
                'display_name': f"Playlist ({len(tracks)} tracks)",
                'track_ids': track_ids,
            }

            # Resolve metadata for any new tracks
            for video_id in track_ids:
                if video_id not in ambient.get('tracks', {}):
                    try:
                        entry = await self._resolve_track_metadata(
                            video_id, f"https://www.youtube.com/watch?v={video_id}"
                        )
                        ambient['tracks'][video_id] = entry
                    except Exception as e:
                        self.logger.debug(f"[CacheManager] Resolve failed for {video_id}: {e}")
                    await asyncio.sleep(self.RESOLVE_DELAY_SECONDS)

            async with self._ambient_lock:
                ambient['last_refresh'] = time.time()
                self._ambient_cache = ambient
                await self._save_ambient()

        # Restart timer
        self.start_refresh_timer()

        return tracks

    # =========================================================================
    # STATISTICS
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Returns cache statistics.

        Returns:
            Dict with cache statistics including provenance breakdown.
        """
        ambient = self._load_ambient()

        # Count files and calculate size from tracks/
        total_size = 0
        downloaded_count = 0

        if os.path.exists(self.tracks_path):
            for filename in os.listdir(self.tracks_path):
                if filename.endswith('.m4a'):
                    downloaded_count += 1
                    total_size += os.path.getsize(os.path.join(self.tracks_path, filename))

        # Count orphaned
        orphaned_count = 0
        orphaned_size = 0
        if os.path.exists(self.orphaned_path):
            for filename in os.listdir(self.orphaned_path):
                if filename.endswith('.m4a'):
                    orphaned_count += 1
                    orphaned_size += os.path.getsize(os.path.join(self.orphaned_path, filename))

        # Count total tracks across all playlists
        total_tracks = 0
        for data in ambient.get('playlists', {}).values():
            total_tracks += len(data.get('track_ids', []))

        # Provenance stats from tracks section
        tracks_section = ambient.get('tracks', {})
        provenance_fields = ['title_source', 'artist_source', 'album_source',
                             'duration_source', 'is_explicit_source']
        ytm_resolved = 0
        ytdlp_fallback = 0
        unresolved = 0
        filenames_modified = 0
        pending_downloads = 0

        for entry in tracks_section.values():
            if entry.get('orphan') is not None:
                continue  # Don't count orphans in provenance stats

            # Count provenance
            sources = [entry.get(f) for f in provenance_fields]
            non_null_sources = [s for s in sources if s is not None]
            if non_null_sources and all(s == 'ytm' for s in non_null_sources):
                ytm_resolved += 1
            elif any(s == 'ytdlp' for s in sources):
                ytdlp_fallback += 1
            elif any(s is None for s in sources):
                unresolved += 1

            if entry.get('filename_modified'):
                filenames_modified += 1

            if entry.get('downloaded_at') is None:
                pending_downloads += 1

        last_refresh = ambient.get('last_refresh', 0)

        return {
            'total_playlists': len(ambient.get('playlists', {})),
            'total_tracks': total_tracks,
            'downloaded_tracks': downloaded_count,
            'orphaned_tracks': orphaned_count,
            'size_mb': total_size / (1024 * 1024),
            'orphaned_size_mb': orphaned_size / (1024 * 1024),
            'last_refresh': last_refresh,
            'last_refresh_ago': time.time() - last_refresh if last_refresh else None,
            # Provenance stats
            'ytm_resolved_count': ytm_resolved,
            'ytdlp_fallback_count': ytdlp_fallback,
            'unresolved_count': unresolved,
            'filenames_modified_count': filenames_modified,
            'pending_download_count': pending_downloads,
        }

    # =========================================================================
    # RESIDENTIAL PROXY CACHE
    # =========================================================================
    # Tracks downloaded via residential proxy are cached here permanently.
    # NO TTL - these files cost real money ($4/GB) and if a track needs
    # residential proxy once, it will likely need it forever (datacenter IP blocked).
    # Only manual clear should remove these files.

    async def download_residential(
        self,
        track: 'Track',
        timeout: float = 180.0,
        max_duration: int = 900  # 15 minutes max by default
    ) -> Tuple[bool, Optional[str], int, Optional[str]]:
        """Download a track via residential proxy and cache it as MP3.

        Downloads the full audio file through a residential proxy to bypass
        YouTube's IP-based blocks. The file is converted to MP3 and saved
        to the residential cache permanently.

        Args:
            track: Track to download.
            timeout: Maximum download time in seconds.
            max_duration: Maximum track duration in seconds (default 15 min).
                          Prevents downloading 10-hour meme videos over paid proxy.

        Returns:
            Tuple of (success, error_message, bytes_downloaded, cached_file_path).

        SAFEGUARDS:
        - Duration limit prevents downloading absurdly long videos
        - Timeout prevents hanging downloads
        - Returns byte count for cost tracking
        - Does NOT retry internally (caller handles retries)
        - Converts to MP3 for consistency with ambient cache
        """
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            return False, "Residential proxy not configured", 0, None

        if not YTDLP_AVAILABLE:
            return False, "yt-dlp not available", 0, None

        if not track.video_id:
            return False, "Track has no video ID", 0, None

        # Duration safeguard - don't waste money on 10-hour videos
        if track.duration > max_duration:
            duration_str = f"{track.duration // 60}:{track.duration % 60:02d}"
            max_str = f"{max_duration // 60}:{max_duration % 60:02d}"
            self.logger.warning(
                f"[Residential] Refusing to download '{track.title}' - "
                f"duration {duration_str} exceeds limit {max_str}"
            )
            return False, f"Track too long ({duration_str} > {max_str} limit)", 0, None

        import config
        from typing import cast

        # Output path (without extension - yt-dlp will add it, then postprocessor changes to .mp3)
        output_base = os.path.join(self.residential_path, track.video_id)

        # Build yt-dlp options with MP3 conversion (like ambient downloads)
        ydl_opts = get_ytdlp_options({
            'proxy': proxy_url,
            'outtmpl': output_base + '.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',  # 192kbps is fine for streaming, saves bandwidth
            }],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'ignoreerrors': False,  # We want errors to surface for retry logic
        })

        try:
            self.logger.info(f"[Residential] Downloading via proxy: {track.title}")

            def do_download() -> Dict[str, Any]:
                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                    return ydl.extract_info(track.url, download=True)  # type: ignore[return-value]

            await asyncio.wait_for(
                asyncio.to_thread(do_download),
                timeout=timeout
            )

            # Find the downloaded file (should be .mp3 after postprocessing)
            cached_path = self._find_residential_file(track.video_id)
            if cached_path:
                file_size = os.path.getsize(cached_path)
                cost_per_gb = getattr(config, 'RESIDENTIAL_PROXY_COST_PER_GB', 4.0)
                cost = (file_size / (1024 ** 3)) * cost_per_gb
                self.logger.info(
                    f"[Residential] Downloaded '{track.title}' - "
                    f"{file_size / (1024*1024):.2f} MB (~${cost:.4f})"
                )
                return True, None, file_size, cached_path

            return False, "Download completed but file not found", 0, None

        except asyncio.TimeoutError:
            self.logger.warning(f"[Residential] Timeout downloading: {track.title}")
            return False, f"Download timed out after {timeout}s", 0, None
        except Exception as e:
            error_msg = str(e)
            # Check for 403 in proxy download too
            if '403' in error_msg:
                self.logger.warning(f"[Residential] 403 error even via proxy: {track.title}")
            else:
                self.logger.error(f"[Residential] Error downloading {track.title}: {e}")
            return False, error_msg, 0, None

    def get_residential_stats(self) -> Dict[str, Any]:
        """Returns residential cache statistics.

        Returns:
            Dict with file count, total size, and estimated cost.
        """
        file_count = 0
        total_size = 0

        if os.path.exists(self.residential_path):
            for filename in os.listdir(self.residential_path):
                file_path = os.path.join(self.residential_path, filename)
                if os.path.isfile(file_path):
                    file_count += 1
                    total_size += os.path.getsize(file_path)

        import config
        cost_per_gb = getattr(config, 'RESIDENTIAL_PROXY_COST_PER_GB', 4.0)
        estimated_cost = (total_size / (1024 ** 3)) * cost_per_gb

        return {
            'file_count': file_count,
            'size_mb': total_size / (1024 * 1024),
            'estimated_cost': estimated_cost
        }

    # =========================================================================
    # SHUTDOWN
    # =========================================================================

    async def shutdown(self) -> None:
        """Gracefully shuts down background tasks."""
        self._shutdown = True

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
            try:
                await self._download_task
            except asyncio.CancelledError:
                pass

        self.logger.info("[CacheManager] Shutdown complete")

"""Music cache management for ambient playlists.

Provides proactive caching system that:
- Refreshes ALL playlists from ambience.toml on startup and every 24 hours
- Downloads tracks in background
- Manages orphaned tracks with 90-day TTL
- Handles residential proxy fallback for blocked tracks

File Structure:
    cache/music/
        playlist.json      # YouTube metadata for all playlists
        manifest.json      # Download registry + orphan tracking
        orphaned/          # 90-day holding area for removed tracks
            <video_id>.mp3
        playlists/
            <playlist_hash>/  # 12-char MD5 of playlist URL
                <video_id>.mp3
        residential/       # Permanent cache for proxy-downloaded tracks
            <video_id>.mp3
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .music_data import Track

from .music_data import Track
from .music_helpers import (
    MUTAGEN_AVAILABLE,
    YTDLP_AVAILABLE,
    download_track_as_mp3,
    extract_video_id,
    fetch_playlist_metadata,
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
    - Background downloading of tracks
    - Orphan management with 90-day TTL
    - Cache miss handling with immediate refresh

    File schemas:
        playlist.json: YouTube metadata for all tracked playlists
        manifest.json: Download state and orphan tracking
    """

    PLAYLIST_SCHEMA_VERSION = 1
    MANIFEST_SCHEMA_VERSION = 1
    ORPHAN_TTL_DAYS = 90
    REFRESH_INTERVAL_HOURS = 24

    def __init__(self, cache_root: str, logger: logging.Logger):
        """Initialize the music cache manager.

        Args:
            cache_root: Path to cache/music/ directory.
            logger: Logger instance for messages.
        """
        self.cache_root = cache_root
        self.logger = logger

        # Paths
        self.playlists_path = os.path.join(cache_root, "playlists")
        self.orphaned_path = os.path.join(cache_root, "orphaned")
        self.residential_path = os.path.join(cache_root, "residential")
        self.playlist_file = os.path.join(cache_root, "playlist.json")
        self.manifest_file = os.path.join(cache_root, "manifest.json")

        # In-memory caches (lazy-loaded)
        self._playlist_cache: Optional[Dict[str, Any]] = None
        self._manifest: Optional[Dict[str, Any]] = None

        # Background tasks
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._download_task: Optional[asyncio.Task[None]] = None
        self._download_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # Lock for manifest writes
        self._manifest_lock = asyncio.Lock()

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
        self._load_playlist_cache()
        self._load_manifest()
        self.logger.info("[CacheManager] Initialized")

    def _ensure_directories(self) -> None:
        """Creates cache directories if they don't exist."""
        os.makedirs(self.cache_root, exist_ok=True)
        os.makedirs(self.playlists_path, exist_ok=True)
        os.makedirs(self.orphaned_path, exist_ok=True)
        os.makedirs(self.residential_path, exist_ok=True)

    # =========================================================================
    # PLAYLIST.JSON - YouTube Metadata
    # =========================================================================

    def _load_playlist_cache(self) -> Dict[str, Any]:
        """Loads playlist.json or returns empty structure."""
        if self._playlist_cache is not None:
            return self._playlist_cache

        if os.path.exists(self.playlist_file):
            try:
                with open(self.playlist_file, 'r', encoding='utf-8') as f:
                    self._playlist_cache = json.load(f)
                    if self._playlist_cache is not None:
                        return self._playlist_cache
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"[CacheManager] playlist.json corrupted: {e}")

        self._playlist_cache = {
            'version': self.PLAYLIST_SCHEMA_VERSION,
            'last_refresh': 0,
            'playlists': {}
        }
        return self._playlist_cache

    def _save_playlist_cache(self) -> None:
        """Saves playlist.json to disk."""
        if self._playlist_cache is None:
            return
        try:
            # Write to temp file first, then rename (atomic on most systems)
            temp_file = self.playlist_file + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._playlist_cache, f, indent=2, ensure_ascii=False)
            shutil.move(temp_file, self.playlist_file)
        except IOError as e:
            self.logger.error(f"[CacheManager] Failed to save playlist.json: {e}")

    # =========================================================================
    # MANIFEST.JSON - Download Registry
    # =========================================================================

    def _load_manifest(self) -> Dict[str, Any]:
        """Loads manifest.json or returns empty structure."""
        if self._manifest is not None:
            return self._manifest

        if os.path.exists(self.manifest_file):
            try:
                with open(self.manifest_file, 'r', encoding='utf-8') as f:
                    self._manifest = json.load(f)
                    if self._manifest is not None:
                        return self._manifest
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"[CacheManager] manifest.json corrupted: {e}")

        self._manifest = {
            'version': self.MANIFEST_SCHEMA_VERSION,
            'files': {},
            'orphaned': {}
        }
        return self._manifest

    async def _save_manifest(self) -> None:
        """Saves manifest.json to disk (with lock)."""
        async with self._manifest_lock:
            if self._manifest is None:
                return
            try:
                temp_file = self.manifest_file + '.tmp'
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self._manifest, f, indent=2, ensure_ascii=False)
                shutil.move(temp_file, self.manifest_file)
            except IOError as e:
                self.logger.error(f"[CacheManager] Failed to save manifest.json: {e}")

    def _save_manifest_sync(self) -> None:
        """Synchronous manifest save (for use in non-async contexts)."""
        if self._manifest is None:
            return
        try:
            temp_file = self.manifest_file + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._manifest, f, indent=2, ensure_ascii=False)
            shutil.move(temp_file, self.manifest_file)
        except IOError as e:
            self.logger.error(f"[CacheManager] Failed to save manifest.json: {e}")

    # =========================================================================
    # URL UTILITIES
    # =========================================================================

    @staticmethod
    def _url_to_hash(url: str) -> str:
        """Returns 12-char MD5 hash of URL for folder naming.

        Args:
            url: Playlist URL.

        Returns:
            12-character hex string.
        """
        return hashlib.md5(url.encode()).hexdigest()[:12]  # noqa: S324

    def _get_playlist_folder(self, playlist_url: str) -> str:
        """Gets the folder path for a playlist.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            Absolute path to the playlist's cache folder.
        """
        folder_hash = self._url_to_hash(playlist_url)
        return os.path.join(self.playlists_path, folder_hash)

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

    async def refresh_all_playlists(self) -> Dict[str, List[Track]]:
        """Fetches ALL playlist URLs from ambience.toml and updates cache.

        This hits YouTube for each playlist. Run in background after startup.

        Returns:
            Dict mapping playlist URLs to their track lists.
        """
        urls = self.get_all_playlist_urls()
        if not urls:
            self.logger.warning("[CacheManager] No playlist URLs found in ambience.toml")
            return {}

        self.logger.info(f"[CacheManager] Refreshing {len(urls)} playlists from YouTube...")

        results: Dict[str, List[Track]] = {}
        playlist_cache = self._load_playlist_cache()

        for url in urls:
            try:
                tracks = await fetch_playlist_metadata(url, self.logger)
                if tracks:
                    results[url] = tracks

                    # Update playlist.json
                    folder_hash = self._url_to_hash(url)
                    playlist_cache['playlists'][url] = {
                        'display_name': f"Playlist ({len(tracks)} tracks)",
                        'folder_hash': folder_hash,
                        'tracks': [t.to_dict() for t in tracks]
                    }
                    self.logger.debug(f"[CacheManager] Fetched {len(tracks)} tracks from {url[:50]}...")
                else:
                    self.logger.warning(f"[CacheManager] No tracks from {url[:50]}...")
            except Exception as e:
                self.logger.error(f"[CacheManager] Failed to fetch {url[:50]}: {e}")

        playlist_cache['last_refresh'] = time.time()
        self._playlist_cache = playlist_cache
        self._save_playlist_cache()

        self.logger.info(f"[CacheManager] Refresh complete: {len(results)} playlists updated")
        return results

    def get_cached_tracks(self, playlist_url: str) -> List[Track]:
        """Returns tracks from cache without hitting YouTube.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            List of Track objects, or empty list if not cached.
        """
        playlist_cache = self._load_playlist_cache()
        playlist_data = playlist_cache.get('playlists', {}).get(playlist_url)

        if not playlist_data:
            return []

        tracks = []
        for track_data in playlist_data.get('tracks', []):
            track = Track.from_dict(track_data)
            tracks.append(track)

        return tracks

    # =========================================================================
    # DOWNLOAD MANAGEMENT
    # =========================================================================

    def get_local_path(self, video_id: str, playlist_url: str) -> Optional[str]:
        """Returns local file path if track is downloaded.

        Args:
            video_id: YouTube video ID.
            playlist_url: Playlist URL to check.

        Returns:
            Absolute path to MP3 if exists, None otherwise.
        """
        folder_hash = self._url_to_hash(playlist_url)
        file_path = os.path.join(self.playlists_path, folder_hash, f"{video_id}.mp3")

        if os.path.exists(file_path):
            return file_path

        # Check orphaned folder as fallback
        orphan_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")
        if os.path.exists(orphan_path):
            return orphan_path

        return None

    def get_any_local_path(self, video_id: str) -> Optional[str]:
        """Finds any existing copy of a track across all locations.

        Args:
            video_id: YouTube video ID.

        Returns:
            Path to existing MP3 file, or None.
        """
        # Check orphaned first
        orphan_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")
        if os.path.exists(orphan_path):
            return orphan_path

        # Check all playlist folders
        manifest = self._load_manifest()
        file_info = manifest.get('files', {}).get(video_id)
        if file_info:
            for location in file_info.get('locations', []):
                file_path = os.path.join(self.playlists_path, location, f"{video_id}.mp3")
                if os.path.exists(file_path):
                    return file_path

        return None

    async def download_track(
        self,
        track: Track,
        playlist_url: str
    ) -> Optional[str]:
        """Downloads a single track to the playlist folder.

        Checks for existing copies (orphaned or other playlists) first.
        If direct download fails with 403/IP-block, retries via residential
        proxy (ambient tracks deserve resilient downloading).

        Args:
            track: Track to download.
            playlist_url: Target playlist URL.

        Returns:
            Local path on success, None on failure.
        """
        if not track.video_id:
            self.logger.warning(f"[CacheManager] Track has no video_id: {track.title}")
            return None

        folder_hash = self._url_to_hash(playlist_url)
        folder_path = os.path.join(self.playlists_path, folder_hash)
        os.makedirs(folder_path, exist_ok=True)

        target_path = os.path.join(folder_path, f"{track.video_id}.mp3")

        # Already exists in target?
        if os.path.exists(target_path):
            return target_path

        # Check for existing copy elsewhere
        existing_path = self.get_any_local_path(track.video_id)
        if existing_path:
            try:
                shutil.copy2(existing_path, target_path)
                await self._register_download(track.video_id, folder_hash)
                self.logger.info(f"[CacheManager] Copied {track.video_id} from existing location")
                return target_path
            except IOError as e:
                self.logger.warning(f"[CacheManager] Copy failed: {e}")

        # Download fresh
        if not YTDLP_AVAILABLE or not MUTAGEN_AVAILABLE:
            return None

        try:
            result = await download_track_as_mp3(
                url=track.url,
                output_dir=folder_path,
                logger=self.logger,
                custom_title=track.title,
                custom_artist=track.artist,
                embed_thumbnail=True
            )

            if result.success and result.file_path:
                # Rename to video_id.mp3
                final_path = target_path
                if result.file_path != final_path:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(result.file_path, final_path)

                await self._register_download(track.video_id, folder_hash)
                self.logger.info(f"[CacheManager] Downloaded: {track.title}")
                return final_path

            # Check if this looks like a 403/IP-block error
            error_msg = (result.error_message or '').lower()
            is_ip_block = any(ind in error_msg for ind in ['403', 'forbidden', 'sign in', 'age-restricted'])

            if is_ip_block:
                self.logger.info(f"[CacheManager] Direct download blocked, trying residential: {track.title}")
                residential_result = await self._download_track_via_residential(
                    track=track,
                    target_path=target_path,
                    folder_hash=folder_hash
                )
                if residential_result:
                    return residential_result

            self.logger.warning(f"[CacheManager] Download failed: {track.title} - {result.error_message}")
            return None

        except Exception as e:
            self.logger.error(f"[CacheManager] Download error: {e}")
            return None

    async def _download_track_via_residential(
        self,
        track: Track,
        target_path: str,
        folder_hash: str
    ) -> Optional[str]:
        """Downloads a track via residential proxy with ambient-quality settings.

        This is used as a fallback for ambient downloads when direct download
        fails due to IP blocking. Unlike download_residential() which saves to
        the residential cache at 192kbps, this saves to the proper ambient
        location with full 320kbps quality and metadata.

        Args:
            track: Track to download.
            target_path: Final destination path (playlists/<hash>/<video_id>.mp3).
            folder_hash: Playlist folder hash for manifest registration.

        Returns:
            Path to downloaded file on success, None on failure.
        """
        proxy_url = get_residential_proxy_url()
        if not proxy_url:
            self.logger.info("[CacheManager] Residential proxy not configured, cannot retry")
            return None

        if not track.video_id:
            return None

        output_dir = os.path.dirname(target_path)

        self.logger.info(f"[CacheManager] Retrying via residential proxy: {track.title}")

        # Reuse download_track_as_mp3 with proxy - same 320kbps quality, full metadata
        result = await download_track_as_mp3(
            url=track.url,
            output_dir=output_dir,
            logger=self.logger,
            custom_title=track.title,
            custom_artist=track.artist,
            embed_thumbnail=True,
            proxy=proxy_url
        )

        if not result.success:
            error_msg = (result.error_message or '').lower()
            if '403' in error_msg:
                self.logger.warning(f"[CacheManager] 403 even via residential: {track.title}")
            else:
                self.logger.warning(f"[CacheManager] Residential download failed: {result.error_message}")
            return None

        if not result.file_path:
            return None

        # Rename to video_id.mp3 (download_track_as_mp3 uses "Artist - Title.mp3" format)
        if result.file_path != target_path:
            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(result.file_path, target_path)

        # Log cost estimate
        import config
        file_size = os.path.getsize(target_path)
        cost_per_gb = getattr(config, 'RESIDENTIAL_PROXY_COST_PER_GB', 4.0)
        cost = (file_size / (1024 ** 3)) * cost_per_gb
        self.logger.info(
            f"[CacheManager] Downloaded via residential: {track.title} - "
            f"{file_size / (1024*1024):.2f} MB (~${cost:.4f})"
        )

        await self._register_download(track.video_id, folder_hash)
        return target_path

    async def _register_download(self, video_id: str, folder_hash: str) -> None:
        """Registers a downloaded file in the manifest.

        Args:
            video_id: YouTube video ID.
            folder_hash: Playlist folder hash.
        """
        manifest = self._load_manifest()

        if video_id not in manifest['files']:
            manifest['files'][video_id] = {
                'locations': [],
                'downloaded_at': time.time()
            }

        locations = manifest['files'][video_id]['locations']
        if folder_hash not in locations:
            locations.append(folder_hash)

        # Remove from orphaned if present
        if video_id in manifest.get('orphaned', {}):
            del manifest['orphaned'][video_id]

        self._manifest = manifest
        await self._save_manifest()

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

        Returns:
            Number of tracks queued.
        """
        playlist_cache = self._load_playlist_cache()
        queued = 0

        for playlist_url, data in playlist_cache.get('playlists', {}).items():
            folder_hash = data.get('folder_hash', self._url_to_hash(playlist_url))

            for track_data in data.get('tracks', []):
                video_id = track_data.get('video_id')
                if not video_id:
                    # Try to extract from URL
                    video_id = extract_video_id(track_data.get('url', ''))
                    if not video_id:
                        continue

                # Check if already downloaded for this playlist
                target_path = os.path.join(self.playlists_path, folder_hash, f"{video_id}.mp3")
                if not os.path.exists(target_path):
                    await self._download_queue.put((playlist_url, video_id))
                    queued += 1

        self.logger.info(f"[CacheManager] Queued {queued} tracks for download")
        return queued

    async def _download_worker(self) -> None:
        """Background worker that processes download queue."""
        while not self._shutdown:
            try:
                # Wait for item with timeout to allow shutdown check
                try:
                    playlist_url, video_id = await asyncio.wait_for(
                        self._download_queue.get(),
                        timeout=5.0
                    )
                except TimeoutError:
                    continue

                # Find track data
                playlist_cache = self._load_playlist_cache()
                playlist_data = playlist_cache.get('playlists', {}).get(playlist_url)
                if not playlist_data:
                    continue

                track_data = None
                for t in playlist_data.get('tracks', []):
                    tid = t.get('video_id') or extract_video_id(t.get('url', ''))
                    if tid == video_id:
                        track_data = t
                        break

                if not track_data:
                    continue

                track = Track.from_dict(track_data)
                if not track.video_id:
                    track.video_id = video_id

                await self.download_track(track, playlist_url)

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

    def reconcile_downloads(self, new_playlists: Dict[str, List[Track]]) -> None:
        """Compares manifest against new playlist data.

        Handles:
        - Tracks removed from playlists → orphan
        - Tracks that returned → unorphan
        - Tracks in multiple playlists → keep all copies

        Args:
            new_playlists: Dict of {playlist_url: [Track, ...]} from refresh.
        """
        manifest = self._load_manifest()

        # Build set of all video IDs currently in any playlist
        current_video_ids: Dict[str, Set[str]] = {}  # video_id -> set of playlist hashes
        for playlist_url, tracks in new_playlists.items():
            folder_hash = self._url_to_hash(playlist_url)
            for track in tracks:
                if track.video_id:
                    if track.video_id not in current_video_ids:
                        current_video_ids[track.video_id] = set()
                    current_video_ids[track.video_id].add(folder_hash)

        # Check each downloaded file
        files_to_orphan: List[tuple[str, str]] = []  # (video_id, from_hash)
        files_to_delete: List[str] = []  # full paths

        for video_id, file_info in list(manifest.get('files', {}).items()):
            locations = file_info.get('locations', [])
            new_locations = []

            for folder_hash in locations:
                file_path = os.path.join(self.playlists_path, folder_hash, f"{video_id}.mp3")

                # Skip if file doesn't actually exist on disk
                if not os.path.exists(file_path):
                    self.logger.debug(f"[CacheManager] File missing from manifest: {video_id} in {folder_hash}")
                    continue

                if video_id in current_video_ids and folder_hash in current_video_ids[video_id]:
                    # Still in this playlist
                    new_locations.append(folder_hash)
                else:
                    # Removed from this playlist
                    if video_id in current_video_ids:
                        # Still in another playlist - just delete this copy
                        files_to_delete.append(file_path)
                    else:
                        # Not in any playlist - orphan this copy
                        files_to_orphan.append((video_id, folder_hash))

            # Update locations
            if new_locations:
                manifest['files'][video_id]['locations'] = new_locations
            elif video_id not in current_video_ids:
                # Completely removed - will be orphaned
                pass

        # Process orphans
        for video_id, from_hash in files_to_orphan:
            self._orphan_track(video_id, from_hash, manifest)

        # Delete extra copies
        for file_path in files_to_delete:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self.logger.debug(f"[CacheManager] Deleted extra copy: {file_path}")
            except IOError as e:
                self.logger.warning(f"[CacheManager] Could not delete {file_path}: {e}")

        # Check for tracks that returned from orphaned state
        for video_id, playlist_hashes in current_video_ids.items():
            if video_id in manifest.get('orphaned', {}):
                # Track returned! Unorphan it
                for target_hash in playlist_hashes:
                    self._unorphan_track(video_id, target_hash, manifest)
                break  # Only unorphan once, copies will be made as needed

        self._manifest = manifest
        self._save_manifest_sync()
        self.logger.info("[CacheManager] Reconciliation complete")

    def _orphan_track(self, video_id: str, from_hash: str, manifest: Dict[str, Any]) -> None:
        """Moves a track to orphaned folder.

        Args:
            video_id: YouTube video ID.
            from_hash: Playlist folder hash to move from.
            manifest: Manifest dict (modified in place).
        """
        source_path = os.path.join(self.playlists_path, from_hash, f"{video_id}.mp3")
        target_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")

        if not os.path.exists(source_path):
            return

        try:
            # Move file
            if os.path.exists(target_path):
                os.remove(source_path)  # Already orphaned, just delete
            else:
                shutil.move(source_path, target_path)

            # Update manifest
            if video_id in manifest.get('files', {}):
                del manifest['files'][video_id]

            manifest.setdefault('orphaned', {})[video_id] = {
                'orphaned_at': time.time(),
                'original_playlist': from_hash
            }

            self.logger.info(f"[CacheManager] Orphaned: {video_id}")

        except IOError as e:
            self.logger.warning(f"[CacheManager] Could not orphan {video_id}: {e}")

    def _unorphan_track(self, video_id: str, to_hash: str, manifest: Dict[str, Any]) -> None:
        """Moves a track from orphaned back to a playlist folder.

        Args:
            video_id: YouTube video ID.
            to_hash: Target playlist folder hash.
            manifest: Manifest dict (modified in place).
        """
        source_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")
        target_folder = os.path.join(self.playlists_path, to_hash)
        target_path = os.path.join(target_folder, f"{video_id}.mp3")

        if not os.path.exists(source_path):
            return

        try:
            os.makedirs(target_folder, exist_ok=True)
            shutil.copy2(source_path, target_path)
            os.remove(source_path)

            # Update manifest
            if video_id in manifest.get('orphaned', {}):
                del manifest['orphaned'][video_id]

            manifest.setdefault('files', {})[video_id] = {
                'locations': [to_hash],
                'downloaded_at': time.time()
            }

            self.logger.info(f"[CacheManager] Unorphaned: {video_id}")

        except IOError as e:
            self.logger.warning(f"[CacheManager] Could not unorphan {video_id}: {e}")

    async def cleanup_expired_orphans(self) -> int:
        """Deletes orphaned files older than 90 days.

        Returns:
            Number of files deleted.
        """
        manifest = self._load_manifest()
        now = time.time()
        ttl_seconds = self.ORPHAN_TTL_DAYS * 24 * 3600
        deleted = 0

        for video_id, orphan_info in list(manifest.get('orphaned', {}).items()):
            orphaned_at = orphan_info.get('orphaned_at', 0)
            if now - orphaned_at > ttl_seconds:
                file_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    del manifest['orphaned'][video_id]
                    deleted += 1
                    self.logger.debug(f"[CacheManager] Expired orphan deleted: {video_id}")
                except IOError as e:
                    self.logger.warning(f"[CacheManager] Could not delete orphan {video_id}: {e}")

        if deleted > 0:
            self._manifest = manifest
            await self._save_manifest()
            self.logger.info(f"[CacheManager] Cleaned up {deleted} expired orphans")

        return deleted

    def clear_orphaned(self) -> int:
        """Manually clears all orphaned files.

        Returns:
            Number of files deleted.
        """
        manifest = self._load_manifest()
        deleted = 0

        for video_id in list(manifest.get('orphaned', {}).keys()):
            file_path = os.path.join(self.orphaned_path, f"{video_id}.mp3")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                del manifest['orphaned'][video_id]
                deleted += 1
            except IOError as e:
                self.logger.warning(f"[CacheManager] Could not delete orphan {video_id}: {e}")

        self._manifest = manifest
        self._save_manifest_sync()
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
                playlists = await self.refresh_all_playlists()
                self.reconcile_downloads(playlists)
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
            playlist_cache = self._load_playlist_cache()
            folder_hash = self._url_to_hash(playlist_url)
            playlist_cache['playlists'][playlist_url] = {
                'display_name': f"Playlist ({len(tracks)} tracks)",
                'folder_hash': folder_hash,
                'tracks': [t.to_dict() for t in tracks]
            }
            playlist_cache['last_refresh'] = time.time()
            self._playlist_cache = playlist_cache
            self._save_playlist_cache()

        # Restart timer
        self.start_refresh_timer()

        return tracks

    # =========================================================================
    # STATISTICS
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Returns cache statistics.

        Returns:
            Dict with cache statistics.
        """
        playlist_cache = self._load_playlist_cache()

        # Count files and calculate size
        total_size = 0
        downloaded_count = 0

        for folder_hash in os.listdir(self.playlists_path) if os.path.exists(self.playlists_path) else []:
            folder_path = os.path.join(self.playlists_path, folder_hash)
            if os.path.isdir(folder_path):
                for filename in os.listdir(folder_path):
                    if filename.endswith('.mp3'):
                        downloaded_count += 1
                        total_size += os.path.getsize(os.path.join(folder_path, filename))

        # Count orphaned
        orphaned_count = 0
        orphaned_size = 0
        if os.path.exists(self.orphaned_path):
            for filename in os.listdir(self.orphaned_path):
                if filename.endswith('.mp3'):
                    orphaned_count += 1
                    orphaned_size += os.path.getsize(os.path.join(self.orphaned_path, filename))

        # Count total tracks across all playlists
        total_tracks = 0
        for data in playlist_cache.get('playlists', {}).values():
            total_tracks += len(data.get('tracks', []))

        last_refresh = playlist_cache.get('last_refresh', 0)

        return {
            'total_playlists': len(playlist_cache.get('playlists', {})),
            'total_tracks': total_tracks,
            'downloaded_tracks': downloaded_count,
            'orphaned_tracks': orphaned_count,
            'size_mb': total_size / (1024 * 1024),
            'orphaned_size_mb': orphaned_size / (1024 * 1024),
            'last_refresh': last_refresh,
            'last_refresh_ago': time.time() - last_refresh if last_refresh else None
        }

    # =========================================================================
    # RESIDENTIAL PROXY CACHE
    # =========================================================================
    # Tracks downloaded via residential proxy are cached here permanently.
    # NO TTL - these files cost real money ($4/GB) and if a track needs
    # residential proxy once, it will likely need it forever (datacenter IP blocked).
    # Only manual clear should remove these files.

    def get_residential_path(self, video_id: str) -> str:
        """Get the cache path for a residentially-downloaded track.

        Args:
            video_id: YouTube video ID.

        Returns:
            Absolute path to the MP3 file location.
        """
        return os.path.join(self.residential_path, f"{video_id}.mp3")

    def check_residential(self, video_id: str) -> Optional[str]:
        """Check if a track exists in the residential cache.

        Args:
            video_id: YouTube video ID.

        Returns:
            Path to cached MP3 file if exists, None otherwise.
        """
        # Check for MP3 (our target format)
        mp3_path = os.path.join(self.residential_path, f"{video_id}.mp3")
        if os.path.exists(mp3_path):
            return mp3_path

        # Also check legacy formats in case old files exist
        for ext in ['.webm', '.opus', '.m4a', '.ogg']:
            path = os.path.join(self.residential_path, f"{video_id}{ext}")
            if os.path.exists(path):
                return path
        return None

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
                    return ydl.extract_info(track.url, download=True)  # type: ignore

            await asyncio.wait_for(
                asyncio.to_thread(do_download),
                timeout=timeout
            )

            # Find the downloaded file (should be .mp3 after postprocessing)
            cached_path = self.check_residential(track.video_id)
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

"""cogs/music.py

This cog implements an ambient music presence system. The bot appears to be
"listening" to music via its Discord status, and users can request the bot
to join a voice channel and play the actual audio.

Key Features:
- Ambient Presence: The bot cycles through a playlist in its status, simulating
  listening to music even when not in a voice channel.
- Listen Along: Users can trigger the bot to join their VC and play the current
  track, continuing through the playlist.
- Global Session: The bot can only be in one voice channel at a time across all
  guilds. Other guilds are notified if the bot is busy.
- Player Controls: Skip, view queue, toggle shuffle, see now playing.
- Idle Timeout: If no one joins the VC within 5 minutes, the bot leaves.

Dependencies:
- yt-dlp: For extracting audio URLs from YouTube.
- PyNaCl: For Discord voice encryption.
- FFmpeg: System binary for audio transcoding (must be in PATH or bundled).
"""

import asyncio
import logging
import os
import random
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord
from discord.ext import commands

import config
from utils import ambience
from utils.ambience import (
    MusicAmbience,
    ensure_music_for_user,
    get_context_value,
    get_current_activity,
    get_current_playlist,
    maybe_cycle,
    start_music,
    subscribe_playlist_change,
    unsubscribe_playlist_change,
)
from utils.base_cog import BaseCog
from utils.database import DatabaseManager
from utils.musicutils import (
    YTDLP_AVAILABLE,
    ActiveSession,
    AmbienceState,
    AudioFetcher,
    AudioFetchResult,
    FetchContext,
    LoopMode,
    ManagedPlayer,
    MusicCacheManager,
    MusicCommandsMixin,
    PlaybackState,
    SearchResult,
    Track,
    fetch_playlist_metadata,
    fetch_url_info,
    get_thumbnail_bytes,
    search_youtube,
    search_query_mode,
    search_url_mode,
)
from utils.views import (
    TrackFailureAction,
    show_track_failed,
)

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


class Music(MusicCommandsMixin, BaseCog):
    """A cog for ambient music presence and voice playback."""

    def __init__(self, bot: 'CoreBot'):
        """Initializes the Music cog.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager

        # Playlist state
        self.playlist: List[Track] = []
        self.current_index: int = 0
        self.loop_mode: LoopMode = LoopMode.ALL

        # Presence cycling state (idle mode)
        self.track_started_at: float = time.time()
        self.presence_task: Optional[asyncio.Task[None]] = None

        # Voice session state
        self.active_session: Optional[ActiveSession] = None
        self.playback_task: Optional[asyncio.Task[None]] = None
        self.idle_timeout_task: Optional[asyncio.Task[None]] = None

        # Grouped mutable state (see music_helpers.py for dataclass definitions)
        self._playback = PlaybackState()
        self._ambience = AmbienceState()

        # Phase 12: New prefetch buffer (cog owns storage, AudioFetcher owns strategy)
        self._next_prepared: Optional[AudioFetchResult] = None
        self._next_prepared_track: Optional[Track] = None  # Track this prefetch is for
        self._prefetch_task: Optional[asyncio.Task[None]] = None  # Background prefetch task

        # Managed audio player - owns playback state, eliminates callback dance
        # Created when session starts, destroyed when session ends
        self._player: Optional[ManagedPlayer] = None

        # Tracks if the playlist was modified during a voice session.
        # Set True when tracks are added/removed. Used to decide whether to
        # reset to the cached playlist when returning to idle.
        self._playlist_modified_during_session: bool = False

        # Cache paths
        self.cache_path = config.MUSIC_CACHE_PATH

        # Music cache manager for proactive caching
        self.cache_manager = MusicCacheManager(
            self.cache_path,
            self.logger
        )

        # Audio fetcher for retry orchestration (replaces self._retry)
        self._audio_fetcher = AudioFetcher(
            self.cache_manager,
            self.logger,
            on_residential_attempt=self._on_residential_attempt
        )

        # Track if we've notified the user about residential proxy for the current track
        self._residential_notified_this_track: bool = False

        # PO Token Provider server subprocess (started in cog_ready)
        self._pot_server_process: Optional[asyncio.subprocess.Process] = None
        self._pot_server_healthy: bool = False

    async def _on_residential_attempt(self, track_title: str) -> None:
        """Callback fired BEFORE residential proxy attempt starts.

        Notifies the user that we're having trouble and trying an alternative,
        BEFORE the alternative method succeeds or fails.

        Args:
            track_title: Title of the track being fetched.
        """
        if not self._residential_notified_this_track:
            self._residential_notified_this_track = True
            await self._send_system_message(
                f"🔄 Hmm, having some trouble with **{track_title}**... "
                f"Let me try another way!"
            )

    def _on_playlist_change(self, playlist_url: Optional[str], description: Optional[str]) -> None:
        """Callback from ambience system when playlist should change.

        This is called by the ambience system, not from within an async context,
        so we just set a flag for the presence loop to handle.

        Args:
            playlist_url: New playlist URL, or None to stop.
            description: Mood description.
        """
        if playlist_url is None:
            # Ambience wants us to stop
            self._ambience.request_switch(None)
            self.logger.info("Ambience requested music stop")
        elif playlist_url != self._ambience.current_playlist_url:
            # Ambience wants a different playlist
            self._ambience.request_switch(playlist_url)
            self.logger.info(
                f"Ambience requested playlist change: {description}")

    def _ensure_music_for_user(self) -> tuple[Optional[str], Optional[str]]:
        """Ask ambience to start music if not already playing."""
        return ensure_music_for_user()

    def _get_listen_along_response(self) -> str:
        """Get personality-aware response for listen-along."""
        return MusicAmbience.get_listen_along_response()

    def _get_presence_for_activity(self) -> str:
        """Get a presence string for the current foreground activity.

        In the new ambience system, activities have their status built-in.
        We just need to potentially inject dynamic context.

        Returns:
            A string suitable for Discord presence.
        """
        activity = get_current_activity()
        if activity is None:
            return "vibing ✨"

        # Get base status from activity
        status = activity.status

        # If activity has dynamic context, try to get it
        context = get_context_value()
        if context and activity.context_key:
            # Some activities can have context injected into their status
            # E.g., "reading 📚" could become "reading The House in the Cerulean Sea 📚"
            # For now, we just use the base status - context is available for
            # cog-specific behaviors
            pass

        return status

    async def _start_pot_server(self) -> bool:
        """Start the PO Token Provider HTTP server if available.

        The server generates proof-of-origin tokens for YouTube requests,
        helping bypass 403 errors on datacenter IPs.

        Returns:
            True if server started successfully, False otherwise.
        """
        pot_script = getattr(config, 'POT_PROVIDER_PATH', None)
        pot_port = getattr(config, 'POT_PROVIDER_PORT', None)

        # If port not configured, POT system is disabled
        if pot_port is None:
            self.logger.debug("POT_PROVIDER_PORT not configured, skipping POT server")
            return False

        if not pot_script or not os.path.isfile(pot_script):
            self.logger.debug(f"POT provider script not found at {pot_script}")
            return False

        # Check if node is available
        import shutil
        if not shutil.which('node'):
            self.logger.warning("Node.js not found in PATH, cannot start POT server")
            return False

        try:
            self.logger.info(f"Starting POT provider server on port {pot_port}...")

            # Start the Node.js server
            self._pot_server_process = await asyncio.create_subprocess_exec(
                'node', pot_script, '--port', str(pot_port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                # Don't let it inherit our stdin
                stdin=asyncio.subprocess.DEVNULL,
            )

            # Wait for server to become healthy with retries
            # Node.js server can take 2-5 seconds to fully initialize
            max_wait = 10.0  # Total max wait time
            check_interval = 0.5  # Check every 500ms
            elapsed = 0.0

            while elapsed < max_wait:
                await asyncio.sleep(check_interval)
                elapsed += check_interval

                # Check if process died
                if self._pot_server_process.returncode is not None:
                    stderr_data = await self._pot_server_process.stderr.read() if self._pot_server_process.stderr else b''
                    stderr_text = stderr_data.decode('utf-8', errors='replace')[:500]
                    self.logger.error(f"POT server failed to start: {stderr_text}")
                    self._pot_server_process = None
                    return False

                # Check if healthy
                if await self._check_pot_server_health():
                    self._pot_server_healthy = True
                    self.logger.info(f"POT provider server started successfully (PID: {self._pot_server_process.pid}, took {elapsed:.1f}s)")
                    return True

            # Timed out waiting for health
            self.logger.warning(f"POT server started but not responding after {max_wait}s")
            return False

        except Exception as e:
            self.logger.error(f"Failed to start POT server: {e}", exc_info=True)
            return False

    async def _check_pot_server_health(self) -> bool:
        """Check if the POT server is responding.

        Returns:
            True if server is healthy, False otherwise.
        """
        pot_port = getattr(config, 'POT_PROVIDER_PORT', None)
        if pot_port is None:
            return False

        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
                async with session.get(f'http://127.0.0.1:{pot_port}/ping') as resp:
                    return resp.status == 200
        except Exception as e:
            self.logger.debug(f"POT server health check failed: {e}")
            return False

    async def _stop_pot_server(self) -> None:
        """Stop the PO Token Provider server if running."""
        if self._pot_server_process is None:
            return

        try:
            self.logger.info("Stopping POT provider server...")

            # Try graceful termination first
            self._pot_server_process.terminate()

            try:
                await asyncio.wait_for(self._pot_server_process.wait(), timeout=5.0)
                self.logger.info("POT server stopped gracefully")
            except asyncio.TimeoutError:
                # Force kill
                self._pot_server_process.kill()
                await self._pot_server_process.wait()
                self.logger.warning("POT server force-killed after timeout")

        except Exception as e:
            self.logger.error(f"Error stopping POT server: {e}")
        finally:
            self._pot_server_process = None
            self._pot_server_healthy = False

    async def cog_ready(self) -> None:
        """Called after the bot is fully ready. Sets up ambience subscription and loads playlist."""
        if not YTDLP_AVAILABLE:
            self.logger.warning(
                "yt-dlp is not installed. Music cog will be limited.")
            return

        # Start POT provider server (for YouTube 403 bypass)
        await self._start_pot_server()

        # Ensure cache directory exists
        os.makedirs(self.cache_path, exist_ok=True)

        # Initialize cache manager (loads cached data, no YouTube hit)
        await self.cache_manager.initialize()

        # Initialize ambience state (picks random mood/activity)
        ambience.initialize()

        # Subscribe to ambience playlist changes
        subscribe_playlist_change(self._on_playlist_change)
        self.logger.info("Subscribed to ambience playlist changes")

        # Tell ambience to start music (it will pick a mood/playlist)
        playlist_url, description = start_music()

        if not playlist_url:
            self.logger.info(
                "No playlists configured in ambience.toml. Music cog idle (can still play user requests).")
            # Start presence loop anyway - it will handle the no-playlist case
            # and react when ambience eventually has a playlist
            self.presence_task = self.bot.loop.create_task(
                self._presence_loop())
            # Still start background refresh for when playlists are added
            self._start_cache_background_tasks()
            return

        self._ambience.confirm_switch(playlist_url)
        self.logger.info(
            f"Ambience selected playlist ({description}): {playlist_url}")

        # Load playlist from cache (no YouTube hit, quick startup)
        await self._load_playlist()

        if self.playlist:
            # Start presence cycling
            self.presence_task = self.bot.loop.create_task(
                self._presence_loop())
            self.logger.info(
                f"Music cog ready with {len(self.playlist)} tracks.")
        else:
            self.logger.warning(
                "No tracks loaded. Music cog will not cycle presence.")
            # Start loop anyway to handle future playlist changes
            self.presence_task = self.bot.loop.create_task(
                self._presence_loop())

        # Start background cache refresh (hits YouTube, downloads, etc.)
        self._start_cache_background_tasks()

    def _start_cache_background_tasks(self) -> None:
        """Starts background cache tasks without blocking startup."""
        self._cache_init_task: Optional[asyncio.Task[None]] = asyncio.create_task(self._background_cache_init())

    async def _background_cache_init(self) -> None:
        """Background task to refresh playlists and start downloads.

        This runs after startup so the bot is responsive immediately.
        """
        try:
            # Give the bot a moment to fully start
            await asyncio.sleep(2.0)

            self.logger.info("[Cache] Starting background playlist refresh...")

            # Refresh all playlists from YouTube
            playlists = await self.cache_manager.refresh_all_playlists()

            if playlists:
                # Reconcile downloads (handle orphans)
                await self.cache_manager.reconcile_downloads(playlists)

                # Cleanup expired orphans
                await self.cache_manager.cleanup_expired_orphans()

                # Queue missing downloads
                await self.cache_manager.queue_missing_downloads()

                # Start background download worker
                await self.cache_manager.start_background_downloads()

            # Start the 24-hour refresh timer
            self.cache_manager.start_refresh_timer()

            self.logger.info("[Cache] Background initialization complete")

        except Exception as e:
            self.logger.error(f"[Cache] Background init error: {e}", exc_info=True)

    async def cog_unload(self) -> None:
        """Cleanup when cog is unloaded."""
        # Unsubscribe from ambience
        unsubscribe_playlist_change(self._on_playlist_change)

        # Stop POT provider server
        await self._stop_pot_server()

        # Shutdown cache manager
        await self.cache_manager.shutdown()

        # Cancel presence task
        if self.presence_task:
            self.presence_task.cancel()
            try:
                await self.presence_task
            except asyncio.CancelledError:
                pass

        # Cancel playback task
        if self.playback_task:
            self.playback_task.cancel()
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass

        # Cancel idle timeout task
        if self.idle_timeout_task:
            self.idle_timeout_task.cancel()
            try:
                await self.idle_timeout_task
            except asyncio.CancelledError:
                pass

        # Cancel prefetch task
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
            except asyncio.CancelledError:
                pass

        # Disconnect from voice if connected
        if self.active_session and self.active_session.voice_client:
            await self.active_session.voice_client.disconnect()
            self.active_session = None

        # Clear presence (respects visibility setting)
        await self.bot.change_presence_safe(activity=None)
        self.logger.info("Music cog unloaded.")

    # ==========================================================================
    # PLAYLIST MANAGEMENT
    # ==========================================================================

    async def _load_playlist(self) -> None:
        """Loads playlist from cache manager."""
        playlist_url = self._ambience.current_playlist_url or get_current_playlist()

        if not playlist_url:
            self.logger.info("No playlist URL available to load")
            return

        # Load from cache manager (no YouTube hit)
        tracks = self.cache_manager.get_cached_tracks(playlist_url)

        if tracks:
            self.playlist = tracks
            self.logger.info(f"Loaded {len(tracks)} tracks from cache.")
        else:
            # Cache miss - trigger immediate refresh
            self.logger.info("Cache miss, fetching from YouTube...")
            tracks = await self.cache_manager.handle_cache_miss(playlist_url)
            if tracks:
                self.playlist = tracks
                self.logger.info(f"Fetched {len(tracks)} tracks from YouTube.")

        if self.playlist:
            # Shuffle for initial playback
            self._apply_shuffle()
            self._dedupe_playlist()

    async def _fetch_playlist(self) -> None:
        """Fetches playlist metadata from YouTube using yt-dlp.

        Note: This is now primarily used for fallback. The cache manager
        handles most playlist fetching via refresh_all_playlists().
        """
        playlist_url = self._ambience.current_playlist_url or get_current_playlist()
        if not playlist_url or not YTDLP_AVAILABLE:
            return

        self.logger.info(f"Fetching playlist from YouTube: {playlist_url}")

        tracks = await fetch_playlist_metadata(playlist_url, self.logger)

        if tracks:
            self.playlist = tracks
            self.logger.info(f"Fetched {len(tracks)} tracks from playlist.")
        else:
            self.logger.error("Failed to fetch playlist or playlist is empty.")

    def _apply_shuffle(self, preserve_current: bool = True) -> None:
        """Shuffles the playlist.

        Places the current track at index 0, then shuffles all other tracks
        after it. This ensures the currently playing song isn't interrupted.

        Args:
            preserve_current: If True (default), keeps the current track at
                index 0. If False, shuffles entire playlist and resets to 0.
        """
        if not self.playlist:
            return

        current = self._get_current_track() if preserve_current else None

        if current and preserve_current:
            # Remove current track, shuffle the rest, put current at front
            remaining = [t for t in self.playlist if t is not current]
            random.shuffle(remaining)
            self.playlist = [current] + remaining
            self.current_index = 0
        else:
            # Full shuffle, reset to beginning
            random.shuffle(self.playlist)
            self.current_index = 0

        # Re-prefetch if next track changed
        self._refresh_prefetch_if_stale()

    def _get_current_track(self) -> Optional[Track]:
        """Gets the current track."""
        if not self.playlist:
            return None
        return self.playlist[self.current_index % len(self.playlist)]

    def _get_elapsed_seconds(self) -> float:
        """Gets the current elapsed time in the track, accounting for pause state.

        When paused, returns the position where we paused.
        When playing, calculates from track_started_at.

        Returns:
            Elapsed seconds into the current track.
        """
        if self._playback.paused_at_position is not None:
            return self._playback.paused_at_position
        return time.time() - self.track_started_at

    async def _send_system_message(
        self,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        view: Optional[discord.ui.View] = None,
        files: Optional[List[discord.File]] = None,
    ) -> Optional[discord.Message]:
        """Send a system message to the origin channel (and voice channel if different).

        System messages are notifications that don't result from direct user commands,
        like "track unavailable", "trying alternate method", "disconnecting", etc.

        Messages go to:
        1. Origin channel (where the session was started) - primary
        2. Voice channel (if different from origin) - secondary, for users in VC

        Args:
            content: Text content to send.
            embed: Optional embed to include.
            view: Optional view (buttons, etc.) to include.
            files: Optional files to attach.

        Returns:
            The message sent to the origin channel, or None if send failed.
        """
        if not self.active_session:
            return None

        sent_to: set[int] = set()
        result_message: Optional[discord.Message] = None

        # Build kwargs dynamically (avoid passing None to discord.py)
        kwargs: dict = {}
        if content is not None:
            kwargs['content'] = content
        if embed is not None:
            kwargs['embed'] = embed
        if view is not None:
            kwargs['view'] = view
        if files is not None:
            kwargs['files'] = files

        # Send to origin channel first (where users expect bot messages)
        origin = self.bot.get_channel(self.active_session.origin_channel_id)
        if origin and isinstance(origin, discord.abc.Messageable):
            try:
                result_message = await origin.send(**kwargs)
                sent_to.add(self.active_session.origin_channel_id)
            except Exception as e:
                self.logger.warning(f"Failed to send to origin channel: {e}")

        # Also send to voice channel if different (for people watching VC text)
        if self.active_session.channel_id not in sent_to:
            vc_channel = self.bot.get_channel(self.active_session.channel_id)
            if vc_channel and isinstance(vc_channel, discord.abc.Messageable):
                try:
                    # Don't re-send files (can't reuse discord.File objects)
                    vc_kwargs = {k: v for k, v in kwargs.items() if k != 'files'}
                    await vc_channel.send(**vc_kwargs)
                except Exception as e:
                    self.logger.debug(f"Failed to send to voice channel: {e}")

        return result_message

    def _get_next_track(self) -> Optional[Track]:
        """Gets the next track without advancing the index.

        Returns:
            The next track, or None if playlist is empty or at end with loop off.
        """
        if not self.playlist:
            return None

        # Loop ONE: next track is current track
        if self.loop_mode == LoopMode.ONE:
            return self._get_current_track()

        next_index = self.current_index + 1

        # Check if we'd reach the end
        if next_index >= len(self.playlist):
            if self.loop_mode == LoopMode.ALL:
                return self.playlist[0]  # Would wrap to start
            else:
                return None  # Loop OFF, no next track

        return self.playlist[next_index]

    def _get_next_sequential_track(self) -> Optional[Track]:
        """Gets the next sequential track for prefetch purposes.

        Unlike _get_next_track(), this ignores loop mode and always returns
        the next track in sequence. This is used for prefetch, which should
        always prepare the next sequential track regardless of loop mode:
        - If loop mode changes, the prefetch is already ready
        - Avoids wasteful re-fetching of the currently playing track

        Always wraps at playlist end (prefetch benefits from having first
        track ready even with loop OFF, in case user enables loop).

        Returns:
            The next sequential track, or None if playlist is empty.
        """
        if not self.playlist:
            return None

        next_index = (self.current_index + 1) % len(self.playlist)
        return self.playlist[next_index]

    def _advance_track(self) -> Optional[Track]:
        """Advances to the next track, handling loop modes.

        Loop modes:
        - OFF: Stop at end of playlist
        - ONE: Repeat current track
        - ALL: Loop entire playlist
        """
        if not self.playlist:
            return None

        # Loop ONE: stay on same track
        if self.loop_mode == LoopMode.ONE:
            self.track_started_at = time.time()
            return self._get_current_track()

        self.current_index += 1

        # Check if we've reached the end
        if self.current_index >= len(self.playlist):
            if self.loop_mode == LoopMode.ALL:
                self.current_index = 0
            else:
                # Loop OFF: stop playback
                return None

        self.track_started_at = time.time()
        self._playback.paused_at_position = None  # Clear pause state on track change
        return self._get_current_track()

    def _do_pause(self) -> bool:
        """Pauses playback via ManagedPlayer.

        Returns:
            True if pause succeeded, False if not playing.
        """
        if not self._player:
            return False

        if self._player.pause():
            # Store position for UI display
            self._playback.paused_at_position = self._player.position
            self.logger.info(f"[Playback] Paused at position: {self._playback.paused_at_position:.1f}s")
            return True
        return False

    async def _do_resume(self) -> bool:
        """Resumes playback from paused position, rewinding 1 second for smoothness.

        Uses ManagedPlayer's seek to restart at (pause_position - 1 second).

        Returns:
            True if resume succeeded, False if not paused or failed.
        """
        if not self._player or not self._player.is_paused:
            return False

        # Calculate seek position (rewind 1 second for smooth continuation)
        current_pos = self._player.position
        seek_position = max(0.0, current_pos - 1.0)

        # Seek to the rewound position (this restarts FFmpeg internally)
        self._player.seek(seek_position)

        # Resume playback
        self._player.resume()

        # Update track timing for position display
        self.track_started_at = time.time() - seek_position
        self._playback.paused_at_position = None

        self.logger.info(f"[Playback] Resumed at position: {seek_position:.1f}s")
        return True

    def _dedupe_playlist(self) -> int:
        """Removes duplicate tracks from the playlist, keeping later occurrences.

        When a track appears multiple times, keeps the one at the later position
        (i.e., duplicates are effectively "moved" to their final occurrence).
        Adjusts current_index if tracks before it are removed.

        Returns:
            The number of duplicates removed.
        """
        if not self.playlist:
            return 0

        seen_urls: dict[str, int] = {}  # url -> index of last occurrence
        indices_to_remove: list[int] = []

        # First pass: find all duplicate indices (keep the last occurrence)
        for i, track in enumerate(self.playlist):
            if track.url in seen_urls:
                # Mark the earlier occurrence for removal
                indices_to_remove.append(seen_urls[track.url])
            seen_urls[track.url] = i

        if not indices_to_remove:
            return 0

        # Sort in reverse order so we can remove without index shifting issues
        indices_to_remove.sort(reverse=True)

        # Count how many removed indices are before current_index
        adjustment = sum(1 for i in indices_to_remove if i < self.current_index)

        # Remove duplicates
        for idx in indices_to_remove:
            self.playlist.pop(idx)

        # Adjust current_index
        self.current_index = max(0, self.current_index - adjustment)

        self.logger.debug(f"Removed {len(indices_to_remove)} duplicate tracks from playlist")

        # Re-prefetch if next track changed
        self._refresh_prefetch_if_stale()

        return len(indices_to_remove)

    def _remove_track(self, index: int) -> None:
        """Removes a track from the playlist by index.

        Adjusts current_index appropriately to maintain playback position.
        Removal only affects the current session - playlist reloads from
        cache when returning to idle if modified.

        Args:
            index: The index of the track to remove.
        """
        if not self.playlist or index < 0 or index >= len(self.playlist):
            return

        removed_track = self.playlist.pop(index)
        self.logger.info(f"Removed track from playlist: {removed_track.title}")

        # Mark playlist as modified during this session
        if self.active_session:
            self._playlist_modified_during_session = True

        # Adjust current_index if needed
        if not self.playlist:
            self.current_index = 0
        elif index < self.current_index:
            # Removed a track before current position, shift back
            self.current_index -= 1
        elif index == self.current_index:
            # Removed current track - index now points to next track
            # Make sure we don't go past end of playlist
            if self.current_index >= len(self.playlist):
                self.current_index = 0

        # Re-prefetch if next track changed
        self._refresh_prefetch_if_stale()

    def _move_track(self, from_index: int, to_index: int) -> Optional[Track]:
        """Moves a track from one position to another in the playlist.

        Adjusts current_index appropriately to maintain playback position.

        Args:
            from_index: The current index of the track to move.
            to_index: The target index to move the track to.

        Returns:
            The moved track, or None if indices are invalid.
        """
        if not self.playlist:
            return None
        if from_index < 0 or from_index >= len(self.playlist):
            return None
        if to_index < 0 or to_index >= len(self.playlist):
            return None
        if from_index == to_index:
            return self.playlist[from_index]  # No-op but valid

        track = self.playlist.pop(from_index)
        self.playlist.insert(to_index, track)

        self.logger.info(
            f"Moved track '{track.title}' from position {from_index + 1} to {to_index + 1}")

        # Mark playlist as modified during this session
        if self.active_session:
            self._playlist_modified_during_session = True

        # Adjust current_index to maintain playback position
        if from_index == self.current_index:
            # We moved the currently playing track
            self.current_index = to_index
        elif from_index < self.current_index <= to_index:
            # Track moved from before current to after current
            self.current_index -= 1
        elif to_index <= self.current_index < from_index:
            # Track moved from after current to before current
            self.current_index += 1

        # Re-prefetch if next track changed
        self._refresh_prefetch_if_stale()

        return track

    def _swap_tracks(self, index_a: int, index_b: int) -> Optional[tuple[Track, Track]]:
        """Swaps two tracks in the playlist.

        Adjusts current_index appropriately to maintain playback position.

        Args:
            index_a: The index of the first track.
            index_b: The index of the second track.

        Returns:
            A tuple of (track_a, track_b), or None if indices are invalid.
        """
        if not self.playlist:
            return None
        if index_a < 0 or index_a >= len(self.playlist):
            return None
        if index_b < 0 or index_b >= len(self.playlist):
            return None
        if index_a == index_b:
            return (self.playlist[index_a], self.playlist[index_a])

        # Perform the swap
        self.playlist[index_a], self.playlist[index_b] = self.playlist[index_b], self.playlist[index_a]

        self.logger.info(
            f"Swapped tracks: '{self.playlist[index_a].title}' (pos {index_a + 1}) "
            f"<-> '{self.playlist[index_b].title}' (pos {index_b + 1})"
        )

        # Mark playlist as modified during this session
        if self.active_session:
            self._playlist_modified_during_session = True

        # Adjust current_index if we swapped the currently playing track
        if self.current_index == index_a:
            self.current_index = index_b
        elif self.current_index == index_b:
            self.current_index = index_a

        # Re-prefetch if next track changed
        self._refresh_prefetch_if_stale()

        return (self.playlist[index_a], self.playlist[index_b])

    def _find_track_by_query(self, query: str) -> Optional[int]:
        """Finds a track index by matching against title or artist.

        Uses fuzzy matching - returns the best match if confidence is high enough.

        Args:
            query: Search query (song title, artist, or partial match).

        Returns:
            The index of the best matching track, or None if no good match.
        """
        query_lower = query.lower().strip()
        if not query_lower or not self.playlist:
            return None

        best_match_index: Optional[int] = None
        best_score = 0.0

        for i, track in enumerate(self.playlist):
            title_lower = track.title.lower()
            artist_lower = track.artist.lower()

            # Check for exact substring match (high confidence)
            if query_lower in title_lower or query_lower in artist_lower:
                # Prefer title matches over artist matches
                if query_lower in title_lower:
                    score = len(query_lower) / len(title_lower) + 0.5
                else:
                    score = len(query_lower) / len(artist_lower) + 0.3

                if score > best_score:
                    best_score = score
                    best_match_index = i

            # Check for word overlap
            query_words = set(query_lower.split())
            title_words = set(title_lower.split())
            artist_words = set(artist_lower.split())

            title_overlap = len(query_words & title_words) / \
                max(len(query_words), 1)
            artist_overlap = len(query_words & artist_words) / \
                max(len(query_words), 1)

            overlap_score = max(title_overlap * 0.8, artist_overlap * 0.6)
            if overlap_score > best_score:
                best_score = overlap_score
                best_match_index = i

        # Require minimum confidence threshold
        if best_score >= 0.3:
            return best_match_index
        return None

    async def _restore_idle_playlist(self) -> None:
        """Restores playlist state when returning to idle mode after a voice session.

        Behavior:
        - If playlist was NOT modified during the session: Keep current position
          and order intact (the presence loop will continue from where we left off).
        - If playlist WAS modified (tracks added/removed): Reload the cached/original
          playlist. If the current track still exists in the reloaded playlist,
          start from that track; otherwise, start from the beginning.
        """
        current_track = self._get_current_track()

        if not self._playlist_modified_during_session:
            # Playlist unchanged - keep current state, just reset the timer
            # so presence loop shows correct elapsed time
            self.track_started_at = time.time()
            self.logger.info(
                "Returning to idle - playlist unchanged, keeping position.")
            return

        # Playlist was modified - reload from cache
        self.logger.info(
            "Returning to idle - playlist was modified, reloading from cache.")
        await self._load_playlist()  # Reloads from cache and applies shuffle

        # Try to find the current track in the reloaded playlist
        if current_track:
            for i, track in enumerate(self.playlist):
                if track.url == current_track.url:
                    self.current_index = i
                    self.logger.info(
                        f"Found current track in reloaded playlist: {track.title}")
                    break
            else:
                # Track not found, start from beginning
                self.current_index = 0
                self.logger.info(
                    "Current track not in reloaded playlist, starting from beginning.")
        else:
            self.current_index = 0

        self.track_started_at = time.time()

    # ==========================================================================
    # PRESENCE CYCLING (IDLE MODE)
    # ==========================================================================

    async def _presence_loop(self) -> None:
        """Background task that cycles through the playlist in presence."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                # Check for ambience-driven playlist changes
                had_switch, new_url = self._ambience.consume_switch()
                if had_switch:
                    if new_url is None:
                        # Ambience wants us to stop
                        self.playlist = []
                        self._ambience.confirm_switch(None)
                        await self.bot.change_presence_safe(activity=None)
                        self.logger.info("Stopped music per ambience request")
                    elif new_url != self._ambience.current_playlist_url:
                        # Switch to new playlist
                        self._ambience.confirm_switch(new_url)
                        await self._load_playlist()
                        self.current_index = 0
                        self.track_started_at = time.time()
                        self.logger.info(
                            "Switched to new playlist from ambience")

                # Let ambience decide if it's time to change mood/activity
                # This may trigger _on_playlist_change callback
                maybe_cycle()

                # Don't update presence while in VC - playback handles that
                if self.active_session:
                    await asyncio.sleep(5)
                    continue

                # No playlist loaded - check if ambience has one now
                if not self.playlist:
                    playlist_url = get_current_playlist()
                    if playlist_url and playlist_url != self._ambience.current_playlist_url:
                        self._ambience.confirm_switch(playlist_url)
                        await self._load_playlist()
                        self.track_started_at = time.time()

                current_track = self._get_current_track()
                if not current_track:
                    await asyncio.sleep(30)
                    continue

                # Presence logic:
                # - When music is playing, ALWAYS show "Listening to X"
                # - Activity status is handled when music ISN'T playing
                # Format: "Listening to [title]" with "by [artist]" on second line
                presence_activity = discord.Activity(
                    type=discord.ActivityType.listening,
                    name=current_track.title,
                    state=f"by {current_track.artist}"
                )
                await self.bot.change_presence_safe(activity=presence_activity)

                # Calculate remaining time for current track
                elapsed = time.time() - self.track_started_at
                remaining = max(current_track.duration - elapsed, 0)

                if remaining <= 0:
                    # Track "finished", advance (always loop in idle mode)
                    # Presence cycling ignores loop_mode - we always want to cycle
                    self.current_index = (
                        self.current_index + 1) % len(self.playlist)
                    self.track_started_at = time.time()
                    continue

                # Sleep until track "ends" or 30 seconds, whichever is shorter
                # (To handle very long tracks gracefully)
                await asyncio.sleep(min(remaining, 30))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    f"Error in presence loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _update_playing_presence(self, track: Track) -> None:
        """Updates presence while actively playing."""
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=track.title,
            state=f"by {track.artist}"
        )
        await self.bot.change_presence_safe(activity=activity)

    # ==========================================================================
    # VOICE PLAYBACK
    # ==========================================================================

    async def _search_youtube(self, query: str, max_results: int = 5) -> List[Track]:
        """Searches YouTube for tracks matching the query.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of Track objects representing search results.
        """
        return await search_youtube(query, max_results, self.logger)

    async def _search_with_ytm(
        self,
        query: str,
        is_url: bool = False,
    ) -> tuple[Optional[SearchResult], List[SearchResult], List[SearchResult], Optional[str]]:
        """Unified search using YTM + YouTube with deduplication.

        Searches both YouTube Music (for better metadata/thumbnails) and
        regular YouTube (via yt-dlp), deduplicates by video ID, and returns
        combined results prioritized by source quality.

        Args:
            query: Search query string or video ID (if is_url=True).
            is_url: If True, query is a video ID to look up.

        Returns:
            Tuple of (original_result, songs, videos, recommended_id):
            - original_result: The user's URL result (URL mode only, else None)
            - songs: List of YTM song results (ATVs)
            - videos: List of video results (YTM videos + YouTube)
            - recommended_id: Video ID of high-confidence match (URL mode only, else None)
        """
        if is_url:
            return await search_url_mode(query)
        # Query mode returns (songs, videos, recommended_id)
        songs, videos, recommended_id = await search_query_mode(query)
        return (None, songs, videos, recommended_id)

    async def _fetch_url_info(
        self,
        url: str,
        force_playlist: bool = False
    ) -> tuple[List[Track], Optional[str], Optional[str]]:
        """Fetches track info from a YouTube URL (video or playlist).

        Args:
            url: The YouTube URL to fetch.
            force_playlist: If True, extract as playlist even if URL has video ID.

        Returns:
            A tuple of (tracks, error_message, warning_message) where:
            - tracks: List of Track objects (single for video, multiple for playlist)
            - error_message: Human-readable error if failed, None if success
            - warning_message: Non-fatal warning (e.g., mix truncation), None if none
        """
        return await fetch_url_info(url, self.logger, force_playlist=force_playlist)

    async def _prefetch_next_track(self) -> None:
        """Pre-fetches everything needed for the next sequential track.

        Always prefetches the next track in sequence, regardless of loop mode.
        This ensures the prefetch is ready if:
        - User skips to next track
        - Loop mode changes from ONE to ALL/OFF
        - Current track ends with loop ALL/OFF

        Phase 12 Design:
        - Cog owns buffer storage (_next_prepared)
        - AudioFetcher owns retry strategy (PREFETCH = conservative)
        - Cache checks happen HERE (cog has playlist context)
        - YouTube fetch delegates to AudioFetcher

        Phase 5 Enhancement (validated prefetch):
        - For streaming URLs: spawn FFmpeg and prebuffer 30s (or full track if ≤10min)
        - This validates the URL works (YouTube throws late 403s at 15-25s)
        - If valid: store prebuffered source for instant playback handoff
        - If invalid: mark auth failure, cleanup source, LIVE will retry

        Priority order:
        1. Ambient cache (playlist-specific, cog context required)
        2. Residential cache (checked by AudioFetcher)
        3. YouTube via AudioFetcher (conservative - no residential)
        """
        if not self.playlist:
            self.logger.debug("[Prefetch] No playlist, clearing")
            self._clear_prefetch_v2()
            return

        next_track = self._get_next_sequential_track()
        if not next_track:
            self.logger.debug("[Prefetch] No next track")
            self._clear_prefetch_v2()
            return

        # Check if we already have a valid prefetch for this track
        if (self._next_prepared and
            self._next_prepared.success and
            self._next_prepared_track and
                self._next_prepared_track.video_id == next_track.video_id):
            self.logger.debug(f"[Prefetch] Already valid for: {next_track.title}")
            return

        # Note: Stale prefetch is cleared by caller (_play_current_track)
        # before creating this task, to avoid self-cancellation
        self._next_prepared_track = next_track

        try:
            self.logger.info(
                f"[Prefetch] Starting for: {next_track.title}"
            )

            # AudioFetcher handles all cache checks (residential + ambient)
            # PREFETCH context = conservative, stops at direct failure
            result = await self._audio_fetcher.fetch(next_track, FetchContext.PREFETCH)

            # Update track thumbnail if we found one
            if result.thumbnail and not next_track.thumbnail:
                next_track.thumbnail = result.thumbnail
                next_track.thumbnail_is_square = result.thumbnail_is_square
                self.logger.info(f"[Prefetch] Updated thumbnail (is_square={result.thumbnail_is_square})")

            # Fetch thumbnail bytes for instant display
            try:
                result.thumbnail_bytes = await get_thumbnail_bytes(next_track, self.cache_manager)
                if result.thumbnail_bytes:
                    self.logger.info(f"[Prefetch] Got thumbnail: {len(result.thumbnail_bytes)} bytes")
            except Exception as e:
                self.logger.warning(f"[Prefetch] Thumbnail fetch failed (non-fatal): {e}")

            # Phase 5: Validate streaming URLs by spawning FFmpeg and prebuffering
            # This catches late 403s that YouTube throws 15-25s into playback
            if result.success and result.url and not result.local_path:
                await self._validate_prefetch_url(result, next_track)

            self._next_prepared = result

            if result.success:
                prebuf_info = ""
                if result.prebuffered_source:
                    prebuf_info = f", prebuffered={result.prebuffered_source.buffered_seconds:.1f}s"
                self.logger.info(
                    f"[Prefetch] Success: {next_track.title} | "
                    f"url={bool(result.url)}, local={bool(result.local_path)}{prebuf_info}"
                )
            else:
                self.logger.info(
                    f"[Prefetch] Failed: {next_track.title} | "
                    f"auth_fail={result.is_auth_failure}, unavail={result.is_unavailable}, "
                    f"ffmpeg_err={result.ffmpeg_error_type}"
                )

        except asyncio.CancelledError:
            self.logger.debug("[Prefetch] Task cancelled")
            self._clear_prefetch_v2()
            raise
        except Exception as e:
            self.logger.debug(f"[Prefetch] Exception: {e}")
            self._clear_prefetch_v2()

    async def _validate_prefetch_url(
        self,
        result: AudioFetchResult,
        track: Track
    ) -> None:
        """Validate a streaming URL by spawning FFmpeg and prebuffering.

        This is the "track playing in shadow" - FFmpeg is running, audio is buffered,
        ready for instant handoff when playback transitions.

        Modifies result in-place:
        - On success: sets result.prebuffered_source with validated source
        - On failure: sets result.success=False, result.is_auth_failure=True,
          result.ffmpeg_error_type with what went wrong

        Args:
            result: AudioFetchResult to validate (modified in-place)
            track: Track for logging and duration info
        """
        from utils.musicutils.audio_source import SeekableAudioSource

        # Ensure we have a URL (caller checked but assert for type safety)
        if not result.url:
            return

        # Duration-based buffering: short tracks buffer fully, long tracks validate with 30s
        # This balances memory usage with validation confidence
        duration = track.duration or 0
        if duration > 0 and duration <= 600:  # 10 minutes or less
            target_seconds = float(duration)  # Buffer entire track
        else:
            target_seconds = 60.0  # Buffer 60s for long/unknown tracks

        min_valid_seconds = 30.0  # YouTube throws 403s at 15-25s, so 30s proves URL works

        self.logger.debug(
            f"[Prefetch] Validating URL: target={target_seconds}s, min_valid={min_valid_seconds}s"
        )

        source: Optional[SeekableAudioSource] = None
        try:
            # Spawn FFmpeg subprocess (non-blocking, handled in thread)
            source = SeekableAudioSource(
                result.url,
                http_headers=result.http_headers,
            )

            # Prebuffer in thread - returns once we have enough or hit error/EOF
            is_valid = await asyncio.to_thread(
                source.prebuffer,
                target_seconds,
                min_valid_seconds,
            )

            if is_valid:
                # URL validated! Store source for instant handoff
                result.prebuffered_source = source
                self.logger.info(
                    f"[Prefetch] URL validated: {source.buffered_seconds:.1f}s buffered, "
                    f"health={source.health}"
                )
            else:
                # Validation failed - extract error info
                health = source.health
                result.success = False
                result.is_auth_failure = True  # Signal LIVE fetch to retry
                result.ffmpeg_error_type = health.error_type.value if health.error_type else None
                self.logger.warning(
                    f"[Prefetch] URL validation failed: {health.error_type}, "
                    f"only got {source.buffered_seconds:.1f}s"
                )
                source.cleanup()

        except Exception as e:
            self.logger.warning(f"[Prefetch] URL validation exception: {e}")
            result.success = False
            result.is_auth_failure = True
            if source:
                source.cleanup()

    def _clear_prefetch_v2(self) -> None:
        """Clears Phase 12 prefetch buffer and cancels any pending task.

        Also clears AudioFetcher state for the prefetched track, since a
        cancelled prefetch shouldn't count against the retry budget.

        Phase 5: Also cleans up any prebuffered source (FFmpeg subprocess).
        """
        # Clear AudioFetcher state for the track we were prefetching
        # A cancelled attempt shouldn't count against retry budget
        if self._next_prepared_track and self._next_prepared_track.video_id:
            self._audio_fetcher.clear_state(self._next_prepared_track.video_id)

        # Phase 5: Cleanup prebuffered source (kills FFmpeg subprocess)
        if self._next_prepared:
            self._next_prepared.cleanup()

        self._next_prepared = None
        self._next_prepared_track = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None

    def _refresh_prefetch_if_stale(self) -> None:
        """Re-prefetch if a playlist mutation changed the next sequential track.

        Called after operations that can change what's at current_index + 1:
        move, swap, remove, dedup, shuffle, clear queue.

        Uses _get_next_sequential_track() since prefetch always targets the
        next track in sequence, regardless of loop mode.
        """
        if not self._next_prepared_track:
            return  # No prefetch to invalidate

        next_track = self._get_next_sequential_track()

        # Check if prefetch still matches
        if next_track and next_track.video_id == self._next_prepared_track.video_id:
            return  # Still valid

        # Prefetch is stale - clear and re-prefetch
        self.logger.debug(
            f"[Prefetch] Stale after playlist mutation: had {self._next_prepared_track.title}, "
            f"next is now {next_track.title if next_track else 'None'}"
        )
        self._clear_prefetch_v2()

        if next_track and self.active_session:
            self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

    def _clear_prefetch(self) -> None:
        """Clears prefetch cache and cancels any pending prefetch task."""
        self.logger.debug("[Prefetch] Clearing prefetch cache")
        self._clear_prefetch_v2()

    def _clear_current_track_cache(self) -> None:
        """Clears only the current track's cached audio URL.

        Use this for retry logic where we want to re-fetch the current track
        but preserve the prefetch (which is for the NEXT track).
        """
        self._playback.current_audio_url = None
        self._playback.current_audio_track_url = None
        self._playback.current_audio_headers = None
        self.logger.debug("Cleared current track audio cache (prefetch preserved)")

    def _clear_audio_caches(self) -> None:
        """Clears all audio URL caches (prefetch and current track).

        Use this when changing tracks (skip, jump) where the next track
        is now different from what we prefetched.
        """
        self._clear_prefetch()
        self._clear_current_track_cache()
        self.logger.debug("Cleared all audio URL caches")

    async def _play_current_track(self) -> None:
        """Plays the current track in the active voice session.

        Phase 12 Design:
        - Check Phase 12 prefetch buffer (_next_prepared) first
        - If prefetch valid: use it directly
        - If prefetch failed: call AudioFetcher with LIVE context (will go to residential)
        - If no prefetch: call AudioFetcher with LIVE context
        - Legacy caches (loop replay, ambient) checked for backwards compat
        """
        if not self.active_session or not self.active_session.voice_client:
            return

        track = self._get_current_track()
        if not track:
            return

        # Debug: trace who called this and with what index (guarded - extract_stack is expensive)
        if self.logger.isEnabledFor(logging.DEBUG):
            import traceback
            caller = traceback.extract_stack()[-2]
            self.logger.debug(
                f"[PlayTrack] Called from {caller.filename.split('/')[-1]}:{caller.lineno} "
                f"({caller.name}), index={self.current_index}, track={track.title[:30]}"
            )

        if not self._player:
            self.logger.error("[PlayTrack] No player available!")
            return

        # -----------------------------------------------------------
        # Priority 1: Loop ONE cache (memory optimization)
        # -----------------------------------------------------------
        audio_source: Optional[str] = None
        http_headers: Optional[Dict[str, str]] = None
        prebuffered_source: Optional[Any] = None  # SeekableAudioSource
        is_local_file = False
        used_loop_replay_cache = False

        if self._playback.current_audio_track_url == track.url and self._playback.current_audio_url:
            audio_source = self._playback.current_audio_url
            http_headers = self._playback.current_audio_headers
            used_loop_replay_cache = True
            self.logger.info(f"[PlayTrack] Using loop-replay cache for: {track.title}")

        # -----------------------------------------------------------
        # Priority 2: Phase 12 prefetch buffer (with Phase 5 prebuffered source)
        # -----------------------------------------------------------
        if not audio_source and self._next_prepared:
            if (self._next_prepared_track and
                    self._next_prepared_track.video_id == track.video_id):

                if self._next_prepared.success:
                    # Prefetch succeeded - use it
                    if self._next_prepared.local_path:
                        audio_source = self._next_prepared.local_path
                        is_local_file = True
                    elif self._next_prepared.prebuffered_source:
                        # Phase 5: Use validated prebuffered source (instant playback)
                        prebuffered_source = self._next_prepared.prebuffered_source
                        # Take ownership - don't let cleanup() kill it
                        self._next_prepared.prebuffered_source = None
                        buffered_secs = getattr(prebuffered_source, 'buffered_seconds', 0.0)
                        self.logger.info(
                            f"[PlayTrack] Using prebuffered source for: {track.title} "
                            f"({buffered_secs:.1f}s ready)"
                        )
                    else:
                        audio_source = self._next_prepared.url
                        http_headers = self._next_prepared.http_headers
                        self.logger.info(f"[PlayTrack] Using prefetched URL for: {track.title}")
                    self._clear_prefetch_v2()

                elif self._next_prepared.is_auth_failure:
                    # Prefetch failed with auth - need LIVE fetch (will go residential)
                    self.logger.info(
                        f"[PlayTrack] Prefetch auth-failed, calling LIVE fetch: {track.title}"
                    )
                    self._clear_prefetch_v2()
                    # Fall through to LIVE fetch below

        # -----------------------------------------------------------
        # Priority 3: AudioFetcher with LIVE context (full retry)
        # -----------------------------------------------------------
        if not audio_source:
            result = await self._audio_fetcher.fetch(track, FetchContext.LIVE)

            if result.success:
                if result.local_path:
                    audio_source = result.local_path
                    is_local_file = True
                else:
                    audio_source = result.url
                    http_headers = result.http_headers

                # Note: Residential notification now happens BEFORE the attempt via callback
                # (see _on_residential_attempt), so we only need to track bandwidth cost here

                # Track bandwidth cost
                if result.residential_bytes > 0 and self.db_manager:
                    await self.db_manager.increment_proxy_usage(result.residential_bytes)

                # Update thumbnail if we found one
                if result.thumbnail and not track.thumbnail:
                    track.thumbnail = result.thumbnail
                    track.thumbnail_is_square = result.thumbnail_is_square
            else:
                # Fetch failed
                if result.is_unavailable:
                    self.logger.info(f"Removing unavailable track: {track.title}")
                    self._remove_track(self.current_index)
                    if self.playlist:
                        await self._play_current_track()
                    else:
                        await self._handle_empty_playlist_after_removal()
                else:
                    self.logger.warning(f"Could not get audio for {track.title}: {result.error}")
                    self._advance_track()
                    await self._play_current_track()
                return

        # -----------------------------------------------------------
        # Play the track
        # -----------------------------------------------------------
        if audio_source is None and prebuffered_source is None:
            self.logger.error("[PlayTrack] No audio source available - this shouldn't happen")
            self._advance_track()
            await self._play_current_track()
            return

        await self._update_playing_presence(track)
        self.track_started_at = time.time()
        self._playback.paused_at_position = None

        try:
            vc = self.active_session.voice_client if self.active_session else None
            if not self.active_session or not vc or not vc.is_connected():
                self.logger.debug("Session ended during track preparation, aborting playback.")
                # Clean up prebuffered source if we're not using it
                if prebuffered_source:
                    prebuffered_source.cleanup()
                return

            if prebuffered_source:
                # Phase 5: Use pre-validated source (instant playback)
                self._player.play(track, source=prebuffered_source)
                self.logger.info(f"Now playing (prebuffered): {track.title}")
            else:
                # Traditional: play from URL
                self._player.play(
                    track,
                    audio_source,
                    http_headers=http_headers if not is_local_file else None,
                )
                self.logger.info(f"Now playing: {track.title}" + (" (local)" if is_local_file else ""))

            # Cache streaming URL for loop ONE replay
            # (Don't cache prebuffered - those are one-shot validated sources)
            if not is_local_file and audio_source and not prebuffered_source:
                self._playback.current_audio_url = audio_source
                self._playback.current_audio_track_url = track.url
                self._playback.current_audio_headers = http_headers

            # Start prefetching next track (Phase 12)
            # Skip if using loop-replay cache - track isn't changing, so
            # existing prefetch for next sequential track remains valid.
            # This avoids wastefully re-fetching the currently playing track.
            if not used_loop_replay_cache:
                # Clear old prefetch FIRST to avoid self-cancellation
                self._clear_prefetch_v2()
                self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

        except discord.ClientException as e:
            self.logger.debug(f"Playback aborted (likely disconnected): {e}")
            # Clean up prebuffered source on error
            if prebuffered_source:
                prebuffered_source.cleanup()
        except Exception as e:
            self.logger.error(f"Error playing track: {e}", exc_info=True)
            # Clean up prebuffered source on error
            if prebuffered_source:
                prebuffered_source.cleanup()
            # Try next track
            self._advance_track()
            await asyncio.sleep(1)
            await self._play_current_track()

    async def _play_with_result(self, result: AudioFetchResult) -> None:
        """Play a track using an already-fetched AudioFetchResult.

        Used by _on_track_end() when retrying - we already have a fresh
        result from AudioFetcher.fetch(RETRY), no need to re-fetch.

        Args:
            result: The AudioFetchResult containing the audio URL to play.
        """
        if not self.active_session or not self._player:
            return

        track = self._get_current_track()
        if not track:
            return

        # Determine what to play
        if result.local_path:
            audio_source = result.local_path
            http_headers = None
            is_local_file = True
        elif result.url:
            audio_source = result.url
            http_headers = result.http_headers
            is_local_file = False
        else:
            self.logger.error("[PlayWithResult] Result has no audio_url or local_path")
            return

        await self._update_playing_presence(track)
        self.track_started_at = time.time()
        self._playback.paused_at_position = None

        try:
            vc = self.active_session.voice_client if self.active_session else None
            if not self.active_session or not vc or not vc.is_connected():
                self.logger.debug("Session ended during retry playback, aborting.")
                return

            self._player.play(
                track,
                audio_source,
                http_headers=http_headers if not is_local_file else None,
            )
            self.logger.info(f"Retry playing: {track.title}" + (" (local)" if is_local_file else ""))

            # Cache streaming URL for loop ONE replay
            if not is_local_file:
                self._playback.current_audio_url = audio_source
                self._playback.current_audio_track_url = track.url
                self._playback.current_audio_headers = http_headers

        except discord.ClientException as e:
            self.logger.debug(f"Retry playback aborted (likely disconnected): {e}")
        except Exception as e:
            self.logger.error(f"Error during retry playback: {e}", exc_info=True)
            # At this point we've already exhausted retries, so give up on this track
            self._advance_track()
            await asyncio.sleep(1)
            await self._play_current_track()

    async def _handle_track_failure(self, track: Track) -> None:
        """Handle a track that failed to play after exhausting retries.

        Shows an interactive view to users, letting them choose to skip or
        remove the track. If no one responds within 60 seconds, auto-removes.

        Args:
            track: The track that failed to play.
        """
        if not self.active_session:
            return

        # Get origin channel for the interactive dialog
        origin = self.bot.get_channel(self.active_session.origin_channel_id)
        if not origin or not isinstance(origin, discord.abc.Messageable):
            # Can't send interactive message, auto-skip
            self.logger.warning("No origin channel for failure notification, auto-skipping")
            self._advance_track()
            await self._play_current_track()
            return

        # Also notify voice channel if different (non-interactive, just informational)
        if self.active_session.channel_id != self.active_session.origin_channel_id:
            vc_channel = self.bot.get_channel(self.active_session.channel_id)
            if vc_channel and isinstance(vc_channel, discord.abc.Messageable):
                try:
                    await vc_channel.send(
                        f"⚠️ **{track.title}** isn't available. "
                        f"Check <#{self.active_session.origin_channel_id}> to choose what to do!"
                    )
                except discord.HTTPException as e:
                    self.logger.debug(f"Failed to send track failure notice to VC channel: {e}")

        # Show interactive failure dialog in origin channel
        action = await show_track_failed(origin, track.title, track.url)
        self.logger.info(f"Track failure action: {action.name} for '{track.title}'")

        if action == TrackFailureAction.SKIP:
            # Skip to next track (keep in playlist for potential retry later)
            self._audio_fetcher.reset()
            self._advance_track()
            await self._play_current_track()

        else:  # REMOVE or TIMEOUT - both remove the track
            # Remove from playlist and play next
            self._audio_fetcher.reset()
            self._remove_track(self.current_index)
            if self.playlist:
                await self._play_current_track()
            else:
                await self._handle_empty_playlist_after_removal()

    async def _handle_empty_playlist_after_removal(self) -> None:
        """Handle the case where playlist is empty after removing a failed track."""
        self.logger.info("Playlist empty after track removal - waiting for user action.")

        if not self.active_session:
            return

        await self._send_system_message(
            "🎵 No more tracks in playlist! I'll wait here for 5 minutes.\n"
            "Add more songs or I'll have to disconnect!~"
        )

        # Start idle timeout - disconnect if no activity
        self.active_session.waiting_for_users = True
        if self.idle_timeout_task:
            self.idle_timeout_task.cancel()

        self.idle_timeout_task = asyncio.create_task(
            self._playlist_end_timeout_loop()
        )

    def _on_player_track_end(self, error: Optional[Exception]) -> None:
        """Callback from ManagedPlayer when track naturally ends or errors.

        This is the ONLY entry point for track-end handling. ManagedPlayer
        guarantees this is NOT called for intentional stops (skip, pause, etc.).

        Args:
            error: Exception if playback failed, None if track ended normally.
                   ManagedPlayer converts suspiciously fast ends to ConnectionError.
        """
        if not self.active_session:
            return

        # Dispatch to the main handler on the event loop with error flag
        asyncio.run_coroutine_threadsafe(
            self._on_track_end(needs_retry=error is not None),
            self.bot.loop
        )

    async def _on_track_end(self, needs_retry: bool = False) -> None:
        """Handle track end - either retry or advance to next track.

        Phase 12: Uses FetchContext.RETRY for FFmpeg failures. This tells
        AudioFetcher to be aggressive (use residential if auth fails).

        Args:
            needs_retry: True if the track failed and should be retried.
        """
        if not self.active_session:
            return

        if needs_retry:
            track = self._get_current_track()
            if not track:
                self._audio_fetcher.reset()
                return

            self.logger.info(f"[Retry] Track failed during FFmpeg playback: {track.title}")

            # RETRY context: FFmpeg failed, get fresh URL (aggressive strategy)
            result = await self._audio_fetcher.fetch(track, FetchContext.RETRY)

            if result.success:
                # Note: Residential notification now happens BEFORE the attempt via callback
                # (see _on_residential_attempt), so we only need to track bandwidth cost here

                # Track bandwidth cost
                if result.residential_bytes > 0 and self.db_manager:
                    await self.db_manager.increment_proxy_usage(result.residential_bytes)

                # Play with the fresh URL
                await self._play_with_result(result)
                return

            # Fetch failed after all retries
            if result.is_unavailable:
                self.logger.info(f"Track permanently unavailable: {track.title}")
                self._remove_track(self.current_index)
                if self.playlist:
                    await self._play_current_track()
                else:
                    await self._handle_empty_playlist_after_removal()
                return

            # All retries exhausted - show interactive failure view
            self.logger.warning(f"Track failed after all attempts: {track.title}")
            await self._handle_track_failure(track)
            return

        # Normal track end - reset notification flag for next track
        self._residential_notified_this_track = False

        # Check session duration limit (8 hours)
        session_duration = time.time() - self.active_session.started_at
        max_session_duration = 8 * 60 * 60
        if session_duration >= max_session_duration:
            hours = int(session_duration // 3600)
            await self._end_session(f"Session ended after {hours} hours. Take a break! 🎧")
            return

        next_track = self._advance_track()
        if next_track:
            await self._play_current_track()
        else:
            # Playlist ended with loop OFF
            self.current_index = max(0, len(self.playlist) - 1)
            self.logger.info("Playlist finished with loop OFF - waiting for user action.")

            if self.active_session:
                await self._send_system_message(
                    "🎵 Playlist finished! I'll wait here for 5 minutes.\n"
                    "Add more songs, enable loop, or I'll have to disconnect!~"
                )

                self.active_session.waiting_for_users = True
                if self.idle_timeout_task:
                    self.idle_timeout_task.cancel()

                self.idle_timeout_task = asyncio.create_task(
                    self._playlist_end_timeout_loop()
                )

    async def _start_session(
        self,
        channel: discord.VoiceChannel,
        ctx: commands.Context,
        join_message: Optional[str] = None
    ) -> None:
        """Starts a new voice session.

        Args:
            channel: The voice channel to join.
            ctx: The command context.
            join_message: Optional custom message to send. If None, uses a default.
        """
        try:
            vc = await channel.connect()
            self.active_session = ActiveSession(
                guild_id=channel.guild.id,
                channel_id=channel.id,
                voice_client=vc,
                origin_channel_id=ctx.channel.id,
            )

            # Create managed player for this session
            self._player = ManagedPlayer(vc, self._on_player_track_end)

            self.logger.info(f"Started voice session in {channel.name} (guild={channel.guild.id}, channel={channel.id})")

            # Start playback from current track
            await self._play_current_track()

            message = join_message or f"🎵 Now playing in {channel.mention}!"
            await ctx.send(message)

        except discord.ClientException as e:
            self.logger.error(f"Failed to connect to voice: {e}")
            await ctx.send("I couldn't connect to the voice channel. Please try again.")
        except Exception as e:
            self.logger.error(f"Error starting session: {e}", exc_info=True)
            await ctx.send("Something went wrong starting playback.")

    async def _end_session(self, reason: str = "Session ended.") -> None:
        """Ends the current voice session."""
        if not self.active_session:
            return

        vc = self.active_session.voice_client

        # Stop playback via managed player (no callback triggered)
        if self._player:
            self._player.stop()
            self._player = None

        # Notify users before disconnecting (while we still have session info)
        await self._send_system_message(f"🎵 {reason}")

        # Disconnect
        await vc.disconnect()

        self.active_session = None

        # Cancel idle timeout if running
        if self.idle_timeout_task:
            self.idle_timeout_task.cancel()
            self.idle_timeout_task = None

        # Clear all audio URL caches
        self._clear_audio_caches()

        # Restore playlist state for idle mode
        await self._restore_idle_playlist()

        # Reset modification flag for next session
        self._playlist_modified_during_session = False

        self.logger.info(f"Voice session ended: {reason}")

    async def _idle_timeout_loop(self) -> None:
        """Waits for users to join, disconnects if none do within timeout."""
        try:
            await asyncio.sleep(300)  # 5 minutes

            if self.active_session and self.active_session.waiting_for_users:
                # Check if anyone joined
                vc = self.active_session.voice_client
                if vc and len(vc.channel.members) <= 1:  # Just the bot
                    await self._send_system_message(
                        "No one joined, so I'm heading out! Type 'listen along' when you're ready."
                    )
                    await self._end_session("No one joined within 5 minutes.")

        except asyncio.CancelledError:
            pass

    async def _playlist_end_timeout_loop(self) -> None:
        """Waits for user action after playlist ends with loop OFF.

        If no new songs are added, loop mode changed, or manual play within 5 minutes,
        disconnects from voice.
        """
        try:
            await asyncio.sleep(300)  # 5 minutes

            # Still waiting and no playback resumed?
            if self.active_session and self.active_session.waiting_for_users:
                if not self._player or not self._player.is_playing:
                    await self._send_system_message(
                        "No activity for 5 minutes - disconnecting. "
                        "Type 'listen along' when you're ready to continue!"
                    )
                    await self._end_session("Playlist ended with no user activity.")

        except asyncio.CancelledError:
            pass

    # ==========================================================================
    # VOICE STATE TRACKING
    # ==========================================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """Handles voice state updates to detect when to leave."""
        if not self.active_session:
            return

        # Ignore bot's own state changes
        if member.id == self.bot.user.id:  # type: ignore
            return

        vc = self.active_session.voice_client
        if not vc or not vc.channel:
            return

        # Check if this is our channel
        if before.channel == vc.channel or after.channel == vc.channel:
            # Someone joined our channel
            if after.channel == vc.channel and before.channel != vc.channel:
                # Cancel idle timeout if waiting
                if self.active_session.waiting_for_users:
                    self.active_session.waiting_for_users = False
                    if self.idle_timeout_task:
                        self.idle_timeout_task.cancel()
                        self.idle_timeout_task = None

            # Check if we're alone
            if len(vc.channel.members) <= 1:
                await self._end_session("Everyone left the voice channel.")

    # ==========================================================================
    # TODO: FUTURE NLP HANDLERS
    # ==========================================================================

    # TODO: seek_nlp - Seek to a specific timestamp in the current track
    # Example triggers: "seek 1:30", "rewind 10s", "forward 30s", "go to 2:00"
    # Implementation notes:
    # - Parse timestamp from query (MM:SS or seconds)
    # - Recreate FFmpeg source with -ss offset
    # - Track elapsed time needs adjustment
    # - Consider relative seeking (forward/back N seconds)

    # TODO: replay_nlp - Restart the current track from the beginning
    # Example triggers: "replay", "restart", "play again", "from the top"
    # Implementation notes:
    # - Set track_started_at to current time
    # - Stop and restart playback with same track
    # - Could reuse cached audio URL (_current_audio_url)

    # TODO: autoplay_nlp - Toggle autoplay/radio mode
    # Example triggers: "autoplay on", "radio mode", "keep playing"
    # Implementation notes:
    # - When queue ends, fetch related tracks via YouTube recommendations
    # - Use yt-dlp's --flat-playlist with related video extraction
    # - Consider user preference storage in DB
    # - Need to handle "autoplay off" to disable

    # =========================================================================


async def setup(bot: 'CoreBot') -> None:
    """Sets up the Music cog."""
    await bot.add_cog(Music(bot))

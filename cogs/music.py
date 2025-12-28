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
import functools
import io
import os
import random
import re
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, cast

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
    GeniusScraper,
    LoopMode,
    LRCLIBProvider,
    LyricalNonsenseScraper,
    LyricsResult,
    ManagedPlayer,
    MusicCacheManager,
    PlaybackState,
    PrefetchState,
    Track,
    chunk_text,
    detect_mix_in_url,
    fetch_playlist_metadata,
    fetch_url_info,
    get_audio_url,
    get_best_thumbnail_bytes,
    search_youtube,
)
from utils.views import (
    NowPlayingState,
    TrackFailureAction,
    show_track_failed,
)

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


# Type alias for NLP handler methods
NlpHandler = Callable[['Music', commands.Context, str], Awaitable[None]]


def requires_voice(func: NlpHandler) -> NlpHandler:
    """Decorator for NLP handlers that require the user to be in VC with the bot.

    Checks (in order):
    1. Bot has an active voice session
    2. User is bot owner (bypass for debugging) OR
    3. User is in the same voice channel as the bot

    Sends appropriate error message and returns early if check fails.

    Usage:
        @requires_voice
        async def pause_nlp(self, ctx: commands.Context, query: str) -> None:
            # VC check passed, safe to access self.active_session
            ...
    """
    @functools.wraps(func)
    async def wrapper(self: 'Music', ctx: commands.Context, query: str) -> None:
        # Check 1: Is there an active session?
        if not self.active_session:
            await ctx.send("I'm not playing music right now!")
            return

        # Check 2: Bot owner bypasses VC check (debugging)
        if ctx.author.id == self.bot.owner_id:
            await func(self, ctx, query)
            return

        # Check 3: Is user in a voice channel?
        author_voice = getattr(ctx.author, 'voice', None)
        if not author_voice or not author_voice.channel:
            await ctx.send("You need to be in the voice channel to control playback!")
            return

        # Check 4: Is user in the SAME channel as the bot?
        if author_voice.channel.id != self.active_session.channel_id:
            await ctx.send("You need to be in the voice channel to control playback!")
            return

        await func(self, ctx, query)
    return wrapper


class Music(BaseCog):
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
        self._prefetch = PrefetchState()  # Legacy - being replaced by _next_prepared
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
        self._audio_fetcher = AudioFetcher(self.cache_manager, self.logger)

        # Track if we've notified the user about residential proxy for the current track
        self._residential_notified_this_track: bool = False

        # PO Token Provider server subprocess (started in cog_ready)
        self._pot_server_process: Optional[asyncio.subprocess.Process] = None
        self._pot_server_healthy: bool = False

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
        pot_port = getattr(config, 'POT_PROVIDER_PORT', 4416)

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
        pot_port = getattr(config, 'POT_PROVIDER_PORT', 4416)
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
                async with session.get(f'http://127.0.0.1:{pot_port}/ping') as resp:
                    return resp.status == 200
        except Exception:
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
                self.cache_manager.reconcile_downloads(playlists)

                # Cleanup expired orphans
                await self.cache_manager.cleanup_expired_orphans()

                # Queue missing downloads
                await self.cache_manager.queue_missing_downloads()

                # Start background download worker
                await self.cache_manager.start_background_downloads()

                # If we're currently playing from a playlist, refresh local paths
                if self._ambience.current_playlist_url and self.playlist:
                    self._populate_local_paths(self._ambience.current_playlist_url)

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
        if self._prefetch.task:
            self._prefetch.task.cancel()
            try:
                await self._prefetch.task
            except asyncio.CancelledError:
                pass

        # Disconnect from voice if connected
        if self.active_session and self.active_session.voice_client:
            await self.active_session.voice_client.disconnect()
            self.active_session = None

        # Clear presence
        await self.bot.change_presence(activity=None)
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
            # Populate local_path from downloaded files
            self._populate_local_paths(playlist_url)
            # Shuffle for initial playback
            self._apply_shuffle()
            self._dedupe_playlist()

    def _populate_local_paths(self, playlist_url: str) -> None:
        """Populates local_path on tracks from downloaded cache.

        Args:
            playlist_url: The playlist URL to look up in cache.
        """
        if not self.playlist:
            return

        cached_count = 0
        for track in self.playlist:
            if track.video_id:
                local_path = self.cache_manager.get_local_path(track.video_id, playlist_url)
                if local_path:
                    track.local_path = local_path
                    cached_count += 1

        if cached_count > 0:
            self.logger.debug(
                f"Populated {cached_count}/{len(self.playlist)} "
                "tracks with local cache paths"
            )

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
            self.logger.debug(f"Paused at position: {self._playback.paused_at_position:.1f}s")
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

        self.logger.debug(f"Resumed playback at position: {seek_position:.1f}s")
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
                        await self.bot.change_presence(activity=None)
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
                await self.bot.change_presence(activity=presence_activity)

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
        await self.bot.change_presence(activity=activity)

    # ==========================================================================
    # VOICE PLAYBACK
    # ==========================================================================

    async def _get_audio_url(self, track: Track) -> tuple[Optional[str], bool, Optional[str], bool, Optional[Dict[str, str]]]:
        """Gets the actual streamable audio URL for a track.

        Args:
            track: The track to get the audio URL for.

        Returns:
            A tuple of (url, is_unavailable, thumbnail, needs_crop, http_headers) where:
            - url: The streamable URL, or None if failed
            - is_unavailable: True if the video is permanently unavailable and should be removed
            - thumbnail: Best thumbnail URL found, or None
            - needs_crop: True if thumbnail needs center-cropping to extract album art
            - http_headers: Dict of HTTP headers needed to fetch the URL, or None
        """
        return await get_audio_url(track, self.logger)

    async def _search_youtube(self, query: str, max_results: int = 5) -> List[Track]:
        """Searches YouTube for tracks matching the query.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of Track objects representing search results.
        """
        return await search_youtube(query, max_results, self.logger)

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
        """Pre-fetches everything needed for the next track in the background.

        Phase 12 Design:
        - Cog owns buffer storage (_next_prepared)
        - AudioFetcher owns retry strategy (PREFETCH = conservative)
        - Cache checks happen HERE (cog has playlist context)
        - YouTube fetch delegates to AudioFetcher

        Priority order:
        1. Ambient cache (playlist-specific, cog context required)
        2. Residential cache (checked by AudioFetcher)
        3. YouTube via AudioFetcher (conservative - no residential)
        """
        if not self.playlist:
            self.logger.debug("[Prefetch] No playlist, clearing")
            self._clear_prefetch_v2()
            return

        next_track = self._get_next_track()
        if not next_track:
            self.logger.debug("[Prefetch] No next track (loop off at end?)")
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
            self.logger.debug(
                f"[Prefetch] Starting for: {next_track.title} ({next_track.url})"
            )

            # Priority 1: Ambient cache (cog has playlist context)
            if self._ambience.current_playlist_url and next_track.video_id:
                local_path = self.cache_manager.get_local_path(
                    next_track.video_id,
                    self._ambience.current_playlist_url
                )
                if local_path:
                    self.logger.debug(f"[Prefetch] Ambient cache hit: {next_track.title}")
                    self._next_prepared = AudioFetchResult(
                        success=True,
                        local_path=local_path
                    )
                    return

            # Priority 2 & 3: Residential cache + YouTube (via AudioFetcher)
            # AudioFetcher checks residential cache, then tries yt-dlp
            # PREFETCH context = conservative, stops at direct failure
            result = await self._audio_fetcher.fetch(next_track, FetchContext.PREFETCH)

            # Update track thumbnail if we found one
            if result.thumbnail and not next_track.thumbnail:
                next_track.thumbnail = result.thumbnail
                next_track.thumbnail_needs_crop = result.thumbnail_needs_crop
                self.logger.debug(f"[Prefetch] Updated thumbnail (needs_crop={result.thumbnail_needs_crop})")

            # Fetch thumbnail bytes for instant display (if URL succeeded)
            if result.success and result.url:
                try:
                    result.thumbnail_bytes = await get_best_thumbnail_bytes(next_track, self.logger)
                    if result.thumbnail_bytes:
                        self.logger.debug(f"[Prefetch] Got thumbnail: {len(result.thumbnail_bytes)} bytes")
                except Exception as e:
                    self.logger.debug(f"[Prefetch] Thumbnail fetch failed (non-fatal): {e}")

            self._next_prepared = result

            if result.success:
                self.logger.debug(
                    f"[Prefetch] Success: {next_track.title} | "
                    f"url={bool(result.url)}, local={bool(result.local_path)}"
                )
            else:
                self.logger.debug(
                    f"[Prefetch] Failed: {next_track.title} | "
                    f"auth_fail={result.is_auth_failure}, unavail={result.is_unavailable}"
                )

        except asyncio.CancelledError:
            self.logger.debug("[Prefetch] Task cancelled")
            self._clear_prefetch_v2()
            raise
        except Exception as e:
            self.logger.debug(f"[Prefetch] Exception: {e}")
            self._clear_prefetch_v2()

    def _clear_prefetch_v2(self) -> None:
        """Clears Phase 12 prefetch buffer and cancels any pending task.

        Also clears AudioFetcher state for the prefetched track, since a
        cancelled prefetch shouldn't count against the retry budget.
        """
        # Clear AudioFetcher state for the track we were prefetching
        # A cancelled attempt shouldn't count against retry budget
        if self._next_prepared_track and self._next_prepared_track.video_id:
            self._audio_fetcher.clear_state(self._next_prepared_track.video_id)

        self._next_prepared = None
        self._next_prepared_track = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None

    def _refresh_prefetch_if_stale(self) -> None:
        """Re-prefetch if a playlist mutation changed the next track.

        Called after operations that can change what's at current_index + 1:
        move, swap, remove, dedup, shuffle, clear queue.

        If prefetch no longer matches the actual next track, clears it and
        starts a new prefetch for the correct track.
        """
        if not self._next_prepared_track:
            return  # No prefetch to invalidate

        next_track = self._get_next_track()

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
        # Legacy PrefetchState
        self._prefetch.cancel_task()
        self._prefetch.clear()
        # Phase 12 buffer
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

        # Debug: trace who called this and with what index
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
        is_local_file = False

        if self._playback.current_audio_track_url == track.url and self._playback.current_audio_url:
            audio_source = self._playback.current_audio_url
            http_headers = self._playback.current_audio_headers
            self.logger.debug(f"Using loop-replay cache for: {track.title}")

        # -----------------------------------------------------------
        # Priority 2: Phase 12 prefetch buffer
        # -----------------------------------------------------------
        if not audio_source and self._next_prepared:
            if (self._next_prepared_track and
                    self._next_prepared_track.video_id == track.video_id):

                if self._next_prepared.success:
                    # Prefetch succeeded - use it
                    if self._next_prepared.local_path:
                        audio_source = self._next_prepared.local_path
                        is_local_file = True
                        track.local_path = self._next_prepared.local_path
                    else:
                        audio_source = self._next_prepared.url
                        http_headers = self._next_prepared.http_headers
                    self.logger.debug(f"Using prefetched result for: {track.title}")
                    self._clear_prefetch_v2()

                elif self._next_prepared.is_auth_failure:
                    # Prefetch failed with auth - need LIVE fetch (will go residential)
                    self.logger.debug(
                        f"Prefetch auth-failed, calling LIVE fetch: {track.title}"
                    )
                    self._clear_prefetch_v2()
                    # Fall through to LIVE fetch below

        # -----------------------------------------------------------
        # Priority 3: Ambient cache (cog has playlist context)
        # -----------------------------------------------------------
        if not audio_source and self._ambience.current_playlist_url and not track.user_added and track.video_id:
            local_path = self.cache_manager.get_local_path(track.video_id, self._ambience.current_playlist_url)
            if local_path:
                audio_source = local_path
                is_local_file = True
                track.local_path = local_path
                self.logger.debug(f"Found ambient cache: {track.title}")

        # -----------------------------------------------------------
        # Priority 4: AudioFetcher with LIVE context (full retry)
        # -----------------------------------------------------------
        if not audio_source:
            result = await self._audio_fetcher.fetch(track, FetchContext.LIVE)

            if result.success:
                if result.local_path:
                    audio_source = result.local_path
                    is_local_file = True
                    track.local_path = result.local_path
                else:
                    audio_source = result.url
                    http_headers = result.http_headers

                # Notify if residential was used
                if result.residential_used and not self._residential_notified_this_track:
                    self._residential_notified_this_track = True
                    await self._send_system_message(
                        f"🔄 Hmm, having some trouble with **{track.title}**... "
                        f"Let me try another way!"
                    )

                # Track bandwidth cost
                if result.residential_bytes > 0 and self.db_manager:
                    await self.db_manager.increment_proxy_usage(result.residential_bytes)

                # Update thumbnail if we found one
                if result.thumbnail and not track.thumbnail:
                    track.thumbnail = result.thumbnail
                    track.thumbnail_needs_crop = result.thumbnail_needs_crop
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
        if audio_source is None:
            self.logger.error("[PlayTrack] audio_source is None - this shouldn't happen")
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
                return

            self._player.play(
                track,
                audio_source,
                http_headers=http_headers if not is_local_file else None,
            )
            self.logger.info(f"Now playing: {track.title}" + (" (local)" if is_local_file else ""))

            # Cache streaming URL for loop ONE replay
            if not is_local_file:
                self._playback.current_audio_url = audio_source
                self._playback.current_audio_track_url = track.url
                self._playback.current_audio_headers = http_headers

            # Start prefetching next track (Phase 12)
            # Clear old prefetch FIRST to avoid self-cancellation
            self._clear_prefetch_v2()
            self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

        except discord.ClientException as e:
            self.logger.debug(f"Playback aborted (likely disconnected): {e}")
        except Exception as e:
            self.logger.error(f"Error playing track: {e}", exc_info=True)
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
                except Exception:
                    pass

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

            self.logger.debug(f"[Retry] Track failed during FFmpeg playback: {track.title}")

            # RETRY context: FFmpeg failed, get fresh URL (aggressive strategy)
            result = await self._audio_fetcher.fetch(track, FetchContext.RETRY)

            if result.success:
                # Notify user if residential was used for the first time
                if result.residential_used and not self._residential_notified_this_track:
                    self._residential_notified_this_track = True
                    await self._send_system_message(
                        f"🔄 Hmm, having some trouble with **{track.title}**... "
                        f"Let me try another way!"
                    )

                # Update track with local path if residential succeeded
                if result.local_path:
                    track.local_path = result.local_path

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
    # COMMAND HELPERS (Complex operations only)
    # ==========================================================================
    # These methods contain substantial logic that benefits from being named,
    # testable units. Simple operations are inlined directly into NLP handlers.

    async def _do_listen_along(self, ctx: commands.Context) -> None:
        """Internal implementation for listen-along.

        Args:
            ctx: The command context.
        """
        if not YTDLP_AVAILABLE:
            await ctx.send("Music playback isn't available - yt-dlp is not installed.")
            return

        # If we don't have a playlist, ask ambience to start music
        if not self.playlist:
            playlist_url, _ = ensure_music_for_user()
            if playlist_url:
                self._ambience.confirm_switch(playlist_url)
                await self._load_playlist()
                self.track_started_at = time.time()

        if not self.playlist:
            await ctx.send("I don't have any music loaded! Check if playlists are configured in `ambience.toml`.")
            return

        # Check if already in a session
        if self.active_session:
            if ctx.guild and self.active_session.guild_id == ctx.guild.id:
                await ctx.send(f"I'm already playing music in <#{self.active_session.channel_id}>!")
            else:
                other_guild = self.bot.get_guild(self.active_session.guild_id)
                guild_name = other_guild.name if other_guild else "another server"
                await ctx.send(f"I'm currently playing music in **{guild_name}**. I can only be in one place at a time!")
            return

        # Check if user is in a voice channel
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        if not member or not member.voice or not member.voice.channel:
            # User isn't in a VC - try the guild's designated music channel as fallback
            if ctx.guild:
                designated_channel_id = await self.db_manager.get_guild_config(ctx.guild.id, 'music_channel_id')
                if designated_channel_id:
                    channel = ctx.guild.get_channel(int(designated_channel_id))
                    if channel and isinstance(channel, discord.VoiceChannel):
                        await ctx.send(f"I'll be in {channel.mention}! Join me there within 5 minutes.")
                        await self._start_session(channel, ctx)

                        # Mark session as waiting and start 5-minute timeout
                        # If no one joins, _idle_timeout_loop will disconnect
                        self.active_session.waiting_for_users = True  # type: ignore
                        self.idle_timeout_task = self.bot.loop.create_task(
                            self._idle_timeout_loop()
                        )
                        return

            await ctx.send("Join a voice channel first, or ask an admin to set a music channel!")
            return

        # Join user's channel
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.send("I can only join regular voice channels, not stage channels.")
            return

        # Send personality-aware flavor text based on current idle activity
        await ctx.send(MusicAmbience.get_listen_along_response())

        await self._start_session(channel, ctx)

    async def _do_play(self, ctx: commands.Context, query: str) -> None:
        """Internal implementation for play/queue.

        Handles both URL-based and search-based track additions.

        Args:
            ctx: The command context.
            query: URL or search query for the track(s).
        """
        from utils.views import get_selection

        if not YTDLP_AVAILABLE:
            await ctx.send("Music playback isn't available - yt-dlp is not installed.")
            return

        if not query.strip():
            await ctx.send("Please provide a song name or URL to play!")
            return

        # Determine if it's a URL or search query
        is_url = query.startswith(('http://', 'https://', 'www.'))

        tracks_to_add: List[Track] = []

        if is_url:
            # Check if this is a video URL that also contains a mix playlist
            has_mix, single_url, mix_url = detect_mix_in_url(query)

            if has_mix and single_url and mix_url:
                # Prompt the user: single song or whole mix?
                embed = discord.Embed(
                    title="🎵 Mix Playlist Detected",
                    description=(
                        "This link includes a Mix playlist. Would you like to add:\n\n"
                        "**1.** Just this single song\n"
                        "**2.** Up to 60 songs from the mix playlist"
                    ),
                    color=discord.Color.blue()
                )
                embed.set_footer(text="Defaults to single song in 10 seconds...")

                options = {"1. Single Song": "single", "2. Mix Playlist": "mix"}
                selection = await get_selection(ctx, embed, options, timeout=10.0, buttons_only=True)

                if selection == "mix":
                    # User wants the mix playlist
                    await ctx.send("🔍 Fetching mix playlist (up to 60 songs)...")
                    tracks, error, warning = await self._fetch_url_info(mix_url, force_playlist=True)
                else:
                    # Default: single song (timeout or explicit selection)
                    await ctx.send("🔍 Fetching track info...")
                    tracks, error, warning = await self._fetch_url_info(single_url)
            else:
                # Regular URL (not a video+mix combo)
                await ctx.send("🔍 Fetching track info...")
                tracks, error, warning = await self._fetch_url_info(query)

            if error:
                await ctx.send(f"❌ {error}")
                return

            tracks_to_add = tracks

            # Send warning if mix playlist was truncated
            if warning:
                await ctx.send(warning)

        else:
            # Search YouTube
            await ctx.send(f"🔍 Searching for: **{query}**")

            results = await self._search_youtube(query, max_results=5)
            if not results:
                await ctx.send("No results found. Try a different search term!")
                return

            if len(results) == 1:
                # Only one result - use it directly
                tracks_to_add = results
            else:
                # Multiple results - let user choose
                embed = discord.Embed(
                    title="🎵 Select a Track",
                    description="Choose the track you want to play:",
                    color=discord.Color.blue()
                )

                options = {}
                for i, track in enumerate(results, 1):
                    duration_str = f"{int(track.duration) // 60}:{int(track.duration) % 60:02d}"
                    embed.add_field(
                        name=f"{i}. {track.title}",
                        value=f"by {track.artist} • {duration_str}",
                        inline=False
                    )
                    options[str(i)] = str(i)

                selection = await get_selection(ctx, embed, options, timeout=30.0)

                if not selection:
                    await ctx.send("Selection timed out. Call me again when you're ready!")
                    return

                try:
                    selected_idx = int(selection) - 1
                    if 0 <= selected_idx < len(results):
                        tracks_to_add = [results[selected_idx]]
                    else:
                        await ctx.send("Invalid selection.")
                        return
                except ValueError:
                    await ctx.send("Invalid selection.")
                    return

        if not tracks_to_add:
            await ctx.send("No tracks to add.")
            return

        # Queue size limit: 2000 tracks max
        MAX_QUEUE_SIZE = 2000
        current_queue_size = len(self.playlist) if self.active_session else 0
        available_slots = MAX_QUEUE_SIZE - current_queue_size

        if available_slots <= 0:
            await ctx.send(f"❌ The queue is full ({MAX_QUEUE_SIZE} tracks max). Remove some tracks first!")
            return

        if len(tracks_to_add) > available_slots:
            tracks_to_add = tracks_to_add[:available_slots]
            await ctx.send(f"⚠️ Only adding {available_slots} tracks to stay within the {MAX_QUEUE_SIZE} track limit.")

        # Mark all tracks as user-added (should already be, but ensure it)
        for track in tracks_to_add:
            track.user_added = True

        # If not in a voice session, join the user's VC and start fresh
        if not self.active_session:
            # Get the member's voice channel
            member = ctx.author if isinstance(
                ctx.author, discord.Member) else None
            if not member or not member.voice or not member.voice.channel:
                await ctx.send("Join a voice channel first so I can play your request!")
                return

            channel = member.voice.channel
            if not isinstance(channel, discord.VoiceChannel):
                await ctx.send("I can only join regular voice channels, not stage channels.")
                return

            # Clear existing playlist and set to just the requested tracks
            self.playlist = tracks_to_add.copy()
            self.current_index = 0
            self._playlist_modified_during_session = True  # Mark as modified

            # Join and start playing
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

                await self._play_current_track()

                if len(tracks_to_add) == 1:
                    await ctx.send(f"🎵 Now playing **{tracks_to_add[0].title}** in {channel.mention}!")
                else:
                    await ctx.send(f"🎵 Now playing **{len(tracks_to_add)} tracks** in {channel.mention}!")

            except discord.ClientException as e:
                self.logger.error(f"Failed to connect to voice: {e}")
                await ctx.send("I couldn't connect to the voice channel. Please try again.")
            except Exception as e:
                self.logger.error(
                    f"Error starting session: {e}", exc_info=True)
                await ctx.send("Something went wrong starting playback.")

        else:
            # Already in a session - append to playlist
            if ctx.guild and self.active_session.guild_id != ctx.guild.id:
                await ctx.send("I'm currently playing in another server!")
                return

            # Handle duplicates - move existing tracks to end instead of adding twice
            moved_tracks: List[Track] = []
            new_tracks: List[Track] = []

            for track in tracks_to_add:
                # Find if this track already exists in playlist (by URL)
                existing_idx = next(
                    (i for i, t in enumerate(self.playlist) if t.url == track.url),
                    None
                )
                if existing_idx is not None:
                    # Remove from current position (will re-add at end)
                    existing_track = self.playlist.pop(existing_idx)
                    # Adjust current_index if we removed before it
                    if existing_idx < self.current_index:
                        self.current_index -= 1
                    moved_tracks.append(existing_track)
                else:
                    new_tracks.append(track)

            # Add all tracks (moved + new) at the end
            self.playlist.extend(moved_tracks + new_tracks)

            # Mark as modified since we added user tracks
            self._playlist_modified_during_session = True

            # Note: No need to clear prefetch here - _play_current_track
            # validates by video_id match at use-time

            # Build response message
            if len(tracks_to_add) == 1:
                if moved_tracks:
                    await ctx.send(f"⭐ Moved **{tracks_to_add[0].title}** to the end of the queue!")
                else:
                    await ctx.send(f"⭐ Added **{tracks_to_add[0].title}** to the end of the queue!")
            else:
                parts = []
                if new_tracks:
                    parts.append(f"added {len(new_tracks)}")
                if moved_tracks:
                    parts.append(f"moved {len(moved_tracks)}")
                await ctx.send(f"⭐ **{' and '.join(parts).capitalize()} tracks** to the end of the queue!")

    async def _do_queue(self, ctx: commands.Context) -> None:
        """Internal implementation for queue.

        Args:
            ctx: The command context.
        """
        from utils.views import PaginatorView

        if not self.playlist:
            await ctx.send("No playlist loaded.")
            return

        current = self._get_current_track()

        # Build pages with 10 tracks each
        tracks_per_page = 10
        pages: List[discord.Embed] = []
        total_tracks = len(self.playlist)
        total_pages = (total_tracks + tracks_per_page - 1) // tracks_per_page

        for page_num in range(total_pages):
            start_idx = page_num * tracks_per_page
            end_idx = min(start_idx + tracks_per_page, total_tracks)

            lines = []
            for i in range(start_idx, end_idx):
                track = self.playlist[i]
                track_num = i + 1
                # Star icon for user-added tracks
                star = "⭐ " if track.user_added else ""
                if i == self.current_index:
                    # Highlight currently playing track
                    lines.append(
                        f"▶️ **{track_num}. {star}{track.title}** - {track.artist}")
                else:
                    lines.append(
                        f"{track_num}. {star}{track.title} - {track.artist}")

            embed = discord.Embed(
                title="🎶 Playlist",
                description="\n".join(lines),
                color=discord.Color.blue()
            )

            # Add now playing info in the author field
            if current:
                embed.set_author(
                    name=f"Now Playing: {current.title} - {current.artist}")

            embed.set_footer(
                text=f"Page {page_num + 1}/{total_pages} • {total_tracks} tracks • "
                f"Loop: {self.loop_mode.display}"
            )
            pages.append(embed)

        # Find the page containing the current track and start there
        current_page_idx = self.current_index // tracks_per_page

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PaginatorView(ctx, pages, start_index=current_page_idx)
            msg = await ctx.send(embed=pages[current_page_idx], view=view)
            view.message = msg

    async def _do_jump(self, ctx: commands.Context, position: int) -> None:
        """Internal implementation for jump.

        Args:
            ctx: The command context.
            position: The 1-indexed track number to jump to.
        """
        if not self.playlist:
            await ctx.send("No playlist loaded.")
            return

        index = position - 1

        if index < 0 or index >= len(self.playlist):
            await ctx.send(f"❌ Invalid position. Please choose a number between 1 and {len(self.playlist)}.")
            return

        self.current_index = index
        self.track_started_at = time.time()

        # Only clear current track cache - let _play_current_track decide
        # whether to use or discard the prefetch based on video_id match
        self._clear_current_track_cache()

        track = self._get_current_track()

        if track:
            await ctx.send(f"⏭️ Jumped to **#{position}**: {track.title} - {track.artist}")

            # If playing, stop and play the new track
            if self._player and (self._player.is_playing or self._player.is_paused):
                # Stop current playback (no callback triggered)
                self._player.stop()
                # Start playing the new track
                await self._play_current_track()
            elif self.active_session:
                # If we're connected but not playing, start playback immediately
                await self._play_current_track()

    async def _do_skip(self) -> bool:
        """Skip to the next track, ignoring Loop ONE mode.

        Unlike natural track end, an explicit skip should always advance
        to the next track even when Loop ONE is enabled.

        Returns:
            True if skip was initiated, False if not playing/no session.
        """
        if not self._player:
            return False

        if not (self._player.is_playing or self._player.is_paused):
            return False

        if not self.playlist:
            return False

        # Manually advance index (ignoring Loop ONE)
        self.current_index += 1
        if self.current_index >= len(self.playlist):
            if self.loop_mode == LoopMode.ALL:
                self.current_index = 0
            else:
                # At end with loop OFF - wrap to start but don't auto-play
                self.current_index = 0

        self.track_started_at = time.time()
        self._playback.paused_at_position = None
        # Only clear CURRENT track cache, preserve prefetch for next track
        self._clear_current_track_cache()

        # Stop current playback (no callback triggered)
        self._player.stop()

        # Start the next track - intentional stops don't trigger callbacks
        await self._play_current_track()
        return True

    async def _do_lyrics(self, ctx: commands.Context, query: Optional[str] = None) -> None:
        """Internal implementation for lyrics search.

        Searches multiple providers for lyrics, presents options to the user,
        and displays the selected lyrics with translation if available.

        Args:
            ctx: The command context.
            query: Search query. If None, uses current playing track.
        """
        from utils.views import PaginatorView, get_selection

        # Determine search query - use title directly (user can specify manually if needed)
        artist_hint: Optional[str] = None
        if not query:
            # Use currently playing track
            current = self._get_current_track()
            if current:
                query = current.title
                artist_hint = current.artist
            else:
                await ctx.send("🎵 No song is currently playing. Please provide a search query!\n"
                               "Example: 'lyrics [song name]'")
                return

        searching_msg = await ctx.send(f"🔍 Searching for lyrics: **{query}**...")

        # Search all providers concurrently
        # NOTE: LyricalNonsenseScraper is disabled (no public search API)
        search_tasks = [
            GeniusScraper.search(query),
            LRCLIBProvider.search(query),
        ]
        provider_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # Flatten and dedupe results
        all_results: List[LyricsResult] = []
        seen_keys: set[str] = set()

        for result_list in provider_results:
            if isinstance(result_list, (Exception, BaseException)):
                continue
            # result_list is now List[LyricsResult]
            for result in cast(List[LyricsResult], result_list):
                # Dedupe by normalized title+artist
                key = f"{result.title.lower()}|{result.artist.lower()}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_results.append(result)

        # If we have an artist hint and too many results, filter by artist
        if artist_hint and len(all_results) > 5:
            artist_lower = artist_hint.lower()
            # Try to find results matching the artist
            filtered = [r for r in all_results if artist_lower in r.artist.lower(
            ) or r.artist.lower() in artist_lower]
            if filtered:
                all_results = filtered

        if not all_results:
            if searching_msg:
                try:
                    await searching_msg.edit(
                        content=f"❌ No lyrics found for **{query}**.\n"
                        "Try a different search term or check the spelling."
                    )
                except Exception:
                    pass
            return

        # If only one result, use it directly
        if len(all_results) == 1:
            selected = all_results[0]
        else:
            # Build selection options (max 5 for button layout)
            display_results = all_results[:5]
            options: Dict[str, str] = {}

            for i, result in enumerate(display_results):
                label = f"{i + 1}. {result.source}"
                # Value maps to index
                options[label] = str(i)

            # Build embed for selection
            embed = discord.Embed(
                title=f"🎵 Lyrics Search: {query}",
                description="Select a source to view lyrics:\n\n" + "\n".join([
                    f"**{i + 1}.** {r.title} - {r.artist} ({r.source}){' 🌐' if r.has_translation else ''}"
                    for i, r in enumerate(display_results)
                ]),
                color=discord.Color.blue()
            )
            embed.set_footer(
                text="🌐 = Translation available • Select within 30s")

            # Delete searching message
            if searching_msg:
                try:
                    await searching_msg.delete()
                except Exception:
                    pass

            # Get user selection
            # Use buttons_only=True to ignore text input
            selection = await get_selection(ctx, embed, options, buttons_only=True)

            if selection is None:
                return  # Timeout or cancelled

            try:
                selected_idx = int(selection)
                selected = display_results[selected_idx]
            except (ValueError, IndexError):
                return

        # Fetch full lyrics if not already populated
        fetching_msg = None
        if not selected.lyrics_text:
            try:
                fetching_msg = await ctx.send(f"📜 Fetching lyrics from {selected.source}...")
            except Exception:
                pass

            if selected.source == "Lyrical Nonsense":
                selected = await LyricalNonsenseScraper.fetch_lyrics(selected)
            elif selected.source == "Genius":
                selected = await GeniusScraper.fetch_lyrics(selected)
            elif selected.source == "LRCLIB":
                selected = await LRCLIBProvider.fetch_lyrics(selected)

            if fetching_msg:
                try:
                    await fetching_msg.delete()
                except Exception:
                    pass

        if not selected.lyrics_text:
            await ctx.send(f"❌ Couldn't retrieve lyrics from {selected.source}. Try another source.")
            return

        # Build lyrics embeds (paginated if long)
        pages: List[discord.Embed] = []

        # Split lyrics into smaller chunks for better readability (1200 chars per page)
        # This prevents embeds from being cut off on mobile/smaller screens
        lyrics_chunks = chunk_text(selected.lyrics_text, 1200)

        for i, chunk in enumerate(lyrics_chunks):
            embed = discord.Embed(
                title=f"🎵 {selected.title}",
                description=chunk,
                color=discord.Color.purple(),
                url=selected.url
            )
            embed.set_author(name=selected.artist)
            embed.set_footer(
                text=f"Source: {selected.source} • Page {i + 1}/{len(lyrics_chunks)}")
            pages.append(embed)

        # If translation exists, add it as additional pages
        if selected.translation_text:
            trans_chunks = chunk_text(selected.translation_text, 1200)
            for i, chunk in enumerate(trans_chunks):
                embed = discord.Embed(
                    title=f"🌐 {selected.title} (Translation)",
                    description=chunk,
                    color=discord.Color.green(),
                    url=selected.url
                )
                embed.set_author(name=selected.artist)
                embed.set_footer(
                    text=f"Source: {selected.source} • Translation {i + 1}/{len(trans_chunks)}")
                pages.append(embed)

        # Send lyrics
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PaginatorView(ctx, pages)
            msg = await ctx.send(embed=pages[0], view=view)
            view.message = msg

    async def _do_remove(self, ctx: commands.Context, query: str) -> None:
        """Internal implementation for removing a track from the playlist.

        Supports both position numbers and song name matching.

        Args:
            ctx: The command context.
            query: Track number or song name to remove.
        """
        if not self.playlist:
            await ctx.send("The playlist is empty!")
            return

        if not query:
            await ctx.send(
                "What should I remove? Give me a track number or song name.\n"
                "Example: 'remove 5' or 'remove bohemian rhapsody'"
            )
            return

        target_index: Optional[int] = None

        # Try to parse as a number first
        number_match = re.search(r'\b(\d+)\b', query)
        if number_match:
            position = int(number_match.group(1))
            if 1 <= position <= len(self.playlist):
                target_index = position - 1
            else:
                await ctx.send(f"Invalid track number. Playlist has {len(self.playlist)} tracks.")
                return
        else:
            # Try to find by song name
            target_index = self._find_track_by_query(query)

        if target_index is None:
            await ctx.send(f"Couldn't find a track matching '{query}'.")
            return

        track = self.playlist[target_index]
        is_current = (target_index == self.current_index)

        self._remove_track(target_index)
        await ctx.send(f"🗑️ Removed **{track.title}** from the playlist.")

        # If we removed the currently playing track, play the next one
        if is_current and self._player:
            self._player.stop()  # Stop current (no callback)
            await self._play_current_track()  # Play whatever is now at current_index

    def _parse_track_reference(self, text: str) -> Optional[int]:
        """Parses a track reference (number or name) into a playlist index.

        Args:
            text: The text to parse (e.g., "5", "bohemian rhapsody").

        Returns:
            The 0-based playlist index, or None if not found/invalid.
        """
        text = text.strip()
        if not text:
            return None

        # Try as number first
        number_match = re.search(r'\b(\d+)\b', text)
        if number_match:
            pos = int(number_match.group(1))
            if 1 <= pos <= len(self.playlist):
                return pos - 1
            return None

        # Try as song name
        return self._find_track_by_query(text)

    def _parse_destination(self, text: str, mode: str = 'to') -> Optional[int]:
        """Parses a destination reference for move commands.

        Args:
            text: The destination text (e.g., "2", "top", "after 5").
            mode: How to interpret the destination - 'to' (exact), 'after', 'before'.

        Returns:
            The 0-based target index, or None if invalid.
        """
        text = text.strip().lower()
        if not text:
            return None

        # Handle keywords
        if text in ('top', 'first', 'beginning', 'start'):
            return 0
        if text in ('bottom', 'last', 'end'):
            return len(self.playlist) - 1

        # Try as number
        number_match = re.search(r'\b(\d+)\b', text)
        if number_match:
            pos = int(number_match.group(1))
            if 1 <= pos <= len(self.playlist):
                if mode == 'after':
                    # After pos X = index X
                    return min(pos, len(self.playlist) - 1)
                elif mode == 'before':
                    return pos - 1  # Before pos X = index X-1
                return pos - 1  # Exact position
            return None

        # Try as song name
        track_index = self._find_track_by_query(text)
        if track_index is not None:
            if mode == 'after':
                return min(track_index + 1, len(self.playlist) - 1)
            elif mode == 'before':
                return track_index  # Before track at index X = insert at index X
            return track_index

        return None

    async def _do_move(self, ctx: commands.Context, query: str) -> None:
        """Internal implementation for moving a track in the playlist.

        Parses natural language queries like:
        - "5 to 2", "track 5 to position 2"
        - "bohemian rhapsody to the top"
        - "3 after 7", "3 before 5"

        Args:
            ctx: The command context.
            query: The move instruction.
        """
        if not self.playlist:
            await ctx.send("The playlist is empty!")
            return

        if len(self.playlist) < 2:
            await ctx.send("Need at least 2 tracks to move anything!")
            return

        if not query:
            await ctx.send(
                "What should I move? Examples:\n"
                "• 'move 5 to 2'\n"
                "• 'move bohemian rhapsody to the top'\n"
                "• 'move 3 after 7'"
            )
            return

        from_index: Optional[int] = None
        to_index: Optional[int] = None
        use_swap = False  # 'to' uses swap, 'after'/'before' use insert

        # Try different patterns
        # Check more specific patterns first (after/before), then fall back to 'to'
        # This handles "move 5 to after 7" correctly (matches 'after', not 'to')
        for pattern, mode in [
            (r'^(.+?)\s+(?:to\s+)?after\s+(.+)$', 'after'),
            (r'^(.+?)\s+(?:to\s+)?before\s+(.+)$', 'before'),
            (r'^(.+?)\s+to\s+(?:position\s+|#)?(.+)$', 'to'),
        ]:
            match = re.match(pattern, query, re.IGNORECASE)
            if match:
                from_index = self._parse_track_reference(match.group(1))
                to_index = self._parse_destination(match.group(2), mode)
                use_swap = (mode == 'to')
                break

        if from_index is None:
            await ctx.send("I couldn't figure out which track you want to move.")
            return

        if to_index is None:
            await ctx.send("I couldn't figure out where you want to move it to.")
            return

        if from_index == to_index:
            await ctx.send("That track is already at that position!")
            return

        if use_swap:
            # "move X to Y" swaps the two tracks
            result = self._swap_tracks(from_index, to_index)
            if result:
                track_a, track_b = result
                await ctx.send(
                    f"🔄 Swapped **{track_a.title}** (#{from_index + 1}) "
                    f"with **{track_b.title}** (#{to_index + 1})."
                )
            else:
                await ctx.send("Something went wrong swapping those tracks.")
        else:
            # "move X after/before Y" inserts the track
            track = self._move_track(from_index, to_index)
            if track:
                await ctx.send(f"📋 Moved **{track.title}** to position {to_index + 1}.")
            else:
                await ctx.send("Something went wrong moving that track.")

    # chunk_text moved to utils.music_helpers

    # ==========================================================================
    # HYBRID COMMANDS (Admin/Config only)
    # ==========================================================================

    @commands.hybrid_command(
        name='set-music-channel',
        help='Sets the default voice channel for music playback.'
    )
    @commands.has_guild_permissions(manage_channels=True)
    async def set_music_channel(self, ctx: commands.Context, channel: discord.VoiceChannel) -> None:
        """Sets the designated music channel for this guild.

        Args:
            channel: The voice channel to use as default.
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            return

        await self.db_manager.set_guild_config(ctx.guild.id, 'music_channel_id', str(channel.id))
        await ctx.send(f"✅ Music channel set to {channel.mention}! I'll join there if users aren't in a VC.")

    # ==========================================================================
    # NLP HANDLERS
    # ==========================================================================
    # These handle natural language queries via the prefix system (e.g., ".s play music").
    # They're registered in config.NLP_COMMANDS and called by the bot's NLP dispatcher.
    # Each handler receives the full query string, parses any needed arguments,
    # and delegates to the corresponding `_do_*` method.

    async def listen_along_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for listen along requests."""
        await self._do_listen_along(ctx)

    @requires_voice
    async def skip_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for skip requests."""
        if await self._do_skip():
            await ctx.send("⏭️ Skipped!")
        else:
            await ctx.send("Nothing is playing right now.")

    @requires_voice
    async def pause_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for pause requests."""
        if self._player and self._player.is_paused:
            await ctx.send("Already paused! Type 'resume' to continue!")
            return

        if self._do_pause():
            await ctx.send("⏸️ Paused!")
        else:
            await ctx.send("Nothing is playing right now.")

    @requires_voice
    async def resume_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for resume/play requests.

        Note: This is only triggered when 'play' has no text after it,
        otherwise it would be interpreted as a song request.
        The NLP pattern matching in config.py handles this distinction.
        """
        if self._player and self._player.is_playing:
            await ctx.send("Already playing!")
            return

        if self._player and self._player.is_paused:
            if await self._do_resume():
                await ctx.send("▶️ Resumed!")
            else:
                await ctx.send("Failed to resume playback.")
        else:
            await ctx.send("Nothing to resume. Type 'listen along' to start playback!")

    async def play_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for play/queue requests with a song/URL.

        Extracts the song query from the message, removing the 'play' or 'queue' keyword.
        """
        # Remove 'play' or 'queue' keyword and any leading/trailing whitespace
        # The query might be "play something", "queue something", or URLs
        song_query = re.sub(r'^\s*(play|queue)\s+', '',
                            query, flags=re.IGNORECASE).strip()

        if not song_query:
            # No query provided, treat as resume - inline the resume logic
            if not self.active_session:
                await ctx.send("I'm not in a voice channel! Type 'listen along' to start.")
                return

            if ctx.guild and self.active_session.guild_id != ctx.guild.id:
                await ctx.send("I'm not playing music in this server!")
                return

            if self._player and self._player.is_playing:
                await ctx.send("Already playing!")
                return

            if self._player and self._player.is_paused:
                if await self._do_resume():
                    await ctx.send("▶️ Resumed!")
                else:
                    await ctx.send("Failed to resume playback.")
            else:
                await ctx.send("Nothing to resume. Type 'listen along' to start playback!")
            return

        await self._do_play(ctx, song_query)

    async def now_playing_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for now playing requests."""
        track = self._get_current_track()
        if not track:
            await ctx.send("No track is loaded.")
            return

        # Get thumbnail
        thumbnail_bytes = await get_best_thumbnail_bytes(track, self.logger)
        files: List[discord.File] = []
        thumbnail_url: Optional[str] = None

        if thumbnail_bytes:
            files.append(discord.File(io.BytesIO(thumbnail_bytes), filename="thumbnail.jpg"))
            thumbnail_url = "attachment://thumbnail.jpg"

        # Build initial state
        state = self.get_now_playing_state(thumbnail_url)

        # Create view with action callbacks and state getter
        # Note: We import here to avoid circular import at module level
        from utils.views import NowPlayingView
        view = NowPlayingView(
            state=state,
            get_state=self.get_now_playing_state,
            on_play_pause=self.toggle_playback,
            on_skip=self.skip_track,
            on_shuffle=self.shuffle_playlist,
            on_loop=self.cycle_loop_mode,
        )

        if files:
            await ctx.send(view=view, files=files)
        else:
            await ctx.send(view=view)

    # =========================================================================
    # MusicPlayerProtocol Implementation
    # =========================================================================
    # These methods implement the MusicPlayerProtocol from utils.views,
    # enabling show_now_playing() to work with this cog.

    def get_now_playing_state(self, thumbnail_url: Optional[str] = None) -> NowPlayingState:
        """Build current now playing state for view construction.

        Implements MusicPlayerProtocol.get_now_playing_state().

        Args:
            thumbnail_url: Thumbnail attachment URL.

        Returns:
            NowPlayingState with current player state.
        """
        track = self._get_current_track()

        # Use _get_elapsed_seconds for accurate time tracking (handles pause state)
        elapsed = int(self._get_elapsed_seconds())
        elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        duration_str = f"{track.duration // 60}:{track.duration % 60:02d}" if track else "0:00"
        progress = elapsed / track.duration if track and track.duration > 0 else 0.0

        is_playing = False
        is_paused = False
        if self._player:
            is_playing = self._player.is_playing
            is_paused = self._player.is_paused

        return NowPlayingState(
            track_title=track.title if track else "Unknown",
            track_artist=track.artist if track else "Unknown",
            track_url=track.url if track else "",
            elapsed_str=elapsed_str,
            duration_str=duration_str,
            progress=progress,
            loop_display=self.loop_mode.display,
            is_playing=is_playing,
            is_paused=is_paused,
            in_voice=self.active_session is not None,
            playlist_count=len(self.playlist),
            thumbnail_url=thumbnail_url,
        )

    async def toggle_playback(self) -> None:
        """Toggle between play and pause states.

        Implements MusicPlayerProtocol.toggle_playback().
        """
        if self._player:
            if self._player.is_playing:
                self._do_pause()
            elif self._player.is_paused:
                await self._do_resume()

    async def skip_track(self) -> Optional[bytes]:
        """Skip to next track and return new thumbnail.

        Implements MusicPlayerProtocol.skip_track().

        Returns:
            New thumbnail bytes if track changed, None otherwise.
        """
        if not self.playlist:
            return None

        # Use _do_skip to properly handle Loop ONE mode
        if not await self._do_skip():
            # Not playing - manually advance for UI update
            self.current_index = (self.current_index + 1) % len(self.playlist)
            self.track_started_at = time.time()
        else:
            # Give time for track to start playing
            await asyncio.sleep(0.5)

        # Fetch new thumbnail
        new_track = self._get_current_track()
        if new_track:
            return await get_best_thumbnail_bytes(new_track, self.logger)
        return None

    async def shuffle_playlist(self) -> None:
        """Shuffle the playlist, preserving current track position.

        Implements MusicPlayerProtocol.shuffle_playlist().
        """
        if self.playlist:
            self._apply_shuffle(preserve_current=True)

    async def cycle_loop_mode(self) -> None:
        """Cycle through loop modes (ALL -> ONE -> OFF -> ALL).

        Implements MusicPlayerProtocol.cycle_loop_mode().
        """
        if self.loop_mode == LoopMode.ALL:
            self.loop_mode = LoopMode.ONE
        elif self.loop_mode == LoopMode.ONE:
            self.loop_mode = LoopMode.OFF
        else:
            self.loop_mode = LoopMode.ALL

    async def get_current_thumbnail(self) -> Optional[bytes]:
        """Fetch thumbnail bytes for the current track.

        Implements MusicPlayerProtocol.get_current_thumbnail().

        Returns:
            Thumbnail image bytes, or None if unavailable.
        """
        track = self._get_current_track()
        if track:
            return await get_best_thumbnail_bytes(track, self.logger)
        return None

    async def queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for queue requests."""
        await self._do_queue(ctx)

    @requires_voice
    async def shuffle_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for shuffle toggle requests."""
        if not self.playlist:
            await ctx.send("No playlist to shuffle.")
            return

        self._apply_shuffle(preserve_current=True)
        # Note: _apply_shuffle now handles prefetch invalidation internally
        await ctx.send("🔀 Playlist shuffled!")

    @requires_voice
    async def jump_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for jump requests.

        Parses the query for a number to jump to.
        """

        match = re.search(r'\b(\d+)\b', query)
        if match:
            position = int(match.group(1))
            await self._do_jump(ctx, position)
        else:
            await ctx.send(
                f"🎵 Currently on track **#{self.current_index + 1}** of {len(self.playlist)}.\n"
                "Usage: `jump 5` to jump to track #5"
            )

    async def loop_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for loop mode requests.

        Parses the query for 'one', 'all', or 'off' to set loop mode.
        """
        query_lower = query.lower()
        mode: Optional[LoopMode] = None

        if 'one' in query_lower or 'single' in query_lower or 'track' in query_lower:
            mode = LoopMode.ONE
        elif 'all' in query_lower or 'playlist' in query_lower:
            mode = LoopMode.ALL
        elif 'off' in query_lower or 'disable' in query_lower or 'none' in query_lower:
            mode = LoopMode.OFF

        if mode is None:
            # Just showing info, no VC required
            await ctx.send(
                f"{self.loop_mode.emoji} Current loop mode: **{self.loop_mode.display}**\n"
                "Usage: 'loop one' (repeat track), 'loop all' (repeat playlist), or 'loop off'"
            )
            return

        # Require user in VC to change mode (inline check since this handler has dual behavior)
        if not self.active_session:
            await ctx.send("I'm not playing music right now!")
            return

        if ctx.author.id != self.bot.owner_id:
            author_voice = getattr(ctx.author, 'voice', None)
            if not author_voice or not author_voice.channel or author_voice.channel.id != self.active_session.channel_id:
                await ctx.send("You need to be in the voice channel to control playback!")
                return

        # Cancel any playlist-end timeout if enabling loop
        if mode != LoopMode.OFF and self.active_session and self.active_session.waiting_for_users:
            self.active_session.waiting_for_users = False
            if self.idle_timeout_task:
                self.idle_timeout_task.cancel()
                self.idle_timeout_task = None

        self.loop_mode = mode

        if mode == LoopMode.OFF:
            await ctx.send(
                f"{self.loop_mode.emoji} Loop mode: **{self.loop_mode.display}**\n"
                "Playback will stop after the last track."
            )
        else:
            await ctx.send(f"{self.loop_mode.emoji} Loop mode: **{self.loop_mode.display}**")

    @requires_voice
    async def leave_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for leave/disconnect requests."""
        await self._end_session("Disconnected by user request.")
        await ctx.send("👋 Disconnected!")

    @requires_voice
    async def remove_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for removing tracks from the playlist."""
        # Strip trigger words - config.py already matched on 'remove'/'delete'
        clean_query = re.sub(
            r'^\s*(remove|delete)\s*(track|song|number|#)?\s*',
            '', query, flags=re.IGNORECASE
        ).strip()
        await self._do_remove(ctx, clean_query)

    @requires_voice
    async def move_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for moving tracks in the playlist."""
        # Strip trigger word - config.py already matched on 'move'
        clean_query = re.sub(
            r'^\s*move\s*(track|song|number|#)?\s*',
            '', query, flags=re.IGNORECASE
        ).strip()
        await self._do_move(ctx, clean_query)

    async def lyrics_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for lyrics search.

        Parses the query for song name/artist, or uses current track if empty.
        """
        # Strip common trigger words from the query
        clean_query = re.sub(
            r'^\s*(lyrics?\s*(for|of|to)?|find\s*lyrics?\s*(for|of|to)?|search\s*lyrics?\s*(for|of|to)?|get\s*lyrics?\s*(for|of|to)?)\s*',
            '',
            query,
            flags=re.IGNORECASE
        ).strip()

        # Pass None if query is empty (will use current track)
        await self._do_lyrics(ctx, clean_query if clean_query else None)

    @requires_voice
    async def clear_queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for clearing the queue.

        Removes all tracks except the currently playing one.
        """
        if not self.playlist:
            await ctx.send("The queue is already empty!")
            return

        # Keep only the current track
        current_track = self._get_current_track()
        if current_track:
            self.playlist = [current_track]
            self.current_index = 0
            self._playlist_modified_during_session = True
            # Re-prefetch if next track changed (likely cleared)
            self._refresh_prefetch_if_stale()
            await ctx.send(f"🗑️ Queue cleared! Only **{current_track.title}** remains.")
        else:
            self.playlist = []
            self.current_index = 0
            await ctx.send("🗑️ Queue cleared!")

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

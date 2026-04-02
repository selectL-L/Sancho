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
import os
import random
import time
from typing import TYPE_CHECKING, List, Optional

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
    FFmpegResponseAction,
    LoopMode,
    ManagedPlayer,
    MusicCacheManager,
    MusicCommandsMixin,
    PlaybackEndReport,
    PlaybackState,
    SearchResult,
    Track,
    TrackIssueKind,
    fetch_playlist_metadata,
    fetch_url_info,
    search_query_mode,
    search_url_mode,
)
from utils.musicutils.source_acquisition import (
    FailureAction,
    PlayableSource,
    SourceAcquisitionMixin,
    classify_failure,
)
from utils.musicutils.search import get_thumbnail_bytes
from utils.views import (
    TrackFailureAction,
    show_track_failed,
)

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


class Music(SourceAcquisitionMixin, MusicCommandsMixin, BaseCog):
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
        self.track_started_at: float = 0.0
        self.presence_task: Optional[asyncio.Task[None]] = None

        # Voice session state
        self.active_session: Optional[ActiveSession] = None
        self.playback_task: Optional[asyncio.Task[None]] = None
        self.idle_timeout_task: Optional[asyncio.Task[None]] = None

        # Grouped mutable state (see music_helpers.py for dataclass definitions)
        self._playback = PlaybackState()
        self._ambience = AmbienceState()

        # Prefetch state (cog owns scheduling, mixin owns acquisition)
        self._prefetch_task: Optional[asyncio.Task[None]] = None
        self._enrichment_task: Optional[asyncio.Task[None]] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None
        self._enrichment_video_id: Optional[str] = None
        self._prefetched_source: Optional[PlayableSource] = None
        self._prefetched_track: Optional[Track] = None
        self._prefetched_thumbnail: Optional[bytes] = None

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
            on_track_unavailable=self._on_track_unavailable,
        )

        # Source acquisition mixin state (per-track retry counters).
        from utils.musicutils.source_acquisition import TrackAttempts
        self._track_attempts: dict[str, TrackAttempts] = {}

        # Background cache init task (started in cog_ready)
        self._cache_init_task: Optional[asyncio.Task[None]] = None

        # PO Token Provider startup/health state (started in cog_ready)
        self._pot_server_process: Optional[asyncio.subprocess.Process] = None
        self._pot_start_task: Optional[asyncio.Task[None]] = None
        self._pot_health_task: Optional[asyncio.Task[None]] = None

    async def _on_track_unavailable(self, title: str, artist: str, url: str) -> None:
        """Callback fired when a track is marked unavailable after download failure.

        Sends a DM to each bot owner reporting the unavailable track so they
        can investigate or remove it from the playlist.

        Args:
            title: Track title.
            artist: Track artist.
            url: YouTube URL of the track.
        """
        for owner_id in config.OWNER_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(
                    f"⚠️ **Ambient track unavailable**\n"
                    f"**{title}** by {artist}\n"
                    f"{url}\n\n"
                    f"Both direct and residential proxy downloads failed. "
                    f"The track has been excluded from playback until the next "
                    f"24h refresh. It may be age-restricted, region-locked, or "
                    f"otherwise inaccessible."
                )
            except Exception as e:
                self.logger.debug(f"Failed to DM owner {owner_id} about unavailable track: {e}")

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
        """Start the PO Token Provider server if available.

        Launches the compiled Deno binary directly. The server generates
        proof-of-origin tokens for YouTube requests, helping bypass 403
        errors on datacenter IPs.

        Returns:
            True if server started successfully, False otherwise.
        """
        from utils.musicutils.music_auth import _check_pot_server_running, get_youtube_auth_status
        auth_status = get_youtube_auth_status()

        # Idempotency guard: Don't start another server if one is already running
        if self._pot_server_process is not None and self._pot_server_process.returncode is None:
            self.logger.info("POT server already running, skipping start")
            return True

        pot_binary = config.POT_PROVIDER_PATH
        pot_port = config.POT_PROVIDER_PORT

        if pot_port is None:
            self.logger.info("POT_PROVIDER_PORT not configured, skipping POT server")
            return False

        # Check if an external instance is already listening on the port
        if await asyncio.to_thread(_check_pot_server_running, pot_port):
            self.logger.info(f"POT server already responding on port {pot_port}, skipping start")
            auth_status.update_pot_health(True)
            return True

        if not pot_binary:
            self.logger.info("POT server binary not found in venv, skipping")
            return False

        self.logger.info(f"Starting POT server: {pot_binary} --port {pot_port}")

        try:
            self._pot_server_process = await asyncio.create_subprocess_exec(
                pot_binary, '--port', str(pot_port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )

            # Poll for readiness using the single TCP probe
            max_wait = 10.0
            check_interval = 0.5
            elapsed = 0.0

            while elapsed < max_wait:
                await asyncio.sleep(check_interval)
                elapsed += check_interval

                # Check if process died during startup
                if self._pot_server_process.returncode is not None:
                    stderr_data = await self._pot_server_process.stderr.read() if self._pot_server_process.stderr else b''
                    stderr_text = stderr_data.decode('utf-8', errors='replace')[:500]
                    self.logger.error(f"POT server failed to start: {stderr_text}")
                    self._pot_server_process = None
                    auth_status.update_pot_health(False)
                    return False

                if await asyncio.to_thread(_check_pot_server_running, pot_port):
                    auth_status.update_pot_health(True)
                    self.logger.info(
                        f"POT server started successfully "
                        f"(PID: {self._pot_server_process.pid}, took {elapsed:.1f}s)"
                    )
                    return True

            self.logger.warning(f"POT server started but not responding after {max_wait}s")
            auth_status.update_pot_health(False)
            return False

        except Exception as e:
            self.logger.error(f"Failed to start POT server: {e}", exc_info=True)
            auth_status.update_pot_health(False)
            return False

    def _schedule_pot_startup(self) -> None:
        """Start POT initialization in the background.

        This keeps cog_ready responsive while the POT server performs its
        readiness checks.
        """
        if self._pot_start_task is not None and not self._pot_start_task.done():
            self.logger.info("POT provider initialization already running, skipping reschedule")
            return

        self.logger.info("Scheduling POT provider initialization in background")
        self._pot_start_task = asyncio.create_task(self._initialize_pot_system())

    async def _initialize_pot_system(self) -> None:
        """Initialize POT startup checks and health monitoring in the background."""
        try:
            await self._start_pot_server()

            from utils.musicutils.music_auth import detect_youtube_auth
            await asyncio.to_thread(detect_youtube_auth, 'startup')

            if self._pot_health_task is None or self._pot_health_task.done():
                self._pot_health_task = asyncio.create_task(self._pot_health_watchdog())

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"Background POT initialization failed: {e}", exc_info=True)
        finally:
            current_task = asyncio.current_task()
            if current_task is not None and self._pot_start_task is current_task:
                self._pot_start_task = None

    async def _stop_pot_server(self) -> None:
        """Stop the PO Token Provider server if running."""
        current_task = asyncio.current_task()

        if (
            self._pot_start_task is not None
            and self._pot_start_task is not current_task
            and not self._pot_start_task.done()
        ):
            self._pot_start_task.cancel()
            try:
                await self._pot_start_task
            except asyncio.CancelledError:
                pass
        self._pot_start_task = None

        # Cancel health watchdog first
        if self._pot_health_task is not None:
            self._pot_health_task.cancel()
            try:
                await self._pot_health_task
            except asyncio.CancelledError:
                pass
            self._pot_health_task = None

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
            from utils.musicutils.music_auth import get_youtube_auth_status
            get_youtube_auth_status().update_pot_health(False)

    async def _pot_health_watchdog(self) -> None:
        """Background watchdog for POT server health.

        Runs every 30 minutes. Uses a layered approach:
        1. If the subprocess has exited -> immediate death detection
        2. If a live fetch confirmed health recently (< 30 min) -> skip probe
        3. Otherwise -> TCP probe (covers idle periods with no fetches)

        On healthy->dead transition, DMs bot owners once.
        """
        from utils.musicutils.music_auth import _check_pot_server_running, get_youtube_auth_status
        auth_status = get_youtube_auth_status()
        pot_port = config.POT_PROVIDER_PORT

        if pot_port is None:
            return

        while True:
            await asyncio.sleep(1800)  # 30 minutes

            # Layer 1: Check if the process itself has exited
            if self._pot_server_process is not None and self._pot_server_process.returncode is not None:
                running = False
                self.logger.warning(
                    f"POT server process exited with code {self._pot_server_process.returncode}"
                )
                self._pot_server_process = None

            # Layer 2: If live traffic has confirmed health recently, skip probe
            elif (time.time() - auth_status.last_pot_confirmed) < 1800:
                self.logger.debug("POT health confirmed by recent live fetch, skipping probe")
                continue

            # Layer 3: No recent live signal — TCP probe
            else:
                running = await asyncio.to_thread(_check_pot_server_running, pot_port)

            transition = auth_status.update_pot_health(running)

            if transition is True:
                # Server just died — notify owners once
                self.logger.error("POT server health check failed — server appears to be down")
                for owner_id in config.OWNER_IDS:
                    try:
                        owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                        await owner.send(
                            "\u26a0\ufe0f **POT server is down**\n"
                            "The PO token server stopped responding. "
                            "YouTube playback may experience 403 errors.\n"
                            "Use the status command to check current state."
                        )
                    except Exception as e:
                        self.logger.debug(f"Failed to DM owner {owner_id} about POT server death: {e}")
            elif transition is False:
                self.logger.info("POT server recovered — health check passing again")

    async def cog_ready(self) -> None:
        """Called after the bot is fully ready. Sets up ambience subscription and loads playlist."""
        # Idempotency guard: Check if we've already initialized
        if self.presence_task is not None and not self.presence_task.done():
            self.logger.info("Music cog already initialized, skipping cog_ready")
            return

        if not YTDLP_AVAILABLE:
            self.logger.warning(
                "yt-dlp is not installed. Music cog will be limited.")
            return

        # Start POT provider initialization without delaying cog readiness.
        self._schedule_pot_startup()

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

        # Load playlist from cache — but only if no user session is already active.
        # A user can initiate playback before cog_ready() fires (during the
        # CONNECT→READY window), so we must not overwrite their state.
        if not self.active_session and not self.playlist:
            await self._load_playlist()
        elif self.active_session:
            self.logger.info(
                "Skipping ambient playlist load — user session already active")

        if self.playlist:
            # Start presence cycling
            self.track_started_at = time.time()
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
        self._cache_init_task = asyncio.create_task(self._background_cache_init())

    async def _background_cache_init(self) -> None:
        """Background task to refresh playlists and start downloads.

        This runs after startup so the bot is responsive immediately.
        """
        try:
            # Give the bot a moment to fully start
            await asyncio.sleep(2.0)

            self.logger.info("[Cache] Starting background playlist refresh...")

            # Refresh all playlists from YouTube
            playlists, old_membership = await self.cache_manager.refresh_all_playlists()

            if playlists:
                # Reconcile downloads (handle orphans)
                await self.cache_manager.reconcile_downloads(playlists, old_membership)

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

        # Stop POT provider server (also cancels health watchdog)
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

        # Stop the player before disconnecting so its generation increment
        # filters out the stale after-callback that disconnect triggers.
        # Without this, _on_track_end races the disconnect and can start
        # yt-dlp resolution during shutdown.
        if self._player:
            self._player.stop()

        # Clear session BEFORE disconnect so _on_track_end's guard sees None.
        session = self.active_session
        self.active_session = None
        if session and session.voice_client:
            await session.voice_client.disconnect()

        # Clear presence (respects visibility setting)
        try:
            await self.bot.change_presence_safe(activity=None)
        except ConnectionError:
            self.logger.debug("Could not clear presence during unload (WS reconnecting)")
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

        tracks = await fetch_playlist_metadata(playlist_url)

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
        """Gets the current playback position in seconds.

        Always uses the audio source's byte-offset position.  This is the
        single source of truth -- it resets correctly on loop-one rewind
        and doesn't drift during pauses or network stalls.

        Returns:
            Elapsed seconds into the current track, or 0.0 if no player.
        """
        if self._playback.paused_at_position is not None:
            return self._playback.paused_at_position
        if self._player:
            return self._player.position
        return 0.0

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

    def _skip_to_different_track(self) -> Optional[Track]:
        """Move to a different track.  Returns None if there isn't one.

        Used by both user skips and failure prompt skips.  Always advances
        forward, overrides loop ONE, and refuses to land on the same track.

        Returns:
            The next track, or None if the playlist has no other track.
        """
        if not self.playlist or len(self.playlist) == 1:
            return None

        self.current_index = (self.current_index + 1) % len(self.playlist)
        self.track_started_at = time.time()
        self._playback.paused_at_position = None
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

        Uses the source archive rewind path so loop one still wraps to the real
        beginning of the track instead of anchoring itself to the resume point.

        Returns:
            True if resume succeeded, False if not paused or failed.
        """
        if not self._player or not self._player.is_paused:
            return False

        # Calculate seek position (rewind 1 second for smooth continuation)
        current_pos = self._player.position
        seek_position = max(0.0, current_pos - 1.0)

        # This should always succeed because pause/resume only rewinds through
        # audio that has already been played and therefore already exists in
        # the source archive.
        if not self._player.rewind(1.0):
            self.logger.error("[Playback] Archive rewind failed during resume", exc_info=False)
            return False

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
                # --- Phase 1: Process ambience switch requests ---
                had_switch, new_url = self._ambience.consume_switch()
                if had_switch:
                    if new_url is None:
                        # Ambience wants us to stop — clear presence first
                        await self.bot.change_presence_safe(activity=None)
                        self.playlist = []
                        self._ambience.confirm_switch(None)
                        self.logger.info("Stopped music per ambience request")
                    elif new_url != self._ambience.current_playlist_url:
                        # Switch to new playlist — confirm only after load
                        await self._load_playlist()
                        if self.playlist:
                            self._ambience.confirm_switch(new_url)
                            self.current_index = 0
                            self.track_started_at = time.time()
                            self.logger.info(
                                "Switched to new playlist from ambience")
                        else:
                            self.logger.warning(
                                "Playlist switch failed (no tracks loaded), retrying next cycle")

                # --- Phase 2: Let ambience cycle mood/activity ---
                maybe_cycle()

                # --- Phase 3: Yield to playback if in VC ---
                if self.active_session:
                    vc = self.active_session.voice_client
                    if not vc or not vc.is_connected():
                        self.logger.warning(
                            "Zombie voice session detected — VC no longer connected."
                        )
                        await self._end_session("Voice connection lost (detected by presence loop).")
                        # Fall through to idle presence cycling
                    else:
                        await asyncio.sleep(5)
                        continue

                # --- Phase 4: Try to acquire a playlist if we don't have one ---
                if not self.playlist:
                    playlist_url = get_current_playlist()
                    if playlist_url and playlist_url != self._ambience.current_playlist_url:
                        await self._load_playlist()
                        if self.playlist:
                            self._ambience.confirm_switch(playlist_url)
                            self.current_index = 0
                            self.track_started_at = time.time()

                # --- Phase 5: Get current track ---
                current_track = self._get_current_track()
                if not current_track:
                    await asyncio.sleep(30)
                    continue

                # --- Phase 6: Check if track has "finished" ---
                elapsed = time.time() - self.track_started_at
                remaining = max(current_track.duration - elapsed, 0)

                if remaining <= 0:
                    # Track "finished", advance (always loop in idle mode)
                    if not self.playlist:
                        await asyncio.sleep(30)
                        continue
                    self.current_index = (
                        self.current_index + 1) % len(self.playlist)
                    self.track_started_at = time.time()
                    continue

                # --- Phase 7: Update presence for current track ---
                presence_activity = discord.Activity(
                    type=discord.ActivityType.listening,
                    name=current_track.title,
                    state=f"by {current_track.artist}"
                )
                await self.bot.change_presence_safe(activity=presence_activity)

                # Sleep until track ends or 30 seconds (handles long tracks)
                await asyncio.sleep(min(remaining, 30))

            except asyncio.CancelledError:
                break
            except ConnectionError:
                self.logger.debug("Presence update skipped (WS reconnecting)")
                await asyncio.sleep(10)
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
        try:
            await self.bot.change_presence_safe(activity=activity)
        except ConnectionError:
            self.logger.debug("Playback presence update skipped (WS reconnecting)")

    # ==========================================================================
    # VOICE PLAYBACK
    # ==========================================================================

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
        return await fetch_url_info(url, force_playlist=force_playlist)

    async def _prefetch_next_track(self) -> None:
        """Speculatively acquire and validate a source for the next track.

        Uses ``_acquire_source`` from the mixin then, for streaming URLs,
        spawns FFmpeg to prebuffer and prove the URL isn't stale.
        """
        if not self.playlist:
            self._clear_prefetch()
            return

        next_track = self._get_next_sequential_track()
        if not next_track or not next_track.video_id:
            self._clear_prefetch()
            return

        # Don't prefetch the track that's already playing.  With a single-track
        # playlist (or when next sequential wraps to the same track), prefetch
        # would burn the direct budget on a track that's already in the player.
        current = self._get_current_track()
        if current and current.video_id == next_track.video_id:
            return

        if (self._prefetched_track and
                self._prefetched_track.video_id == next_track.video_id):
            return  # Already prepared

        # Clear stale prefetch before starting fresh.
        self._prefetched_source = None
        self._prefetched_track = None
        self._prefetched_thumbnail = None

        try:
            # Enrich metadata BEFORE acquisition so thumbnail and artist are
            # correct by the time the now-playing widget renders.
            await self._enrich_track_metadata(next_track)

            # Prefetch is direct-only.  If direct doesn't work, live play
            # handles the residential escalation.  This prevents prefetch
            # from spending money on tracks the user might never reach.
            source = await self._acquire_source(next_track, prefetch=True)
            if not source:
                return

            # Thumbnail for instant now-playing display.
            thumbnail_bytes: Optional[bytes] = None
            try:
                thumbnail_bytes = await get_thumbnail_bytes(next_track, self.cache_manager)
            except Exception:
                pass

            # Validate streaming URLs with prebuffer (catches late YouTube 403s).
            if source.url and not source.local_path:
                from utils.musicutils.audio_source import SeekableAudioSource

                duration = next_track.duration or 0
                target = float(duration) if 0 < duration <= 600 else 60.0

                audio_source = SeekableAudioSource(source.url, http_headers=source.http_headers)
                is_valid = await asyncio.to_thread(audio_source.prebuffer, target, 30.0)

                if is_valid:
                    source.prebuffered = audio_source
                    self.logger.info(
                        f"[Prefetch] Validated URL ({audio_source.buffered_seconds:.1f}s): "
                        f"{next_track.title}"
                    )
                else:
                    audio_source.cleanup()
                    source = None
                    self.logger.info(f"[Prefetch] Prebuffer validation failed: {next_track.title}")

            self._prefetched_source = source
            self._prefetched_track = next_track
            self._prefetched_thumbnail = thumbnail_bytes

            if source:
                self.logger.info(f"[Prefetch] Ready: {next_track.title}")

        except asyncio.CancelledError:
            self.logger.debug("[Prefetch] Task cancelled")
            raise
        except Exception as e:
            self.logger.error(f"[Prefetch] Exception: {e}", exc_info=True)
            self._clear_prefetch()

    def _consume_prefetch(self, track: Track) -> Optional[PlayableSource]:
        """Take the prefetched source if it matches the given track.

        Returns the source and clears prefetch state, or returns None.
        """
        if (self._prefetched_source and
            self._prefetched_track and
                self._prefetched_track.video_id == track.video_id):
            source = self._prefetched_source
            self._prefetched_source = None
            self._prefetched_track = None
            # Keep thumbnail -- _play_with_source may use it.
            self.logger.info(f"[Prefetch] Consumed for: {track.title}")
            return source
        return None

    def _clear_prefetch(self) -> None:
        """Cancel speculative prefetch work and drop stored state."""
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None
        if self._prefetched_source:
            self._prefetched_source.cleanup()
        self._prefetched_source = None
        self._prefetched_track = None
        self._prefetched_thumbnail = None

    def _refresh_prefetch_if_stale(self) -> None:
        """Re-prefetch if a playlist mutation changed the next sequential track."""
        if not self._prefetched_track:
            return

        next_track = self._get_next_sequential_track()
        if next_track and self._prefetched_track.video_id == next_track.video_id:
            return  # Still valid

        self.logger.debug(
            f"[Prefetch] Stale after playlist mutation: "
            f"had {self._prefetched_track.title}, "
            f"next is now {next_track.title if next_track else 'None'}"
        )
        self._clear_prefetch()

        if next_track and self.active_session:
            self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

    def _reset_playback_runtime_state(self) -> None:
        """Reset all session-local playback bookkeeping."""
        self._clear_all_attempts()
        self._clear_prefetch()
        self._playback.clear()
        self.track_started_at = 0.0
        self.logger.debug("[Playback] Cleared session runtime state")

    async def _play_current_track(self) -> None:
        """The play loop.  Acquires a source and hands it to ManagedPlayer.

        Loops on failure (via _on_track_end -> retry -> _play_current_track)
        until acquisition is exhausted, at which point a prompt is shown.
        """
        while True:
            if not self.active_session or not self.active_session.voice_client:
                return

            track = self._get_current_track()
            if not track:
                return

            if not self._player:
                self.logger.error("[PlayTrack] No player available!")
                return

            # Fire enrichment immediately so np can await it while we acquire.
            # Skip if an enrichment for this track is already in flight
            # (e.g. _do_play already started one before _play_current_track ran).
            already_enriching = (
                self._enrichment_task
                and not self._enrichment_task.done()
                and self._enrichment_video_id == track.video_id
            )
            if not already_enriching:
                self._enrichment_video_id = track.video_id
                self._enrichment_task = asyncio.create_task(
                    self._enrich_track_metadata(track)
                )

            # Check prefetch first, fall back to live acquisition.
            source = self._consume_prefetch(track)
            if source is None:
                self.logger.info(f"[Play] Acquiring source for: {track.title}")
                source = await self._acquire_source(track)

            if source is None:
                self.logger.info(f"[Play] No source available for: {track.title}")
                attempts = self._get_attempts(track.video_id) if track.video_id else None
                is_unavailable = attempts.unavailable if attempts else False
                await self._handle_track_failure(
                    track,
                    issue_kind=TrackIssueKind.UNAVAILABLE if is_unavailable else TrackIssueKind.TRANSIENT,
                    timeout_action=TrackFailureAction.REMOVE if is_unavailable else TrackFailureAction.SKIP,
                )
                return

            try:
                await self._play_with_source(track, source)
                return  # _on_track_end handles what comes next
            except discord.ClientException as e:
                self.logger.debug(f"Playback aborted (likely disconnected): {e}")
                source.cleanup()
                return
            except Exception as e:
                self.logger.error(f"Error playing track: {e}", exc_info=True)
                source.cleanup()
                if track.video_id:
                    self._clear_attempts(track.video_id)
                self._advance_track()
                await asyncio.sleep(1)
                continue

    async def _enrich_track_metadata(self, track: Track) -> None:
        """Ensure a track has full YTM metadata (thumbnail, artist, etc.).

        Playlist-imported tracks arrive with only flat extraction data.  This
        calls ``_build_quick_result`` (the same YTM lookup the ambient pipeline
        uses) to fill in square thumbnails, album info, version labels, and
        corrected artist names.  Skipped if the track already has a square
        thumbnail (meaning it was already enriched via search or ambient).
        """
        if track.thumbnail_is_square or not track.video_id:
            return

        from utils.musicutils.search import _build_quick_result

        try:
            result = await _build_quick_result(track.video_id)
            if result.title == 'Unknown' and result.artist == 'Unknown':
                return  # YTM doesn't know this track

            if result.thumbnail_url and result.thumbnail_is_square:
                track.thumbnail = result.thumbnail_url
                track.thumbnail_is_square = True
            if result.artist and result.artist != 'Unknown':
                track.artist = result.artist
            if result.album:
                track.album = result.album
            if result.video_type:
                track.video_type = result.video_type
                track.source = result.source
            if result.version_label and result.version_label != 'Video':
                track.version_label = result.version_label
            if result.is_explicit is not None:
                track.is_explicit = result.is_explicit

            self.logger.info(f"[Metadata] Enriched: {track.title} (square={track.thumbnail_is_square})")
        except Exception as e:
            self.logger.debug(f"[Metadata] Enrichment failed for {track.title}: {e}")

    async def _play_with_source(self, track: Track, source: PlayableSource) -> None:
        """Hand a source to ManagedPlayer and start prefetching the next track."""
        if not self.active_session or not self._player:
            return

        await self._update_playing_presence(track)
        self.track_started_at = time.time()
        self._playback.paused_at_position = None

        # Take ownership of the prebuffered FFmpeg process (if any).
        prebuffered = source.prebuffered
        source.prebuffered = None

        vc = self.active_session.voice_client if self.active_session else None
        if not self.active_session or not vc or not vc.is_connected():
            self.logger.debug("Session ended during track preparation, aborting playback.")
            if prebuffered is not None:
                prebuffered.cleanup()
            return

        try:
            if prebuffered is not None:
                self._player.play(track, source=prebuffered)
                self.logger.debug(f"[Play] Dispatching prebuffered: {track.title}")
            elif source.local_path:
                self._player.play(track, source.local_path)
                self.logger.debug(f"[Play] Dispatching local: {track.title}")
            elif source.url:
                self._player.play(track, source.url, http_headers=source.http_headers)
                self.logger.debug(f"[Play] Dispatching direct: {track.title}")
            else:
                raise ValueError("PlayableSource has no usable playback input")
        except Exception:
            if prebuffered is not None:
                prebuffered.cleanup()
            raise

        # Start prefetching the next track.
        self._clear_prefetch()
        self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

    async def _apply_track_issue_action(self, track: Track, action: TrackFailureAction) -> None:
        """Apply the user's skip/remove choice after a failure prompt."""
        if track.video_id:
            self._clear_attempts(track.video_id)

        if action == TrackFailureAction.SKIP:
            next_track = self._skip_to_different_track()
            if next_track:
                await self._play_current_track()
                return

            self.logger.info("[Play] No alternate track available after skip — waiting for user action.")
            await self._send_system_message(
                "🎵 There's nothing else for me to play right now. "
                "I'll wait here in case you add something else or change the queue."
            )

            if self.active_session:
                self.active_session.waiting_for_users = True
                if self.idle_timeout_task:
                    self.idle_timeout_task.cancel()
                self.idle_timeout_task = asyncio.create_task(
                    self._playlist_end_timeout_loop()
                )
            return

        # REMOVE
        self._remove_track(self.current_index)
        if self.playlist:
            await self._play_current_track()
        else:
            await self._handle_empty_playlist_after_removal()

    async def _handle_track_failure(
        self,
        track: Track,
        *,
        issue_kind: TrackIssueKind = TrackIssueKind.TRANSIENT,
        timeout_action: TrackFailureAction = TrackFailureAction.REMOVE,
    ) -> None:
        """Show an interactive skip/remove prompt for a failed track."""
        if not self.active_session:
            return

        self.logger.info(
            f"[Play] Prompting track issue for {track.title}: "
            f"kind={issue_kind.value} | default={timeout_action.name}"
        )

        origin = self.bot.get_channel(self.active_session.origin_channel_id)
        if not origin or not isinstance(origin, discord.abc.Messageable):
            self.logger.warning(
                f"No origin channel for failure notification, auto-applying {timeout_action.name} for {track.title}"
            )
            await self._apply_track_issue_action(track, timeout_action)
            return

        # Notify voice channel if different from origin.
        if self.active_session.channel_id != self.active_session.origin_channel_id:
            vc_channel = self.bot.get_channel(self.active_session.channel_id)
            if vc_channel and isinstance(vc_channel, discord.abc.Messageable):
                try:
                    await vc_channel.send(
                        f"⚠️ Having trouble with **{track.title}**! "
                        f"Head over to <#{self.active_session.origin_channel_id}> to let me know what to do~"
                    )
                except discord.HTTPException as e:
                    self.logger.debug(f"Failed to send track failure notice to VC channel: {e}")

        action = await show_track_failed(
            origin,
            track.title,
            track.url,
            issue_kind=issue_kind,
            timeout_action=timeout_action,
        )
        self.logger.info(f"[Play] Track failure action: {action.name} for '{track.title}'")
        await self._apply_track_issue_action(track, action)

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

    def _on_player_track_end(self, report: PlaybackEndReport) -> None:
        """Callback from ManagedPlayer when track naturally ends or errors.

        This is the ONLY entry point for track-end handling. ManagedPlayer
        guarantees this is NOT called for intentional stops (skip, pause, etc.).

        Args:
            report: Typed playback report from ManagedPlayer.
        """
        if not self.active_session:
            return

        # ManagedPlayer already reduced the raw FFmpeg/session outcome into a
        # typed report. Keep the thread-hop here minimal and let the async
        # handler decide whether this track should retry, be removed, or fail.
        # Dispatch to the main handler on the event loop.
        asyncio.run_coroutine_threadsafe(
            self._on_track_end(report),
            self.bot.loop
        )

    async def _on_track_end(self, report: PlaybackEndReport) -> None:
        """Handle track end using ``classify_failure`` from the acquisition mixin."""
        if not self.active_session:
            return

        track = self._get_current_track()
        if not track:
            return

        action, issue_kind = classify_failure(report)
        self.logger.info(
            f"[Play] Track ended: {track.title} | "
            f"action={action.name} | elapsed={report.elapsed:.1f}s"
        )

        if action == FailureAction.RETRY:
            # Clean up broken residential files before retrying.
            if track.video_id:
                attempts = self._get_attempts(track.video_id)
                if attempts.last_residential_path:
                    self._delete_failed_residential_file(attempts.last_residential_path)
                    attempts.last_residential_path = None

            if report.ffmpeg.response_action == FFmpegResponseAction.RETRY_WITH_BACKOFF:
                self.logger.info(f"[Retry] Backing off 2s before retrying {track.title}")
                await asyncio.sleep(2.0)

            await self._play_current_track()
            return

        if action in (FailureAction.PROMPT_SKIP, FailureAction.PROMPT_REMOVE):
            timeout_action = (
                TrackFailureAction.REMOVE if action == FailureAction.PROMPT_REMOVE
                else TrackFailureAction.SKIP
            )
            await self._handle_track_failure(
                track, issue_kind=issue_kind, timeout_action=timeout_action,
            )
            return

        # Normal track end (FailureAction.DONE) - clean up per-track state.
        if track.video_id:
            self._clear_attempts(track.video_id)
        self.logger.info(f"[Play] Track finished: {track.title} — {track.artist}")

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

        Errors are handled internally -- if the connection fails, an
        in-character error message is sent to the user and the method
        returns without raising.  Callers should check
        ``self.active_session`` after the call to know if it succeeded.

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
            self._player.set_repeat_one(self.loop_mode == LoopMode.ONE)

            self.logger.info(f"Started voice session in {channel.name} (guild={channel.guild.id}, channel={channel.id})")

            # Start playback from current track
            await self._play_current_track()

            message = join_message or f"🎵 Now playing in {channel.mention}!"
            await ctx.send(message)

        except discord.ClientException as e:
            bot_name = self.bot.user.display_name if self.bot.user else "I"
            self.logger.error(f"Failed to connect to voice: {e}")
            await ctx.send(f"{bot_name} is a little confused, can you contact her author?")
        except Exception as e:
            self.logger.error(f"Error starting session: {e}", exc_info=True)
            await ctx.send("Something went wrong starting playback.")

    async def _end_session(self, reason: str = "Session ended.") -> None:
        """End the current voice session.

        Always follows the same sequence regardless of why we're leaving:
        1. Stop the player (silence audio immediately)
        2. Say goodbye (session is still alive, we know where to send)
        3. Disconnect the voice client (leave the VC)
        4. Clean up session state and restore idle

        Step 2 and 3 are best-effort -- if the VC is already dead or
        Discord's API is down, we log and continue with cleanup.
        """
        if not self.active_session:
            return

        # 1. Stop playback (no callback triggered)
        if self._player:
            self._player.stop()
            self._player = None

        # 2. Say goodbye while we still have session channel references
        try:
            await self._send_system_message(f"🎵 {reason}")
        except Exception as e:
            self.logger.debug(f"Could not send goodbye message: {e}")

        # 3. Disconnect voice client.  force=True ensures discord.py's
        #    auto-reconnect is stopped even if the connection is mid-reconnect.
        vc = self.active_session.voice_client
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                self.logger.debug(f"Could not disconnect voice client: {e}")

        # 4. Clean up session state
        self.active_session = None

        if self.idle_timeout_task:
            self.idle_timeout_task.cancel()
            self.idle_timeout_task = None

        self._reset_playback_runtime_state()

        await self._restore_idle_playlist()

        self._playlist_modified_during_session = False

        self.logger.info(f"[Session] Ended: {reason}")

    async def _handle_voice_disconnect(self) -> None:
        """Handle a voice disconnect with a grace period for reconnection.

        Pauses playback and waits 5 seconds for discord.py's auto-reconnect.
        If the connection recovers, resumes playback.  If not, ends the session.
        """
        if not self.active_session:
            return

        self.logger.warning("[Session] Voice connection dropped — pausing and waiting for reconnect...")

        # Pause immediately to preserve position
        if self._player and self._player.is_playing:
            self._player.pause()

        # Let users know what's happening
        bot_name = self.bot.user.display_name if self.bot.user else "I"
        try:
            await self._send_system_message(
                f"⚠️ {bot_name} is having a little difficulty, please wait!~"
            )
        except Exception:
            pass  # Can't send -- connection might be too broken

        await asyncio.sleep(5.0)

        # Check if we reconnected during the wait
        vc = self.active_session.voice_client if self.active_session else None
        if vc and vc.is_connected():
            self.logger.info("[Session] Voice reconnected, resuming playback.")
            if self._player and self._player.is_paused:
                self._player.resume()
        else:
            self.logger.warning("[Session] Voice did not reconnect, ending session.")
            await self._end_session("Discord seems a little unstable today, I'll have to leave the deck for now!")

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
    # RECONNECT RECOVERY
    # ==========================================================================

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        """Re-push presence after WebSocket reconnect."""
        track = self._get_current_track()
        if not track:
            return

        presence_activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=track.title,
            state=f"by {track.artist}"
        )
        try:
            await self.bot.change_presence_safe(activity=presence_activity)
        except ConnectionError:
            pass  # WS still settling, loop will catch up

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

        # Detect bot's own voice state changes
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                # Guard: if we're already handling a reconnect attempt, ignore
                # rapid-fire disconnect events.
                if self._reconnect_task and not self._reconnect_task.done():
                    self.logger.debug("[Session] Reconnect already in progress, ignoring duplicate disconnect.")
                    return

                self._reconnect_task = asyncio.create_task(
                    self._handle_voice_disconnect()
                )

            elif not before.channel and after.channel and self.active_session:
                self.logger.info(f"[Session] Bot rejoined voice: {after.channel.name}")

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
    # - Could reuse the current coordinator activation path with a forced restart

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

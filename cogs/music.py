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
import json
import os
import random
import re
import time
from typing import cast, Dict, List, Optional, TYPE_CHECKING

import discord
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.database import DatabaseManager
from utils.music_helpers import (
    YTDLP_AVAILABLE,
    FFMPEG_OPTIONS,
    get_ffmpeg_path,
    LoopMode,
    Track,
    LyricsResult,
    ActiveSession,
    LyricalNonsenseScraper,
    LRCLIBProvider,
    GeniusScraper,
    get_audio_url,
    search_youtube,
    fetch_url_info,
    fetch_playlist_metadata,
    chunk_text,
)

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


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
        self.original_playlist: List[Track] = []  # Unshuffled copy
        self.current_index: int = 0
        self.loop_mode: LoopMode = LoopMode.ALL

        # Presence cycling state (idle mode)
        self.track_started_at: float = time.time()
        self.presence_task: Optional[asyncio.Task[None]] = None

        # Voice session state
        self.active_session: Optional[ActiveSession] = None
        self.playback_task: Optional[asyncio.Task[None]] = None
        self.idle_timeout_task: Optional[asyncio.Task[None]] = None

        # Pre-buffering: cache the next track's audio URL for smoother transitions
        self._prefetched_url: Optional[str] = None
        self._prefetched_track_url: Optional[str] = None  # Track URL this prefetch is for
        self._prefetch_task: Optional[asyncio.Task[None]] = None

        # Internal flag: when True the next track-end callback will NOT advance
        # the playlist (used for intentional stops like jump/skip that manually
        # set `current_index`). The flag is consumed by `_on_track_end`.
        self._suppress_next_track_end: bool = False

        # Tracks if the playlist was modified during a voice session.
        # Set True when tracks are added/removed. Used to decide whether to
        # reset to the cached playlist when returning to idle.
        self._playlist_modified_during_session: bool = False

        # Cache path
        self.cache_path = config.MUSIC_CACHE_PATH

    async def cog_ready(self) -> None:
        """Called after the bot is fully ready. Loads playlist and starts presence cycling."""
        if not YTDLP_AVAILABLE:
            self.logger.warning("yt-dlp is not installed. Music cog will be limited.")
            return

        if not config.YOUTUBE_PLAYLIST_URL:
            self.logger.info("No YOUTUBE_PLAYLIST_URL configured. Music cog idle.")
            return

        # Ensure cache directory exists
        os.makedirs(self.cache_path, exist_ok=True)

        # Load or fetch playlist
        await self._load_playlist()

        if self.playlist:
            # Start presence cycling
            self.presence_task = self.bot.loop.create_task(self._presence_loop())
            self.logger.info(f"Music cog ready with {len(self.playlist)} tracks.")
        else:
            self.logger.warning("No tracks loaded. Music cog will not cycle presence.")

    async def cog_unload(self) -> None:
        """Cleanup when cog is unloaded."""
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
        if self._prefetch_task:
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
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
        """Loads playlist from cache or fetches from YouTube."""
        cache_file = os.path.join(self.cache_path, 'playlist.json')

        # Try loading from cache first
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    cached_url = data.get('playlist_url', '')
                    cache_time = data.get('cached_at', 0)

                    # Invalidate cache if playlist URL changed
                    if cached_url != config.YOUTUBE_PLAYLIST_URL:
                        self.logger.info("Playlist URL changed, invalidating cache.")
                    # Refresh if cache is older than 24 hours
                    elif time.time() - cache_time < 86400:
                        self.original_playlist = [Track.from_dict(t) for t in data.get('tracks', [])]
                        if self.original_playlist:
                            self.logger.info(f"Loaded {len(self.original_playlist)} tracks from cache.")
                            # Copy to playlist and shuffle for initial playback
                            self.playlist = self.original_playlist.copy()
                            self._apply_shuffle()
                            return
            except (json.JSONDecodeError, KeyError) as e:
                self.logger.warning(f"Cache corrupted, will re-fetch: {e}")

        # Fetch from YouTube
        await self._fetch_playlist()
        # Copy to playlist and shuffle for initial playback
        self.playlist = self.original_playlist.copy()
        self._apply_shuffle()

    async def _fetch_playlist(self) -> None:
        """Fetches playlist metadata from YouTube using yt-dlp."""
        if not config.YOUTUBE_PLAYLIST_URL or not YTDLP_AVAILABLE:
            return

        self.logger.info("Fetching playlist from YouTube...")

        tracks = await fetch_playlist_metadata(config.YOUTUBE_PLAYLIST_URL, self.logger)

        if tracks:
            self.original_playlist = tracks
            self.logger.info(f"Fetched {len(tracks)} tracks from playlist.")

            # Save to cache
            await self._save_playlist_cache()
        else:
            self.logger.error("Failed to fetch playlist or playlist is empty.")

    async def _save_playlist_cache(self) -> None:
        """Saves playlist to cache file."""
        cache_file = os.path.join(self.cache_path, 'playlist.json')
        try:
            data = {
                'tracks': [t.to_dict() for t in self.original_playlist],
                'cached_at': time.time(),
                'playlist_url': config.YOUTUBE_PLAYLIST_URL
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save playlist cache: {e}")

    def _apply_shuffle(self, preserve_current: bool = False) -> None:
        """Shuffles the playlist.

        Args:
            preserve_current: If True, keeps the current track at index 0
                after shuffling so playback continues from the same song.
        """
        current = self._get_current_track() if preserve_current else None

        random.shuffle(self.playlist)

        # Move current track to front if requested
        if current and preserve_current:
            try:
                self.current_index = self.playlist.index(current)
            except ValueError:
                self.current_index = 0
        else:
            self.current_index = 0

    def _get_current_track(self) -> Optional[Track]:
        """Gets the current track."""
        if not self.playlist:
            return None
        return self.playlist[self.current_index % len(self.playlist)]

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
        - ALL: Loop entire playlist (reshuffle if shuffle enabled)
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
        return self._get_current_track()

    def _remove_track(self, index: int) -> None:
        """Removes a track from the playlist by index.

        Adjusts current_index appropriately to maintain playback position.
        Also removes from original_playlist so the change persists to cache.

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

        # Also remove from original_playlist so it persists to cache
        # Match by URL since order may differ between shuffled and original
        self.original_playlist = [t for t in self.original_playlist if t.url != removed_track.url]

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
            self.logger.info("Returning to idle - playlist unchanged, keeping position.")
            return

        # Playlist was modified - reload from cache
        self.logger.info("Returning to idle - playlist was modified, reloading from cache.")
        await self._load_playlist()  # Reloads original_playlist and applies shuffle

        # Try to find the current track in the reloaded playlist
        if current_track:
            for i, track in enumerate(self.playlist):
                if track.url == current_track.url:
                    self.current_index = i
                    self.logger.info(f"Found current track in reloaded playlist: {track.title}")
                    break
            else:
                # Track not found, start from beginning
                self.current_index = 0
                self.logger.info("Current track not in reloaded playlist, starting from beginning.")
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
                # Don't update presence while in VC - playback handles that
                if self.active_session:
                    await asyncio.sleep(5)
                    continue

                current_track = self._get_current_track()
                if not current_track:
                    await asyncio.sleep(30)
                    continue

                # Update presence
                activity = discord.Activity(
                    type=discord.ActivityType.listening,
                    name=f"{current_track.title} - {current_track.artist}"
                )
                await self.bot.change_presence(activity=activity)

                # Calculate remaining time for current track
                elapsed = time.time() - self.track_started_at
                remaining = max(current_track.duration - elapsed, 0)

                if remaining <= 0:
                    # Track "finished", advance
                    self._advance_track()
                    continue

                # Sleep until track "ends" or 30 seconds, whichever is shorter
                # (To handle very long tracks gracefully)
                await asyncio.sleep(min(remaining, 30))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in presence loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _update_playing_presence(self, track: Track) -> None:
        """Updates presence while actively playing."""
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{track.title} - {track.artist}"
        )
        await self.bot.change_presence(activity=activity)

    # ==========================================================================
    # VOICE PLAYBACK
    # ==========================================================================

    async def _get_audio_url(self, track: Track) -> tuple[Optional[str], bool]:
        """Gets the actual streamable audio URL for a track.

        Args:
            track: The track to get the audio URL for.

        Returns:
            A tuple of (url, is_unavailable) where:
            - url: The streamable URL, or None if failed
            - is_unavailable: True if the video is permanently unavailable and should be removed
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

    async def _fetch_url_info(self, url: str) -> tuple[List[Track], Optional[str]]:
        """Fetches track info from a YouTube URL (video or playlist).

        Args:
            url: The YouTube URL to fetch.

        Returns:
            A tuple of (tracks, error_message) where:
            - tracks: List of Track objects (single for video, multiple for playlist)
            - error_message: Human-readable error if failed, None if success
        """
        return await fetch_url_info(url, self.logger)

    async def _prefetch_next_track(self) -> None:
        """Pre-fetches the audio URL for the next track in the background.

        This runs while the current track is playing, so when it ends,
        we already have the URL ready and can start playback immediately.

        Note: This intentionally ignores loop mode and always prefetches the
        next sequential track. Even if loop is OFF, the user might enable it,
        skip manually, or the prefetch is simply discarded - better to have it
        ready than delay playback.
        """
        if not self.playlist:
            self.logger.debug("[Prefetch] No playlist, clearing prefetch cache")
            self._prefetched_url = None
            self._prefetched_track_url = None
            return

        # Always get the next sequential track, ignoring loop mode
        next_index = (self.current_index + 1) % len(self.playlist)
        next_track = self.playlist[next_index]
        self.logger.debug(f"[Prefetch] Current index: {self.current_index}, next index: {next_index}, playlist size: {len(self.playlist)}")

        # Don't refetch if we already have this track prefetched
        if self._prefetched_track_url == next_track.url and self._prefetched_url:
            self.logger.debug(f"[Prefetch] Already prefetched: {next_track.title}")
            return

        try:
            self.logger.debug(f"[Prefetch] Starting prefetch for: {next_track.title} ({next_track.url})")
            audio_url, is_unavailable = await self._get_audio_url(next_track)

            if audio_url:
                self._prefetched_url = audio_url
                self._prefetched_track_url = next_track.url
                self.logger.debug(f"[Prefetch] Success: {next_track.title} - URL length: {len(audio_url)}")
            else:
                # Clear prefetch cache on failure
                self._prefetched_url = None
                self._prefetched_track_url = None
                if is_unavailable:
                    self.logger.debug(f"[Prefetch] Track unavailable: {next_track.title}")
                else:
                    self.logger.debug(f"[Prefetch] Failed (no URL returned): {next_track.title}")
        except Exception as e:
            self.logger.debug(f"[Prefetch] Exception for {next_track.title}: {e}")
            self._prefetched_url = None
            self._prefetched_track_url = None

    def _clear_prefetch(self) -> None:
        """Clears prefetch cache and cancels any pending prefetch task."""
        self.logger.debug("[Prefetch] Clearing prefetch cache")
        self._prefetched_url = None
        self._prefetched_track_url = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            self._prefetch_task = None

    async def _play_current_track(self) -> None:
        """Plays the current track in the active voice session."""
        if not self.active_session or not self.active_session.voice_client:
            return

        track = self._get_current_track()
        if not track:
            return

        vc = self.active_session.voice_client

        # Stop any current playback
        if vc.is_playing():
            vc.stop()

        # Check if we have a prefetched URL for this track
        audio_url: Optional[str] = None
        is_unavailable = False

        if self._prefetched_track_url == track.url and self._prefetched_url:
            audio_url = self._prefetched_url
            self.logger.debug(f"Using prefetched URL for: {track.title}")
            # Clear the prefetch since we're using it
            self._prefetched_url = None
            self._prefetched_track_url = None
        else:
            # No prefetch available, fetch now
            audio_url, is_unavailable = await self._get_audio_url(track)

        if not audio_url:
            if is_unavailable:
                # Remove unavailable track from playlist
                self.logger.info(f"Removing unavailable track: {track.title}")
                self._remove_track(self.current_index)
                # Save updated playlist to cache
                await self._save_playlist_cache()
            else:
                # Temporary error, just skip
                self.logger.warning(f"Could not get audio URL for {track.title}, skipping...")
                self._advance_track()
            await self._play_current_track()
            return

        # Update presence
        await self._update_playing_presence(track)
        self.track_started_at = time.time()

        # Create audio source and play
        try:
            # Re-check connection state after async work (race condition guard)
            if not self.active_session or not vc.is_connected():
                self.logger.debug("Session ended during track preparation, aborting playback.")
                return

            ffmpeg_path = get_ffmpeg_path()
            source = discord.FFmpegPCMAudio(
                audio_url,
                executable=ffmpeg_path,
                before_options=FFMPEG_OPTIONS['before_options'],
                options=FFMPEG_OPTIONS['options']
            )

            def after_playing(error: Optional[Exception]) -> None:
                """Callback invoked by discord.py when the audio source finishes or errors.

                Runs in a separate thread, so we use run_coroutine_threadsafe to
                schedule the async _on_track_end on the bot's event loop.
                """
                if error:
                    self.logger.error(f"Playback error: {error}")
                if self.active_session:
                    asyncio.run_coroutine_threadsafe(
                        self._on_track_end(),
                        self.bot.loop
                    )

            vc.play(source, after=after_playing)
            self.logger.info(f"Now playing: {track.title}")

            # Start prefetching the next track in the background
            self._prefetch_task = asyncio.create_task(self._prefetch_next_track())

        except discord.ClientException as e:
            # Expected if disconnected during preparation - not an error
            self.logger.debug(f"Playback aborted (likely disconnected): {e}")
        except Exception as e:
            self.logger.error(f"Error playing track: {e}", exc_info=True)
            # Try next track
            self._advance_track()
            await asyncio.sleep(1)
            await self._play_current_track()

    async def _on_track_end(self) -> None:
        """Called when a track finishes playing."""
        if not self.active_session:
            return

        # Check if session has exceeded 8 hours
        session_duration = time.time() - self.active_session.started_at
        max_session_duration = 8 * 60 * 60  # 8 hours in seconds
        if session_duration >= max_session_duration:
            hours = int(session_duration // 3600)
            await self._end_session(f"Session ended after {hours} hours. Take a break! 🎧")
            return

        # When True, the next _on_track_end call will NOT advance the playlist.
        # Used by _do_jump to prevent double-advancement when stopping playback.
        # Automatically cleared after being checked.
        if getattr(self, '_suppress_next_track_end', False):
            self._suppress_next_track_end = False
            # Play the track at the current index (do not advance)
            await self._play_current_track()
            return

        next_track = self._advance_track()
        if next_track:
            await self._play_current_track()
        else:
            # Playlist ended and loop is disabled
            await self._end_session("Playlist finished!")

    async def _start_session(self, channel: discord.VoiceChannel, ctx: commands.Context) -> None:
        """Starts a new voice session."""
        try:
            vc = await channel.connect()
            self.active_session = ActiveSession(
                guild_id=channel.guild.id,
                channel_id=channel.id,
                voice_client=vc
            )

            # Start playback from current track
            await self._play_current_track()

            await ctx.send(f"🎵 Now playing in {channel.mention}!")

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

        # Stop playback
        if vc.is_playing():
            vc.stop()

        # Disconnect
        await vc.disconnect()

        # Try to notify the channel
        try:
            channel = self.bot.get_channel(self.active_session.channel_id)
            if channel and isinstance(channel, discord.abc.Messageable):
                await channel.send(f"🎵 {reason}")
        except Exception:
            pass

        self.active_session = None

        # Cancel idle timeout if running
        if self.idle_timeout_task:
            self.idle_timeout_task.cancel()
            self.idle_timeout_task = None

        # Clear prefetch cache
        self._clear_prefetch()

        # Restore playlist state for idle mode
        await self._restore_idle_playlist()

        # Reset modification flag for next session
        self._playlist_modified_during_session = False

        self.logger.info(f"Voice session ended: {reason}")

    async def _idle_timeout_loop(self, text_channel: discord.abc.Messageable) -> None:
        """Waits for users to join, disconnects if none do within timeout."""
        try:
            await asyncio.sleep(300)  # 5 minutes

            if self.active_session and self.active_session.waiting_for_users:
                # Check if anyone joined
                vc = self.active_session.voice_client
                if vc and len(vc.channel.members) <= 1:  # Just the bot
                    await text_channel.send("No one joined, so I'm heading out! Type 'listen along' when you're ready.")
                    await self._end_session("No one joined within 5 minutes.")

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

        if not self.playlist:
            await ctx.send("I don't have any music loaded! Make sure `YOUTUBE_PLAYLIST_URL` is configured.")
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
                            self._idle_timeout_loop(ctx.channel)  # type: ignore
                        )
                        return

            await ctx.send("Join a voice channel first, or ask an admin to set a music channel!")
            return

        # Join user's channel
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.send("I can only join regular voice channels, not stage channels.")
            return

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
            # Fetch directly from URL - no substitutions
            await ctx.send("🔍 Fetching track info...")

            tracks, error = await self._fetch_url_info(query)
            if error:
                await ctx.send(f"❌ {error}")
                return

            tracks_to_add = tracks

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

        # Mark all tracks as user-added (should already be, but ensure it)
        for track in tracks_to_add:
            track.user_added = True

        # If not in a voice session, join the user's VC and start fresh
        if not self.active_session:
            # Get the member's voice channel
            member = ctx.author if isinstance(ctx.author, discord.Member) else None
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
                    voice_client=vc
                )

                await self._play_current_track()

                if len(tracks_to_add) == 1:
                    await ctx.send(f"🎵 Now playing **{tracks_to_add[0].title}** in {channel.mention}!")
                else:
                    await ctx.send(f"🎵 Now playing **{len(tracks_to_add)} tracks** in {channel.mention}!")

            except discord.ClientException as e:
                self.logger.error(f"Failed to connect to voice: {e}")
                await ctx.send("I couldn't connect to the voice channel. Please try again.")
            except Exception as e:
                self.logger.error(f"Error starting session: {e}", exc_info=True)
                await ctx.send("Something went wrong starting playback.")

        else:
            # Already in a session - append to playlist
            if ctx.guild and self.active_session.guild_id != ctx.guild.id:
                await ctx.send("I'm currently playing in another server!")
                return

            # Add tracks at the end of the playlist
            self.playlist.extend(tracks_to_add)

            # Mark as modified since we added user tracks
            self._playlist_modified_during_session = True

            # Clear prefetch since playlist changed
            self._clear_prefetch()

            if len(tracks_to_add) == 1:
                await ctx.send(f"⭐ Added **{tracks_to_add[0].title}** to the queue!")
            else:
                await ctx.send(f"⭐ Added **{len(tracks_to_add)} tracks** to the queue!")

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
                    lines.append(f"▶️ **{track_num}. {star}{track.title}** - {track.artist}")
                else:
                    lines.append(f"{track_num}. {star}{track.title} - {track.artist}")

            embed = discord.Embed(
                title="🎶 Playlist",
                description="\n".join(lines),
                color=discord.Color.blue()
            )

            # Add now playing info in the author field
            if current:
                embed.set_author(name=f"Now Playing: {current.title} - {current.artist}")

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

        # Clear prefetch since we jumped to a different position
        self._clear_prefetch()

        track = self._get_current_track()

        if track:
            await ctx.send(f"⏭️ Jumped to **#{position}**: {track.title} - {track.artist}")

            # If playing, stop current track - the `after` callback will trigger _on_track_end
            # which calls _play_current_track with our new index
            if self.active_session:
                vc = self.active_session.voice_client
                if vc.is_playing():
                    # Prevent the normal end-callback from advancing the index
                    self._suppress_next_track_end = True
                    vc.stop()
                else:
                    # If we're connected but not playing, start playback immediately
                    await self._play_current_track()

    async def _do_lyrics(self, ctx: commands.Context, query: Optional[str] = None) -> None:
        """Internal implementation for lyrics search.

        Searches multiple providers for lyrics, presents options to the user,
        and displays the selected lyrics with translation if available.

        Args:
            ctx: The command context.
            query: Search query. If None, uses current playing track.
        """
        from utils.views import get_selection, PaginatorView

        # Determine search query - prioritize title over full "artist - title" string
        artist_hint: Optional[str] = None
        if not query:
            # Use currently playing track
            current = self._get_current_track()
            if current:
                # Use just the title for search, keep artist as hint for filtering
                query = current.title
                artist_hint = current.artist
            else:
                await ctx.send("🎵 No song is currently playing. Please provide a search query!\n"
                               "Example: 'lyrics [song name]'")
                return

        # Send initial searching message
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
            filtered = [r for r in all_results if artist_lower in r.artist.lower() or r.artist.lower() in artist_lower]
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
            embed.set_footer(text="🌐 = Translation available • Select within 30s")

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
            embed.set_footer(text=f"Source: {selected.source} • Page {i + 1}/{len(lyrics_chunks)}")
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
                embed.set_footer(text=f"Source: {selected.source} • Translation {i + 1}/{len(trans_chunks)}")
                pages.append(embed)

        # Send lyrics
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PaginatorView(ctx, pages)
            msg = await ctx.send(embed=pages[0], view=view)
            view.message = msg

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

    async def skip_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for skip requests."""
        if not self.active_session:
            await ctx.send("I'm not playing anything right now!")
            return

        if ctx.guild and self.active_session.guild_id != ctx.guild.id:
            await ctx.send("I'm not playing music in this server!")
            return

        vc = self.active_session.voice_client
        if vc.is_playing():
            vc.stop()
            await ctx.send("⏭️ Skipped!")
        else:
            await ctx.send("Nothing is playing right now.")

    async def pause_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for pause requests."""
        if not self.active_session:
            await ctx.send("I'm not playing anything right now!")
            return

        if ctx.guild and self.active_session.guild_id != ctx.guild.id:
            await ctx.send("I'm not playing music in this server!")
            return

        vc = self.active_session.voice_client
        if vc.is_paused():
            await ctx.send("Already paused! Type 'resume' to continue!")
            return

        if vc.is_playing():
            vc.pause()
            await ctx.send("⏸️ Paused!")
        else:
            await ctx.send("Nothing is playing right now.")

    async def resume_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for resume/play requests.

        Note: This is only triggered when 'play' has no text after it,
        otherwise it would be interpreted as a song request.
        The NLP pattern matching in config.py handles this distinction.
        """
        if not self.active_session:
            await ctx.send("I'm not in a voice channel! Type 'listen along' to start.")
            return

        if ctx.guild and self.active_session.guild_id != ctx.guild.id:
            await ctx.send("I'm not playing music in this server!")
            return

        vc = self.active_session.voice_client
        if vc.is_playing():
            await ctx.send("Already playing!")
            return

        if vc.is_paused():
            vc.resume()
            await ctx.send("▶️ Resumed!")
        else:
            await ctx.send("Nothing to resume. Type 'listen along' to start playback!")

    async def play_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for play/queue requests with a song/URL.

        Extracts the song query from the message, removing the 'play' or 'queue' keyword.
        """
        import re
        # Remove 'play' or 'queue' keyword and any leading/trailing whitespace
        # The query might be "play something", "queue something", or URLs
        song_query = re.sub(r'^\s*(play|queue)\s+', '', query, flags=re.IGNORECASE).strip()

        if not song_query:
            # No query provided, treat as resume - inline the resume logic
            if not self.active_session:
                await ctx.send("I'm not in a voice channel! Type 'listen along' to start.")
                return

            if ctx.guild and self.active_session.guild_id != ctx.guild.id:
                await ctx.send("I'm not playing music in this server!")
                return

            vc = self.active_session.voice_client
            if vc.is_playing():
                await ctx.send("Already playing!")
                return

            if vc.is_paused():
                vc.resume()
                await ctx.send("▶️ Resumed!")
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

        elapsed = int(time.time() - self.track_started_at)
        elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        duration_str = f"{track.duration // 60}:{track.duration % 60:02d}"

        embed = discord.Embed(
            title="🎵 Now Playing" if self.active_session else "🎧 Currently Listening To",
            description=f"**{track.title}**\nby {track.artist}",
            color=discord.Color.purple()
        )
        embed.add_field(name="Duration", value=f"{elapsed_str} / {duration_str}", inline=True)
        embed.add_field(name="Loop", value=self.loop_mode.display, inline=True)

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        if self.active_session:
            embed.set_footer(text=f"Playing in voice | {len(self.playlist)} tracks in playlist")
        else:
            embed.set_footer(text="Idle mode | Type 'listen along' to play in voice!")

        await ctx.send(embed=embed)

    async def queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for queue requests."""
        await self._do_queue(ctx)

    async def shuffle_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for shuffle toggle requests."""
        if not self.playlist:
            await ctx.send("No playlist to shuffle.")
            return

        self._apply_shuffle(preserve_current=True)
        # Clear prefetch since playlist order changed
        self._clear_prefetch()
        # Mark as modified so idle mode reloads the preset playlist
        if self.active_session:
            self._playlist_modified_during_session = True
        await ctx.send("🔀 Playlist shuffled!")

    async def jump_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for jump requests.

        Parses the query for a number to jump to.
        """
        import re
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
            await ctx.send(
                f"{self.loop_mode.emoji} Current loop mode: **{self.loop_mode.display}**\n"
                "Usage: 'loop one' (repeat track) or 'loop all' (repeat playlist)"
            )
            return

        if mode == LoopMode.OFF:
            await ctx.send(
                "➡️ Loop **Off** mode isn't available yet - the music player isn't fully implemented!\n"
                "For now, use **One** (repeat current track) or **All** (repeat playlist)."
            )
            return

        self.loop_mode = mode
        await ctx.send(f"{self.loop_mode.emoji} Loop mode: **{self.loop_mode.display}**")

    async def leave_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for leave/disconnect requests."""
        if not self.active_session:
            await ctx.send("I'm not in a voice channel!")
            return

        if ctx.guild and self.active_session.guild_id != ctx.guild.id:
            await ctx.send("I'm not playing music in this server!")
            return

        await self._end_session("Disconnected by user request.")
        await ctx.send("👋 Disconnected!")

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


async def setup(bot: 'CoreBot') -> None:
    """Sets up the Music cog.

    Raises:
        commands.ExtensionFailed: If YOUTUBE_PLAYLIST_URL is not configured.
    """
    if not config.YOUTUBE_PLAYLIST_URL:
        raise commands.ExtensionFailed(
            'cogs.music',
            RuntimeError("YOUTUBE_PLAYLIST_URL not configured - Music cog disabled.")
        )

    await bot.add_cog(Music(bot))

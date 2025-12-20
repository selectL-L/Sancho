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
import io
import json
import os
import random
import re
import time
from typing import TYPE_CHECKING, Dict, List, Optional, cast

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
from utils.music_helpers import (
    FFMPEG_OPTIONS,
    YTDLP_AVAILABLE,
    ActiveSession,
    GeniusScraper,
    LoopMode,
    LRCLIBProvider,
    LyricalNonsenseScraper,
    LyricsResult,
    Track,
    chunk_text,
    crop_thumbnail_to_square,
    fetch_playlist_metadata,
    fetch_url_info,
    get_audio_url,
    get_ffmpeg_path,
    search_youtube,
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
        # Track URL this prefetch is for
        self._prefetched_track_url: Optional[str] = None
        self._prefetch_task: Optional[asyncio.Task[None]] = None

        # Current track cache: stores the audio URL of the currently/last played track
        # Used for loop ONE mode to avoid re-fetching the same track
        self._current_audio_url: Optional[str] = None
        self._current_audio_track_url: Optional[str] = None

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

        # Track current playlist URL to detect ambience-driven changes
        self._current_playlist_url: Optional[str] = None

        # Flag for pending playlist switch from ambience
        self._pending_playlist_url: Optional[str] = None
        self._pending_playlist_switch: bool = False

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
            self._pending_playlist_url = None
            self._pending_playlist_switch = True
            self.logger.info("Ambience requested music stop")
        elif playlist_url != self._current_playlist_url:
            # Ambience wants a different playlist
            self._pending_playlist_url = playlist_url
            self._pending_playlist_switch = True
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

    async def cog_ready(self) -> None:
        """Called after the bot is fully ready. Sets up ambience subscription and loads playlist."""
        if not YTDLP_AVAILABLE:
            self.logger.warning(
                "yt-dlp is not installed. Music cog will be limited.")
            return

        # Ensure cache directory exists
        os.makedirs(self.cache_path, exist_ok=True)

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
            return

        self._current_playlist_url = playlist_url
        self.logger.info(
            f"Ambience selected playlist ({description}): {playlist_url}")

        # Load or fetch playlist
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

    async def cog_unload(self) -> None:
        """Cleanup when cog is unloaded."""
        # Unsubscribe from ambience
        unsubscribe_playlist_change(self._on_playlist_change)

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
        playlist_url = self._current_playlist_url or get_current_playlist()

        if not playlist_url:
            self.logger.info("No playlist URL available to load")
            return

        # Try loading from cache first
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    cached_url = data.get('playlist_url', '')
                    cache_time = data.get('cached_at', 0)

                    # Invalidate cache if playlist URL changed
                    if cached_url != playlist_url:
                        self.logger.info(
                            "Playlist URL changed, invalidating cache.")
                    # Refresh if cache is older than 24 hours
                    elif time.time() - cache_time < 86400:
                        self.original_playlist = [Track.from_dict(
                            t) for t in data.get('tracks', [])]
                        if self.original_playlist:
                            self.logger.info(
                                f"Loaded {len(self.original_playlist)} tracks from cache.")
                            # Copy to playlist and shuffle for initial playback
                            self.playlist = self.original_playlist.copy()
                            self._apply_shuffle()
                            self._dedupe_playlist()
                            return
            except (json.JSONDecodeError, KeyError) as e:
                self.logger.warning(f"Cache corrupted, will re-fetch: {e}")

        # Fetch from YouTube
        await self._fetch_playlist()
        # Copy to playlist and shuffle for initial playback
        self.playlist = self.original_playlist.copy()
        self._apply_shuffle()
        self._dedupe_playlist()

    async def _fetch_playlist(self) -> None:
        """Fetches playlist metadata from YouTube using yt-dlp."""
        playlist_url = self._current_playlist_url or get_current_playlist()
        if not playlist_url or not YTDLP_AVAILABLE:
            return

        self.logger.info(f"Fetching playlist from YouTube: {playlist_url}")

        tracks = await fetch_playlist_metadata(playlist_url, self.logger)

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
        playlist_url = self._current_playlist_url or get_current_playlist()
        try:
            data = {
                'tracks': [t.to_dict() for t in self.original_playlist],
                'cached_at': time.time(),
                'playlist_url': playlist_url or ''
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
        return len(indices_to_remove)

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
        self.original_playlist = [
            t for t in self.original_playlist if t.url != removed_track.url]

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
        await self._load_playlist()  # Reloads original_playlist and applies shuffle

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
                if self._pending_playlist_switch:
                    self._pending_playlist_switch = False
                    if self._pending_playlist_url is None:
                        # Ambience wants us to stop
                        self.playlist = []
                        self.original_playlist = []
                        self._current_playlist_url = None
                        await self.bot.change_presence(activity=None)
                        self.logger.info("Stopped music per ambience request")
                    elif self._pending_playlist_url != self._current_playlist_url:
                        # Switch to new playlist
                        self._current_playlist_url = self._pending_playlist_url
                        await self._load_playlist()
                        self.current_index = 0
                        self.track_started_at = time.time()
                        self.logger.info(
                            "Switched to new playlist from ambience")
                    self._pending_playlist_url = None

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
                    if playlist_url and playlist_url != self._current_playlist_url:
                        self._current_playlist_url = playlist_url
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

    async def _get_audio_url(self, track: Track) -> tuple[Optional[str], bool, Optional[str], bool]:
        """Gets the actual streamable audio URL for a track.

        Args:
            track: The track to get the audio URL for.

        Returns:
            A tuple of (url, is_unavailable, thumbnail, needs_crop) where:
            - url: The streamable URL, or None if failed
            - is_unavailable: True if the video is permanently unavailable and should be removed
            - thumbnail: Best thumbnail URL found, or None
            - needs_crop: True if thumbnail needs center-cropping to extract album art
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

    async def _fetch_url_info(self, url: str) -> tuple[List[Track], Optional[str], Optional[str]]:
        """Fetches track info from a YouTube URL (video or playlist).

        Args:
            url: The YouTube URL to fetch.

        Returns:
            A tuple of (tracks, error_message, warning_message) where:
            - tracks: List of Track objects (single for video, multiple for playlist)
            - error_message: Human-readable error if failed, None if success
            - warning_message: Non-fatal warning (e.g., mix truncation), None if none
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
            self.logger.debug(
                "[Prefetch] No playlist, clearing prefetch cache")
            self._prefetched_url = None
            self._prefetched_track_url = None
            return

        # Always get the next sequential track, ignoring loop mode
        next_index = (self.current_index + 1) % len(self.playlist)
        next_track = self.playlist[next_index]
        self.logger.debug(
            f"[Prefetch] Current index: {self.current_index}, next index: {next_index}, playlist size: {len(self.playlist)}")

        # Don't refetch if we already have this track prefetched
        if self._prefetched_track_url == next_track.url and self._prefetched_url:
            self.logger.debug(
                f"[Prefetch] Already prefetched: {next_track.title}")
            return

        try:
            self.logger.debug(
                f"[Prefetch] Starting prefetch for: {next_track.title} ({next_track.url})")
            audio_url, is_unavailable, thumbnail, needs_crop = await self._get_audio_url(next_track)

            # Update track thumbnail if we found a better one
            if thumbnail and not next_track.thumbnail:
                next_track.thumbnail = thumbnail
                next_track.thumbnail_needs_crop = needs_crop
                self.logger.debug(f"[Prefetch] Updated thumbnail for: {next_track.title} (needs_crop={needs_crop})")

            if audio_url:
                self._prefetched_url = audio_url
                self._prefetched_track_url = next_track.url
                self.logger.debug(
                    f"[Prefetch] Success: {next_track.title} - URL length: {len(audio_url)}")
            else:
                # Clear prefetch cache on failure
                self._prefetched_url = None
                self._prefetched_track_url = None
                if is_unavailable:
                    self.logger.debug(
                        f"[Prefetch] Track unavailable: {next_track.title}")
                else:
                    self.logger.debug(
                        f"[Prefetch] Failed (no URL returned): {next_track.title}")
        except Exception as e:
            self.logger.debug(
                f"[Prefetch] Exception for {next_track.title}: {e}")
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

    def _clear_audio_caches(self) -> None:
        """Clears all audio URL caches (prefetch and current track)."""
        self._clear_prefetch()
        self._current_audio_url = None
        self._current_audio_track_url = None
        self.logger.debug("Cleared all audio URL caches")

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

        # Check caches for audio URL (current track cache first, then prefetch)
        audio_url: Optional[str] = None
        is_unavailable = False

        if self._current_audio_track_url == track.url and self._current_audio_url:
            # Current track cache hit (e.g., loop ONE replaying same track)
            audio_url = self._current_audio_url
            self.logger.debug(f"Using current track cache for: {track.title}")
        elif self._prefetched_track_url == track.url and self._prefetched_url:
            # Prefetch cache hit (next sequential track)
            audio_url = self._prefetched_url
            self.logger.debug(f"Using prefetched URL for: {track.title}")
            # Clear the prefetch since we're using it
            self._prefetched_url = None
            self._prefetched_track_url = None
        else:
            # No cache available, fetch now
            audio_url, is_unavailable, thumbnail, needs_crop = await self._get_audio_url(track)
            # Update track thumbnail if we found a better one
            if thumbnail and not track.thumbnail:
                track.thumbnail = thumbnail
                track.thumbnail_needs_crop = needs_crop
                self.logger.debug(f"Updated thumbnail for: {track.title} (needs_crop={needs_crop})")

        if not audio_url:
            if is_unavailable:
                # Remove unavailable track from playlist
                self.logger.info(f"Removing unavailable track: {track.title}")
                self._remove_track(self.current_index)
                # Save updated playlist to cache
                await self._save_playlist_cache()
            else:
                # Temporary error, just skip
                self.logger.warning(
                    f"Could not get audio URL for {track.title}, skipping...")
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
                self.logger.debug(
                    "Session ended during track preparation, aborting playback.")
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

            # Cache this track's audio URL for potential replay (loop ONE)
            self._current_audio_url = audio_url
            self._current_audio_track_url = track.url

            # Start prefetching the next track in the background
            self._prefetch_task = asyncio.create_task(
                self._prefetch_next_track())

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
            # Playlist ended with loop OFF - stay silent on last track
            # Rewind index to last track so it can be replayed/looped
            self.current_index = max(0, len(self.playlist) - 1)
            self.logger.info(
                "Playlist finished with loop OFF - waiting for user action.")

            # Notify the channel
            if self.active_session:
                try:
                    channel = self.bot.get_channel(
                        self.active_session.channel_id)
                    if channel and isinstance(channel, discord.abc.Messageable):
                        await channel.send(
                            "🎵 Playlist finished! I'll wait here for 5 minutes.\n"
                            "Add more songs, enable loop, or I'll have to disconnect!~"
                        )
                except Exception:
                    pass

                # Start idle timeout - disconnect if no activity
                self.active_session.waiting_for_users = True
                if self.idle_timeout_task:
                    self.idle_timeout_task.cancel()
                # Get a messageable channel for the timeout notification
                text_channel = self.bot.get_channel(
                    self.active_session.channel_id)
                if text_channel and isinstance(text_channel, discord.abc.Messageable):
                    self.idle_timeout_task = asyncio.create_task(
                        self._playlist_end_timeout_loop(text_channel)
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
                voice_client=vc
            )

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

        # Clear all audio URL caches
        self._clear_audio_caches()

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

    async def _playlist_end_timeout_loop(self, text_channel: discord.abc.Messageable) -> None:
        """Waits for user action after playlist ends with loop OFF.

        If no new songs are added, loop mode changed, or manual play within 5 minutes,
        disconnects from voice.
        """
        try:
            await asyncio.sleep(300)  # 5 minutes

            # Still waiting and no playback resumed?
            if self.active_session and self.active_session.waiting_for_users:
                vc = self.active_session.voice_client
                if vc and not vc.is_playing():
                    await text_channel.send(
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

    def _user_in_voice_with_bot(self, ctx: commands.Context) -> bool:
        """Checks if the command author is in the same voice channel as the bot.

        Bot owner bypasses this check for debugging purposes.

        Args:
            ctx: The command context.

        Returns:
            True if the user is in the bot's voice channel (or is owner), False otherwise.
        """
        if not self.active_session:
            return False

        # Bot owner bypasses VC check for debugging
        if ctx.author.id == self.bot.owner_id:
            return True

        # Get user's voice state
        if not ctx.author or not hasattr(ctx.author, 'voice'):
            return False

        author_voice = ctx.author.voice  # type: ignore[union-attr]
        if not author_voice or not author_voice.channel:
            return False

        # Check if user is in the same channel as the bot
        return author_voice.channel.id == self.active_session.channel_id

    async def _require_user_in_vc(self, ctx: commands.Context) -> bool:
        """Checks if user is in VC with bot, sends error message if not.

        Args:
            ctx: The command context.

        Returns:
            True if user is in VC with bot (command can proceed), False otherwise.
        """
        if not self.active_session:
            await ctx.send("I'm not playing music right now!")
            return False

        if not self._user_in_voice_with_bot(ctx):
            await ctx.send("You need to be in the voice channel to control playback!")
            return False

        return True

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
                self._current_playlist_url = playlist_url
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
                            self._idle_timeout_loop(
                                ctx.channel)  # type: ignore
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
            # Fetch directly from URL - no substitutions
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

            # Clear prefetch since playlist changed
            self._clear_prefetch()

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

        # Clear all audio caches since we jumped to a different track
        self._clear_audio_caches()

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

        # If we removed the currently playing track, advance playback
        if is_current and self.active_session and self.active_session.voice_client:
            vc = self.active_session.voice_client
            if vc.is_playing():
                vc.stop()  # Triggers _on_track_end via callback
            else:
                await self._play_current_track()

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

    async def skip_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for skip requests."""
        if not await self._require_user_in_vc(ctx):
            return

        vc = self.active_session.voice_client  # type: ignore[union-attr]
        if vc.is_playing():
            vc.stop()
            await ctx.send("⏭️ Skipped!")
        else:
            await ctx.send("Nothing is playing right now.")

    async def pause_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for pause requests."""
        if not await self._require_user_in_vc(ctx):
            return

        vc = self.active_session.voice_client  # type: ignore[union-attr]
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
        if not await self._require_user_in_vc(ctx):
            return

        vc = self.active_session.voice_client  # type: ignore[union-attr]
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
            description=f"**[{track.title}]({track.url})**\nby {track.artist}",
            color=discord.Color.purple()
        )
        embed.add_field(name="Duration",
                        value=f"{elapsed_str} / {duration_str}", inline=True)
        embed.add_field(name="Loop", value=self.loop_mode.display, inline=True)

        # Use track thumbnail if available (populated during prefetch/play)
        # Fall back to standard YouTube thumbnail URL if not
        thumbnail_url = track.thumbnail
        needs_crop = track.thumbnail_needs_crop
        self.logger.debug(f"Track thumbnail from data: {track.thumbnail} (needs_crop={needs_crop})")
        
        if not thumbnail_url and track.url:
            import re
            video_id_match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', track.url)
            if video_id_match:
                thumbnail_url = f"https://img.youtube.com/vi/{video_id_match.group(1)}/hqdefault.jpg"
                needs_crop = True  # YouTube fallback thumbnails are 16:9
                self.logger.debug(f"Using fallback thumbnail: {thumbnail_url}")

        self.logger.debug(f"Final thumbnail URL: {thumbnail_url}, needs_crop: {needs_crop}")

        # Handle thumbnail - crop if needed, otherwise use URL directly
        thumbnail_file = None
        if thumbnail_url:
            if needs_crop:
                self.logger.debug("Cropping thumbnail to square...")
                cropped_bytes = await crop_thumbnail_to_square(thumbnail_url, self.logger)
                if cropped_bytes:
                    thumbnail_file = discord.File(
                        io.BytesIO(cropped_bytes),
                        filename="thumbnail.jpg"
                    )
                    embed.set_thumbnail(url="attachment://thumbnail.jpg")
                else:
                    # Crop failed, use original URL as fallback
                    self.logger.debug("Crop failed, using original thumbnail URL")
                    embed.set_thumbnail(url=thumbnail_url)
            else:
                embed.set_thumbnail(url=thumbnail_url)

        if self.active_session:
            embed.set_footer(
                text=f"Playing in voice | {len(self.playlist)} tracks in playlist")
        else:
            embed.set_footer(
                text="Idle mode | Type 'listen along' to play in voice!")

        if thumbnail_file:
            await ctx.send(embed=embed, file=thumbnail_file)
        else:
            await ctx.send(embed=embed)

    async def queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for queue requests."""
        await self._do_queue(ctx)

    async def shuffle_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for shuffle toggle requests."""
        if not await self._require_user_in_vc(ctx):
            return

        if not self.playlist:
            await ctx.send("No playlist to shuffle.")
            return

        self._apply_shuffle(preserve_current=True)
        # Clear prefetch since playlist order changed
        self._clear_prefetch()
        await ctx.send("🔀 Playlist shuffled!")

    async def jump_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for jump requests.

        Parses the query for a number to jump to.
        """
        if not await self._require_user_in_vc(ctx):
            return

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

        # Require user in VC to change mode
        if not await self._require_user_in_vc(ctx):
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

    async def leave_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for leave/disconnect requests."""
        if not await self._require_user_in_vc(ctx):
            return

        await self._end_session("Disconnected by user request.")
        await ctx.send("👋 Disconnected!")

    async def remove_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for removing tracks from the playlist."""
        if not await self._require_user_in_vc(ctx):
            return

        # Strip trigger words - config.py already matched on 'remove'/'delete'
        clean_query = re.sub(
            r'^\s*(remove|delete)\s*(track|song|number|#)?\s*',
            '', query, flags=re.IGNORECASE
        ).strip()
        await self._do_remove(ctx, clean_query)

    async def move_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for moving tracks in the playlist."""
        if not await self._require_user_in_vc(ctx):
            return

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

    async def clear_queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for clearing the queue.

        Removes all tracks except the currently playing one.
        """
        if not await self._require_user_in_vc(ctx):
            return

        if not self.playlist:
            await ctx.send("The queue is already empty!")
            return

        # Keep only the current track
        current_track = self._get_current_track()
        if current_track:
            self.playlist = [current_track]
            self.current_index = 0
            self._playlist_modified_during_session = True
            self._clear_prefetch()
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


async def setup(bot: 'CoreBot') -> None:
    """Sets up the Music cog."""
    await bot.add_cog(Music(bot))

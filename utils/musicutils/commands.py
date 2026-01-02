"""Music command handlers mixin.

Architecture
------------
The bot's NLP system has three layers:

    config.py (NLP_COMMANDS)
        ↓  "Music cog, handle 'play'"  (doesn't care how)
    Music cog
        ↓  receives call to play_nlp   (sees it as its own method)
    MusicCommandsMixin
        ↓  implements play_nlp         (separated for human readability)
    Music cog state
           playlist, player, session   (mixin accesses via self)

The central NLP registry in config.py routes string patterns to cog methods.
This design keeps all patterns in one file for easy conflict detection and
priority management. The dispatcher uses getattr(cog, method_name), so
handlers must be methods on the cog.

Separation of Concerns
----------------------
The Music cog grew large (~3000 lines) because it handles both:
- State management: playlist, playback, voice sessions, caching
- Command logic: user interaction, NLP handlers, UI flows

The mixin separates these concerns:
- **Music cog**: State ownership, lifecycle, playback mechanics
- **MusicCommandsMixin**: Command logic, user interaction, NLP handlers

To the cog, these methods are its own (via inheritance). To the config and
dispatcher, nothing changed. Only the maintainers benefit - and that's the
point. Good architecture is invisible to the system, helpful to the humans.

Why Not Plain Functions?
------------------------
Plain functions would require wrapper methods in music.py for every handler:

    async def play_nlp(self, ctx, query):
        await operations.do_play(self, ctx, query)

This adds ~60 lines of boilerplate with no reduction elsewhere. The mixin
avoids this - methods defined here appear on Music automatically via
inheritance, satisfying the dispatcher's getattr lookup.

Stub Declarations
-----------------
The stub methods at the top of MusicCommandsMixin declare what the host cog
must provide. They serve as:
1. Documentation of the contract between mixin and host
2. Type hints enabling IDE autocomplete within mixin methods

These are the cost of file splitting, not overhead - they'd exist as wrapper
signatures anyway if we used plain functions.
"""

from __future__ import annotations

import asyncio
import functools
import io
import re
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    cast,
)

import discord
from discord.ext import commands

from .lyrics import chunk_text
from .music_data import LoopMode, LyricsResult, Track
from .music_helpers import (
    YTDLP_AVAILABLE,
    detect_mix_in_url,
    get_best_thumbnail_bytes,
)

if TYPE_CHECKING:
    from utils.views import NowPlayingState


# Type alias for NLP handler methods
NlpHandler = Callable[['MusicCommandsMixin', commands.Context, str], Awaitable[None]]


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
    async def wrapper(self: 'MusicCommandsMixin', ctx: commands.Context, query: str) -> None:
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


class MusicCommandsMixin:
    """Mixin class providing NLP command handlers for the Music cog.

    This mixin expects to be mixed into a class that has:
    - self.bot: CoreBot instance
    - self.logger: Logger instance
    - self.playlist: List[Track]
    - self.current_index: int
    - self.loop_mode: LoopMode
    - self.active_session: Optional[ActiveSession]
    - self._player: Optional[ManagedPlayer]
    - self._playback: PlaybackState
    - self._ambience: AmbienceState
    - self.db_manager: DatabaseManager
    - And various helper methods from the Music cog
    """

    # These are defined by the Music cog - declared here for type hints
    bot: Any
    logger: Any
    playlist: List[Track]
    current_index: int
    loop_mode: LoopMode
    active_session: Any
    _player: Any
    _playback: Any
    _ambience: Any
    db_manager: Any
    track_started_at: float
    idle_timeout_task: Any
    _playlist_modified_during_session: bool

    # Methods from Music cog that handlers depend on
    def _get_current_track(self) -> Optional[Track]: ...
    def _get_elapsed_seconds(self) -> float: ...
    def _apply_shuffle(self, preserve_current: bool = True) -> None: ...
    def _find_track_by_query(self, query: str) -> Optional[int]: ...
    def _remove_track(self, index: int) -> None: ...
    def _move_track(self, from_index: int, to_index: int) -> Optional[Track]: ...
    def _swap_tracks(self, index_a: int, index_b: int) -> Optional[tuple[Track, Track]]: ...
    def _clear_current_track_cache(self) -> None: ...
    def _refresh_prefetch_if_stale(self) -> None: ...
    def _do_pause(self) -> bool: ...
    async def _do_resume(self) -> bool: ...
    async def _load_playlist(self) -> None: ...
    async def _start_session(self, channel: discord.VoiceChannel, ctx: commands.Context, join_message: Optional[str] = None) -> None: ...
    async def _end_session(self, reason: str = "Session ended.") -> None: ...
    async def _play_current_track(self) -> None: ...
    async def _idle_timeout_loop(self) -> None: ...
    async def _search_youtube(self, query: str, max_results: int = 5) -> List[Track]: ...
    async def _fetch_url_info(self, url: str, force_playlist: bool = False) -> tuple[List[Track], Optional[str], Optional[str]]: ...

    # ==========================================================================
    # COMMAND HELPERS (Complex operations only)
    # ==========================================================================

    async def _do_listen_along(self, ctx: commands.Context) -> None:
        """Internal implementation for listen-along.

        Args:
            ctx: The command context.
        """
        from utils.ambience import MusicAmbience, ensure_music_for_user

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
        from .music_data import ActiveSession
        from .managed_player import ManagedPlayer

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
                    await ctx.send("🔍 Fetching mix playlist (up to 60 songs)...")
                    tracks, error, warning = await self._fetch_url_info(mix_url, force_playlist=True)
                else:
                    await ctx.send("🔍 Fetching track info...")
                    tracks, error, warning = await self._fetch_url_info(single_url)
            else:
                await ctx.send("🔍 Fetching track info...")
                tracks, error, warning = await self._fetch_url_info(query)

            if error:
                await ctx.send(f"❌ {error}")
                return

            tracks_to_add = tracks

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
                tracks_to_add = results
            else:
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

        # Queue size limit
        MAX_QUEUE_SIZE = 2000
        current_queue_size = len(self.playlist) if self.active_session else 0
        available_slots = MAX_QUEUE_SIZE - current_queue_size

        if available_slots <= 0:
            await ctx.send(f"❌ The queue is full ({MAX_QUEUE_SIZE} tracks max). Remove some tracks first!")
            return

        if len(tracks_to_add) > available_slots:
            tracks_to_add = tracks_to_add[:available_slots]
            await ctx.send(f"⚠️ Only adding {available_slots} tracks to stay within the {MAX_QUEUE_SIZE} track limit.")

        for track in tracks_to_add:
            track.user_added = True

        # If not in a voice session, join and start fresh
        if not self.active_session:
            member = ctx.author if isinstance(ctx.author, discord.Member) else None
            if not member or not member.voice or not member.voice.channel:
                await ctx.send("Join a voice channel first so I can play your request!")
                return

            channel = member.voice.channel
            if not isinstance(channel, discord.VoiceChannel):
                await ctx.send("I can only join regular voice channels, not stage channels.")
                return

            self.playlist = tracks_to_add.copy()
            self.current_index = 0
            self._playlist_modified_during_session = True

            try:
                vc = await channel.connect()
                self.active_session = ActiveSession(
                    guild_id=channel.guild.id,
                    channel_id=channel.id,
                    voice_client=vc,
                    origin_channel_id=ctx.channel.id,
                )

                self._player = ManagedPlayer(vc, self._on_player_track_end)  # type: ignore
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
            # Already in session - append to playlist
            if ctx.guild and self.active_session.guild_id != ctx.guild.id:
                await ctx.send("I'm currently playing in another server!")
                return

            moved_tracks: List[Track] = []
            new_tracks: List[Track] = []

            for track in tracks_to_add:
                existing_idx = next(
                    (i for i, t in enumerate(self.playlist) if t.url == track.url),
                    None
                )
                if existing_idx is not None:
                    existing_track = self.playlist.pop(existing_idx)
                    if existing_idx < self.current_index:
                        self.current_index -= 1
                    moved_tracks.append(existing_track)
                else:
                    new_tracks.append(track)

            self.playlist.extend(moved_tracks + new_tracks)
            self._playlist_modified_during_session = True

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
        """Internal implementation for queue display.

        Args:
            ctx: The command context.
        """
        from utils.views import PaginatorView

        if not self.playlist:
            await ctx.send("No playlist loaded.")
            return

        current = self._get_current_track()

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
                star = "⭐ " if track.user_added else ""
                if i == self.current_index:
                    lines.append(f"▶️ **{track_num}. {star}{track.title}** - {track.artist}")
                else:
                    lines.append(f"{track_num}. {star}{track.title} - {track.artist}")

            embed = discord.Embed(
                title="🎶 Playlist",
                description="\n".join(lines),
                color=discord.Color.blue()
            )

            if current:
                embed.set_author(name=f"Now Playing: {current.title} - {current.artist}")

            embed.set_footer(
                text=f"Page {page_num + 1}/{total_pages} • {total_tracks} tracks • "
                f"Loop: {self.loop_mode.display}"
            )
            pages.append(embed)

        current_page_idx = self.current_index // tracks_per_page

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PaginatorView(ctx, pages, start_index=current_page_idx)
            msg = await ctx.send(embed=pages[current_page_idx], view=view)
            view.message = msg

    async def _do_jump(self, ctx: commands.Context, position: int) -> None:
        """Internal implementation for jump to track.

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
        self._clear_current_track_cache()

        track = self._get_current_track()

        if track:
            await ctx.send(f"⏭️ Jumped to **#{position}**: {track.title} - {track.artist}")

            if self._player and (self._player.is_playing or self._player.is_paused):
                self._player.stop()
                await self._play_current_track()
            elif self.active_session:
                await self._play_current_track()

    async def _do_skip(self) -> bool:
        """Skip to the next track, ignoring Loop ONE mode.

        Returns:
            True if skip was initiated, False if not playing/no session.
        """
        if not self._player:
            return False

        if not (self._player.is_playing or self._player.is_paused):
            return False

        if not self.playlist:
            return False

        self.current_index += 1
        if self.current_index >= len(self.playlist):
            if self.loop_mode == LoopMode.ALL:
                self.current_index = 0
            else:
                self.current_index = 0

        self.track_started_at = time.time()
        self._playback.paused_at_position = None
        self._clear_current_track_cache()

        self._player.stop()
        await self._play_current_track()
        return True

    async def _do_lyrics(self, ctx: commands.Context, query: Optional[str] = None) -> None:
        """Internal implementation for lyrics search.

        Args:
            ctx: The command context.
            query: Search query. If None, uses current playing track.
        """
        from utils.views import PaginatorView, get_selection
        from .lyrics import GeniusScraper, LRCLIBProvider, LyricalNonsenseScraper

        artist_hint: Optional[str] = None
        if not query:
            current = self._get_current_track()
            if current:
                query = current.title
                artist_hint = current.artist
            else:
                await ctx.send("🎵 No song is currently playing. Please provide a search query!\n"
                               "Example: 'lyrics [song name]'")
                return

        searching_msg = await ctx.send(f"🔍 Searching for lyrics: **{query}**...")

        search_tasks = [
            GeniusScraper.search(query),
            LRCLIBProvider.search(query),
        ]
        provider_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        all_results: List[LyricsResult] = []
        seen_keys: set[str] = set()

        for result_list in provider_results:
            if isinstance(result_list, (Exception, BaseException)):
                continue
            for result in cast(List[LyricsResult], result_list):
                key = f"{result.title.lower()}|{result.artist.lower()}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_results.append(result)

        if artist_hint and len(all_results) > 5:
            artist_lower = artist_hint.lower()
            filtered = [r for r in all_results if artist_lower in r.artist.lower()
                        or r.artist.lower() in artist_lower]
            if filtered:
                all_results = filtered

        if not all_results:
            if searching_msg:
                try:
                    await searching_msg.edit(
                        content=f"❌ No lyrics found for **{query}**.\n"
                        "Try a different search term or check the spelling."
                    )
                except discord.HTTPException as e:
                    self.logger.debug(f"Could not edit searching message: {e}")
            return

        if len(all_results) == 1:
            selected = all_results[0]
        else:
            display_results = all_results[:5]
            options: Dict[str, str] = {}

            for i, result in enumerate(display_results):
                label = f"{i + 1}. {result.source}"
                options[label] = str(i)

            embed = discord.Embed(
                title=f"🎵 Lyrics Search: {query}",
                description="Select a source to view lyrics:\n\n" + "\n".join([
                    f"**{i + 1}.** {r.title} - {r.artist} ({r.source}){' 🌐' if r.has_translation else ''}"
                    for i, r in enumerate(display_results)
                ]),
                color=discord.Color.blue()
            )
            embed.set_footer(text="🌐 = Translation available • Select within 30s")

            if searching_msg:
                try:
                    await searching_msg.delete()
                except discord.HTTPException as e:
                    self.logger.debug(f"Could not delete searching message: {e}")

            selection = await get_selection(ctx, embed, options, buttons_only=True)

            if selection is None:
                return

            try:
                selected_idx = int(selection)
                selected = display_results[selected_idx]
            except (ValueError, IndexError):
                return

        fetching_msg = None
        if not selected.lyrics_text:
            try:
                fetching_msg = await ctx.send(f"📜 Fetching lyrics from {selected.source}...")
            except discord.HTTPException as e:
                self.logger.debug(f"Could not send fetching message: {e}")

            if selected.source == "Lyrical Nonsense":
                selected = await LyricalNonsenseScraper.fetch_lyrics(selected)
            elif selected.source == "Genius":
                selected = await GeniusScraper.fetch_lyrics(selected)
            elif selected.source == "LRCLIB":
                selected = await LRCLIBProvider.fetch_lyrics(selected)

            if fetching_msg:
                try:
                    await fetching_msg.delete()
                except discord.HTTPException as e:
                    self.logger.debug(f"Could not delete fetching message: {e}")

        if not selected.lyrics_text:
            await ctx.send(f"❌ Couldn't retrieve lyrics from {selected.source}. Try another source.")
            return

        pages: List[discord.Embed] = []
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

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PaginatorView(ctx, pages)
            msg = await ctx.send(embed=pages[0], view=view)
            view.message = msg

    async def _do_remove(self, ctx: commands.Context, query: str) -> None:
        """Internal implementation for removing a track from the playlist.

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

        number_match = re.search(r'\b(\d+)\b', query)
        if number_match:
            position = int(number_match.group(1))
            if 1 <= position <= len(self.playlist):
                target_index = position - 1
            else:
                await ctx.send(f"Invalid track number. Playlist has {len(self.playlist)} tracks.")
                return
        else:
            target_index = self._find_track_by_query(query)

        if target_index is None:
            await ctx.send(f"Couldn't find a track matching '{query}'.")
            return

        track = self.playlist[target_index]
        is_current = (target_index == self.current_index)

        self._remove_track(target_index)
        await ctx.send(f"🗑️ Removed **{track.title}** from the playlist.")

        if is_current and self._player:
            self._player.stop()
            await self._play_current_track()

    def _parse_track_reference(self, text: str) -> Optional[int]:
        """Parses a track reference (number or name) into a playlist index.

        Args:
            text: The text to parse.

        Returns:
            The 0-based playlist index, or None if not found/invalid.
        """
        text = text.strip()
        if not text:
            return None

        number_match = re.search(r'\b(\d+)\b', text)
        if number_match:
            pos = int(number_match.group(1))
            if 1 <= pos <= len(self.playlist):
                return pos - 1
            return None

        return self._find_track_by_query(text)

    def _parse_destination(self, text: str, mode: str = 'to') -> Optional[int]:
        """Parses a destination reference for move commands.

        Args:
            text: The destination text.
            mode: How to interpret - 'to' (exact), 'after', 'before'.

        Returns:
            The 0-based target index, or None if invalid.
        """
        text = text.strip().lower()
        if not text:
            return None

        if text in ('top', 'first', 'beginning', 'start'):
            return 0
        if text in ('bottom', 'last', 'end'):
            return len(self.playlist) - 1

        number_match = re.search(r'\b(\d+)\b', text)
        if number_match:
            pos = int(number_match.group(1))
            if 1 <= pos <= len(self.playlist):
                if mode == 'after':
                    return min(pos, len(self.playlist) - 1)
                elif mode == 'before':
                    return pos - 1
                return pos - 1
            return None

        track_index = self._find_track_by_query(text)
        if track_index is not None:
            if mode == 'after':
                return min(track_index + 1, len(self.playlist) - 1)
            elif mode == 'before':
                return track_index
            return track_index

        return None

    async def _do_move(self, ctx: commands.Context, query: str) -> None:
        """Internal implementation for moving a track in the playlist.

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
        use_swap = False

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
            track = self._move_track(from_index, to_index)
            if track:
                await ctx.send(f"📋 Moved **{track.title}** to position {to_index + 1}.")
            else:
                await ctx.send("Something went wrong moving that track.")

    # ==========================================================================
    # NLP HANDLERS
    # ==========================================================================

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
        """NLP handler for resume requests."""
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
        """NLP handler for play/queue requests with a song/URL."""
        song_query = re.sub(r'^\s*(play|queue)\s+', '', query, flags=re.IGNORECASE).strip()

        if not song_query:
            # Treat as resume
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
        from utils.views import NowPlayingView

        track = self._get_current_track()
        if not track:
            await ctx.send("No track is loaded.")
            return

        thumbnail_bytes = await get_best_thumbnail_bytes(track, self.logger)
        files: List[discord.File] = []
        thumbnail_url: Optional[str] = None

        if thumbnail_bytes:
            files.append(discord.File(io.BytesIO(thumbnail_bytes), filename="thumbnail.jpg"))
            thumbnail_url = "attachment://thumbnail.jpg"

        state = self.get_now_playing_state(thumbnail_url)

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
        await ctx.send("🔀 Playlist shuffled!")

    @requires_voice
    async def jump_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for jump requests."""
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
        """NLP handler for loop mode requests."""
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
                "Usage: 'loop one' (repeat track), 'loop all' (repeat playlist), or 'loop off'"
            )
            return

        if not self.active_session:
            await ctx.send("I'm not playing music right now!")
            return

        if ctx.author.id != self.bot.owner_id:
            author_voice = getattr(ctx.author, 'voice', None)
            if not author_voice or not author_voice.channel or author_voice.channel.id != self.active_session.channel_id:
                await ctx.send("You need to be in the voice channel to control playback!")
                return

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
        clean_query = re.sub(
            r'^\s*(remove|delete)\s*(track|song|number|#)?\s*',
            '', query, flags=re.IGNORECASE
        ).strip()
        await self._do_remove(ctx, clean_query)

    @requires_voice
    async def move_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for moving tracks in the playlist."""
        clean_query = re.sub(
            r'^\s*move\s*(track|song|number|#)?\s*',
            '', query, flags=re.IGNORECASE
        ).strip()
        await self._do_move(ctx, clean_query)

    async def lyrics_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for lyrics search."""
        clean_query = re.sub(
            r'^\s*(lyrics?\s*(for|of|to)?|find\s*lyrics?\s*(for|of|to)?|search\s*lyrics?\s*(for|of|to)?|get\s*lyrics?\s*(for|of|to)?)\s*',
            '',
            query,
            flags=re.IGNORECASE
        ).strip()

        await self._do_lyrics(ctx, clean_query if clean_query else None)

    @requires_voice
    async def clear_queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for clearing the queue."""
        if not self.playlist:
            await ctx.send("The queue is already empty!")
            return

        current_track = self._get_current_track()
        if current_track:
            self.playlist = [current_track]
            self.current_index = 0
            self._playlist_modified_during_session = True
            self._refresh_prefetch_if_stale()
            await ctx.send(f"🗑️ Queue cleared! Only **{current_track.title}** remains.")
        else:
            self.playlist = []
            self.current_index = 0
            await ctx.send("🗑️ Queue cleared!")

    # ==========================================================================
    # MusicPlayerProtocol Implementation
    # ==========================================================================

    def get_now_playing_state(self, thumbnail_url: Optional[str] = None) -> 'NowPlayingState':
        """Build current now playing state for view construction."""
        from utils.views import NowPlayingState

        track = self._get_current_track()

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
        """Toggle between play and pause states."""
        if self._player:
            if self._player.is_playing:
                self._do_pause()
            elif self._player.is_paused:
                await self._do_resume()

    async def skip_track(self) -> Optional[bytes]:
        """Skip to next track and return new thumbnail."""
        if not self.playlist:
            return None

        if not await self._do_skip():
            self.current_index = (self.current_index + 1) % len(self.playlist)
            self.track_started_at = time.time()
        else:
            await asyncio.sleep(0.5)

        new_track = self._get_current_track()
        if new_track:
            return await get_best_thumbnail_bytes(new_track, self.logger)
        return None

    async def shuffle_playlist(self) -> None:
        """Shuffle the playlist, preserving current track position."""
        if self.playlist:
            self._apply_shuffle(preserve_current=True)

    async def cycle_loop_mode(self) -> None:
        """Cycle through loop modes (ALL -> ONE -> OFF -> ALL)."""
        if self.loop_mode == LoopMode.ALL:
            self.loop_mode = LoopMode.ONE
        elif self.loop_mode == LoopMode.ONE:
            self.loop_mode = LoopMode.OFF
        else:
            self.loop_mode = LoopMode.ALL

    async def get_current_thumbnail(self) -> Optional[bytes]:
        """Fetch thumbnail bytes for the current track."""
        track = self._get_current_track()
        if track:
            return await get_best_thumbnail_bytes(track, self.logger)
        return None

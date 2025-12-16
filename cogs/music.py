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
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast, Dict, List, Optional, TYPE_CHECKING, Union

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.database import DatabaseManager

if TYPE_CHECKING:
    from utils.bot_class import CoreBot

# Attempt to import yt-dlp
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore[assignment]
    YTDLP_AVAILABLE = False

# FFmpeg options for Discord audio streaming
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -filter:a "volume=0.5"'
}

# yt-dlp options for extracting audio
YTDLP_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'opus',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,  # We want playlist support
    'nocheckcertificate': True,
    'ignoreerrors': True,  # Skip unavailable videos
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}


class LoopMode(Enum):
    """Loop mode options for the music player."""
    OFF = 0
    ONE = 1
    ALL = 2

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> 'LoopMode':
        """Case-insensitive converter for discord.py commands."""
        try:
            return cls[argument.upper()]
        except KeyError:
            raise commands.BadArgument(f"'{argument}' is not a valid loop mode. Use: off, one, or all")

    @property
    def display(self) -> str:
        """Human-readable display name."""
        return {
            LoopMode.OFF: "Off",
            LoopMode.ONE: "One",
            LoopMode.ALL: "All"
        }[self]

    @property
    def emoji(self) -> str:
        """Emoji representation for the mode."""
        return {
            LoopMode.OFF: "➡️",
            LoopMode.ONE: "🔂",
            LoopMode.ALL: "🔁"
        }[self]

    def next(self) -> 'LoopMode':
        """Returns the next loop mode in the cycle."""
        return {
            LoopMode.OFF: LoopMode.ONE,
            LoopMode.ONE: LoopMode.ALL,
            LoopMode.ALL: LoopMode.OFF
        }[self]


@dataclass
class Track:
    """Represents a single track in the playlist."""
    title: str
    artist: str
    url: str  # YouTube URL
    duration: int  # Duration in seconds
    thumbnail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        return {
            'title': self.title,
            'artist': self.artist,
            'url': self.url,
            'duration': self.duration,
            'thumbnail': self.thumbnail
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Track':
        """Create from dictionary."""
        return cls(
            title=data['title'],
            artist=data['artist'],
            url=data['url'],
            duration=data['duration'],
            thumbnail=data.get('thumbnail')
        )


@dataclass
class ActiveSession:
    """Represents an active voice session."""
    guild_id: int
    channel_id: int
    voice_client: discord.VoiceClient
    started_at: float = field(default_factory=time.time)
    waiting_for_users: bool = False


# Type alias for contexts that support send()
Respondable = Union[commands.Context, discord.Interaction]


async def respond(target: Respondable, *args: Any, **kwargs: Any) -> None:
    """Send a message to either a Context or Interaction.

    Args:
        target: Either a commands.Context or discord.Interaction.
        *args: Positional arguments for send/response.
        **kwargs: Keyword arguments for send/response.
    """
    if isinstance(target, commands.Context):
        await target.send(*args, **kwargs)
    else:
        # Interaction
        if target.response.is_done():
            await target.followup.send(*args, **kwargs)
        else:
            await target.response.send_message(*args, **kwargs)


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
        self.shuffle_enabled: bool = True
        self.loop_mode: LoopMode = LoopMode.ALL

        # Presence cycling state (idle mode)
        self.track_started_at: float = time.time()
        self.presence_task: Optional[asyncio.Task[None]] = None

        # Voice session state
        self.active_session: Optional[ActiveSession] = None
        self.playback_task: Optional[asyncio.Task[None]] = None
        self.idle_timeout_task: Optional[asyncio.Task[None]] = None

        # FFmpeg path (can be overridden for bundled builds)
        self._ffmpeg_path: Optional[str] = None

        # Cache path
        self.cache_path = config.MUSIC_CACHE_PATH

    def _get_ffmpeg_path(self) -> str:
        """Gets the path to FFmpeg executable.

        For bundled builds, checks for FFmpeg in the app directory.
        Otherwise, assumes FFmpeg is in system PATH.

        Returns:
            str: Path to FFmpeg executable.
        """
        if self._ffmpeg_path:
            return self._ffmpeg_path

        # Check for bundled FFmpeg (PyInstaller build)
        if getattr(__import__('sys'), 'frozen', False):
            bundled_path = os.path.join(config.APP_PATH, 'ffmpeg.exe')
            if os.path.exists(bundled_path):
                self._ffmpeg_path = bundled_path
                return self._ffmpeg_path

        # Fallback to system PATH
        ffmpeg_in_path = shutil.which('ffmpeg')
        if ffmpeg_in_path:
            self._ffmpeg_path = ffmpeg_in_path
            return self._ffmpeg_path

        # Last resort - just return 'ffmpeg' and let it fail with a clear error
        self._ffmpeg_path = 'ffmpeg'
        return self._ffmpeg_path

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
                    self.original_playlist = [Track.from_dict(t) for t in data.get('tracks', [])]
                    cache_time = data.get('cached_at', 0)

                    # Refresh if cache is older than 24 hours
                    if time.time() - cache_time < 86400 and self.original_playlist:
                        self.logger.info(f"Loaded {len(self.original_playlist)} tracks from cache.")
                        self._apply_shuffle()
                        return
            except (json.JSONDecodeError, KeyError) as e:
                self.logger.warning(f"Cache corrupted, will re-fetch: {e}")

        # Fetch from YouTube
        await self._fetch_playlist()
        self._apply_shuffle()

    async def _fetch_playlist(self) -> None:
        """Fetches playlist metadata from YouTube using yt-dlp."""
        if not config.YOUTUBE_PLAYLIST_URL or not yt_dlp:
            return

        self.logger.info("Fetching playlist from YouTube...")

        try:
            ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': 'in_playlist'}

            def extract() -> Dict[str, Any]:
                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                    return ydl.extract_info(config.YOUTUBE_PLAYLIST_URL, download=False)  # type: ignore

            info = await asyncio.to_thread(extract)

            if not info:
                self.logger.error("Failed to extract playlist info.")
                return

            tracks: List[Track] = []
            entries = info.get('entries', [])

            for entry in entries:
                if not entry:  # Skip unavailable videos
                    continue

                # For flat extraction, we get minimal info
                # We'll fetch full info when actually playing
                track = Track(
                    title=entry.get('title', 'Unknown Title'),
                    artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                    url=entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                    duration=entry.get('duration', 180),  # Default 3 min if unknown
                    thumbnail=entry.get('thumbnail')
                )
                tracks.append(track)

            self.original_playlist = tracks
            self.logger.info(f"Fetched {len(tracks)} tracks from playlist.")

            # Save to cache
            await self._save_playlist_cache()

        except Exception as e:
            self.logger.error(f"Error fetching playlist: {e}", exc_info=True)

    async def _save_playlist_cache(self) -> None:
        """Saves playlist to cache file."""
        cache_file = os.path.join(self.cache_path, 'playlist.json')
        try:
            data = {
                'tracks': [t.to_dict() for t in self.original_playlist],
                'cached_at': time.time()
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save playlist cache: {e}")

    def _apply_shuffle(self, preserve_current: bool = False) -> None:
        """Applies or removes shuffle from playlist.

        Args:
            preserve_current: If True, attempts to keep the current track at
                the same position after reshuffling. Useful when toggling
                shuffle during idle mode.
        """
        current = self._get_current_track() if preserve_current else None

        if self.shuffle_enabled:
            self.playlist = self.original_playlist.copy()
            random.shuffle(self.playlist)
        else:
            self.playlist = self.original_playlist.copy()

        # Restore current track position if requested
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
                # Reshuffle if shuffle is enabled
                if self.shuffle_enabled:
                    random.shuffle(self.playlist)
                self.current_index = 0
            else:
                # Loop OFF: stop playback
                return None

        self.track_started_at = time.time()
        return self._get_current_track()

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

    async def _get_audio_url(self, track: Track) -> Optional[str]:
        """Gets the actual streamable audio URL for a track."""
        if not yt_dlp:
            return None

        try:
            ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': False}

            def extract() -> Dict[str, Any]:
                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                    return ydl.extract_info(track.url, download=False)  # type: ignore

            info = await asyncio.to_thread(extract)

            if not info:
                return None

            # Find an audio-only format (has audio codec, no video codec)
            # This gives us the smallest stream that Discord can play
            formats = info.get('formats', [])
            for fmt in formats:
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                    return fmt.get('url')

            # Fallback to url directly
            return info.get('url')

        except Exception as e:
            self.logger.error(f"Error getting audio URL for {track.title}: {e}")
            return None

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

        # Get streamable URL
        audio_url = await self._get_audio_url(track)
        if not audio_url:
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

            ffmpeg_path = self._get_ffmpeg_path()
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

        self.logger.info(f"Voice session ended: {reason}")

    async def _idle_timeout_loop(self, text_channel: discord.abc.Messageable) -> None:
        """Waits for users to join, disconnects if none do within timeout."""
        try:
            await asyncio.sleep(300)  # 5 minutes

            if self.active_session and self.active_session.waiting_for_users:
                # Check if anyone joined
                vc = self.active_session.voice_client
                if vc and len(vc.channel.members) <= 1:  # Just the bot
                    await text_channel.send("No one joined, so I'm heading out! Use `/listen-along` when you're ready.")
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
    # COMMANDS (Internal implementations)
    # ==========================================================================
    # These `_do_*` methods contain the actual command logic. Both slash commands
    # and NLP handlers call these, allowing a single implementation to serve both
    # interaction types. They accept `Respondable` (Context or Interaction) and
    # use the `respond()` helper to send messages.

    async def _do_listen_along(
        self,
        target: Respondable,
        user: Union[discord.User, discord.Member],
        guild: Optional[discord.Guild]
    ) -> None:
        """Internal implementation for listen-along."""
        if not YTDLP_AVAILABLE:
            await respond(target, "Music playback isn't available - yt-dlp is not installed.")
            return

        if not self.playlist:
            await respond(target, "I don't have any music loaded! Make sure `YOUTUBE_PLAYLIST_URL` is configured.")
            return

        # Check if already in a session
        if self.active_session:
            if guild and self.active_session.guild_id == guild.id:
                await respond(target, f"I'm already playing music in <#{self.active_session.channel_id}>!")
            else:
                other_guild = self.bot.get_guild(self.active_session.guild_id)
                guild_name = other_guild.name if other_guild else "another server"
                await respond(target, f"I'm currently playing music in **{guild_name}**. I can only be in one place at a time!")
            return

        # Check if user is in a voice channel
        # Note: `user` may be a User (from DMs) or Member (from guild). Only Members have voice state.
        member = user if isinstance(user, discord.Member) else None
        if not member or not member.voice or not member.voice.channel:
            # User isn't in a VC - try the guild's designated music channel as fallback
            if guild:
                designated_channel_id = await self.db_manager.get_guild_config(guild.id, 'music_channel_id')
                if designated_channel_id:
                    channel = guild.get_channel(int(designated_channel_id))
                    if channel and isinstance(channel, discord.VoiceChannel):
                        await respond(target, f"I'll be in {channel.mention}! Join me there within 5 minutes.")
                        # For _start_session we need a context-like object for the text channel
                        if isinstance(target, commands.Context):
                            await self._start_session(channel, target)
                        else:
                            # Create a minimal context adapter for the interaction
                            await self._start_session(channel, target)  # type: ignore

                        # Mark session as waiting and start 5-minute timeout
                        # If no one joins, _idle_timeout_loop will disconnect
                        self.active_session.waiting_for_users = True  # type: ignore
                        text_channel = target.channel if isinstance(target, commands.Context) else target.channel
                        self.idle_timeout_task = self.bot.loop.create_task(
                            self._idle_timeout_loop(text_channel)  # type: ignore
                        )
                        return

            await respond(target, "Join a voice channel first, or ask an admin to set a music channel with `/set-music-channel`!")
            return

        # Join user's channel
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await respond(target, "I can only join regular voice channels, not stage channels.")
            return

        await self._start_session(channel, target)  # type: ignore

    async def _do_skip(self, target: Respondable, guild: Optional[discord.Guild]) -> None:
        """Internal implementation for skip."""
        if not self.active_session:
            await respond(target, "I'm not playing anything right now!")
            return

        if guild and self.active_session.guild_id != guild.id:
            await respond(target, "I'm not playing music in this server!")
            return

        vc = self.active_session.voice_client
        if vc.is_playing():
            vc.stop()
            await respond(target, "⏭️ Skipped!")
        else:
            await respond(target, "Nothing is playing right now.")

    async def _do_now_playing(self, target: Respondable) -> None:
        """Internal implementation for now playing."""
        track = self._get_current_track()
        if not track:
            await respond(target, "No track is loaded.")
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
        embed.add_field(name="Shuffle", value="On" if self.shuffle_enabled else "Off", inline=True)

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        if self.active_session:
            embed.set_footer(text=f"Playing in voice | {len(self.playlist)} tracks in playlist")
        else:
            embed.set_footer(text="Idle mode | Use /listen-along to play in voice")

        await respond(target, embed=embed)

    async def _do_queue(self, target: Respondable) -> None:
        """Internal implementation for queue."""
        if not self.playlist:
            await respond(target, "No playlist loaded.")
            return

        current = self._get_current_track()
        # Calculate starting index for "up next" - wraps around if near end
        upcoming_start = (self.current_index + 1) % len(self.playlist)

        lines = []
        if current:
            lines.append(f"**Now:** {current.title} - {current.artist}")
            lines.append("")

        lines.append("**Up Next:**")
        for i in range(9):
            idx = (upcoming_start + i) % len(self.playlist)
            if idx == self.current_index:
                break
            track = self.playlist[idx]
            lines.append(f"{i + 1}. {track.title} - {track.artist}")

        embed = discord.Embed(
            title="🎶 Queue",
            description="\n".join(lines),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{len(self.playlist)} tracks total | Shuffle: {'On' if self.shuffle_enabled else 'Off'} | Loop: {self.loop_mode.display}")

        await respond(target, embed=embed)

    async def _do_shuffle(self, target: Respondable) -> None:
        """Internal implementation for shuffle."""
        self.shuffle_enabled = not self.shuffle_enabled
        self._apply_shuffle(preserve_current=True)
        status = "enabled" if self.shuffle_enabled else "disabled"
        await respond(target, f"🔀 Shuffle {status}!")

    async def _do_jump(self, target: Respondable, position: int) -> None:
        """Internal implementation for jump."""
        if not self.playlist:
            await respond(target, "No playlist loaded.")
            return

        index = position - 1

        if index < 0 or index >= len(self.playlist):
            await respond(target, f"❌ Invalid position. Please choose a number between 1 and {len(self.playlist)}.")
            return

        self.current_index = index
        self.track_started_at = time.time()
        track = self._get_current_track()

        if track:
            await respond(target, f"⏭️ Jumped to **#{position}**: {track.title} - {track.artist}")

            # If playing, stop current track - the `after` callback will trigger _on_track_end
            # which calls _play_current_track with our new index
            if self.active_session and self.active_session.voice_client.is_playing():
                self.active_session.voice_client.stop()

    async def _do_leave(self, target: Respondable, guild: Optional[discord.Guild]) -> None:
        """Internal implementation for leave."""
        if not self.active_session:
            await respond(target, "I'm not in a voice channel!")
            return

        if guild and self.active_session.guild_id != guild.id:
            await respond(target, "I'm not playing music in this server!")
            return

        await self._end_session("Disconnected by user request.")
        await respond(target, "👋 Disconnected!")

    async def _do_loop(self, target: Respondable, mode: LoopMode) -> None:
        """Internal implementation for loop."""
        if mode == LoopMode.OFF:
            await respond(
                target,
                "➡️ Loop **Off** mode isn't available yet - the music player isn't fully implemented!\n"
                "For now, use **One** (repeat current track) or **All** (repeat playlist)."
            )
            return

        self.loop_mode = mode
        await respond(target, f"{self.loop_mode.emoji} Loop mode: **{self.loop_mode.display}**")

    # ==========================================================================
    # SLASH COMMANDS
    # ==========================================================================
    # These are registered as Discord slash commands (app_commands). They're thin
    # wrappers that extract the needed info from the Interaction and delegate to
    # the corresponding `_do_*` method. No prefix command registration here -
    # prefix input is handled by NLP handlers below.

    @app_commands.command(name='listen-along', description='Have me join your voice channel and play music!')
    async def listen_along_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for listen-along."""
        await self._do_listen_along(interaction, interaction.user, interaction.guild)

    @app_commands.command(name='skip', description='Skip the current song.')
    async def skip_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for skip."""
        await self._do_skip(interaction, interaction.guild)

    @app_commands.command(name='nowplaying', description='Shows the currently playing song.')
    async def now_playing_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for now playing."""
        await self._do_now_playing(interaction)

    @app_commands.command(name='queue', description='Shows the upcoming songs in the queue.')
    async def queue_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for queue."""
        await self._do_queue(interaction)

    @app_commands.command(name='shuffle', description='Toggle shuffle mode for the playlist.')
    async def shuffle_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for shuffle."""
        await self._do_shuffle(interaction)

    @app_commands.command(name='jump', description='Jump to a specific track in the playlist by number.')
    async def jump_slash(self, interaction: discord.Interaction, position: int) -> None:
        """Slash command for jump."""
        await self._do_jump(interaction, position)

    @app_commands.command(name='leave', description='Disconnect from voice channel.')
    async def leave_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for leave."""
        await self._do_leave(interaction, interaction.guild)

    @app_commands.command(name='loop', description='Set loop mode for the playlist.')
    async def loop_slash(self, interaction: discord.Interaction, mode: LoopMode) -> None:
        """Slash command for loop."""
        await self._do_loop(interaction, mode)

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
        await self._do_listen_along(ctx, ctx.author, ctx.guild)

    async def skip_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for skip requests."""
        await self._do_skip(ctx, ctx.guild)

    async def now_playing_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for now playing requests."""
        await self._do_now_playing(ctx)

    async def queue_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for queue requests."""
        await self._do_queue(ctx)

    async def shuffle_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for shuffle toggle requests."""
        await self._do_shuffle(ctx)

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

        if 'one' in query_lower or 'single' in query_lower or 'track' in query_lower:
            await self._do_loop(ctx, LoopMode.ONE)
        elif 'all' in query_lower or 'playlist' in query_lower:
            await self._do_loop(ctx, LoopMode.ALL)
        elif 'off' in query_lower or 'disable' in query_lower or 'none' in query_lower:
            await self._do_loop(ctx, LoopMode.OFF)
        else:
            await ctx.send(
                f"{self.loop_mode.emoji} Current loop mode: **{self.loop_mode.display}**\n"
                "Usage: `loop one` (repeat track) or `loop all` (repeat playlist)"
            )

    async def leave_nlp(self, ctx: commands.Context, query: str) -> None:
        """NLP handler for leave/disconnect requests."""
        await self._do_leave(ctx, ctx.guild)


async def setup(bot: 'CoreBot') -> None:
    """Sets up the Music cog.

    The cog will not load if YOUTUBE_PLAYLIST_URL is not configured.
    """
    if not config.YOUTUBE_PLAYLIST_URL:
        import logging
        logging.getLogger('Music').info("Music cog not loaded: YOUTUBE_PLAYLIST_URL not configured.")
        return

    await bot.add_cog(Music(bot))

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
import html
import json
import os
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus
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

# Attempt to import syncedlyrics (for LRCLIB lyrics fetching)
try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    syncedlyrics = None
    SYNCEDLYRICS_AVAILABLE = False

# aiohttp for web scraping (Genius, etc.)
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

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
class LyricsResult:
    """Represents lyrics search result from a provider."""
    title: str
    artist: str
    source: str  # Provider name (e.g., "Lyrical Nonsense", "LRCLIB")
    url: str  # Direct link to lyrics page
    has_translation: bool = False
    lyrics_text: Optional[str] = None  # Populated when fetched
    translation_text: Optional[str] = None  # EN translation if available

    @property
    def display_name(self) -> str:
        """Formatted display name for selection menus."""
        trans = " 🌐" if self.has_translation else ""
        return f"{self.title} - {self.artist}{trans}"


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


class InteractionPseudoContext:
    """A pseudo-context wrapper for discord.Interaction.

    Allows using Interactions with utilities that expect Context-like objects
    (e.g., get_selection, PaginatorView). Only implements the minimum required
    interface: author, channel, bot, and send().
    """

    def __init__(self, interaction: discord.Interaction):
        self.author = interaction.user
        self.channel = interaction.channel
        self.bot = interaction.client
        self._interaction = interaction

    async def send(self, *args: Any, **kwargs: Any) -> discord.Message:
        """Send a message via interaction followup."""
        return await self._interaction.followup.send(*args, **kwargs)


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


# ==========================================================================
# LYRICS PROVIDERS
# ==========================================================================


class LyricalNonsenseScraper:
    """Scrapes lyrics from lyrical-nonsense.com (best for JP content with EN translations).

    NOTE: This provider is currently NON-FUNCTIONAL. Lyrical Nonsense uses JavaScript-based
    search with no public API endpoint. The /global/search/ URL returns 404.
    Keeping this class for potential future implementation if an API is discovered.
    """

    BASE_URL = "https://www.lyrical-nonsense.com"
    SEARCH_URL = "https://www.lyrical-nonsense.com/global/search/"

    @classmethod
    async def search(cls, query: str) -> List[LyricsResult]:
        """Search for lyrics on Lyrical Nonsense.

        NOTE: Currently non-functional - Lyrical Nonsense uses JavaScript search
        with no public API. Always returns empty list.

        Args:
            query: Search query (song title, artist, or both).

        Returns:
            Empty list (search not implemented).
        """
        # Lyrical Nonsense uses JavaScript-based search with no public API
        # The /global/search/ endpoint returns 404
        # TODO: Investigate if there's a hidden API or consider browser automation
        return []

    @classmethod
    async def fetch_lyrics(cls, result: LyricsResult) -> LyricsResult:
        """Fetch full lyrics and translation from a Lyrical Nonsense page.

        Args:
            result: LyricsResult with URL to fetch.

        Returns:
            Updated LyricsResult with lyrics_text and translation_text populated.
        """
        if not AIOHTTP_AVAILABLE or aiohttp is None:
            return result

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(result.url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return result
                    html_text = await resp.text()

            # Extract original lyrics
            # Look for the Japanese/original lyrics container
            original_pattern = re.compile(
                r'<div[^>]*(?:id="(?:Lyrics|lyricsjpn|lyrics-original)"[^>]*|class="[^"]*(?:olyrictext|lyrics-original|lyrictext)[^"]*")[^>]*>(.*?)</div>',
                re.IGNORECASE | re.DOTALL
            )
            original_match = original_pattern.search(html_text)

            if original_match:
                lyrics_html = original_match.group(1)
                # Clean HTML tags, preserve line breaks
                lyrics_text = re.sub(r'<br\s*/?>', '\n', lyrics_html)
                lyrics_text = re.sub(r'<[^>]+>', '', lyrics_text)
                lyrics_text = html.unescape(lyrics_text).strip()
                result.lyrics_text = lyrics_text

            # Extract English translation
            trans_pattern = re.compile(
                r'<div[^>]*(?:id="(?:Romaji|lyricseng|lyrics-english)"[^>]*|class="[^"]*(?:tlyrictext|lyrics-english|elyrictext)[^"]*")[^>]*>(.*?)</div>',
                re.IGNORECASE | re.DOTALL
            )
            trans_match = trans_pattern.search(html_text)

            if trans_match:
                trans_html = trans_match.group(1)
                trans_text = re.sub(r'<br\s*/?>', '\n', trans_html)
                trans_text = re.sub(r'<[^>]+>', '', trans_text)
                trans_text = html.unescape(trans_text).strip()
                if trans_text:
                    result.translation_text = trans_text
                    result.has_translation = True

            # If we couldn't find structured lyrics, try a more general approach
            if not result.lyrics_text:
                # Look for any large text block that might be lyrics
                general_pattern = re.compile(
                    r'<div[^>]*class="[^"]*lyric[^"]*"[^>]*>(.*?)</div>',
                    re.IGNORECASE | re.DOTALL
                )
                for match in general_pattern.finditer(html_text):
                    text = match.group(1)
                    text = re.sub(r'<br\s*/?>', '\n', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    text = html.unescape(text).strip()
                    if len(text) > 100:  # Likely actual lyrics
                        result.lyrics_text = text
                        break

        except Exception:
            pass

        return result


class LRCLIBProvider:
    """Fetches lyrics from LRCLIB via syncedlyrics library."""

    @classmethod
    async def search(cls, query: str) -> List[LyricsResult]:
        """Search for lyrics on LRCLIB.

        Args:
            query: Search query (song title, artist, or both).

        Returns:
            List of LyricsResult objects (typically one result if found).
        """
        if not SYNCEDLYRICS_AVAILABLE or syncedlyrics is None:
            return []

        results: List[LyricsResult] = []

        try:
            # syncedlyrics.search returns lyrics directly, not a list of results
            # We'll do a simple search and return it as a single result
            # Capture module reference for type narrowing in nested function
            _syncedlyrics = syncedlyrics

            def do_search() -> Optional[str]:
                return _syncedlyrics.search(query, providers=['lrclib'])

            lyrics = await asyncio.to_thread(do_search)

            if lyrics:
                # Extract title/artist from query (best effort)
                parts = query.split(' - ', 1)
                if len(parts) == 2:
                    artist, title = parts[0].strip(), parts[1].strip()
                else:
                    title = query
                    artist = "Unknown Artist"

                results.append(LyricsResult(
                    title=title,
                    artist=artist,
                    source="LRCLIB",
                    url=f"https://lrclib.net/search?q={quote_plus(query)}",
                    has_translation=False,
                    lyrics_text=lyrics
                ))

        except Exception:
            pass

        return results

    @classmethod
    async def fetch_lyrics(cls, result: LyricsResult) -> LyricsResult:
        """LRCLIB results already have lyrics populated from search.

        Args:
            result: LyricsResult (lyrics already populated).

        Returns:
            The same result (no additional fetching needed).
        """
        return result


class GeniusScraper:
    """Scrapes lyrics from Genius (good general coverage, no API key needed)."""

    BASE_URL = "https://genius.com"
    SEARCH_URL = "https://genius.com/api/search/multi"

    @classmethod
    async def search(cls, query: str) -> List[LyricsResult]:
        """Search for lyrics on Genius.

        Args:
            query: Search query (song title, artist, or both).

        Returns:
            List of LyricsResult objects for matching songs.
        """
        if not AIOHTTP_AVAILABLE or aiohttp is None:
            return []

        results: List[LyricsResult] = []

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            params = {'q': query}

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    cls.SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            # Parse API response
            sections = data.get('response', {}).get('sections', [])
            for section in sections:
                if section.get('type') != 'song':
                    continue

                for hit in section.get('hits', [])[:5]:  # Limit results
                    song = hit.get('result', {})
                    title = song.get('title', 'Unknown')
                    artist = song.get('primary_artist', {}).get('name', 'Unknown')
                    url = song.get('url', '')

                    if url:
                        results.append(LyricsResult(
                            title=title,
                            artist=artist,
                            source="Genius",
                            url=url,
                            has_translation=False
                        ))

        except Exception:
            pass

        return results

    @classmethod
    async def fetch_lyrics(cls, result: LyricsResult) -> LyricsResult:
        """Fetch full lyrics from a Genius page.

        Args:
            result: LyricsResult with URL to fetch.

        Returns:
            Updated LyricsResult with lyrics_text populated.
        """
        if not AIOHTTP_AVAILABLE or aiohttp is None:
            return result

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    result.url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return result
                    html_text = await resp.text()

            # Genius embeds lyrics in data-lyrics-container divs
            # Find the start of each lyrics container and extract content properly
            lyrics_parts = []

            # Find all opening tags of lyrics containers
            container_pattern = re.compile(
                r'<div[^>]*data-lyrics-container="true"[^>]*>',
                re.IGNORECASE
            )

            for match in container_pattern.finditer(html_text):
                start_pos = match.end()
                # Find the matching closing div by counting nested divs
                depth = 1
                pos = start_pos
                while depth > 0 and pos < len(html_text):
                    next_open = html_text.find('<div', pos)
                    next_close = html_text.find('</div>', pos)

                    if next_close == -1:
                        break

                    if next_open != -1 and next_open < next_close:
                        depth += 1
                        pos = next_open + 4
                    else:
                        depth -= 1
                        if depth == 0:
                            content = html_text[start_pos:next_close]
                            # Clean HTML
                            content = re.sub(r'<br\s*/?>', '\n', content)
                            content = re.sub(r'<[^>]+>', '', content)
                            content = html.unescape(content).strip()
                            if content:
                                lyrics_parts.append(content)
                        pos = next_close + 6

            if lyrics_parts:
                result.lyrics_text = '\n\n'.join(lyrics_parts)

        except Exception:
            pass

        return result


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

        # Pre-buffering: cache the next track's audio URL for smoother transitions
        self._prefetched_url: Optional[str] = None
        self._prefetched_track_url: Optional[str] = None  # Track URL this prefetch is for
        self._prefetch_task: Optional[asyncio.Task[None]] = None

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
                'cached_at': time.time(),
                'playlist_url': config.YOUTUBE_PLAYLIST_URL
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
                # Reshuffle if shuffle is enabled
                if self.shuffle_enabled:
                    random.shuffle(self.playlist)
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
        if not yt_dlp:
            return None, False

        try:
            ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': False}

            def extract() -> Dict[str, Any]:
                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                    return ydl.extract_info(track.url, download=False)  # type: ignore

            info = await asyncio.to_thread(extract)

            if not info:
                return None, True  # No info usually means unavailable

            # Find an audio-only format (has audio codec, no video codec)
            # This gives us the smallest stream that Discord can play
            formats = info.get('formats', [])
            for fmt in formats:
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                    return fmt.get('url'), False

            # Fallback to url directly
            return info.get('url'), False

        except Exception as e:
            error_str = str(e).lower()
            # Check for unavailability indicators in the error message
            unavailable_indicators = [
                'video unavailable', 'this video is unavailable',
                'video is private', 'private video',
                'video has been removed', 'been removed',
                'this video is no longer available',
                'sign in to confirm your age',  # Age-restricted without workaround
                'join this channel to get access',  # Members-only
                'this video requires payment',  # Paid content
                'copyright claim', 'blocked',
            ]
            is_unavailable = any(indicator in error_str for indicator in unavailable_indicators)

            if is_unavailable:
                self.logger.warning(f"Video unavailable (will be removed): {track.title} - {e}")
            else:
                self.logger.error(f"Error getting audio URL for {track.title}: {e}")

            return None, is_unavailable

    async def _prefetch_next_track(self) -> None:
        """Pre-fetches the audio URL for the next track in the background.

        This runs while the current track is playing, so when it ends,
        we already have the URL ready and can start playback immediately.
        """
        next_track = self._get_next_track()
        if not next_track:
            self._prefetched_url = None
            self._prefetched_track_url = None
            return

        # Don't refetch if we already have this track prefetched
        if self._prefetched_track_url == next_track.url and self._prefetched_url:
            return

        try:
            self.logger.debug(f"Pre-fetching next track: {next_track.title}")
            audio_url, is_unavailable = await self._get_audio_url(next_track)

            if audio_url:
                self._prefetched_url = audio_url
                self._prefetched_track_url = next_track.url
                self.logger.debug(f"Pre-fetched successfully: {next_track.title}")
            else:
                # Clear prefetch cache on failure
                self._prefetched_url = None
                self._prefetched_track_url = None
                if is_unavailable:
                    self.logger.debug(f"Next track unavailable during prefetch: {next_track.title}")
        except Exception as e:
            self.logger.debug(f"Prefetch failed for {next_track.title}: {e}")
            self._prefetched_url = None
            self._prefetched_track_url = None

    def _clear_prefetch(self) -> None:
        """Clears prefetch cache and cancels any pending prefetch task."""
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
        from utils.views import PaginatorView

        if not self.playlist:
            await respond(target, "No playlist loaded.")
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
                if i == self.current_index:
                    # Highlight currently playing track
                    lines.append(f"▶️ **{track_num}. {track.title}** - {track.artist}")
                else:
                    lines.append(f"{track_num}. {track.title} - {track.artist}")

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
                     f"Shuffle: {'On' if self.shuffle_enabled else 'Off'} • Loop: {self.loop_mode.display}"
            )
            pages.append(embed)

        # Find the page containing the current track and start there
        current_page_idx = self.current_index // tracks_per_page

        if len(pages) == 1:
            await respond(target, embed=pages[0])
        else:
            # Use paginator - wrap interaction if needed
            ctx_for_paginator: Any = target if isinstance(target, commands.Context) else InteractionPseudoContext(target)

            view = PaginatorView(ctx_for_paginator, pages, start_index=current_page_idx)
            msg = await ctx_for_paginator.send(embed=pages[current_page_idx], view=view)
            view.message = msg

    async def _do_shuffle(self, target: Respondable) -> None:
        """Internal implementation for shuffle."""
        self.shuffle_enabled = not self.shuffle_enabled
        self._apply_shuffle(preserve_current=True)
        # Clear prefetch since playlist order changed
        self._clear_prefetch()
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

        # Clear prefetch since we jumped to a different position
        self._clear_prefetch()

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

    async def _do_lyrics(self, target: Respondable, query: Optional[str] = None) -> None:
        """Internal implementation for lyrics search.

        Searches multiple providers for lyrics, presents options to the user,
        and displays the selected lyrics with translation if available.

        Args:
            target: Context or Interaction to respond to.
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
                await respond(target, "🎵 No song is currently playing. Please provide a search query!\n"
                              "Usage: `/lyrics [song name]` or `lyrics [song name]`")
                return

        # Send initial searching message
        if isinstance(target, discord.Interaction):
            await target.response.defer()
            searching_msg = await target.followup.send(f"🔍 Searching for lyrics: **{query}**...")
        else:
            searching_msg = await target.send(f"🔍 Searching for lyrics: **{query}**...")

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

            # Get user selection - wrap interaction in pseudo-context if needed
            # Use buttons_only=True to ignore text input
            ctx_for_selection: Any = target if isinstance(target, commands.Context) else InteractionPseudoContext(target)

            selection = await get_selection(ctx_for_selection, embed, options, buttons_only=True)

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
                if isinstance(target, commands.Context):
                    fetching_msg = await target.send(f"📜 Fetching lyrics from {selected.source}...")
                else:
                    fetching_msg = await target.followup.send(f"📜 Fetching lyrics from {selected.source}...")
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
            try:
                error_msg = f"❌ Couldn't retrieve lyrics from {selected.source}. Try another source."
                if isinstance(target, commands.Context):
                    await target.send(error_msg)
                else:
                    await target.followup.send(error_msg)
            except Exception:
                pass
            return

        # Build lyrics embeds (paginated if long)
        pages: List[discord.Embed] = []

        # Split lyrics into smaller chunks for better readability (1200 chars per page)
        # This prevents embeds from being cut off on mobile/smaller screens
        lyrics_chunks = self._chunk_text(selected.lyrics_text, 1200)

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
            trans_chunks = self._chunk_text(selected.translation_text, 1200)
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
            if isinstance(target, commands.Context):
                await target.send(embed=pages[0])
            else:
                await target.followup.send(embed=pages[0])
        else:
            # Use paginator for multiple pages - wrap interaction if needed
            ctx_for_paginator: Any = target if isinstance(target, commands.Context) else InteractionPseudoContext(target)

            view = PaginatorView(ctx_for_paginator, pages)
            msg = await ctx_for_paginator.send(embed=pages[0], view=view)
            view.message = msg

    def _chunk_text(self, text: str, max_length: int) -> List[str]:
        """Split text into chunks that fit within a character limit.

        Tries to split on paragraph boundaries, then sentences, then words.

        Args:
            text: Text to split.
            max_length: Maximum characters per chunk.

        Returns:
            List of text chunks.
        """
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        current_chunk = ""

        # Split by paragraphs first
        paragraphs = text.split('\n\n')

        for para in paragraphs:
            if len(current_chunk) + len(para) + 2 <= max_length:
                current_chunk = current_chunk + '\n\n' + para if current_chunk else para
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                # Handle paragraphs longer than max_length
                if len(para) > max_length:
                    # Split by lines
                    lines = para.split('\n')
                    current_chunk = ""
                    for line in lines:
                        if len(current_chunk) + len(line) + 1 <= max_length:
                            current_chunk = current_chunk + '\n' + line if current_chunk else line
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            # Truncate very long lines
                            if len(line) > max_length:
                                chunks.append(line[:max_length - 3] + "...")
                                current_chunk = ""
                            else:
                                current_chunk = line
                else:
                    current_chunk = para

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text[:max_length]]

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

    @app_commands.command(name='lyrics', description='Search for song lyrics with translations.')
    @app_commands.describe(query='Song name and/or artist to search for. Leave empty to use current track.')
    async def lyrics_slash(self, interaction: discord.Interaction, query: Optional[str] = None) -> None:
        """Slash command for lyrics search."""
        await self._do_lyrics(interaction, query)

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

    The cog will not load if YOUTUBE_PLAYLIST_URL is not configured.
    """
    if not config.YOUTUBE_PLAYLIST_URL:
        import logging
        logging.getLogger('Music').info("Music cog not loaded: YOUTUBE_PLAYLIST_URL not configured.")
        return

    await bot.add_cog(Music(bot))

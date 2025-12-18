"""utils/music_helpers.py

Helper classes and utilities for the Music cog.

This module contains:
- Data classes (Track, LyricsResult, ActiveSession, LoopMode)
- Lyrics provider scrapers (Genius, LRCLIB, LyricalNonsense)
- Text utilities (chunk_text)
- yt-dlp wrapper functions for audio extraction and search
"""

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast, Dict, List, Optional, TYPE_CHECKING
from urllib.parse import quote_plus

import discord
from discord.ext import commands

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

if TYPE_CHECKING:
    pass  # Add any type-only imports here if needed

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

# FFmpeg options for Discord audio streaming
# Key flags explained:
#   -reconnect 1              : Reconnect on connection loss
#   -reconnect_streamed 1     : Reconnect even for streamed content
#   -reconnect_delay_max 2    : Max 2 seconds between reconnect attempts
#   -fflags +genpts+discardcorrupt : Generate fresh timestamps (prevents speed drift),
#                                    discard corrupt frames instead of trying to fix them
#   -analyzeduration 0        : Don't spend time analyzing the stream format
#   -probesize 32768          : Minimal probe size (32KB) since we know it's audio
#   -thread_queue_size 512    : Larger input queue to absorb network jitter
#   -nostdin                  : Don't read from stdin (minor optimization)
FFMPEG_BEFORE_OPTIONS = (
    '-reconnect 1 '
    '-reconnect_streamed 1 '
    '-reconnect_delay_max 2 '
    '-fflags +genpts+discardcorrupt '
    '-analyzeduration 0 '
    '-probesize 32768 '
    '-thread_queue_size 512 '
    '-nostdin'
)

FFMPEG_OPTIONS = {
    'before_options': FFMPEG_BEFORE_OPTIONS,
    'options': '-vn -filter:a "volume=0.5"'
}

# Cached FFmpeg path (set on first call to get_ffmpeg_path)
_ffmpeg_path: Optional[str] = None


def get_ffmpeg_path() -> str:
    """Gets the path to FFmpeg executable.

    For bundled builds, checks for FFmpeg in the app directory.
    Otherwise, assumes FFmpeg is in system PATH.
    Result is cached after first call.

    Platform behavior:
        - Windows: Looks for ffmpeg.exe in bundled path
        - Linux: Looks for ffmpeg (no extension) in bundled path

    Returns:
        str: Path to FFmpeg executable.
    """
    global _ffmpeg_path

    if _ffmpeg_path:
        return _ffmpeg_path

    # Import here to avoid circular imports
    import os
    import shutil
    import sys
    import config

    # Check for bundled FFmpeg (PyInstaller build)
    if getattr(sys, 'frozen', False):
        ffmpeg_name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
        bundled_path = os.path.join(config.APP_PATH, ffmpeg_name)
        if os.path.exists(bundled_path):
            _ffmpeg_path = bundled_path
            return _ffmpeg_path

    # Fallback to system PATH
    ffmpeg_in_path = shutil.which('ffmpeg')
    if ffmpeg_in_path:
        _ffmpeg_path = ffmpeg_in_path
        return _ffmpeg_path

    # Last resort - just return 'ffmpeg' and let it fail with a clear error
    _ffmpeg_path = 'ffmpeg'
    return _ffmpeg_path


# ==========================================================================
# DATA CLASSES & ENUMS
# ==========================================================================


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
    user_added: bool = False  # True if added by user (not from ambient playlist)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching.

        Note: user_added is intentionally NOT serialized - it's session-only.
        When reloading from cache, all tracks are considered ambient.
        """
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


# ==========================================================================
# TEXT UTILITIES
# ==========================================================================


def chunk_text(text: str, max_length: int) -> List[str]:
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


# ==========================================================================
# YT-DLP WRAPPER FUNCTIONS
# ==========================================================================


async def get_audio_url(track: 'Track', logger: Any) -> tuple[Optional[str], bool]:
    """Gets the actual streamable audio URL for a track.

    Args:
        track: The track to get the audio URL for.
        logger: Logger instance for debug/error messages.

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

        formats = info.get('formats', [])

        # Priority 1: Find audio-only formats (no video codec)
        # These are smallest and most efficient for Discord
        audio_only = [
            fmt for fmt in formats
            if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none' and fmt.get('url')
        ]
        if audio_only:
            # Prefer higher audio bitrate among audio-only formats
            best = max(audio_only, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            return best.get('url'), False

        # Priority 2: Video+audio combined formats (muxed)
        # Less efficient but necessary for some videos that lack audio-only streams
        combined = [
            fmt for fmt in formats
            if fmt.get('acodec') != 'none' and fmt.get('vcodec') != 'none' and fmt.get('url')
        ]
        if combined:
            # Prefer by audio bitrate, then lowest video bitrate (less bandwidth waste)
            best = max(combined, key=lambda f: (f.get('abr') or 0, -(f.get('vbr') or f.get('tbr') or 0)))
            logger.debug(f"Using combined format for {track.title} (no audio-only available)")
            return best.get('url'), False

        # Priority 3: Direct URL fallback (rare, usually livestreams or direct file links)
        if info.get('url'):
            logger.debug(f"Using direct URL fallback for {track.title}")
            return info.get('url'), False

        # No usable format found
        logger.warning(f"No playable format found for {track.title}")
        return None, False

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
            logger.warning(f"Video unavailable (will be removed): {track.title} - {e}")
        else:
            logger.error(f"Error getting audio URL for {track.title}: {e}")

        return None, is_unavailable


async def search_youtube(query: str, max_results: int, logger: Any) -> List['Track']:
    """Searches YouTube for tracks matching the query.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return.
        logger: Logger instance for debug messages.

    Returns:
        A list of Track objects representing search results.
    """
    if not yt_dlp:
        logger.debug("[Search] yt-dlp not available")
        return []

    logger.debug(f"[Search] Starting search for: '{query}' (max_results={max_results})")

    try:
        ydl_opts = {
            **YTDLP_OPTIONS,
            'extract_flat': 'in_playlist',  # Only flatten playlist entries, not search
            'noplaylist': True,  # We want individual videos from search
        }

        # Prefix with ytsearch to explicitly trigger YouTube search
        search_query = f"ytsearch{max_results}:{query}"
        logger.debug(f"[Search] Full search query: '{search_query}'")

        def search() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(search_query, download=False)  # type: ignore

        info = await asyncio.to_thread(search)

        if not info:
            logger.debug("[Search] yt-dlp returned None/empty info")
            return []

        logger.debug(f"[Search] yt-dlp returned info with keys: {list(info.keys())}")
        logger.debug(f"[Search] extractor: {info.get('extractor', 'N/A')}, _type: {info.get('_type', 'N/A')}")

        tracks: List[Track] = []
        entries = info.get('entries', [])

        # If no entries but we have direct video info, treat as single result
        if not entries and info.get('id'):
            logger.debug("[Search] No entries but found direct video, using as single result")
            entries = [info]

        logger.debug(f"[Search] Found {len(entries) if entries else 0} entries")

        for i, entry in enumerate(entries):
            if not entry:
                logger.debug(f"[Search] Entry {i} is None/empty, skipping")
                continue

            logger.debug(f"[Search] Entry {i}: id={entry.get('id')}, title={entry.get('title')}, duration={entry.get('duration')}")

            track = Track(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                url=entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                duration=int(entry.get('duration', 180) or 180),
                thumbnail=entry.get('thumbnail'),
                user_added=True  # Search results are always user-added
            )
            tracks.append(track)

        logger.debug(f"[Search] Returning {len(tracks)} tracks")
        return tracks

    except Exception as e:
        logger.error(f"Error searching YouTube: {e}", exc_info=True)
        return []


async def fetch_url_info(url: str, logger: Any) -> tuple[List['Track'], Optional[str]]:
    """Fetches track info from a YouTube URL (video or playlist).

    Behavior:
    - If the URL contains both a video ID and a playlist ID (e.g., a video link
      with ?list= param), only the single video is extracted.
    - Pure playlist URLs (no video context) extract the entire playlist.
    - "Mix" playlists (list=RD...) are always rejected as they're auto-generated
      and absurdly large.

    Args:
        url: The YouTube URL to fetch.
        logger: Logger instance for error messages.

    Returns:
        A tuple of (tracks, error_message) where:
        - tracks: List of Track objects (single for video, multiple for playlist)
        - error_message: Human-readable error if failed, None if success
    """
    if not yt_dlp:
        return [], "yt-dlp is not available."

    try:
        # Detect URL type and extract relevant parts
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # Check for playlist parameter
        list_param = query_params.get('list', [None])[0]

        # Determine if URL points to a specific video or is a pure playlist link
        has_video_id = False

        # youtu.be/VIDEO_ID format
        if 'youtu.be' in parsed.netloc:
            has_video_id = bool(parsed.path and parsed.path.strip('/'))
        # youtube.com/watch?v=VIDEO_ID format
        elif 'v' in query_params:
            has_video_id = bool(query_params.get('v', [None])[0])
        # youtube.com/shorts/VIDEO_ID format
        elif '/shorts/' in parsed.path:
            has_video_id = True

        # If URL has a video ID, always treat as single video (ignore playlist param)
        # Only reject Mix playlists when it's a pure playlist URL with no video context
        if list_param and list_param.startswith('RD') and not has_video_id:
            logger.info(f"Rejected Mix playlist: {list_param}")
            return [], (
                "I can't add YouTube Mix playlists — they're auto-generated and grow "
                "indefinitely, which could destabilize my queue. Please link a specific "
                "video or a regular playlist instead!"
            )

        # If URL has both video ID and playlist param, treat as single video
        # User linked a specific video, just happens to be from a playlist
        is_playlist = bool(list_param) and not has_video_id

        ydl_opts = {
            **YTDLP_OPTIONS,
            'extract_flat': 'in_playlist' if is_playlist else False,
            'noplaylist': not is_playlist,  # Only extract playlist if pure playlist URL
        }

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            return [], "Could not fetch video information. The URL may be invalid or the video unavailable."

        tracks: List[Track] = []

        # Check if it's a playlist result
        if info.get('_type') == 'playlist' or 'entries' in info:
            entries = info.get('entries', [])
            for entry in entries:
                if not entry:  # Skip unavailable videos
                    continue

                track = Track(
                    title=entry.get('title', 'Unknown Title'),
                    artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                    url=entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                    duration=int(entry.get('duration', 180) or 180),
                    thumbnail=entry.get('thumbnail'),
                    user_added=True
                )
                tracks.append(track)

            if not tracks:
                return [], "The playlist is empty or all videos are unavailable."

        else:
            # Single video
            track = Track(
                title=info.get('title', 'Unknown Title'),
                artist=info.get('uploader', info.get('channel', 'Unknown Artist')),
                url=info.get('webpage_url', url),
                duration=int(info.get('duration', 180) or 180),
                thumbnail=info.get('thumbnail'),
                user_added=True
            )
            tracks.append(track)

        return tracks, None

    except Exception as e:
        error_str = str(e).lower()
        # User-friendly error messages
        if 'video unavailable' in error_str or 'unavailable' in error_str:
            return [], "This video is unavailable. It may be private, deleted, or region-locked."
        elif 'private video' in error_str:
            return [], "This video is private."
        elif 'sign in' in error_str:
            return [], "This video is age-restricted and cannot be played."
        elif 'copyright' in error_str or 'blocked' in error_str:
            return [], "This video is blocked due to copyright."
        else:
            logger.error(f"Error fetching URL info: {e}", exc_info=True)
            return [], f"Could not access that URL: {e}"


async def fetch_playlist_metadata(playlist_url: str, logger: Any) -> List['Track']:
    """Fetches playlist metadata from YouTube using yt-dlp (flat extraction).

    This is used for loading the ambient playlist at startup. It only fetches
    metadata (title, artist, duration) without extracting audio URLs.

    Args:
        playlist_url: The YouTube playlist URL to fetch.
        logger: Logger instance for info/error messages.

    Returns:
        A list of Track objects representing the playlist entries.
    """
    if not yt_dlp:
        return []

    try:
        ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': 'in_playlist'}

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(playlist_url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            logger.error("Failed to extract playlist info.")
            return []

        tracks: List[Track] = []
        entries = info.get('entries', [])

        for entry in entries:
            if not entry:  # Skip unavailable videos
                continue

            track = Track(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                url=entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                duration=entry.get('duration', 180),  # Default 3 min if unknown
                thumbnail=entry.get('thumbnail')
            )
            tracks.append(track)

        return tracks

    except Exception as e:
        logger.error(f"Error fetching playlist: {e}", exc_info=True)
        return []

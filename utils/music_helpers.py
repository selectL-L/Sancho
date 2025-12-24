"""utils/music_helpers.py

Helper classes and utilities for the Music cog.

This module contains:
- Data classes (Track, LyricsResult, ActiveSession, LoopMode, DownloadResult)
- Lyrics provider scrapers (Genius, LRCLIB, LyricalNonsense)
- Text utilities (chunk_text)
- yt-dlp wrapper functions for audio extraction and search
- MP3 download with full metadata embedding
"""

from typing import Set
import shutil
import logging
import json as json_module
import hashlib
import asyncio
import html
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, cast
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

# Attempt to import mutagen for MP3 metadata editing
try:
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK, TYER, TCON, COMM  # type: ignore[attr-defined]
    from mutagen.mp3 import MP3
    MUTAGEN_AVAILABLE = True
except ImportError:
    APIC = ID3 = TALB = TIT2 = TPE1 = TRCK = TYER = TCON = COMM = None  # type: ignore[misc, assignment]
    MP3 = None  # type: ignore[misc, assignment]
    MUTAGEN_AVAILABLE = False

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
    # loudnorm: EBU R128 volume normalization for consistent loudness across tracks
    # I=target integrated loudness (-14 LUFS is typical for streaming)
    # LRA=loudness range (7 is moderate dynamic range preservation)
    # TP=true peak limit (-1 dB prevents clipping)
    # volume=0.5: additional headroom reduction for safety
    'options': '-vn -filter:a "loudnorm=I=-14:LRA=7:TP=-1,volume=0.5"'
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
        except KeyError as e:
            raise commands.BadArgument(f"'{argument}' is not a valid loop mode. Use: off, one, or all") from e

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
    thumbnail_needs_crop: bool = False  # True if thumbnail needs center-crop to square
    user_added: bool = False  # True if added by user (not from ambient playlist)
    local_path: Optional[str] = None  # Path to locally cached MP3 file
    video_id: Optional[str] = None  # YouTube video ID (extracted from URL)

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
            'thumbnail': self.thumbnail,
            'thumbnail_needs_crop': self.thumbnail_needs_crop,
            'local_path': self.local_path,
            'video_id': self.video_id
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Track':
        """Create from dictionary."""
        return cls(
            title=data['title'],
            artist=data['artist'],
            url=data['url'],
            duration=data['duration'],
            thumbnail=data.get('thumbnail'),
            thumbnail_needs_crop=data.get('thumbnail_needs_crop', False),
            local_path=data.get('local_path'),
            video_id=data.get('video_id') or extract_video_id(data['url'])
        )

    @property
    def is_cached(self) -> bool:
        """Returns True if this track has a valid local cache file."""
        return self.local_path is not None and os.path.exists(self.local_path)


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


async def get_audio_url(track: 'Track', logger: Any) -> tuple[Optional[str], bool, Optional[str], bool]:
    """Gets the actual streamable audio URL for a track.

    Args:
        track: The track to get the audio URL for.
        logger: Logger instance for debug/error messages.

    Returns:
        A tuple of (url, is_unavailable, thumbnail, needs_crop) where:
        - url: The streamable URL, or None if failed
        - is_unavailable: True if the video is permanently unavailable and should be removed
        - thumbnail: Best thumbnail URL found, or None
        - needs_crop: True if thumbnail needs center-cropping to extract album art
    """
    if not yt_dlp:
        return None, False, None, False

    try:
        ydl_opts = {**YTDLP_OPTIONS, 'extract_flat': False}

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(track.url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            return None, True, None, False  # No info usually means unavailable

        # Extract best thumbnail - prefer square (for album art)
        thumbnail_url, needs_crop = await _extract_best_thumbnail(info, logger)

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
            return best.get('url'), False, thumbnail_url, needs_crop

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
            return best.get('url'), False, thumbnail_url, needs_crop

        # Priority 3: Direct URL fallback (rare, usually livestreams or direct file links)
        if info.get('url'):
            logger.debug(f"Using direct URL fallback for {track.title}")
            return info.get('url'), False, thumbnail_url, needs_crop

        # No usable format found
        logger.warning(f"No playable format found for {track.title}")
        return None, False, thumbnail_url, needs_crop

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

        return None, is_unavailable, None, False


async def _probe_thumbnail_dimensions(url: str, logger: Any) -> Optional[tuple[int, int]]:
    """Uses ffprobe to get actual dimensions of a thumbnail URL.

    Args:
        url: The thumbnail URL to probe.
        logger: Logger for debug output.

    Returns:
        Tuple of (width, height) or None if probe fails.
    """
    ffmpeg_path = get_ffmpeg_path()
    # ffprobe is in the same directory as ffmpeg
    if ffmpeg_path.endswith('.exe'):
        ffprobe_path = ffmpeg_path.replace('ffmpeg.exe', 'ffprobe.exe')
    else:
        ffprobe_path = ffmpeg_path.replace('ffmpeg', 'ffprobe')

    cmd = [
        ffprobe_path,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=p=0',
        url
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

        output = stdout.decode().strip()
        if ',' in output:
            w, h = output.split(',')
            return int(w), int(h)
    except asyncio.TimeoutError:
        logger.debug(f"[Thumbnail] ffprobe timeout for {url[:60]}...")
    except Exception as e:
        logger.debug(f"[Thumbnail] ffprobe failed: {e}")

    return None


def extract_mp3_thumbnail(mp3_path: str, logger: Any) -> Optional[bytes]:
    """Extracts embedded cover art from an MP3 file.

    Reads the APIC (Attached Picture) frame from the MP3's ID3 tags.
    This is used to retrieve thumbnails from cached MP3 files without
    hitting YouTube.

    Args:
        mp3_path: Path to the MP3 file.
        logger: Logger for debug output.

    Returns:
        Image bytes if found, None otherwise.
    """
    if not MUTAGEN_AVAILABLE or not os.path.exists(mp3_path):
        return None

    try:
        audio = MP3(mp3_path, ID3=ID3)  # type: ignore[misc]
        if audio.tags is None:
            logger.debug(f"[Thumbnail] No ID3 tags in {os.path.basename(mp3_path)}")
            return None

        # Look for APIC frames (cover art)
        for key in audio.tags.keys():
            if key.startswith('APIC'):
                apic = audio.tags[key]
                if apic.data:
                    logger.debug(f"[Thumbnail] Extracted {len(apic.data)} bytes from MP3 ({apic.mime})")
                    return apic.data

        logger.debug(f"[Thumbnail] No APIC frame in {os.path.basename(mp3_path)}")
    except Exception as e:
        logger.debug(f"[Thumbnail] Failed to extract from MP3: {e}")

    return None


async def get_best_thumbnail_bytes(
    track: 'Track',
    logger: Any,
    yt_info: Optional[Dict[str, Any]] = None
) -> Optional[bytes]:
    """Gets the best thumbnail bytes for a track using unified logic.

    Priority order:
    1. Extract from cached MP3 file (already processed, best quality)
    2. Use _extract_best_thumbnail to find best URL, then crop/convert if needed
    3. Fall back to basic thumbnail URL with cropping

    This is THE ONLY function that should be used to get thumbnail bytes.
    All other thumbnail handling should go through this function.

    Args:
        track: The Track object (may have local_path for cached file).
        logger: Logger for debug output.
        yt_info: Optional yt-dlp info dict (avoids re-fetching if already available).

    Returns:
        JPEG image bytes, or None if no thumbnail available.
    """
    # Priority 1: Extract from cached MP3 (already has processed thumbnail)
    if track.local_path and os.path.exists(track.local_path):
        logger.debug(f"[Thumbnail] Checking cached MP3: {os.path.basename(track.local_path)}")
        thumbnail_data = extract_mp3_thumbnail(track.local_path, logger)
        if thumbnail_data:
            logger.debug("[Thumbnail] Using embedded thumbnail from cached MP3")
            return thumbnail_data

    # Priority 2: Use _extract_best_thumbnail if we have yt_info
    if yt_info:
        logger.debug("[Thumbnail] Using yt-dlp info for thumbnail selection")
        thumbnail_url, needs_crop = await _extract_best_thumbnail(yt_info, logger)
        if thumbnail_url:
            if needs_crop:
                logger.debug(f"[Thumbnail] Cropping: {thumbnail_url[:60]}...")
                return await crop_thumbnail_to_square(thumbnail_url, logger)
            else:
                # Square thumbnail - just download and convert to JPEG
                logger.debug(f"[Thumbnail] Square thumbnail, converting to JPEG: {thumbnail_url[:60]}...")
                return await crop_thumbnail_to_square(thumbnail_url, logger)  # Still use this for JPEG conversion

    # Priority 3: Use track's cached thumbnail URL (from flat extraction)
    if track.thumbnail:
        logger.debug(f"[Thumbnail] Using track's thumbnail URL: {track.thumbnail[:60]}...")
        # Always crop/convert YouTube thumbnails
        return await crop_thumbnail_to_square(track.thumbnail, logger)

    # Priority 4: Construct URL from video ID as last resort
    if track.video_id:
        fallback_url = f"https://img.youtube.com/vi/{track.video_id}/hqdefault.jpg"
        logger.debug(f"[Thumbnail] Using constructed fallback URL: {fallback_url}")
        return await crop_thumbnail_to_square(fallback_url, logger)

    logger.debug("[Thumbnail] No thumbnail source available")
    return None


async def crop_thumbnail_to_square(url: str, logger: Any) -> Optional[bytes]:
    """Crops a thumbnail to a square by extracting the center portion.

    YouTube Music "Art Track" videos have album art centered in a 16:9 frame.
    This function extracts the center square, which contains the album art.

    Args:
        url: The thumbnail URL to crop.
        logger: Logger for debug output.

    Returns:
        JPEG bytes of the cropped square image, or None if cropping fails.
    """
    ffmpeg_path = get_ffmpeg_path()

    # crop filter: crop=out_w:out_h:x:y
    # For center square: crop=min(iw,ih):min(iw,ih):(iw-min(iw,ih))/2:(ih-min(iw,ih))/2
    # Simplified: crop=ih:ih:(iw-ih)/2:0 for landscape (width > height)
    crop_filter = "crop='min(iw,ih):min(iw,ih):(iw-min(iw,ih))/2:(ih-min(iw,ih))/2'"

    cmd = [
        ffmpeg_path,
        '-y',  # Overwrite output
        '-i', url,
        '-vf', crop_filter,
        '-frames:v', '1',  # Only one frame (it's an image)
        '-c:v', 'mjpeg',  # Encode as JPEG (converts webp/png to jpeg)
        '-f', 'image2',  # Single image output format
        '-q:v', '2',  # High quality (1-31, lower = better)
        'pipe:1'  # Output to stdout
    ]

    logger.debug(f"[Thumbnail] Cropping to square: {url[:80]}...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

        if proc.returncode == 0 and stdout:
            logger.debug(f"[Thumbnail] Cropped successfully, {len(stdout)} bytes")
            return stdout
        else:
            logger.debug(f"[Thumbnail] Crop failed: {stderr.decode()[:200] if stderr else 'no stderr'}")
    except asyncio.TimeoutError:
        logger.debug(f"[Thumbnail] Crop timeout for {url[:60]}...")
    except Exception as e:
        logger.debug(f"[Thumbnail] Crop failed: {e}")

    return None


async def _extract_best_thumbnail(info: Dict[str, Any], logger: Any) -> tuple[Optional[str], bool]:
    """Extracts the best thumbnail URL from yt-dlp info, preferring square images.

    Uses ffprobe to determine dimensions of thumbnails that don't have them.
    Filters out thumbnails under 480px and prefers square album art.

    Args:
        info: The yt-dlp extraction info dict.
        logger: Logger for debug output.

    Returns:
        Tuple of (thumbnail_url, needs_crop). If needs_crop is True, the caller
        should use crop_thumbnail_to_square() to extract the center square.
    """
    MIN_SIZE = 480  # Minimum acceptable dimension

    thumbnails = info.get('thumbnails', [])

    logger.debug(f"[Thumbnail] Found {len(thumbnails)} raw thumbnails from yt-dlp")

    if not thumbnails:
        fallback = info.get('thumbnail')
        logger.debug(f"[Thumbnail] No thumbnails list, using fallback: {fallback}")
        return fallback, True  # Assume fallback needs crop

    # Log all thumbnails and probe those missing dimensions
    unknown_dims = []
    for i, t in enumerate(thumbnails):
        w, h = t.get('width'), t.get('height')
        if w and h:
            url_preview = t.get('url', 'no-url')[:80] + '...' if len(t.get('url', '')) > 80 else t.get('url', 'no-url')
            logger.debug(f"[Thumbnail] [{i}] {w}x{h} - {url_preview}")
        else:
            unknown_dims.append((i, t))

    # Probe unknown dimensions with ffprobe (limit to avoid slowdown)
    if unknown_dims:
        logger.debug(f"[Thumbnail] Probing {len(unknown_dims)} thumbnails with unknown dimensions...")
        for i, t in unknown_dims[:5]:  # Probe max 5 to avoid delays
            url = t.get('url')
            if url:
                dims = await _probe_thumbnail_dimensions(url, logger)
                if dims:
                    t['width'], t['height'] = dims
                    t['_probed'] = True
                    logger.debug(f"[Thumbnail] [{i}] {dims[0]}x{dims[1]} (probed) - {url[:80]}...")
                else:
                    logger.debug(f"[Thumbnail] [{i}] ?x? (probe failed) - {url[:80]}...")

    # Filter to only thumbnails with known dimensions >= MIN_SIZE
    usable = [
        t for t in thumbnails
        if t.get('width') and t.get('height')
        and t.get('width') >= MIN_SIZE and t.get('height') >= MIN_SIZE
    ]

    logger.debug(f"[Thumbnail] {len(usable)} thumbnails pass {MIN_SIZE}px minimum filter")

    if not usable:
        # Nothing meets minimum size - fall back to largest available
        with_dims = [t for t in thumbnails if t.get('width') and t.get('height')]
        if with_dims:
            best = max(with_dims, key=lambda t: t.get('width', 0) * t.get('height', 0))
            logger.debug(f"[Thumbnail] No thumbnails >= {MIN_SIZE}px, using largest: {best.get('width')}x{best.get('height')}")
            is_square = best.get('width') == best.get('height')
            return best.get('url'), not is_square
        # No dimensions at all - return any URL
        for t in thumbnails:
            if t.get('url'):
                logger.debug("[Thumbnail] No dimension info available, using first URL")
                return t.get('url'), True  # Assume needs crop
        return info.get('thumbnail'), True

    # Try to find square thumbnails (width == height) - these are album art
    square_thumbnails = [t for t in usable if t.get('width') == t.get('height')]

    if square_thumbnails:
        best = max(square_thumbnails, key=lambda t: t.get('width', 0))
        logger.debug(f"[Thumbnail] Found {len(square_thumbnails)} square thumbnails >= {MIN_SIZE}px, using: {best.get('width')}x{best.get('height')}")
        return best.get('url'), False  # Already square, no crop needed

    # No square thumbnail - return the largest one for cropping
    best = max(usable, key=lambda t: t.get('width', 0) * t.get('height', 0))
    logger.debug(f"[Thumbnail] No square found, will crop largest: {best.get('width')}x{best.get('height')}")
    return best.get('url'), True  # Needs cropping


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


def detect_mix_in_url(url: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Detects if a URL contains both a video ID and a mix playlist.

    This is used to prompt users whether they want just the single video
    or the entire mix playlist.

    Args:
        url: The YouTube URL to check.

    Returns:
        A tuple of (has_mix, single_video_url, mix_playlist_url) where:
        - has_mix: True if the URL has both a video ID and a mix playlist
        - single_video_url: URL for just the single video (stripped of playlist param)
        - mix_playlist_url: URL for fetching the mix playlist (stripped of video param)
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)

    # Check for playlist parameter that's a mix (starts with RD)
    list_param = query_params.get('list', [None])[0]
    if not list_param or not list_param.startswith('RD'):
        return False, None, None

    # Check if there's also a video ID
    video_id = None
    if 'youtu.be' in parsed.netloc:
        video_id = parsed.path.strip('/')
    elif 'v' in query_params:
        video_id = query_params.get('v', [None])[0]
    elif '/shorts/' in parsed.path:
        video_id = parsed.path.split('/shorts/')[-1].split('/')[0]

    if not video_id:
        return False, None, None

    # Build single video URL (no playlist param)
    single_params = {k: v[0] for k, v in query_params.items() if k != 'list'}
    single_query = urlencode(single_params)
    single_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', single_query, ''))

    # For mix playlists, keep the original URL format - mixes require video context
    # (playlist?list=RD... URLs don't work, you need watch?v=VIDEO&list=RD...)
    # Just return the original URL; fetch_url_info with force_playlist=True will handle it
    mix_url = url

    return True, single_url, mix_url


async def fetch_url_info(
    url: str,
    logger: Any,
    force_playlist: bool = False
) -> tuple[List['Track'], Optional[str], Optional[str]]:
    """Fetches track info from a YouTube URL (video or playlist).

    Behavior:
    - If the URL contains both a video ID and a playlist ID (e.g., a video link
      with ?list= param), only the single video is extracted UNLESS force_playlist=True.
    - Pure playlist URLs (no video context) extract the entire playlist.
    - "Mix" playlists (list=RD...) are limited to 60 tracks to prevent crashes.

    Args:
        url: The YouTube URL to fetch.
        logger: Logger instance for error messages.
        force_playlist: If True, extract the playlist even if URL has a video ID.

    Returns:
        A tuple of (tracks, error_message, warning_message) where:
        - tracks: List of Track objects (single for video, multiple for playlist)
        - error_message: Human-readable error if failed, None if success
        - warning_message: Non-fatal warning (e.g., mix truncation), None if none
    """
    if not yt_dlp:
        return [], "yt-dlp is not available.", None

    try:
        # Detect URL type and extract relevant parts
        from urllib.parse import parse_qs, urlparse

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

        # Check if this is a Mix playlist (auto-generated, potentially infinite)
        is_mix_playlist = bool(list_param and list_param.startswith('RD'))

        # Determine if we should extract as playlist
        # - If force_playlist is True, always treat as playlist (for mix prompts)
        # - Otherwise, only treat as playlist if there's no video ID
        if force_playlist and list_param:
            is_playlist = True
        else:
            # If URL has both video ID and playlist param, treat as single video
            # User linked a specific video, just happens to be from a playlist
            is_playlist = bool(list_param) and not has_video_id

        # Playlist limits: mixes capped at 60, regular playlists at 1000
        if is_mix_playlist:
            playlist_limit = 60
        elif is_playlist:
            playlist_limit = 1000
        else:
            playlist_limit = None

        ydl_opts = {
            **YTDLP_OPTIONS,
            'extract_flat': 'in_playlist' if is_playlist else False,
            'noplaylist': not is_playlist,  # Only extract playlist if pure playlist URL
        }

        # Add playlist limit
        if playlist_limit:
            ydl_opts['playlistend'] = playlist_limit
            if is_mix_playlist:
                logger.info(f"Mix playlist detected - limiting to {playlist_limit} tracks")
            else:
                logger.info(f"Playlist detected - limiting to {playlist_limit} tracks")

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            return [], "Could not fetch video information. The URL may be invalid or the video unavailable.", None

        tracks: List[Track] = []
        was_truncated = False

        # Check if it's a playlist result
        if info.get('_type') == 'playlist' or 'entries' in info:
            entries = info.get('entries', [])

            # Check if playlist was truncated
            if playlist_limit and len(entries) >= playlist_limit:
                was_truncated = True

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
                return [], "The playlist is empty or all videos are unavailable.", None

            # Return warning if playlist was truncated
            warning = None
            if was_truncated:
                if is_mix_playlist:
                    warning = (
                        f"⚠️ This is a Mix playlist - I only loaded the first {len(tracks)} tracks. "
                        "Mix playlists grow indefinitely and could crash the bot!"
                    )
                else:
                    warning = f"⚠️ This playlist was truncated to {len(tracks)} tracks (limit: {playlist_limit})."

            return tracks, None, warning

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

        return tracks, None, None

    except Exception as e:
        error_str = str(e).lower()
        # User-friendly error messages
        if 'video unavailable' in error_str or 'unavailable' in error_str:
            return [], "This video is unavailable. It may be private, deleted, or region-locked.", None
        elif 'private video' in error_str:
            return [], "This video is private.", None
        elif 'sign in' in error_str:
            return [], "This video is age-restricted and cannot be played.", None
        elif 'copyright' in error_str or 'blocked' in error_str:
            return [], "This video is blocked due to copyright.", None
        else:
            logger.error(f"Error fetching URL info: {e}", exc_info=True)
            return [], f"Could not access that URL: {e}", None


def extract_video_id(url: str) -> Optional[str]:
    """Extracts the YouTube video ID from a URL.

    Handles various YouTube URL formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - URLs with additional parameters

    Args:
        url: YouTube video URL.

    Returns:
        11-character video ID, or None if not found.
    """
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'  # Raw video ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


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

            # Get video ID from entry or extract from URL
            video_id = entry.get('id')
            video_url = entry.get('url') or f"https://www.youtube.com/watch?v={video_id or ''}"
            if not video_id:
                video_id = extract_video_id(video_url)

            track = Track(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', entry.get('channel', 'Unknown Artist')),
                url=video_url,
                duration=entry.get('duration', 180),  # Default 3 min if unknown
                thumbnail=entry.get('thumbnail'),
                video_id=video_id
            )
            tracks.append(track)

        return tracks

    except Exception as e:
        logger.error(f"Error fetching playlist: {e}", exc_info=True)
        return []


# ==========================================================================
# MP3 DOWNLOAD WITH METADATA
# ==========================================================================


@dataclass
class DownloadResult:
    """Result of an MP3 download operation."""
    success: bool
    file_path: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    thumbnail_embedded: bool = False

    @property
    def filename(self) -> Optional[str]:
        """Returns just the filename without the full path."""
        if self.file_path:
            return os.path.basename(self.file_path)
        return None


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitizes a string for use as a filename.

    Args:
        name: The string to sanitize.
        max_length: Maximum length of the resulting filename.

    Returns:
        A filesystem-safe filename string.
    """
    # Remove or replace problematic characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    # Remove control characters
    name = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', name)
    # Collapse multiple underscores/spaces
    name = re.sub(r'[_\s]+', '_', name)
    # Strip leading/trailing whitespace and dots
    name = name.strip(' ._')
    # Truncate if too long
    if len(name) > max_length:
        name = name[:max_length].rstrip(' ._')
    return name or 'untitled'


async def download_track_as_mp3(
    url: str,
    output_dir: str,
    logger: Any,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    custom_album: Optional[str] = None,
    custom_genre: Optional[str] = None,
    custom_year: Optional[str] = None,
    custom_track_num: Optional[str] = None,
    custom_comment: Optional[str] = None,
    embed_thumbnail: bool = True
) -> DownloadResult:
    """Downloads a track from YouTube as MP3 with full metadata.

    Downloads audio from a YouTube URL, converts to MP3, and embeds
    ID3 metadata including cover art. Metadata can be customized or
    auto-populated from YouTube.

    Args:
        url: YouTube URL to download.
        output_dir: Directory to save the MP3 file.
        logger: Logger instance for messages.
        custom_title: Override the track title (None = use YouTube title).
        custom_artist: Override the artist (None = use uploader/channel).
        custom_album: Album name to embed (None = use YouTube album if available).
        custom_genre: Genre tag to embed.
        custom_year: Year tag to embed (None = auto-detect from upload date).
        custom_track_num: Track number tag (e.g., "1" or "1/12").
        custom_comment: Comment tag to embed.
        embed_thumbnail: Whether to embed the thumbnail as cover art.

    Returns:
        DownloadResult with success status, file path, and metadata.
    """
    if not YTDLP_AVAILABLE:
        return DownloadResult(
            success=False,
            error_message="yt-dlp is not installed. Install with: pip install yt-dlp"
        )

    if not MUTAGEN_AVAILABLE:
        return DownloadResult(
            success=False,
            error_message="mutagen is not installed. Install with: pip install mutagen"
        )

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Temporary file template - we'll rename after getting metadata
    temp_template = os.path.join(output_dir, 'temp_%(id)s.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': temp_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'writethumbnail': embed_thumbnail,  # Download thumbnail for manual embedding
        'nocheckcertificate': True,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        logger.info(f"[Download] Starting download: {url}")

        def do_download() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=True)  # type: ignore

        info = await asyncio.to_thread(do_download)

        if not info:
            return DownloadResult(
                success=False,
                error_message="Failed to extract video information."
            )

        # Extract metadata from yt-dlp info
        video_id = info.get('id', 'unknown')
        yt_title = info.get('title', 'Unknown Title')
        yt_artist = info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown Artist')
        yt_album = info.get('album')
        yt_duration = info.get('duration', 0)
        yt_year = None
        if info.get('upload_date'):
            yt_year = info['upload_date'][:4]  # YYYYMMDD -> YYYY

        # Apply custom metadata or use YouTube defaults
        final_title = custom_title or yt_title
        final_artist = custom_artist or yt_artist
        final_album = custom_album or yt_album
        final_year = custom_year or yt_year

        # Find the downloaded MP3 file
        temp_mp3_path = os.path.join(output_dir, f'temp_{video_id}.mp3')

        if not os.path.exists(temp_mp3_path):
            # Sometimes yt-dlp uses different naming
            for f in os.listdir(output_dir):
                if f.startswith(f'temp_{video_id}') and f.endswith('.mp3'):
                    temp_mp3_path = os.path.join(output_dir, f)
                    break

        if not os.path.exists(temp_mp3_path):
            return DownloadResult(
                success=False,
                error_message=f"Downloaded file not found. Expected: temp_{video_id}.mp3"
            )

        # Generate final filename
        safe_artist = sanitize_filename(final_artist, 60)
        safe_title = sanitize_filename(final_title, 120)
        final_filename = f"{safe_artist} - {safe_title}.mp3"
        final_path = os.path.join(output_dir, final_filename)

        # Handle filename collision
        counter = 1
        while os.path.exists(final_path):
            final_filename = f"{safe_artist} - {safe_title} ({counter}).mp3"
            final_path = os.path.join(output_dir, final_filename)
            counter += 1

        # Rename temp file to final name
        os.rename(temp_mp3_path, final_path)
        logger.info(f"[Download] MP3 saved as: {final_filename}")

        # Embed metadata using mutagen
        thumbnail_embedded = False
        try:
            audio = MP3(final_path, ID3=ID3)  # type: ignore[misc]

            # Create ID3 tag if it doesn't exist
            try:
                audio.add_tags()
            except Exception:
                pass  # Tags already exist

            # Set metadata tags
            audio.tags.add(TIT2(encoding=3, text=final_title))  # type: ignore[misc]  # Title
            audio.tags.add(TPE1(encoding=3, text=final_artist))  # type: ignore[misc]  # Artist

            if final_album:
                audio.tags.add(TALB(encoding=3, text=final_album))  # type: ignore[misc]  # Album

            if final_year:
                audio.tags.add(TYER(encoding=3, text=final_year))  # type: ignore[misc]  # Year

            if custom_genre:
                audio.tags.add(TCON(encoding=3, text=custom_genre))  # type: ignore[misc]  # Genre

            if custom_track_num:
                audio.tags.add(TRCK(encoding=3, text=custom_track_num))  # type: ignore[misc]  # Track number

            if custom_comment:
                audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=custom_comment))  # type: ignore[misc]  # Comment

            # Embed thumbnail as cover art
            if embed_thumbnail:
                thumbnail_embedded = await _embed_thumbnail_in_mp3(
                    audio, info, output_dir, video_id, logger
                )

            audio.save()
            logger.info("[Download] Metadata embedded successfully")

        except Exception as e:
            logger.warning(f"[Download] Failed to embed some metadata: {e}")

        # Cleanup thumbnail files
        for f in os.listdir(output_dir):
            if f.startswith(f'temp_{video_id}') and not f.endswith('.mp3'):
                try:
                    os.remove(os.path.join(output_dir, f))
                except Exception:
                    pass

        return DownloadResult(
            success=True,
            file_path=final_path,
            title=final_title,
            artist=final_artist,
            album=final_album,
            duration=yt_duration,
            thumbnail_embedded=thumbnail_embedded
        )

    except Exception as e:
        error_str = str(e).lower()
        logger.error(f"[Download] Error: {e}", exc_info=True)

        # Cleanup any temp files
        for f in os.listdir(output_dir):
            if f.startswith('temp_'):
                try:
                    os.remove(os.path.join(output_dir, f))
                except Exception:
                    pass

        # User-friendly error messages
        if 'video unavailable' in error_str or 'unavailable' in error_str:
            return DownloadResult(success=False, error_message="Video is unavailable.")
        elif 'private video' in error_str:
            return DownloadResult(success=False, error_message="Video is private.")
        elif 'sign in' in error_str:
            return DownloadResult(success=False, error_message="Video is age-restricted.")
        elif 'copyright' in error_str or 'blocked' in error_str:
            return DownloadResult(success=False, error_message="Video is blocked due to copyright.")
        else:
            return DownloadResult(success=False, error_message=str(e))


async def _embed_thumbnail_in_mp3(
    audio: Any,
    info: Dict[str, Any],
    output_dir: str,
    video_id: str,
    logger: Any
) -> bool:
    """Embeds thumbnail as cover art in an MP3 file.

    Uses _extract_best_thumbnail() for proper thumbnail selection (prefers square),
    then crop_thumbnail_to_square() for processing. This ensures we use the same
    logic as the now playing widget.

    Args:
        audio: Mutagen MP3 object with ID3 tags.
        info: yt-dlp extraction info dict.
        output_dir: Directory where temp files are stored.
        video_id: YouTube video ID.
        logger: Logger for debug messages.

    Returns:
        True if thumbnail was embedded, False otherwise.
    """
    thumbnail_data = None

    # Use _extract_best_thumbnail for proper thumbnail selection
    # This prefers square album art over 16:9 video thumbnails
    try:
        thumbnail_url, needs_crop = await _extract_best_thumbnail(info, logger)
        if thumbnail_url:
            # Always run through crop_thumbnail_to_square for JPEG conversion
            # (it handles both cropping and format conversion)
            thumbnail_data = await crop_thumbnail_to_square(thumbnail_url, logger)
            if thumbnail_data:
                logger.debug(f"[Download] Thumbnail: {thumbnail_url[:50]}... (crop={needs_crop})")
    except Exception as e:
        logger.debug(f"[Download] Failed to process thumbnail: {e}")

    if thumbnail_data:
        try:
            audio.tags.add(APIC(  # type: ignore[misc]
                encoding=3,
                mime='image/jpeg',  # crop_thumbnail_to_square always outputs JPEG
                type=3,  # Front cover
                desc='Cover',
                data=thumbnail_data
            ))
            return True
        except Exception as e:
            logger.warning(f"[Download] Failed to embed thumbnail: {e}")

    return False


async def get_track_info_for_download(url: str, logger: Any) -> Optional[Dict[str, Any]]:
    """Gets track metadata without downloading, for preview purposes.

    Useful for showing the user what will be downloaded before committing.

    Args:
        url: YouTube URL to inspect.
        logger: Logger instance.

    Returns:
        Dict with title, artist, album, duration, thumbnail, or None on failure.
    """
    if not YTDLP_AVAILABLE:
        return None

    try:
        ydl_opts = {
            **YTDLP_OPTIONS,
            'extract_flat': False,
            'skip_download': True,
        }

        def extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)  # type: ignore

        info = await asyncio.to_thread(extract)

        if not info:
            return None

        return {
            'title': info.get('title', 'Unknown Title'),
            'artist': info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown Artist'),
            'album': info.get('album'),
            'duration': info.get('duration', 0),
            'thumbnail': info.get('thumbnail'),
            'upload_date': info.get('upload_date'),
            'url': url,
        }

    except Exception as e:
        logger.error(f"[Download] Error getting track info: {e}")
        return None


# ==========================================================================
# LOCAL MUSIC CACHE SYSTEM
# ==========================================================================
# Maps playlist URLs to local folders containing cached MP3 files.
# Structure:
#   cache/music/library/
#     manifest.json          - Index of all cached playlists
#     <playlist_id>/         - Folder per playlist (hash of URL)
#       <artist> - <title>.mp3
#       ...


@dataclass
class CachedPlaylist:
    """Represents a cached playlist in the local library."""
    playlist_url: str
    folder_name: str  # Hash-based folder name
    display_name: str  # Human-readable name (from YouTube)
    track_count: int
    cached_count: int  # How many tracks are actually downloaded
    last_updated: float  # Unix timestamp
    tracks: List[Dict[str, Any]]  # Track metadata with local_path


class LocalMusicCache:
    """Manages the local cache of downloaded music for ambient playlists.

    The cache maps playlist URLs to local folders. Each unique playlist URL
    gets a deterministic folder name (hash of URL), so the same playlist
    referenced multiple times in ambience.toml only needs to be downloaded once.

    Manifest structure (manifest.json):
        {
            "version": 1,
            "playlists": {
                "<folder_name>": {
                    "playlist_url": "...",
                    "display_name": "...",
                    "track_count": N,
                    "cached_count": N,
                    "last_updated": timestamp,
                    "tracks": [...]
                }
            }
        }
    """

    MANIFEST_VERSION = 1

    def __init__(self, base_path: str, logger: Any):
        """Initialize the local music cache.

        Args:
            base_path: Base path for music cache (config.MUSIC_CACHE_PATH).
            logger: Logger instance for messages.
        """
        self.library_path = os.path.join(base_path, 'library')
        self.manifest_path = os.path.join(self.library_path, 'manifest.json')
        self.logger = logger
        self._manifest: Optional[Dict[str, Any]] = None

    def _ensure_library_exists(self) -> None:
        """Creates the library directory if it doesn't exist."""
        os.makedirs(self.library_path, exist_ok=True)

    def _load_manifest(self) -> Dict[str, Any]:
        """Loads or creates the manifest file."""
        if self._manifest is not None:
            return self._manifest

        self._ensure_library_exists()

        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, 'r', encoding='utf-8') as f:
                    self._manifest = json_module.load(f)
                    # Version migration could happen here
                    if self._manifest is not None:
                        return self._manifest
            except (json_module.JSONDecodeError, IOError) as e:
                self.logger.warning(f"[Cache] Manifest corrupted, recreating: {e}")

        # Create empty manifest
        self._manifest = {
            'version': self.MANIFEST_VERSION,
            'playlists': {}
        }
        return self._manifest

    def _save_manifest(self) -> None:
        """Saves the manifest to disk."""
        self._ensure_library_exists()
        try:
            with open(self.manifest_path, 'w', encoding='utf-8') as f:
                json_module.dump(self._manifest, f, indent=2, ensure_ascii=False)
        except IOError as e:
            self.logger.error(f"[Cache] Failed to save manifest: {e}")

    @staticmethod
    def _url_to_folder_name(playlist_url: str) -> str:
        """Generates a deterministic folder name from a playlist URL.

        Uses MD5 hash truncated to 12 chars for brevity while maintaining
        uniqueness for practical purposes. Note: MD5 is used for deterministic
        naming, not for security purposes.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            A 12-character hex string folder name.
        """
        return hashlib.md5(playlist_url.encode()).hexdigest()[:12]  # noqa: S324

    def get_playlist_folder(self, playlist_url: str) -> str:
        """Gets the folder path for a playlist (creates if needed).

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            Absolute path to the playlist's cache folder.
        """
        folder_name = self._url_to_folder_name(playlist_url)
        folder_path = os.path.join(self.library_path, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        return folder_path

    def get_cached_playlist(self, playlist_url: str) -> Optional[CachedPlaylist]:
        """Gets cached playlist info if it exists.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            CachedPlaylist if found, None otherwise.
        """
        manifest = self._load_manifest()
        folder_name = self._url_to_folder_name(playlist_url)
        playlist_data = manifest.get('playlists', {}).get(folder_name)

        if not playlist_data:
            return None

        return CachedPlaylist(
            playlist_url=playlist_data['playlist_url'],
            folder_name=folder_name,
            display_name=playlist_data.get('display_name', 'Unknown Playlist'),
            track_count=playlist_data.get('track_count', 0),
            cached_count=playlist_data.get('cached_count', 0),
            last_updated=playlist_data.get('last_updated', 0),
            tracks=playlist_data.get('tracks', [])
        )

    def get_all_cached_playlists(self) -> List[CachedPlaylist]:
        """Gets all cached playlists.

        Returns:
            List of CachedPlaylist objects.
        """
        manifest = self._load_manifest()
        playlists = []

        for folder_name, data in manifest.get('playlists', {}).items():
            playlists.append(CachedPlaylist(
                playlist_url=data['playlist_url'],
                folder_name=folder_name,
                display_name=data.get('display_name', 'Unknown Playlist'),
                track_count=data.get('track_count', 0),
                cached_count=data.get('cached_count', 0),
                last_updated=data.get('last_updated', 0),
                tracks=data.get('tracks', [])
            ))

        return playlists

    def register_playlist(
        self,
        playlist_url: str,
        display_name: str,
        tracks: List[Track]
    ) -> str:
        """Registers a playlist in the cache (without downloading).

        This creates the manifest entry and folder, but doesn't download
        any tracks yet. Call cache_track() to download individual tracks.

        Args:
            playlist_url: YouTube playlist URL.
            display_name: Human-readable playlist name.
            tracks: List of Track objects from the playlist.

        Returns:
            The folder path for this playlist's cache.
        """
        manifest = self._load_manifest()
        folder_name = self._url_to_folder_name(playlist_url)
        folder_path = self.get_playlist_folder(playlist_url)

        # Build track list with local paths (not yet downloaded)
        track_list = []
        for track in tracks:
            safe_name = sanitize_filename(f"{track.artist} - {track.title}", 180)
            local_path = os.path.join(folder_path, f"{safe_name}.mp3")
            track_data = track.to_dict()
            track_data['local_path'] = local_path
            track_list.append(track_data)

        manifest['playlists'][folder_name] = {
            'playlist_url': playlist_url,
            'display_name': display_name,
            'track_count': len(tracks),
            'cached_count': sum(1 for t in track_list if os.path.exists(t['local_path'])),
            'last_updated': time.time(),
            'tracks': track_list
        }

        self._manifest = manifest
        self._save_manifest()
        self.logger.info(f"[Cache] Registered playlist: {display_name} ({len(tracks)} tracks)")

        return folder_path

    async def cache_track(
        self,
        track: Track,
        playlist_url: str,
        progress_callback: Optional[Callable[[str], Any]] = None
    ) -> Optional[str]:
        """Downloads and caches a single track.

        Args:
            track: The Track to download.
            playlist_url: The playlist this track belongs to.
            progress_callback: Optional async callback for progress updates.

        Returns:
            Local file path if successful, None on failure.
        """
        if not YTDLP_AVAILABLE or not MUTAGEN_AVAILABLE:
            return None

        folder_path = self.get_playlist_folder(playlist_url)
        safe_name = sanitize_filename(f"{track.artist} - {track.title}", 180)
        local_path = os.path.join(folder_path, f"{safe_name}.mp3")

        # Already cached?
        if os.path.exists(local_path):
            self.logger.debug(f"[Cache] Already cached: {track.title}")
            return local_path

        if progress_callback:
            await progress_callback(f"Downloading: {track.title}")

        # Download the track
        result = await download_track_as_mp3(
            url=track.url,
            output_dir=folder_path,
            logger=self.logger,
            custom_title=track.title,
            custom_artist=track.artist,
            embed_thumbnail=True
        )

        if result.success and result.file_path:
            # Rename to our standardized name if different
            if result.file_path != local_path:
                try:
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    os.rename(result.file_path, local_path)
                except OSError as e:
                    self.logger.warning(f"[Cache] Could not rename file: {e}")
                    local_path = result.file_path

            # Update manifest with cached count
            self._update_cached_count(playlist_url)

            self.logger.info(f"[Cache] Cached: {track.title}")
            return local_path

        self.logger.warning(f"[Cache] Failed to cache: {track.title} - {result.error_message}")
        return None

    async def cache_playlist(
        self,
        playlist_url: str,
        tracks: List[Track],
        display_name: str,
        progress_callback: Optional[Callable[[str, int, int], Any]] = None,
        max_tracks: Optional[int] = None
    ) -> int:
        """Downloads and caches an entire playlist.

        Args:
            playlist_url: YouTube playlist URL.
            tracks: List of tracks to cache.
            display_name: Human-readable playlist name.
            progress_callback: Optional async callback(status, current, total).
            max_tracks: Maximum tracks to download (None = all).

        Returns:
            Number of tracks successfully cached.
        """
        # Register the playlist first
        self.register_playlist(playlist_url, display_name, tracks)

        tracks_to_cache = tracks[:max_tracks] if max_tracks else tracks
        cached_count = 0

        for i, track in enumerate(tracks_to_cache):
            if progress_callback:
                await progress_callback(
                    f"Caching: {track.title[:40]}...",
                    i + 1,
                    len(tracks_to_cache)
                )

            result = await self.cache_track(track, playlist_url)
            if result:
                track.local_path = result
                cached_count += 1

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)

        self.logger.info(f"[Cache] Playlist cached: {cached_count}/{len(tracks_to_cache)} tracks")
        return cached_count

    def _update_cached_count(self, playlist_url: str) -> None:
        """Updates the cached_count for a playlist in the manifest."""
        manifest = self._load_manifest()
        folder_name = self._url_to_folder_name(playlist_url)
        playlist_data = manifest.get('playlists', {}).get(folder_name)

        if playlist_data:
            # Count actual files
            folder_path = os.path.join(self.library_path, folder_name)
            if os.path.exists(folder_path):
                mp3_count = len([f for f in os.listdir(folder_path) if f.endswith('.mp3')])
                playlist_data['cached_count'] = mp3_count
                playlist_data['last_updated'] = time.time()
                self._save_manifest()

    def get_local_track(self, track: Track, playlist_url: str) -> Optional[str]:
        """Gets the local path for a track if it's cached.

        Args:
            track: The track to look up.
            playlist_url: The playlist this track belongs to.

        Returns:
            Local file path if cached and exists, None otherwise.
        """
        folder_path = self.get_playlist_folder(playlist_url)
        safe_name = sanitize_filename(f"{track.artist} - {track.title}", 180)
        local_path = os.path.join(folder_path, f"{safe_name}.mp3")

        if os.path.exists(local_path):
            return local_path
        return None

    def load_tracks_with_cache(self, playlist_url: str) -> List[Track]:
        """Loads tracks from cache manifest with local_path set.

        Args:
            playlist_url: YouTube playlist URL.

        Returns:
            List of Track objects with local_path populated if cached.
        """
        cached = self.get_cached_playlist(playlist_url)
        if not cached:
            return []

        tracks = []
        for track_data in cached.tracks:
            track = Track.from_dict(track_data)
            # Verify the file actually exists
            if track.local_path and os.path.exists(track.local_path):
                pass  # Keep local_path
            else:
                track.local_path = None  # File missing
            tracks.append(track)

        return tracks

    def get_cache_stats(self) -> Dict[str, Any]:
        """Gets statistics about the cache.

        Returns:
            Dict with total_playlists, total_tracks, cached_tracks, size_mb.
        """
        manifest = self._load_manifest()
        playlists = manifest.get('playlists', {})

        total_tracks = 0
        cached_tracks = 0
        total_size = 0

        for folder_name, data in playlists.items():
            total_tracks += data.get('track_count', 0)
            folder_path = os.path.join(self.library_path, folder_name)
            if os.path.exists(folder_path):
                for f in os.listdir(folder_path):
                    if f.endswith('.mp3'):
                        cached_tracks += 1
                        total_size += os.path.getsize(os.path.join(folder_path, f))

        return {
            'total_playlists': len(playlists),
            'total_tracks': total_tracks,
            'cached_tracks': cached_tracks,
            'size_mb': total_size / (1024 * 1024)
        }

    def clear_playlist_cache(self, playlist_url: str) -> bool:
        """Removes a playlist from the cache.

        Args:
            playlist_url: YouTube playlist URL to remove.

        Returns:
            True if removed, False if not found.
        """
        import shutil as shutil_mod

        manifest = self._load_manifest()
        folder_name = self._url_to_folder_name(playlist_url)

        if folder_name not in manifest.get('playlists', {}):
            return False

        # Remove folder
        folder_path = os.path.join(self.library_path, folder_name)
        if os.path.exists(folder_path):
            shutil_mod.rmtree(folder_path)

        # Remove from manifest
        del manifest['playlists'][folder_name]
        self._manifest = manifest
        self._save_manifest()

        self.logger.info(f"[Cache] Cleared playlist cache: {folder_name}")
        return True

    def clear_all_cache(self) -> int:
        """Removes all cached playlists.

        Returns:
            Number of playlists removed.
        """
        import shutil as shutil_mod

        manifest = self._load_manifest()
        count = len(manifest.get('playlists', {}))

        # Remove all playlist folders
        for folder_name in list(manifest.get('playlists', {}).keys()):
            folder_path = os.path.join(self.library_path, folder_name)
            if os.path.exists(folder_path):
                shutil_mod.rmtree(folder_path)

        # Reset manifest
        self._manifest = {
            'version': self.MANIFEST_VERSION,
            'playlists': {}
        }
        self._save_manifest()

        self.logger.info(f"[Cache] Cleared all cache: {count} playlists")
        return count

    def needs_refresh(self, playlist_url: str, max_age_hours: float = 24.0) -> bool:
        """Checks if a cached playlist's metadata needs refreshing.

        Args:
            playlist_url: YouTube playlist URL.
            max_age_hours: Maximum age in hours before refresh is needed.

        Returns:
            True if playlist is not cached or is older than max_age_hours.
        """
        cached = self.get_cached_playlist(playlist_url)
        if not cached:
            return True

        age_seconds = time.time() - cached.last_updated
        max_age_seconds = max_age_hours * 3600
        return age_seconds > max_age_seconds

    def refresh_playlist_metadata(
        self,
        playlist_url: str,
        tracks: List[Track],
        display_name: Optional[str] = None
    ) -> int:
        """Refreshes playlist metadata while preserving existing cached files.

        This updates the track list from fresh YouTube data while keeping
        local_path mappings for any files that still exist on disk.

        Args:
            playlist_url: YouTube playlist URL.
            tracks: Fresh list of tracks from YouTube.
            display_name: Optional new display name.

        Returns:
            Number of tracks that have local files cached.
        """
        manifest = self._load_manifest()
        folder_name = self._url_to_folder_name(playlist_url)
        folder_path = os.path.join(self.library_path, folder_name)

        # Get existing cached file paths for matching
        existing_files: Dict[str, str] = {}
        if os.path.exists(folder_path):
            for filename in os.listdir(folder_path):
                if filename.endswith('.mp3'):
                    existing_files[filename] = os.path.join(folder_path, filename)

        # Get existing display name if not provided
        if not display_name:
            existing_data = manifest.get('playlists', {}).get(folder_name)
            if existing_data:
                display_name = existing_data.get('display_name', 'Unknown Playlist')
            else:
                display_name = 'Unknown Playlist'

        # Ensure folder exists
        os.makedirs(folder_path, exist_ok=True)

        # Build new track list with preserved local_path mappings
        track_list = []
        cached_count = 0
        for track in tracks:
            track_data = track.to_dict()

            # Match by expected filename
            safe_name = sanitize_filename(f"{track.artist} - {track.title}", 180)
            expected_filename = f"{safe_name}.mp3"

            if expected_filename in existing_files:
                local_path = existing_files[expected_filename]
                track_data['local_path'] = local_path
                cached_count += 1
            else:
                track_data['local_path'] = os.path.join(folder_path, expected_filename)

            track_list.append(track_data)

        manifest['playlists'][folder_name] = {
            'playlist_url': playlist_url,
            'display_name': display_name,
            'track_count': len(tracks),
            'cached_count': cached_count,
            'last_updated': time.time(),
            'tracks': track_list
        }

        self._manifest = manifest
        self._save_manifest()
        self.logger.info(
            f"[Cache] Refreshed playlist metadata: {display_name} "
            f"({len(tracks)} tracks, {cached_count} cached)"
        )

        return cached_count

    def get_stale_playlists(self, max_age_hours: float = 24.0) -> List[CachedPlaylist]:
        """Gets all playlists that need metadata refresh.

        Args:
            max_age_hours: Maximum age in hours before considered stale.

        Returns:
            List of CachedPlaylist objects that need refresh.
        """
        stale = []
        for cached in self.get_all_cached_playlists():
            if self.needs_refresh(cached.playlist_url, max_age_hours):
                stale.append(cached)
        return stale


# ==========================================================================
# NEW MUSIC CACHE MANAGER (v2)
# ==========================================================================
# Proactive caching system that:
# - Refreshes ALL playlists from ambience.toml on startup and every 24 hours
# - Downloads tracks in background
# - Manages orphaned tracks with 90-day TTL
#
# File Structure:
#   cache/music/
#     playlist.json      # YouTube metadata for all playlists
#     manifest.json      # Download registry + orphan tracking
#     orphaned/          # 90-day holding area for removed tracks
#       <video_id>.mp3
#     playlists/
#       <playlist_hash>/ # 12-char MD5 of playlist URL
#         <video_id>.mp3


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
                    self._playlist_cache = json_module.load(f)
                    if self._playlist_cache is not None:
                        return self._playlist_cache
            except (json_module.JSONDecodeError, IOError) as e:
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
                json_module.dump(self._playlist_cache, f, indent=2, ensure_ascii=False)
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
                    self._manifest = json_module.load(f)
                    if self._manifest is not None:
                        return self._manifest
            except (json_module.JSONDecodeError, IOError) as e:
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
                    json_module.dump(self._manifest, f, indent=2, ensure_ascii=False)
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
                json_module.dump(self._manifest, f, indent=2, ensure_ascii=False)
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

        toml = _load_toml()
        playlists_section = toml.get('playlists', {})

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
            # Populate local_path if downloaded
            if track.video_id:
                local_path = self.get_local_path(track.video_id, playlist_url)
                if local_path:
                    track.local_path = local_path
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
                self.logger.debug(f"[CacheManager] Copied {track.video_id} from existing location")
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

            self.logger.warning(f"[CacheManager] Download failed: {track.title} - {result.error_message}")
            return None

        except Exception as e:
            self.logger.error(f"[CacheManager] Download error: {e}")
            return None

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
                if video_id in current_video_ids and folder_hash in current_video_ids[video_id]:
                    # Still in this playlist
                    new_locations.append(folder_hash)
                else:
                    # Removed from this playlist
                    file_path = os.path.join(self.playlists_path, folder_hash, f"{video_id}.mp3")
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

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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast
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
            'thumbnail_needs_crop': self.thumbnail_needs_crop
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
            thumbnail_needs_crop=data.get('thumbnail_needs_crop', False)
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
        '-f', 'mjpeg',  # Output as JPEG
        '-q:v', '2',  # High quality
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

        # For mix playlists, we'll limit extraction to 60 songs
        mix_limit = 60 if is_mix_playlist else None

        ydl_opts = {
            **YTDLP_OPTIONS,
            'extract_flat': 'in_playlist' if is_playlist else False,
            'noplaylist': not is_playlist,  # Only extract playlist if pure playlist URL
        }

        # Add playlist limit for mix playlists
        if mix_limit:
            ydl_opts['playlistend'] = mix_limit
            logger.info(f"Mix playlist detected - limiting to {mix_limit} tracks")

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

            # Check if mix playlist was truncated
            if is_mix_playlist and len(entries) >= mix_limit:  # type: ignore[arg-type]
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

            # Return warning if mix playlist was truncated
            warning = None
            if was_truncated:
                warning = (
                    f"⚠️ This is a Mix playlist - I only loaded the first {len(tracks)} tracks. "
                    "Mix playlists grow indefinitely and could crash the bot!"
                )

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

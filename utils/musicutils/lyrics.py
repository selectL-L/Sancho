"""Lyrics providers and text utilities.

Contains scrapers for Genius, LRCLIB, and LyricalNonsense,
plus text chunking utilities for display.
"""

import asyncio
import html
import logging
import re
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import quote_plus

# Optional imports for runtime
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    syncedlyrics = None  # type: ignore[assignment]
    SYNCEDLYRICS_AVAILABLE = False

if TYPE_CHECKING:
    pass  # Keep block for future type hints

from .music_data import LyricsResult

logger = logging.getLogger(__name__)


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

        except Exception as e:
            logger.debug(f"LyricalNonsense page scrape failed for {result.url}: {e}")

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

        except Exception as e:
            logger.debug(f"LRCLIB search failed for '{query}': {e}")

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

        except Exception as e:
            logger.debug(f"Genius search failed for '{query}': {e}")

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

        except Exception as e:
            logger.debug(f"Genius lyrics fetch failed for {result.url}: {e}")

        return result

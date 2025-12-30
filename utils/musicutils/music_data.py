"""utils/musicutils/music_data.py

Pure data classes and enums for the music system.

This module contains no I/O operations and minimal dependencies.
Other musicutils modules import from here.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Tuple

if TYPE_CHECKING:
    from discord.ext import commands

# Runtime imports - these are needed for actual functionality
try:
    import discord as _discord
    from discord.ext import commands as _commands
    DISCORD_AVAILABLE = True
except ImportError:
    _discord = None  # type: ignore[assignment]
    _commands = None  # type: ignore[assignment]
    DISCORD_AVAILABLE = False


# ==========================================================================
# VIDEO ID EXTRACTION (needed by Track.from_dict)
# ==========================================================================

def _extract_video_id(url: str) -> Optional[str]:
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


# ==========================================================================
# ENUMS
# ==========================================================================


class FetchContext(Enum):
    """Context for AudioFetcher.fetch() calls.

    Tells AudioFetcher how aggressive to be with retries:
    - PREFETCH: Background preparation, conservative - stops at direct failure
    - LIVE: Playing now, aggressive - full retry including residential
    - RETRY: FFmpeg failed, need fresh URL or residential
    """
    PREFETCH = "prefetch"
    LIVE = "live"
    RETRY = "retry"


class LoopMode(Enum):
    """Loop mode options for the music player."""
    OFF = 0
    ONE = 1
    ALL = 2

    @classmethod
    async def convert(cls, ctx: 'commands.Context', argument: str) -> 'LoopMode':
        """Case-insensitive converter for discord.py commands."""
        try:
            return cls[argument.upper()]
        except KeyError as e:
            if _commands is not None:
                raise _commands.BadArgument(f"'{argument}' is not a valid loop mode. Use: off, one, or all") from e
            raise ValueError(f"'{argument}' is not a valid loop mode. Use: off, one, or all") from e

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


# ==========================================================================
# TRACK & LYRICS DATA
# ==========================================================================


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
            video_id=data.get('video_id') or _extract_video_id(data['url'])
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


# ==========================================================================
# SESSION & PLAYBACK STATE
# ==========================================================================


@dataclass
class ActiveSession:
    """Represents an active voice session."""
    guild_id: int
    channel_id: int  # Voice channel ID
    voice_client: Any  # discord.VoiceClient at runtime
    origin_channel_id: int  # Text channel where session was initiated (for system messages)
    started_at: float = field(default_factory=time.time)
    waiting_for_users: bool = False


@dataclass
class PlaybackState:
    """Mutable state for track playback within a session.

    Groups variables that track what's currently playing, timing info,
    and pause state. Reset when session ends.
    """
    current_audio_url: Optional[str] = None  # Cached audio URL for current track
    current_audio_track_url: Optional[str] = None  # YouTube URL this audio URL is for
    current_audio_headers: Optional[Dict[str, str]] = None  # HTTP headers for current URL
    track_started_timestamp: float = 0.0  # When FFmpeg started (for failure detection)
    paused_at_position: Optional[float] = None  # Seek position when paused, None if not paused

    def clear(self) -> None:
        """Reset all playback state."""
        self.current_audio_url = None
        self.current_audio_track_url = None
        self.current_audio_headers = None
        self.track_started_timestamp = 0.0
        self.paused_at_position = None


@dataclass
class PrefetchState:
    """State for pre-buffering the next track in the playlist.

    Enables smooth transitions by fetching the next track's audio URL and
    metadata while the current track is still playing. When the track ends,
    we already have everything ready for instant playback.

    DESIGN PRINCIPLES:
    1. Keyed by playlist index, not URL - index determines "next track"
    2. Stores the index we prefetched FOR, so we know if it's still valid
    3. Tracks fetch timestamp - YouTube URLs expire (~6 hours)
    4. Stores complete metadata for instant display (audio, headers, thumbnail)

    INVALIDATION RULES:
    - Prefetch is valid if target_index still equals (current_index + 1) % len
    - Playlist mutations only invalidate if they affect the target index
    - Retry logic does NOT invalidate (we're replaying current, not next)
    - Skip/jump always invalidates (current_index changed)

    The is_valid_for() method encapsulates all validation logic.
    """
    # What we prefetched
    audio_url: Optional[str] = None  # Pre-fetched streaming URL
    http_headers: Optional[Dict[str, str]] = None  # HTTP headers for the URL
    thumbnail_bytes: Optional[bytes] = None  # Pre-loaded thumbnail image

    # How to identify what this prefetch is for
    target_index: Optional[int] = None  # Playlist index we prefetched
    target_video_id: Optional[str] = None  # Video ID for extra validation

    # Metadata
    fetched_at: float = 0.0  # Timestamp when fetched (for expiration check)
    task: Optional[asyncio.Task[None]] = None  # Background prefetch task

    # URL expiration (YouTube streaming URLs expire after ~6 hours)
    URL_EXPIRY_SECONDS: ClassVar[float] = 5 * 60 * 60  # 5 hours to be safe

    def is_valid_for(self, playlist_index: int, playlist_len: int,
                     current_index: int, video_id: Optional[str] = None) -> bool:
        """Check if this prefetch is valid for advancing to the given index.

        Args:
            playlist_index: The index we want to play next.
            playlist_len: Current playlist length.
            current_index: Current playing index.
            video_id: Optional video ID to verify track identity.

        Returns:
            True if the prefetch can be used, False if it should be discarded.
        """
        # No prefetch data
        if self.audio_url is None or self.target_index is None:
            return False

        # Index mismatch - playlist was mutated
        expected_next = (current_index + 1) % playlist_len if playlist_len > 0 else None
        if self.target_index != expected_next or self.target_index != playlist_index:
            return False

        # Video ID mismatch - track at that index changed
        if video_id is not None and self.target_video_id is not None:
            if self.target_video_id != video_id:
                return False

        # URL expired
        if time.time() - self.fetched_at > self.URL_EXPIRY_SECONDS:
            return False

        return True

    def clear(self) -> None:
        """Reset prefetch state (does not cancel task).

        Call cancel_task() first if you need to stop an in-progress fetch.
        """
        self.audio_url = None
        self.http_headers = None
        self.thumbnail_bytes = None
        self.target_index = None
        self.target_video_id = None
        self.fetched_at = 0.0

    def cancel_task(self) -> None:
        """Cancel prefetch task if running."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

    def invalidate_if_affected(self, affected_indices: set[int], new_playlist_len: int) -> bool:
        """Invalidate prefetch if any affected index matches our target.

        Used by playlist mutation methods to conditionally invalidate.

        Args:
            affected_indices: Set of playlist indices that were modified.
            new_playlist_len: Playlist length after the mutation.

        Returns:
            True if prefetch was invalidated, False if still valid.
        """
        if self.target_index is None:
            return False  # Nothing to invalidate

        # If our target index was directly affected, invalidate
        if self.target_index in affected_indices:
            self.cancel_task()
            self.clear()
            return True

        # If target index is now out of bounds, invalidate
        if self.target_index >= new_playlist_len:
            self.cancel_task()
            self.clear()
            return True

        return False


@dataclass
class AmbienceState:
    """Tracks playlist changes requested by the ambience system.

    The ambience system runs independently and signals playlist changes
    via callbacks. These are queued here for the main loop to handle.
    """
    current_playlist_url: Optional[str] = None  # Currently active playlist
    pending_playlist_url: Optional[str] = None  # Requested new playlist
    pending_switch: bool = False  # True if a switch is pending

    def request_switch(self, playlist_url: Optional[str]) -> None:
        """Queue a playlist switch request."""
        self.pending_playlist_url = playlist_url
        self.pending_switch = True

    def consume_switch(self) -> Tuple[bool, Optional[str]]:
        """Consume pending switch, returns (had_switch, new_url)."""
        if not self.pending_switch:
            return False, None
        self.pending_switch = False
        url = self.pending_playlist_url
        self.pending_playlist_url = None
        return True, url

    def confirm_switch(self, playlist_url: Optional[str]) -> None:
        """Confirm that a switch has completed."""
        self.current_playlist_url = playlist_url


# ==========================================================================
# RESULT DATA CLASSES
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


@dataclass
class AudioUrlResult:
    """Result from get_audio_url() - replaces the unwieldy 5-tuple.

    Contains all data needed to start playback or handle errors.
    """
    url: Optional[str] = None  # Streamable audio URL
    is_unavailable: bool = False  # True if video is permanently unavailable (remove from playlist)
    thumbnail: Optional[str] = None  # Best thumbnail URL found
    thumbnail_needs_crop: bool = False  # True if thumbnail needs center-cropping
    http_headers: Optional[Dict[str, str]] = None  # Headers needed for FFmpeg
    error: Optional[str] = None  # Error message if fetch failed

    @property
    def success(self) -> bool:
        """True if we got a playable URL."""
        return self.url is not None and not self.is_unavailable

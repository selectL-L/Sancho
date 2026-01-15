"""utils/musicutils/music_data.py

Pure data classes and enums for the music system.

This module contains no I/O operations and minimal dependencies.
Other musicutils modules import from here.
"""

import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

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


class AudioErrorType(Enum):
    """Error types parsed from FFmpeg stderr.

    Used by SeekableAudioSource.health to report why playback failed.
    This enables smart retry logic: 403 means auth issue, 404 means
    track removed, CONNECTION might be transient, etc.

    TODO(Phase 3): These patterns are placeholders. Once we collect real
    FFmpeg stderr output from failures, refine the parsing in
    audio_source.py._parse_stderr_line() to match actual error formats.
    """
    NONE = "none"              # No error detected
    HTTP_403 = "http_403"      # Auth failure - URL expired or blocked
    HTTP_404 = "http_404"      # Track removed from YouTube
    HTTP_OTHER = "http_other"  # Other HTTP error (5xx, etc.)
    CONNECTION = "connection"  # Network failure (reset, refused, timeout)
    FORMAT = "format"          # Corrupt or incompatible stream
    TIMEOUT = "timeout"        # Prebuffer timeout (not currently used)
    UNKNOWN = "unknown"        # EOF with no clear error in stderr


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


@dataclass
class FFmpegHealth:
    """Health status from an FFmpeg process.

    Populated by SeekableAudioSource as it reads stderr. Used to report
    why playback failed (instead of guessing from elapsed time).

    TODO(Phase 3): The error_type classification depends on patterns in
    _parse_stderr_line(). Once we have real FFmpeg error output, refine
    those patterns and this dataclass may need additional fields.
    """
    error_type: 'AudioErrorType' = field(default_factory=lambda: AudioErrorType.NONE)
    error_detail: Optional[str] = None  # Raw stderr line that triggered classification
    frames_read: int = 0                # Frames successfully read before error
    stderr_lines: list[str] = field(default_factory=list)  # All captured stderr

    @property
    def is_healthy(self) -> bool:
        """True if no fatal error detected."""
        return self.error_type == AudioErrorType.NONE

    @property
    def has_error(self) -> bool:
        """True if a fatal error was detected."""
        return self.error_type != AudioErrorType.NONE


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
    thumbnail_is_square: bool = False  # True if thumbnail is already square (no processing needed)
    user_added: bool = False  # True if added by user (not from ambient playlist)
    video_id: Optional[str] = None  # YouTube video ID (extracted from URL)

    # Display metadata (from YTM when available)
    album: Optional[str] = None  # Album name (YTM songs only)
    source: str = 'youtube'  # 'ytm_song', 'ytm_video', 'youtube'
    is_explicit: Optional[bool] = None  # True if explicit, False if clean
    version_label: str = "Video"  # "Official Audio", "Music Video", etc.
    view_count: Optional[int] = None  # Raw view count for display

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
            'thumbnail_is_square': self.thumbnail_is_square,
            'video_id': self.video_id,
            'album': self.album,
            'source': self.source,
            'is_explicit': self.is_explicit,
            'version_label': self.version_label,
            'view_count': self.view_count,
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
            thumbnail_is_square=data.get('thumbnail_is_square', False),
            video_id=data.get('video_id') or _extract_video_id(data['url']),
            album=data.get('album'),
            source=data.get('source', 'youtube'),
            is_explicit=data.get('is_explicit'),
            version_label=data.get('version_label', 'Video'),
            view_count=data.get('view_count'),
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
    thumbnail_is_square: bool = False  # True if thumbnail is already square
    http_headers: Optional[Dict[str, str]] = None  # Headers needed for FFmpeg
    error: Optional[str] = None  # Error message if fetch failed

    @property
    def success(self) -> bool:
        """True if we got a playable URL."""
        return self.url is not None and not self.is_unavailable

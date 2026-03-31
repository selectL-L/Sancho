"""Music utilities package.

Public API for music-related functionality. This package consolidates all
music-related utilities that were previously scattered across utils/.

Modules:
    music_data: Pure data classes (Track, LoopMode, etc.)
    lyrics: Lyrics scrapers and text utilities
    music_auth: YouTube authentication and source resolution helpers
    music_cache: File operations and cache management
    music_helpers: yt-dlp wrappers, thumbnails, FFmpeg utilities
    audio_source: SeekableAudioSource for FFmpeg playback
    managed_player: High-level ManagedPlayer abstraction
    source_acquisition: Source acquisition mixin (spending policy)
"""

# Core components
from utils.musicutils.commands import MusicCommandsMixin
from utils.musicutils.managed_player import ManagedPlayer, PlayerState, TrackInfo
from utils.musicutils.audio_source import SeekableAudioSource
from utils.musicutils.music_cache import MusicCacheManager
from utils.musicutils.music_data import (
    AudioErrorType,
    FFmpegHealth,
    FFmpegResponseAction,
    TrackIssuePromptPreference,
    TrackIssueKind,
    LoopMode,
    Track,
    LyricsResult,
    ActiveSession,
    PlaybackEndReport,
    PlaybackState,
    AmbienceState,
    DownloadResult,
    AudioUrlResult,
)

# Source acquisition mixin
from utils.musicutils.source_acquisition import (
    SourceAcquisitionMixin,
    PlayableSource,
    TrackAttempts,
    FailureAction,
    classify_failure,
)

# Lyrics utilities
from utils.musicutils.lyrics import (
    chunk_text,
    GeniusScraper,
    LRCLIBProvider,
    LyricalNonsenseScraper,
)

# yt-dlp wrappers, thumbnails, FFmpeg
from utils.musicutils.music_helpers import (
    # Availability flags
    YTDLP_AVAILABLE,
    MUTAGEN_AVAILABLE,
    # FFmpeg
    FFMPEG_OPTIONS,
    FFMPEG_BEFORE_OPTIONS,
    get_ffmpeg_path,
    get_ffmpeg_stderr_loglevel,
    # Residential proxy
    get_residential_proxy_url,
    # yt-dlp base options
    YTDLP_OPTIONS,
    # Error handling
    is_video_unavailable,
    is_403_error,
    format_youtube_error,
    # Video ID
    extract_video_id,
    # Thumbnails
    extract_m4a_thumbnail,
    # yt-dlp wrappers
    get_audio_url,
    detect_mix_in_url,
    fetch_url_info,
    fetch_playlist_metadata,
    # Download utilities
    sanitize_filename,
    download_track_as_m4a,
    # Ambient filename generation
    generate_ambient_filename,
)

# Thumbnail and search functions
from utils.musicutils.search import (
    fetch_thumbnail_bytes,
    resize_thumbnail_bytes,
    fetch_and_resize_thumbnail,
    extract_best_thumbnail_from_info,
    get_thumbnail_bytes,
    # Availability
    YTMUSIC_AVAILABLE,
    # Constants
    THUMBNAIL_WIDTH,
    # Data class
    SearchResult,
    # Video ID
    extract_video_id as search_extract_video_id,
    resize_ytm_thumbnail,
    # YTM functions
    search_ytm,
    get_ytm_metadata,
    is_atv,
    # YouTube search
    search_youtube as search_youtube_results,
    # Unified search
    search_query_mode,
    search_url_mode,
    # Filtering
    is_relevant,
    dedupe_results,
)

# YouTube auth and source-resolution helpers
from utils.musicutils.music_auth import (
    YouTubeAuthStatus,
    get_youtube_auth_status,
    detect_youtube_auth,
    get_ytdlp_options,
)

__all__ = [
    # Data classes
    'AudioErrorType',
    'FFmpegHealth',
    'FFmpegResponseAction',
    'TrackIssuePromptPreference',
    'TrackIssueKind',
    'LoopMode',
    'Track',
    'LyricsResult',
    'ActiveSession',
    'PlaybackEndReport',
    'PlaybackState',
    'AmbienceState',
    'DownloadResult',
    'AudioUrlResult',
    # Source acquisition
    'SourceAcquisitionMixin',
    'PlayableSource',
    'TrackAttempts',
    'FailureAction',
    'classify_failure',
    # Lyrics
    'chunk_text',
    'GeniusScraper',
    'LRCLIBProvider',
    'LyricalNonsenseScraper',
    # Availability flags
    'YTDLP_AVAILABLE',
    'MUTAGEN_AVAILABLE',
    # FFmpeg
    'FFMPEG_OPTIONS',
    'FFMPEG_BEFORE_OPTIONS',
    'get_ffmpeg_path',
    'get_ffmpeg_stderr_loglevel',
    # Residential proxy
    'get_residential_proxy_url',
    # yt-dlp base options
    'YTDLP_OPTIONS',
    # Error handling
    'is_video_unavailable',
    'is_403_error',
    'format_youtube_error',
    # Video ID
    'extract_video_id',
    # Thumbnails
    'fetch_thumbnail_bytes',
    'resize_thumbnail_bytes',
    'fetch_and_resize_thumbnail',
    'extract_best_thumbnail_from_info',
    'extract_m4a_thumbnail',
    'get_thumbnail_bytes',
    # yt-dlp wrappers
    'get_audio_url',
    'detect_mix_in_url',
    'fetch_url_info',
    'fetch_playlist_metadata',
    # Download utilities
    'sanitize_filename',
    'download_track_as_m4a',
    'generate_ambient_filename',
    # Auth
    'YouTubeAuthStatus',
    'get_youtube_auth_status',
    'detect_youtube_auth',
    'get_ytdlp_options',
    # Cache management
    'MusicCacheManager',
    # Audio source
    'SeekableAudioSource',
    # Managed player
    'ManagedPlayer',
    'PlayerState',
    'TrackInfo',
    # Command handlers mixin
    'MusicCommandsMixin',
    # Search & metadata
    'YTMUSIC_AVAILABLE',
    'THUMBNAIL_WIDTH',
    'SearchResult',
    'search_extract_video_id',
    'resize_ytm_thumbnail',
    'search_ytm',
    'get_ytm_metadata',
    'is_atv',
    'search_youtube_results',
    'search_query_mode',
    'search_url_mode',
    'is_relevant',
    'dedupe_results',
]

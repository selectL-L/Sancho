"""Music utilities package.

Public API for music-related functionality. This package consolidates all
music-related utilities that were previously scattered across utils/.

Modules:
    music_data: Pure data classes (Track, LoopMode, etc.)
    lyrics: Lyrics scrapers and text utilities
    music_auth: YouTube authentication and retry orchestration
    music_cache: File operations and cache management
    music_helpers: yt-dlp wrappers, thumbnails, FFmpeg utilities
    audio_source: SeekableAudioSource for FFmpeg playback
    managed_player: High-level ManagedPlayer abstraction
"""

# Data structures (Phase 2)
from utils.musicutils.commands import MusicCommandsMixin
from utils.musicutils.managed_player import ManagedPlayer, PlayerState, TrackInfo
from utils.musicutils.audio_source import SeekableAudioSource
from utils.musicutils.music_cache import MusicCacheManager
from utils.musicutils.music_data import (
    FetchContext,
    LoopMode,
    Track,
    LyricsResult,
    ActiveSession,
    PlaybackState,
    AmbienceState,
    DownloadResult,
    AudioUrlResult,
)

# Lyrics utilities (Phase 3)
from utils.musicutils.lyrics import (
    chunk_text,
    GeniusScraper,
    LRCLIBProvider,
    LyricalNonsenseScraper,
)

# yt-dlp wrappers, thumbnails, FFmpeg (Phase 4)
from utils.musicutils.music_helpers import (
    # Availability flags
    YTDLP_AVAILABLE,
    MUTAGEN_AVAILABLE,
    # FFmpeg
    FFMPEG_OPTIONS,
    FFMPEG_BEFORE_OPTIONS,
    get_ffmpeg_path,
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
    extract_mp3_thumbnail,
    # yt-dlp wrappers
    get_audio_url,
    search_youtube,
    detect_mix_in_url,
    fetch_url_info,
    fetch_playlist_metadata,
    # MP3 download
    sanitize_filename,
    download_track_as_mp3,
    get_track_info_for_download,
)

# Thumbnail functions are now in search.py
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
    search_youtube_legacy,
    # Unified search
    search_query_mode,
    search_url_mode,
    # Filtering
    is_relevant,
    dedupe_results,
)

__all__ = [
    # Data classes
    'FetchContext',
    'LoopMode',
    'Track',
    'LyricsResult',
    'ActiveSession',
    'PlaybackState',
    'AmbienceState',
    'DownloadResult',
    'AudioUrlResult',
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
    'extract_mp3_thumbnail',
    'get_thumbnail_bytes',
    # yt-dlp wrappers
    'get_audio_url',
    'search_youtube',
    'detect_mix_in_url',
    'fetch_url_info',
    'fetch_playlist_metadata',
    # MP3 download
    'sanitize_filename',
    'download_track_as_mp3',
    'get_track_info_for_download',
]

# YouTube auth and retry orchestration (Phase 5 + Phase 12)
from utils.musicutils.music_auth import (
    YouTubeAuthStatus,
    get_youtube_auth_status,
    get_ytdlp_options,
    AudioFetcher,
    AudioFetchResult,
    TrackFetchState,
)

__all__ += [
    # Auth
    'YouTubeAuthStatus',
    'get_youtube_auth_status',
    'get_ytdlp_options',
    'AudioFetcher',
    'AudioFetchResult',
    'TrackFetchState',
]

# Cache management (Phase 6)

__all__ += [
    'MusicCacheManager',
]

# Audio source (Phase 7)

__all__ += [
    'SeekableAudioSource',
]

# Managed player (Phase 7)

__all__ += [
    'ManagedPlayer',
    'PlayerState',
    'TrackInfo',
]

# Command handlers mixin (Phase 8 - NLP handler extraction)

__all__ += [
    'MusicCommandsMixin',
]

# Search & metadata (YTM integration)

__all__ += [
    # Search availability
    'YTMUSIC_AVAILABLE',
    # Constants
    'THUMBNAIL_WIDTH',
    # Search data
    'SearchResult',
    # Search functions
    'search_extract_video_id',
    'resize_ytm_thumbnail',
    'search_ytm',
    'get_ytm_metadata',
    'is_atv',
    'search_youtube_results',
    'search_query_mode',
    'search_url_mode',
    'search_youtube_legacy',
    'is_relevant',
    'dedupe_results',
]

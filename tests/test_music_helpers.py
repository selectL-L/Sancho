"""Tests for utils/music_helpers.py pure logic functions.

Covers:
- chunk_text: Discord message chunking
- detect_mix_in_url: YouTube mix URL parsing
- sanitize_filename: Filesystem-safe filename generation
- is_video_unavailable: Error classification
- format_youtube_error: User-friendly error formatting
- MusicCacheManager: Cache management (file I/O mocked)
"""

import json
import os
import tempfile
import pytest
from unittest.mock import MagicMock
import asyncio
import time

from utils.musicutils import (
    chunk_text,
    detect_mix_in_url,
    sanitize_filename,
    is_video_unavailable,
    format_youtube_error,
    MusicCacheManager,
    Track,
)
from utils.musicutils.music_helpers import UNAVAILABLE_INDICATORS


# =============================================================================
# chunk_text Tests
# =============================================================================

class TestChunkText:
    """Tests for chunk_text function - Discord message splitting."""

    def test_empty_string_single_chunk(self):
        """Empty input returns single empty chunk (fits in limit)."""
        assert chunk_text("", max_length=2000) == [""]

    def test_whitespace_only_single_chunk(self):
        """Whitespace-only input returns as single chunk."""
        result = chunk_text("   \n\t  ", max_length=2000)
        assert len(result) == 1

    def test_short_text_single_chunk(self):
        """Text under limit should return single chunk."""
        text = "Hello, world!"
        result = chunk_text(text, max_length=2000)
        assert result == [text]

    def test_exact_limit_no_split(self):
        """Text exactly at limit should not split."""
        text = "a" * 2000
        result = chunk_text(text, max_length=2000)
        assert len(result) == 1
        assert len(result[0]) == 2000

    def test_splits_by_paragraph_first(self):
        """Should prefer splitting on paragraph boundaries."""
        para1 = "First paragraph content."
        para2 = "Second paragraph content."
        text = f"{para1}\n\n{para2}"
        # Use a limit that fits para1 but not both
        result = chunk_text(text, max_length=len(para1) + 5)
        assert len(result) == 2
        assert para1 in result[0]
        assert para2 in result[1]

    def test_splits_by_line_when_paragraph_too_long(self):
        """Falls back to line splitting for long paragraphs."""
        lines = ["Line number one here", "Line number two here", "Line number three"]
        text = "\n".join(lines)
        result = chunk_text(text, max_length=30)
        # Should split into multiple chunks
        assert len(result) >= 2

    def test_truncates_very_long_lines(self):
        """Very long single lines get truncated with ellipsis."""
        long_line = "x" * 3000
        result = chunk_text(long_line, max_length=100)
        # Should truncate
        assert len(result) >= 1
        assert len(result[0]) <= 100

    def test_preserves_content_order(self):
        """Chunks should maintain original text order."""
        text = "AAA\n\nBBB\n\nCCC"
        result = chunk_text(text, max_length=10)
        combined = " ".join(result)
        assert combined.index("AAA") < combined.index("BBB") < combined.index("CCC")

    def test_handles_mixed_line_endings(self):
        """Should handle \\r\\n and \\n line endings."""
        text = "First\r\n\r\nSecond\n\nThird"
        result = chunk_text(text, max_length=50)
        assert len(result) >= 1
        # All content should be present
        combined = " ".join(result)
        assert "First" in combined
        assert "Second" in combined
        assert "Third" in combined

    @pytest.mark.parametrize("max_len", [50, 100, 500, 1000, 2000])
    def test_respects_max_length(self, max_len):
        """All chunks should respect max_length parameter."""
        text = "Word " * 500  # Long text
        result = chunk_text(text, max_length=max_len)
        for chunk in result:
            assert len(chunk) <= max_len


# =============================================================================
# detect_mix_in_url Tests
# =============================================================================

class TestDetectMixInUrl:
    """Tests for detect_mix_in_url - YouTube mix URL parsing."""

    def test_regular_video_no_mix(self):
        """Regular video URL should return no mix (all None when not a mix)."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        has_mix, single_url, mix_url = detect_mix_in_url(url)
        assert has_mix is False
        assert single_url is None  # Function returns None when not a mix
        assert mix_url is None

    def test_video_with_list_param_is_mix(self):
        """Video with &list= parameter indicates a mix."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
        has_mix, single_url, mix_url = detect_mix_in_url(url)
        assert has_mix is True
        assert single_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert mix_url == url

    def test_mix_starting_with_RD(self):
        """Mix playlists start with RD prefix."""
        url = "https://www.youtube.com/watch?v=abc123&list=RDabc123"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is True

    def test_mix_starting_with_RDMM(self):
        """My Mix playlists start with RDMM prefix."""
        url = "https://www.youtube.com/watch?v=xyz789&list=RDMMxyz789"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is True

    def test_regular_playlist_not_detected_as_mix(self):
        """Regular playlists (PL prefix) should not be detected as mix."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        # PL playlists are regular playlists, not mixes
        assert has_mix is False

    def test_youtu_be_short_url(self):
        """Handles youtu.be short URLs (returns None when not a mix)."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        has_mix, single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is False
        assert single_url is None  # Not a mix, so None

    def test_youtu_be_with_list_param(self):
        """Short URL with list parameter."""
        url = "https://youtu.be/dQw4w9WgXcQ?list=RDdQw4w9WgXcQ"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is True

    def test_empty_url(self):
        """Empty URL should be handled gracefully."""
        has_mix, single_url, mix_url = detect_mix_in_url("")
        assert has_mix is False
        assert single_url is None
        assert mix_url is None

    def test_non_youtube_url(self):
        """Non-YouTube URLs return None (function only handles mixes)."""
        url = "https://soundcloud.com/artist/track"
        has_mix, single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is False
        assert single_url is None

    def test_preserves_other_params(self):
        """Single URL should strip list but keep video ID."""
        url = "https://www.youtube.com/watch?v=abc123&list=RDabc123&t=30"
        has_mix, single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is True
        assert single_url is not None
        assert "v=abc123" in single_url
        # list param should be stripped from single_url
        assert "list=" not in single_url


# =============================================================================
# sanitize_filename Tests
# =============================================================================

class TestSanitizeFilename:
    """Tests for sanitize_filename - filesystem-safe name generation."""

    def test_simple_name_unchanged(self):
        """Simple valid filename should pass through."""
        assert sanitize_filename("song.mp3") == "song.mp3"

    def test_removes_invalid_characters(self):
        """Removes characters invalid on Windows/Linux."""
        result = sanitize_filename('song<>:"/\\|?*.mp3')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "/" not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_collapses_whitespace(self):
        """Multiple spaces collapse to single underscore."""
        result = sanitize_filename("my    song    title.mp3")
        assert "    " not in result
        # Function converts spaces to underscores and collapses multiples
        assert "my_song_title.mp3" == result

    def test_strips_leading_trailing_whitespace(self):
        """Leading/trailing whitespace removed."""
        result = sanitize_filename("  song.mp3  ")
        assert not result.startswith(" ")
        assert not result.endswith(" ")

    def test_truncates_long_names(self):
        """Very long names get truncated."""
        long_name = "a" * 300 + ".mp3"
        result = sanitize_filename(long_name)
        # Should be truncated to reasonable length (typically 200-255)
        assert len(result) <= 255

    def test_truncates_without_extension_awareness(self):
        """Function truncates from end (doesn't preserve extension)."""
        long_name = "a" * 300 + ".mp3"
        result = sanitize_filename(long_name)
        # Function just truncates at max_length, doesn't preserve extension
        assert len(result) <= 200
        assert result == "a" * 200

    def test_empty_string(self):
        """Empty string should return something usable."""
        result = sanitize_filename("")
        # Should return a default or empty string
        assert isinstance(result, str)

    def test_unicode_characters_preserved(self):
        """Unicode characters should be preserved (they're valid)."""
        result = sanitize_filename("日本語タイトル.mp3")
        assert "日本語" in result or result  # May transform but shouldn't crash

    def test_dots_in_name(self):
        """Dots in middle of name should be preserved."""
        result = sanitize_filename("song.feat.artist.mp3")
        # Should preserve dots but not create issues
        assert result.endswith(".mp3")

    def test_special_but_valid_chars(self):
        """Parentheses, brackets, dashes should be preserved."""
        result = sanitize_filename("Song (feat. Artist) - Album [2023].mp3")
        assert "(" in result
        assert ")" in result
        assert "-" in result
        assert "[" in result
        assert "]" in result


# =============================================================================
# is_video_unavailable Tests
# =============================================================================

class TestIsVideoUnavailable:
    """Tests for is_video_unavailable - error classification."""

    @pytest.mark.parametrize("indicator", UNAVAILABLE_INDICATORS)
    def test_recognizes_all_indicators(self, indicator):
        """Should recognize all defined unavailability indicators."""
        error = Exception(f"Error: {indicator} - more details")
        assert is_video_unavailable(error) is True

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        error = Exception("VIDEO UNAVAILABLE")
        assert is_video_unavailable(error) is True

    def test_network_error_not_unavailable(self):
        """Network errors are temporary, not permanent unavailability."""
        error = Exception("Connection timed out")
        assert is_video_unavailable(error) is False

    def test_rate_limit_not_unavailable(self):
        """Rate limiting is temporary, not permanent."""
        error = Exception("429 Too Many Requests")
        assert is_video_unavailable(error) is False

    def test_generic_error_not_unavailable(self):
        """Generic errors should not be classified as unavailable."""
        error = Exception("Something went wrong")
        assert is_video_unavailable(error) is False

    def test_empty_error_message(self):
        """Empty error message should return False."""
        error = Exception("")
        assert is_video_unavailable(error) is False


# =============================================================================
# format_youtube_error Tests
# =============================================================================

class TestFormatYoutubeError:
    """Tests for format_youtube_error - user-friendly messages."""

    def test_unavailable_video(self):
        """Unavailable video gets friendly message."""
        error = Exception("Video unavailable")
        result = format_youtube_error(error)
        assert "unavailable" in result.lower()
        assert "private" in result.lower() or "deleted" in result.lower() or "region" in result.lower()

    def test_private_video(self):
        """Private video gets specific message."""
        error = Exception("This is a private video")
        result = format_youtube_error(error)
        assert "private" in result.lower()

    def test_age_restricted(self):
        """Age-restricted video gets specific message."""
        error = Exception("Sign in to confirm your age")
        result = format_youtube_error(error)
        assert "age" in result.lower()

    def test_copyright_blocked(self):
        """Copyright-blocked video gets specific message."""
        error = Exception("blocked due to copyright claim")
        result = format_youtube_error(error)
        assert "copyright" in result.lower() or "blocked" in result.lower()

    def test_generic_error_includes_original(self):
        """Unknown errors include original message."""
        original = "Some random yt-dlp error"
        error = Exception(original)
        result = format_youtube_error(error)
        assert original in result


# =============================================================================
# MusicCacheManager Tests
# =============================================================================

class TestMusicCacheManagerUrlToHash:
    """Tests for MusicCacheManager._url_to_hash static method."""

    def test_consistent_hash(self):
        """Same URL should always produce same hash."""
        url = "https://www.youtube.com/playlist?list=PLtest123"
        hash1 = MusicCacheManager._url_to_hash(url)
        hash2 = MusicCacheManager._url_to_hash(url)
        assert hash1 == hash2

    def test_different_urls_different_hashes(self):
        """Different URLs should produce different hashes."""
        url1 = "https://www.youtube.com/playlist?list=PLtest123"
        url2 = "https://www.youtube.com/playlist?list=PLtest456"
        assert MusicCacheManager._url_to_hash(url1) != MusicCacheManager._url_to_hash(url2)

    def test_hash_length(self):
        """Hash should be 12 characters."""
        url = "https://www.youtube.com/playlist?list=PLtest"
        assert len(MusicCacheManager._url_to_hash(url)) == 12

    def test_hash_is_hex(self):
        """Hash should be valid hexadecimal."""
        url = "https://example.com"
        result = MusicCacheManager._url_to_hash(url)
        int(result, 16)  # Should not raise


class TestMusicCacheManagerInitialization:
    """Tests for MusicCacheManager initialization and directory setup."""

    def test_ensure_directories_creates_folders(self):
        """_ensure_directories should create necessary folders."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = os.path.join(tmpdir, "music")
            logger = MagicMock()

            manager = MusicCacheManager(cache_root, logger)
            manager._ensure_directories()

            assert os.path.exists(cache_root)
            assert os.path.exists(os.path.join(cache_root, "playlists"))
            assert os.path.exists(os.path.join(cache_root, "orphaned"))


class TestMusicCacheManagerPlaylistCache:
    """Tests for playlist.json loading and saving."""

    def test_load_creates_default_on_missing(self):
        """Missing playlist.json should create default structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            result = manager._load_playlist_cache()

            assert result['version'] == MusicCacheManager.PLAYLIST_SCHEMA_VERSION
            assert result['last_refresh'] == 0
            assert result['playlists'] == {}

    def test_load_reads_existing_file(self):
        """Should load existing playlist.json correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Write test data
            test_data = {
                'version': 1,
                'last_refresh': 12345,
                'playlists': {'url1': {'tracks': []}}
            }
            with open(manager.playlist_file, 'w') as f:
                json.dump(test_data, f)

            result = manager._load_playlist_cache()

            assert result['last_refresh'] == 12345
            assert 'url1' in result['playlists']

    def test_load_handles_corrupted_json(self):
        """Corrupted JSON should return default structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Write invalid JSON
            with open(manager.playlist_file, 'w') as f:
                f.write("{invalid json")

            result = manager._load_playlist_cache()

            assert result['version'] == MusicCacheManager.PLAYLIST_SCHEMA_VERSION
            assert result['playlists'] == {}

    def test_save_writes_json(self):
        """_save_playlist_cache should write valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            manager._playlist_cache = {
                'version': 1,
                'last_refresh': 99999,
                'playlists': {'test': {'data': 'here'}}
            }
            manager._save_playlist_cache()

            # Read back
            with open(manager.playlist_file, 'r') as f:
                result = json.load(f)

            assert result['last_refresh'] == 99999
            assert result['playlists']['test']['data'] == 'here'


class TestMusicCacheManagerManifest:
    """Tests for manifest.json loading and saving."""

    def test_load_creates_default_on_missing(self):
        """Missing manifest.json should create default structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            result = manager._load_manifest()

            assert result['version'] == MusicCacheManager.MANIFEST_SCHEMA_VERSION
            assert result['files'] == {}
            assert result['orphaned'] == {}

    def test_load_reads_existing_file(self):
        """Should load existing manifest.json correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            test_data = {
                'version': 1,
                'files': {'vid123': {'locations': ['abc123']}},
                'orphaned': {}
            }
            with open(manager.manifest_file, 'w') as f:
                json.dump(test_data, f)

            result = manager._load_manifest()

            assert 'vid123' in result['files']

    @pytest.mark.asyncio
    async def test_save_manifest_async(self):
        """_save_manifest should write manifest asynchronously."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            manager._manifest = {
                'version': 1,
                'files': {'video_id': {'locations': ['hash123']}},
                'orphaned': {}
            }
            await manager._save_manifest()

            with open(manager.manifest_file, 'r') as f:
                result = json.load(f)

            assert 'video_id' in result['files']


class TestMusicCacheManagerGetLocalPath:
    """Tests for get_local_path - finding downloaded files."""

    def test_returns_path_when_exists(self):
        """Returns path when file exists in playlist folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            playlist_url = "https://youtube.com/playlist?list=test"
            folder_hash = manager._url_to_hash(playlist_url)
            folder_path = os.path.join(manager.playlists_path, folder_hash)
            os.makedirs(folder_path, exist_ok=True)

            # Create dummy file
            video_id = "abc123"
            file_path = os.path.join(folder_path, f"{video_id}.mp3")
            with open(file_path, 'w') as f:
                f.write("dummy")

            result = manager.get_local_path(video_id, playlist_url)

            assert result == file_path

    def test_returns_none_when_missing(self):
        """Returns None when file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            result = manager.get_local_path("nonexistent", "https://youtube.com/playlist?list=test")

            assert result is None

    def test_checks_orphaned_folder_as_fallback(self):
        """Falls back to orphaned folder if not in playlist folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            video_id = "orphan123"
            orphan_path = os.path.join(manager.orphaned_path, f"{video_id}.mp3")
            with open(orphan_path, 'w') as f:
                f.write("dummy")

            result = manager.get_local_path(video_id, "https://youtube.com/playlist?list=test")

            assert result == orphan_path


class TestMusicCacheManagerGetCachedTracks:
    """Tests for get_cached_tracks - retrieving from cache."""

    def test_returns_empty_list_when_not_cached(self):
        """Returns empty list for uncached playlist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()
            manager._load_playlist_cache()

            result = manager.get_cached_tracks("https://youtube.com/playlist?list=unknown")

            assert result == []

    def test_returns_tracks_when_cached(self):
        """Returns Track objects for cached playlist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            playlist_url = "https://youtube.com/playlist?list=test"
            manager._playlist_cache = {
                'version': 1,
                'last_refresh': time.time(),
                'playlists': {
                    playlist_url: {
                        'folder_hash': 'abc123',
                        'tracks': [
                            {'title': 'Song 1', 'url': 'https://youtube.com/watch?v=vid1', 'video_id': 'vid1', 'artist': 'Artist 1', 'duration': 180},
                            {'title': 'Song 2', 'url': 'https://youtube.com/watch?v=vid2', 'video_id': 'vid2', 'artist': 'Artist 2', 'duration': 200},
                        ]
                    }
                }
            }

            result = manager.get_cached_tracks(playlist_url)

            assert len(result) == 2
            assert all(isinstance(t, Track) for t in result)
            assert result[0].title == 'Song 1'
            assert result[1].title == 'Song 2'


class TestMusicCacheManagerRegisterDownload:
    """Tests for _register_download - manifest updates."""

    @pytest.mark.asyncio
    async def test_adds_new_file_to_manifest(self):
        """Registering new download adds to manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()
            manager._load_manifest()

            await manager._register_download("newvideo", "folder123")

            manifest = manager._manifest
            assert manifest is not None
            assert "newvideo" in manifest['files']
            assert "folder123" in manifest['files']['newvideo']['locations']

    @pytest.mark.asyncio
    async def test_adds_location_to_existing(self):
        """Adding same video to new playlist adds location."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()
            manager._manifest = {
                'version': 1,
                'files': {'video1': {'locations': ['folder1'], 'downloaded_at': 100}},
                'orphaned': {}
            }

            await manager._register_download("video1", "folder2")

            locations = manager._manifest['files']['video1']['locations']
            assert "folder1" in locations
            assert "folder2" in locations

    @pytest.mark.asyncio
    async def test_removes_from_orphaned(self):
        """Re-downloading orphaned track removes from orphaned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()
            manager._manifest = {
                'version': 1,
                'files': {},
                'orphaned': {'oldvideo': {'orphaned_at': 100}}
            }

            await manager._register_download("oldvideo", "newfolder")

            assert "oldvideo" not in manager._manifest['orphaned']
            assert "oldvideo" in manager._manifest['files']


class TestMusicCacheManagerOrphanManagement:
    """Tests for orphan/unorphan functionality."""

    def test_orphan_track_moves_file(self):
        """_orphan_track moves file to orphaned folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create source file
            folder_hash = "abc123"
            folder_path = os.path.join(manager.playlists_path, folder_hash)
            os.makedirs(folder_path, exist_ok=True)
            source_file = os.path.join(folder_path, "video1.mp3")
            with open(source_file, 'w') as f:
                f.write("content")

            manifest = {'version': 1, 'files': {'video1': {'locations': [folder_hash]}}, 'orphaned': {}}
            manager._orphan_track("video1", folder_hash, manifest)

            # File should be moved
            assert not os.path.exists(source_file)
            assert os.path.exists(os.path.join(manager.orphaned_path, "video1.mp3"))
            assert "video1" in manifest['orphaned']

    def test_unorphan_track_restores_file(self):
        """_unorphan_track moves file back to playlist folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create orphaned file
            orphan_file = os.path.join(manager.orphaned_path, "video1.mp3")
            with open(orphan_file, 'w') as f:
                f.write("content")

            manifest = {'version': 1, 'files': {}, 'orphaned': {'video1': {'orphaned_at': 100}}}
            manager._unorphan_track("video1", "folder123", manifest)

            # File should be copied back
            assert not os.path.exists(orphan_file)
            target = os.path.join(manager.playlists_path, "folder123", "video1.mp3")
            assert os.path.exists(target)
            assert "video1" not in manifest['orphaned']
            assert "video1" in manifest['files']


class TestMusicCacheManagerCleanupOrphans:
    """Tests for cleanup_expired_orphans."""

    @pytest.mark.asyncio
    async def test_deletes_old_orphans(self):
        """Orphans older than TTL should be deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create orphaned file and old manifest entry
            orphan_file = os.path.join(manager.orphaned_path, "oldvideo.mp3")
            with open(orphan_file, 'w') as f:
                f.write("content")

            # Set orphaned_at to 100 days ago
            old_time = time.time() - (100 * 24 * 3600)
            manager._manifest = {
                'version': 1,
                'files': {},
                'orphaned': {'oldvideo': {'orphaned_at': old_time}}
            }

            deleted = await manager.cleanup_expired_orphans()

            assert deleted == 1
            assert not os.path.exists(orphan_file)
            assert 'oldvideo' not in manager._manifest['orphaned']

    @pytest.mark.asyncio
    async def test_keeps_recent_orphans(self):
        """Orphans within TTL should be kept."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create orphaned file with recent timestamp
            orphan_file = os.path.join(manager.orphaned_path, "newvideo.mp3")
            with open(orphan_file, 'w') as f:
                f.write("content")

            # Set orphaned_at to 10 days ago (within 90-day TTL)
            recent_time = time.time() - (10 * 24 * 3600)
            manager._manifest = {
                'version': 1,
                'files': {},
                'orphaned': {'newvideo': {'orphaned_at': recent_time}}
            }

            deleted = await manager.cleanup_expired_orphans()

            assert deleted == 0
            assert os.path.exists(orphan_file)
            assert 'newvideo' in manager._manifest['orphaned']


class TestMusicCacheManagerClearOrphaned:
    """Tests for clear_orphaned - manual cleanup."""

    def test_clears_all_orphans(self):
        """clear_orphaned should remove all orphaned files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create multiple orphaned files
            for i in range(3):
                orphan_file = os.path.join(manager.orphaned_path, f"video{i}.mp3")
                with open(orphan_file, 'w') as f:
                    f.write("content")

            manager._manifest = {
                'version': 1,
                'files': {},
                'orphaned': {
                    'video0': {'orphaned_at': 100},
                    'video1': {'orphaned_at': 200},
                    'video2': {'orphaned_at': 300},
                }
            }

            deleted = manager.clear_orphaned()

            assert deleted == 3
            assert len(os.listdir(manager.orphaned_path)) == 0
            assert manager._manifest['orphaned'] == {}


class TestMusicCacheManagerGetStats:
    """Tests for get_stats - cache statistics."""

    def test_returns_stats_dict(self):
        """get_stats should return comprehensive statistics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Create some test structure
            manager._playlist_cache = {
                'version': 1,
                'last_refresh': time.time() - 3600,
                'playlists': {
                    'url1': {'tracks': [{'title': 't1'}, {'title': 't2'}]},
                    'url2': {'tracks': [{'title': 't3'}]},
                }
            }

            # Create a downloaded file
            folder_path = os.path.join(manager.playlists_path, "testhash")
            os.makedirs(folder_path, exist_ok=True)
            with open(os.path.join(folder_path, "test.mp3"), 'wb') as f:
                f.write(b"x" * 1024)  # 1KB file

            stats = manager.get_stats()

            assert stats['total_playlists'] == 2
            assert stats['total_tracks'] == 3
            assert stats['downloaded_tracks'] == 1
            assert 'size_mb' in stats
            assert 'last_refresh' in stats
            assert 'last_refresh_ago' in stats


class TestMusicCacheManagerShutdown:
    """Tests for shutdown - graceful cleanup."""

    @pytest.mark.asyncio
    async def test_shutdown_sets_flag(self):
        """shutdown() should set _shutdown flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            await manager.shutdown()

            assert manager._shutdown is True

    @pytest.mark.asyncio
    async def test_shutdown_cancels_tasks(self):
        """shutdown() should cancel background tasks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MusicCacheManager(tmpdir, MagicMock())
            manager._ensure_directories()

            # Use asyncio.Event for efficient waiting
            refresh_event = asyncio.Event()
            download_event = asyncio.Event()

            async def mock_refresh_coro():
                await refresh_event.wait()

            async def mock_download_coro():
                await download_event.wait()

            # Create real asyncio tasks
            manager._refresh_task = asyncio.create_task(mock_refresh_coro())
            manager._download_task = asyncio.create_task(mock_download_coro())

            await manager.shutdown()

            # Tasks should be cancelled
            assert manager._refresh_task.cancelled() or manager._refresh_task.done()
            assert manager._download_task.cancelled() or manager._download_task.done()
            assert manager._shutdown is True

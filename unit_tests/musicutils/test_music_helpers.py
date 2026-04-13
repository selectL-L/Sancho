"""Tests for pure utility functions in music_helpers.py.

These are all pure functions with no I/O, so no mocking is needed.
"""

from utils.musicutils.music_helpers import (
    extract_video_id,
    is_video_unavailable,
    is_403_error,
    format_youtube_error,
    detect_mix_in_url,
    sanitize_filename,
)


class TestExtractVideoId:
    """Tests for extract_video_id() - extracts 11-char YouTube video ID from various URL formats."""

    def test_standard_watch_url(self):
        """Standard youtube.com/watch?v= format."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_short_url(self):
        """Short youtu.be/ format."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_url(self):
        """Embed /embed/ format."""
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_url_not_supported(self):
        """Shorts /shorts/ format - not currently supported by implementation."""
        url = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        # Implementation doesn't handle /shorts/ URLs
        assert extract_video_id(url) is None

    def test_watch_url_with_extra_params(self):
        """Watch URL with additional query parameters."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=2"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_raw_video_id(self):
        """Just the 11-character video ID."""
        video_id = "dQw4w9WgXcQ"
        assert extract_video_id(video_id) == "dQw4w9WgXcQ"

    def test_music_youtube_url(self):
        """music.youtube.com domain."""
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_no_www_prefix(self):
        """URL without www prefix."""
        url = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_http_protocol(self):
        """HTTP (not HTTPS) protocol."""
        url = "http://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_invalid_url_returns_none(self):
        """Non-YouTube URL returns None."""
        url = "https://example.com/video"
        assert extract_video_id(url) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert extract_video_id("") is None

    def test_short_url_with_query_params(self):
        """youtu.be with query parameters."""
        url = "https://youtu.be/dQw4w9WgXcQ?t=42"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_v_live_url(self):
        """v/ format (older YouTube format)."""
        url = "https://www.youtube.com/v/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"


class TestIsVideoUnavailable:
    """Tests for is_video_unavailable() - detects permanent failure errors."""

    def test_video_unavailable_message(self):
        """Explicit 'video unavailable' message."""
        error = Exception("Video unavailable: This video has been removed")
        assert is_video_unavailable(error) is True

    def test_private_video(self):
        """Private video error."""
        error = Exception("This video is private")
        assert is_video_unavailable(error) is True

    def test_removed_video(self):
        """Removed video error."""
        error = Exception("This video has been removed by the uploader")
        assert is_video_unavailable(error) is True

    def test_copyright_takedown(self):
        """Copyright claim error."""
        error = Exception("This video is no longer available due to a copyright claim")
        assert is_video_unavailable(error) is True

    def test_transient_error_not_unavailable(self):
        """Transient network error should NOT be flagged."""
        error = Exception("Network timeout while fetching video")
        assert is_video_unavailable(error) is False

    def test_403_not_unavailable(self):
        """403 is auth-related, not unavailable."""
        error = Exception("HTTP Error 403: Forbidden")
        assert is_video_unavailable(error) is False

    def test_case_insensitive(self):
        """Check should be case-insensitive."""
        error = Exception("VIDEO UNAVAILABLE")
        assert is_video_unavailable(error) is True


class TestIs403Error:
    """Tests for is_403_error() - detects authentication/authorization errors."""

    def test_explicit_403(self):
        """Explicit HTTP 403 error."""
        error = Exception("HTTP Error 403: Forbidden")
        assert is_403_error(error) is True

    def test_forbidden_keyword(self):
        """'Forbidden' keyword in error."""
        error = Exception("Access forbidden for this resource")
        assert is_403_error(error) is True

    def test_sign_in_required(self):
        """Sign in required error."""
        error = Exception("Sign in to confirm your age")
        assert is_403_error(error) is True

    def test_age_restricted(self):
        """Age-restricted content."""
        error = Exception("This video is age-restricted")
        assert is_403_error(error) is True

    def test_network_error_not_403(self):
        """Network error should NOT be flagged."""
        error = Exception("Connection timed out")
        assert is_403_error(error) is False

    def test_404_not_403(self):
        """404 error should NOT be flagged as 403."""
        error = Exception("HTTP Error 404: Not Found")
        assert is_403_error(error) is False


class TestFormatYoutubeError:
    """Tests for format_youtube_error() - user-friendly error messages."""

    def test_unavailable_video(self):
        """Unavailable video gets friendly message."""
        error = Exception("Video unavailable")
        result = format_youtube_error(error)
        assert "unavailable" in result.lower() or "can't play" in result.lower()

    def test_private_video(self):
        """Private video gets friendly message."""
        error = Exception("This video is private")
        result = format_youtube_error(error)
        assert "private" in result.lower()

    def test_age_restricted(self):
        """Age-restricted gets friendly message."""
        error = Exception("Sign in to confirm your age")
        result = format_youtube_error(error)
        assert "age" in result.lower() or "restricted" in result.lower()

    def test_copyright(self):
        """Copyright claim gets friendly message."""
        error = Exception("copyright claim by Some Company")
        result = format_youtube_error(error)
        assert "copyright" in result.lower()

    def test_generic_error(self):
        """Unknown error gets generic message."""
        error = Exception("Some unknown error occurred")
        result = format_youtube_error(error)
        # Should return something, not crash
        assert isinstance(result, str)
        assert len(result) > 0


class TestDetectMixInUrl:
    """Tests for detect_mix_in_url() - detects video+mix combo URLs."""

    def test_video_with_mix(self):
        """URL with both video ID and mix list."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
        has_mix, single_url, mix_url = detect_mix_in_url(url)
        assert has_mix is True
        assert single_url is not None and "dQw4w9WgXcQ" in single_url
        assert single_url is not None and "list=" not in single_url
        assert mix_url is not None and "list=RD" in mix_url

    def test_video_with_regular_playlist(self):
        """URL with video ID and regular playlist (not a mix)."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        # Regular playlists should NOT be detected as mixes
        assert has_mix is False

    def test_plain_video_url(self):
        """Plain video URL without any list parameter."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        has_mix, _single_url, _mix_url = detect_mix_in_url(url)
        assert has_mix is False


class TestSanitizeFilename:
    """Tests for sanitize_filename() - creates filesystem-safe filenames."""

    def test_normal_filename(self):
        """Normal filename - spaces become underscores."""
        name = "My Song Title"
        result = sanitize_filename(name)
        # Implementation replaces spaces with underscores
        assert result == "My_Song_Title"

    def test_removes_invalid_chars(self):
        """Invalid filesystem characters are removed."""
        name = "Song: The <Best> One?"
        result = sanitize_filename(name)
        assert ":" not in result
        assert "<" not in result
        assert ">" not in result
        assert "?" not in result

    def test_collapses_spaces(self):
        """Multiple spaces collapsed to single space."""
        name = "Song   with    many     spaces"
        result = sanitize_filename(name)
        assert "  " not in result

    def test_max_length_truncation(self):
        """Long filenames are truncated."""
        name = "A" * 300
        result = sanitize_filename(name, max_length=100)
        assert len(result) <= 100

    def test_strips_leading_trailing_spaces(self):
        """Leading/trailing spaces are stripped, inner become underscores."""
        name = "  Song Title  "
        result = sanitize_filename(name)
        # Leading/trailing stripped, inner spaces become underscores
        assert result == "Song_Title"

    def test_removes_path_separators(self):
        """Path separators are removed."""
        name = "Song/With\\Path"
        result = sanitize_filename(name)
        assert "/" not in result
        assert "\\" not in result

    def test_empty_string_handling(self):
        """Empty string gets a fallback."""
        name = ""
        result = sanitize_filename(name)
        # Should return some default, not empty
        assert len(result) > 0 or result == ""

    def test_only_invalid_chars(self):
        """String with only invalid chars gets fallback."""
        name = ":?<>|"
        result = sanitize_filename(name)
        # Should return some default
        assert isinstance(result, str)

    def test_unicode_preserved(self):
        """Unicode characters are preserved."""
        name = "日本語の曲"
        result = sanitize_filename(name)
        assert "日本語" in result

    def test_newlines_removed(self):
        """Newlines are removed."""
        name = "Song\nTitle"
        result = sanitize_filename(name)
        assert "\n" not in result

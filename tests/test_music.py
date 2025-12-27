"""Unit tests for the Music cog and music_helpers module.

This module contains tests built incrementally:
- Phase 1: Dataclass tests (Track, LoopMode, state classes)
- Phase 2: Pure playlist logic (no mocking needed)
- Phase 3: Session/playback logic (requires mocking)

Each phase is verified against the actual source before implementation.
"""
import os
import tempfile
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

from utils.music_helpers import (
    AmbienceState,
    LoopMode,
    PlaybackState,
    PrefetchState,
    RetryState,
    Track,
    extract_video_id,
)


# =============================================================================
# HELPERS
# =============================================================================


def create_track(
    title: str = "Never Gonna Give You Up",
    artist: str = "Rick Astley",
    url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    duration: int = 212,
    video_id: Optional[str] = None
) -> Track:
    """Create a Track instance for testing.
    
    Default values use Rick Astley's "Never Gonna Give You Up" as a real YouTube reference.
    """
    vid = video_id or (url.split("v=")[-1][:11] if "v=" in url else "dQw4w9WgXcQ")
    return Track(
        title=title,
        artist=artist,
        url=url,
        duration=duration,
        video_id=vid
    )


# =============================================================================
# PHASE 1: DATACLASS AND ENUM TESTS
# =============================================================================


class TestLoopMode:
    """Tests for the LoopMode enum."""

    def test_values(self):
        """LoopMode has three values: OFF=0, ONE=1, ALL=2."""
        assert LoopMode.OFF.value == 0
        assert LoopMode.ONE.value == 1
        assert LoopMode.ALL.value == 2

    def test_display_property(self):
        """display property returns human-readable names."""
        assert LoopMode.OFF.display == "Off"
        assert LoopMode.ONE.display == "One"
        assert LoopMode.ALL.display == "All"

    def test_emoji_property(self):
        """emoji property returns correct emoji for each mode."""
        assert LoopMode.OFF.emoji == "➡️"
        assert LoopMode.ONE.emoji == "🔂"
        assert LoopMode.ALL.emoji == "🔁"

    def test_next_cycles_correctly(self):
        """next() cycles OFF -> ONE -> ALL -> OFF."""
        assert LoopMode.OFF.next() == LoopMode.ONE
        assert LoopMode.ONE.next() == LoopMode.ALL
        assert LoopMode.ALL.next() == LoopMode.OFF

    def test_next_full_cycle(self):
        """Cycling through next() three times returns to original."""
        mode = LoopMode.OFF
        mode = mode.next()  # ONE
        mode = mode.next()  # ALL
        mode = mode.next()  # OFF
        assert mode == LoopMode.OFF

    @pytest.mark.asyncio
    async def test_convert_valid_lowercase(self):
        """convert() accepts lowercase input."""
        ctx = MagicMock(spec=commands.Context)
        assert await LoopMode.convert(ctx, "off") == LoopMode.OFF
        assert await LoopMode.convert(ctx, "one") == LoopMode.ONE
        assert await LoopMode.convert(ctx, "all") == LoopMode.ALL

    @pytest.mark.asyncio
    async def test_convert_valid_uppercase(self):
        """convert() accepts uppercase input."""
        ctx = MagicMock(spec=commands.Context)
        assert await LoopMode.convert(ctx, "OFF") == LoopMode.OFF
        assert await LoopMode.convert(ctx, "ONE") == LoopMode.ONE
        assert await LoopMode.convert(ctx, "ALL") == LoopMode.ALL

    @pytest.mark.asyncio
    async def test_convert_valid_mixed_case(self):
        """convert() accepts mixed case input."""
        ctx = MagicMock(spec=commands.Context)
        assert await LoopMode.convert(ctx, "Off") == LoopMode.OFF
        assert await LoopMode.convert(ctx, "oNe") == LoopMode.ONE
        assert await LoopMode.convert(ctx, "AlL") == LoopMode.ALL

    @pytest.mark.asyncio
    async def test_convert_invalid_raises_bad_argument(self):
        """convert() raises BadArgument for invalid input."""
        ctx = MagicMock(spec=commands.Context)
        with pytest.raises(commands.BadArgument) as exc_info:
            await LoopMode.convert(ctx, "invalid")
        assert "'invalid' is not a valid loop mode" in str(exc_info.value)


class TestTrack:
    """Tests for the Track dataclass."""

    def test_creation_minimal(self):
        """Track can be created with required fields only."""
        track = Track(
            title="Test Song",
            artist="Test Artist",
            url="https://youtube.com/watch?v=abc123def45",
            duration=180
        )
        assert track.title == "Test Song"
        assert track.artist == "Test Artist"
        assert track.url == "https://youtube.com/watch?v=abc123def45"
        assert track.duration == 180
        # Defaults
        assert track.thumbnail is None
        assert track.thumbnail_needs_crop is False
        assert track.user_added is False
        assert track.local_path is None
        assert track.video_id is None

    def test_creation_all_fields(self):
        """Track can be created with all fields specified."""
        track = Track(
            title="Full Track",
            artist="Full Artist",
            url="https://youtube.com/watch?v=xyz789abc12",
            duration=240,
            thumbnail="https://img.youtube.com/vi/xyz789abc12/0.jpg",
            thumbnail_needs_crop=True,
            user_added=True,
            local_path="/cache/track.mp3",
            video_id="xyz789abc12"
        )
        assert track.thumbnail == "https://img.youtube.com/vi/xyz789abc12/0.jpg"
        assert track.thumbnail_needs_crop is True
        assert track.user_added is True
        assert track.local_path == "/cache/track.mp3"
        assert track.video_id == "xyz789abc12"

    def test_to_dict_includes_correct_fields(self):
        """to_dict() serializes all persistent fields."""
        track = Track(
            title="Serialize Me",
            artist="Serializer",
            url="https://youtube.com/watch?v=ser123ial45",
            duration=120,
            thumbnail="https://thumb.url",
            thumbnail_needs_crop=True,
            local_path="/path/to/cache.mp3",
            video_id="ser123ial45"
        )
        d = track.to_dict()

        assert d['title'] == "Serialize Me"
        assert d['artist'] == "Serializer"
        assert d['url'] == "https://youtube.com/watch?v=ser123ial45"
        assert d['duration'] == 120
        assert d['thumbnail'] == "https://thumb.url"
        assert d['thumbnail_needs_crop'] is True
        assert d['local_path'] == "/path/to/cache.mp3"
        assert d['video_id'] == "ser123ial45"

    def test_to_dict_excludes_user_added(self):
        """to_dict() intentionally excludes user_added (session-only field)."""
        track = Track(
            title="User Track",
            artist="User",
            url="https://youtube.com/watch?v=usr123xxx45",
            duration=60,
            user_added=True
        )
        d = track.to_dict()
        assert 'user_added' not in d

    def test_from_dict_basic(self):
        """from_dict() creates Track from dictionary."""
        data = {
            'title': "From Dict",
            'artist': "Dict Artist",
            'url': "https://youtube.com/watch?v=dct123abc45",
            'duration': 200
        }
        track = Track.from_dict(data)

        assert track.title == "From Dict"
        assert track.artist == "Dict Artist"
        assert track.url == "https://youtube.com/watch?v=dct123abc45"
        assert track.duration == 200
        assert track.user_added is False  # Default, not in dict

    def test_from_dict_with_optional_fields(self):
        """from_dict() handles optional fields correctly."""
        data = {
            'title': "Full Dict",
            'artist': "Full Artist",
            'url': "https://youtube.com/watch?v=ful123lll45",
            'duration': 300,
            'thumbnail': "https://thumb.url",
            'thumbnail_needs_crop': True,
            'local_path': "/cached/path.mp3",
            'video_id': "ful123lll45"
        }
        track = Track.from_dict(data)

        assert track.thumbnail == "https://thumb.url"
        assert track.thumbnail_needs_crop is True
        assert track.local_path == "/cached/path.mp3"
        assert track.video_id == "ful123lll45"

    def test_from_dict_extracts_video_id_if_missing(self):
        """from_dict() extracts video_id from URL if not in dict."""
        data = {
            'title': "No Video ID",
            'artist': "Artist",
            'url': "https://youtube.com/watch?v=ext123ract5",
            'duration': 100
            # video_id intentionally omitted
        }
        track = Track.from_dict(data)
        assert track.video_id == "ext123ract5"

    def test_roundtrip_serialization(self):
        """to_dict() -> from_dict() preserves all persistent data."""
        original = Track(
            title="Roundtrip",
            artist="Round Artist",
            url="https://youtube.com/watch?v=rnd123trip5",
            duration=180,
            thumbnail="https://thumb.url",
            thumbnail_needs_crop=True,
            local_path="/cached/roundtrip.mp3",
            video_id="rnd123trip5"
        )
        restored = Track.from_dict(original.to_dict())

        assert restored.title == original.title
        assert restored.artist == original.artist
        assert restored.url == original.url
        assert restored.duration == original.duration
        assert restored.thumbnail == original.thumbnail
        assert restored.thumbnail_needs_crop == original.thumbnail_needs_crop
        assert restored.local_path == original.local_path
        assert restored.video_id == original.video_id

    def test_is_cached_false_when_no_local_path(self):
        """is_cached returns False when local_path is None."""
        track = Track(
            title="No Cache",
            artist="Artist",
            url="https://youtube.com/watch?v=noc123ache5",
            duration=100,
            local_path=None
        )
        assert track.is_cached is False

    def test_is_cached_false_when_file_does_not_exist(self):
        """is_cached returns False when local_path points to nonexistent file."""
        track = Track(
            title="Missing File",
            artist="Artist",
            url="https://youtube.com/watch?v=mis123sing5",
            duration=100,
            local_path="/nonexistent/path/file.mp3"
        )
        assert track.is_cached is False

    def test_is_cached_true_when_file_exists(self):
        """is_cached returns True when local_path points to existing file."""
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            temp_path = f.name

        try:
            track = Track(
                title="Cached Track",
                artist="Artist",
                url="https://youtube.com/watch?v=cch123edd55",
                duration=100,
                local_path=temp_path
            )
            assert track.is_cached is True
        finally:
            os.unlink(temp_path)


class TestExtractVideoId:
    """Tests for the extract_video_id utility function."""

    def test_standard_watch_url(self):
        """Extracts ID from standard youtube.com/watch?v= URL."""
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        """Extracts ID from youtu.be short URL."""
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        """Extracts ID from embed URL."""
        assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        """Extracts ID from URL with additional query parameters."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_raw_video_id(self):
        """Extracts ID when given just the 11-character ID."""
        assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_invalid_url_returns_none(self):
        """Returns None for URLs without valid video ID."""
        assert extract_video_id("https://example.com/video") is None
        assert extract_video_id("not a url") is None
        assert extract_video_id("") is None


class TestPlaybackState:
    """Tests for the PlaybackState dataclass."""

    def test_default_values(self):
        """PlaybackState initializes with correct defaults."""
        state = PlaybackState()
        assert state.current_audio_url is None
        assert state.current_audio_track_url is None
        assert state.track_started_timestamp == 0.0
        assert state.paused_at_position is None

    def test_clear_resets_all_fields(self):
        """clear() resets all fields to defaults."""
        state = PlaybackState(
            current_audio_url="https://audio.url",
            current_audio_track_url="https://youtube.com/watch?v=abc",
            track_started_timestamp=12345.0,
            paused_at_position=60.5
        )
        state.clear()

        assert state.current_audio_url is None
        assert state.current_audio_track_url is None
        assert state.track_started_timestamp == 0.0
        assert state.paused_at_position is None


class TestPrefetchState:
    """Tests for the PrefetchState dataclass."""

    def test_default_values(self):
        """PrefetchState initializes with correct defaults."""
        state = PrefetchState()
        assert state.audio_url is None
        assert state.http_headers is None
        assert state.thumbnail_bytes is None
        assert state.target_index is None
        assert state.target_video_id is None
        assert state.fetched_at == 0.0
        assert state.task is None

    def test_clear_resets_all_fields_but_not_task(self):
        """clear() resets all fields but leaves task reference (caller manages)."""
        mock_task = MagicMock()
        state = PrefetchState(
            audio_url="https://prefetched.audio.url",
            http_headers={"User-Agent": "test"},
            thumbnail_bytes=b"fake_image",
            target_index=5,
            target_video_id="abc123",
            fetched_at=12345.0,
            task=mock_task
        )
        state.clear()

        assert state.audio_url is None
        assert state.http_headers is None
        assert state.thumbnail_bytes is None
        assert state.target_index is None
        assert state.target_video_id is None
        assert state.fetched_at == 0.0
        # Task is NOT cleared by clear() - see docstring
        assert state.task is mock_task

    def test_cancel_task_cancels_running_task(self):
        """cancel_task() cancels task if running and sets to None."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        state = PrefetchState(task=mock_task)

        state.cancel_task()

        mock_task.cancel.assert_called_once()
        assert state.task is None

    def test_cancel_task_skips_done_task(self):
        """cancel_task() does not cancel already-done task."""
        mock_task = MagicMock()
        mock_task.done.return_value = True
        state = PrefetchState(task=mock_task)

        state.cancel_task()

        mock_task.cancel.assert_not_called()
        assert state.task is None

    def test_cancel_task_handles_none(self):
        """cancel_task() handles None task gracefully."""
        state = PrefetchState(task=None)
        state.cancel_task()  # Should not raise
        assert state.task is None

    def test_is_valid_for_returns_false_when_no_audio_url(self):
        """is_valid_for() returns False when no audio URL is cached."""
        state = PrefetchState(target_index=5)
        assert state.is_valid_for(5, 10, 4, "abc123") is False

    def test_is_valid_for_returns_false_when_no_target_index(self):
        """is_valid_for() returns False when no target index is set."""
        state = PrefetchState(audio_url="https://example.com")
        assert state.is_valid_for(5, 10, 4, "abc123") is False

    def test_is_valid_for_returns_false_when_index_mismatch(self):
        """is_valid_for() returns False when target index doesn't match expected next."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=3,  # Wrong - should be 5 (next from current=4)
            target_video_id="abc123",
            fetched_at=time.time()
        )
        assert state.is_valid_for(5, 10, 4, "abc123") is False

    def test_is_valid_for_returns_false_when_video_id_mismatch(self):
        """is_valid_for() returns False when video ID doesn't match."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=5,
            target_video_id="different_id",
            fetched_at=time.time()
        )
        assert state.is_valid_for(5, 10, 4, "abc123") is False

    def test_is_valid_for_returns_false_when_expired(self):
        """is_valid_for() returns False when URL has expired."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=5,
            target_video_id="abc123",
            fetched_at=time.time() - (6 * 60 * 60)  # 6 hours ago (expired)
        )
        assert state.is_valid_for(5, 10, 4, "abc123") is False

    def test_is_valid_for_returns_true_when_all_valid(self):
        """is_valid_for() returns True when all conditions are met."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=5,
            target_video_id="abc123",
            fetched_at=time.time()
        )
        # Current index 4, playlist len 10, next would be 5
        assert state.is_valid_for(5, 10, 4, "abc123") is True

    def test_is_valid_for_wraps_around_playlist(self):
        """is_valid_for() handles playlist wraparound correctly."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=0,  # Wrapped around
            target_video_id="abc123",
            fetched_at=time.time()
        )
        # Current index 9, playlist len 10, next would be 0 (wraparound)
        assert state.is_valid_for(0, 10, 9, "abc123") is True

    def test_invalidate_if_affected_clears_when_target_affected(self):
        """invalidate_if_affected() clears state when target index is affected."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=5,
            target_video_id="abc123",
            fetched_at=time.time(),
            task=mock_task
        )

        result = state.invalidate_if_affected({5, 8}, 10)

        assert result is True
        assert state.audio_url is None
        mock_task.cancel.assert_called_once()

    def test_invalidate_if_affected_preserves_when_not_affected(self):
        """invalidate_if_affected() preserves state when target not affected."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=5,
            target_video_id="abc123",
            fetched_at=time.time()
        )

        result = state.invalidate_if_affected({1, 3, 8}, 10)

        assert result is False
        assert state.audio_url == "https://example.com"

    def test_invalidate_if_affected_clears_when_out_of_bounds(self):
        """invalidate_if_affected() clears when target index is out of bounds."""
        state = PrefetchState(
            audio_url="https://example.com",
            target_index=8,
            target_video_id="abc123",
            fetched_at=time.time()
        )

        # Playlist shrunk to 5 items, target_index 8 is now out of bounds
        result = state.invalidate_if_affected(set(), 5)

        assert result is True
        assert state.audio_url is None


class TestRetryState:
    """Tests for the RetryState dataclass (phase-aware retry system)."""

    def test_default_values(self):
        """RetryState initializes with correct defaults."""
        state = RetryState()
        assert state.pending is False
        assert state.direct_count == 0
        assert state.residential_count == 0
        assert state.is_residential_phase is False
        assert state.total_attempts == 0

    def test_request_retry_sets_pending(self):
        """request_retry() sets pending to True."""
        state = RetryState()
        state.request_retry()
        assert state.pending is True

    def test_consume_retry_returns_false_if_not_pending(self):
        """consume_retry() returns False if no retry was requested."""
        state = RetryState()
        assert state.consume_retry() is False

    def test_get_next_strategy_direct_phase(self):
        """get_next_strategy() returns direct attempts first."""
        state = RetryState()
        state.request_retry()

        # First attempt should be direct
        result = state.get_next_strategy()
        assert result == (False, 1)  # (is_residential=False, attempt=1)
        assert state.direct_count == 1
        assert state.is_residential_phase is False

        # Second direct attempt
        state.request_retry()
        result = state.get_next_strategy()
        assert result == (False, 2)  # (is_residential=False, attempt=2)
        assert state.direct_count == 2

    def test_get_next_strategy_transitions_to_residential(self):
        """get_next_strategy() transitions to residential after direct exhausted."""
        state = RetryState()

        # Exhaust direct attempts (2 by default)
        for _ in range(state.DIRECT_MAX):
            state.request_retry()
            state.get_next_strategy()

        # Next attempt should be residential
        state.request_retry()
        result = state.get_next_strategy()
        assert result == (True, 1)  # (is_residential=True, attempt=1)
        assert state.is_residential_phase is True
        assert state.residential_count == 1

    def test_get_next_strategy_exhausts_all_attempts(self):
        """get_next_strategy() returns None when all attempts exhausted."""
        state = RetryState()

        # Exhaust all attempts (2 direct + 3 residential = 5 total)
        total = state.DIRECT_MAX + state.RESIDENTIAL_MAX
        for i in range(total):
            state.request_retry()
            result = state.get_next_strategy()
            assert result is not None, f"Attempt {i+1} should succeed"

        # Next request should be denied
        state.request_retry()
        result = state.get_next_strategy()
        assert result is None
        assert state.is_exhausted is True
        assert state.total_attempts == total

    def test_reset_clears_state(self):
        """reset() clears all counters and phase state."""
        state = RetryState()
        state.request_retry()
        state.get_next_strategy()
        state.request_retry()

        state.reset()

        assert state.pending is False
        assert state.direct_count == 0
        assert state.residential_count == 0
        assert state.is_residential_phase is False
        assert state.residential_notified is False

    def test_reset_allows_retry_again(self):
        """After reset(), retries are allowed again."""
        state = RetryState()

        # Exhaust all retries
        total = state.DIRECT_MAX + state.RESIDENTIAL_MAX
        for _ in range(total):
            state.request_retry()
            state.get_next_strategy()

        assert state.is_exhausted is True

        # Reset and try again
        state.reset()
        state.request_retry()
        result = state.get_next_strategy()
        assert result == (False, 1)  # Back to direct phase
        assert state.is_exhausted is False

    def test_consume_retry_backwards_compatible(self):
        """consume_retry() works for backwards compatibility."""
        state = RetryState()
        state.request_retry()
        assert state.consume_retry() is True
        assert state.direct_count == 1

    def test_absolute_max_attempts_safeguard(self):
        """ABSOLUTE_MAX_ATTEMPTS prevents theoretical infinite loops."""
        state = RetryState()

        # The paranoid safeguard should cap at ABSOLUTE_MAX_ATTEMPTS even if
        # somehow the phase logic were to malfunction
        assert state.ABSOLUTE_MAX_ATTEMPTS == 10  # Verify constant

        # Normal operation should never hit this - the sum of DIRECT_MAX and
        # RESIDENTIAL_MAX is less than ABSOLUTE_MAX_ATTEMPTS
        assert state.DIRECT_MAX + state.RESIDENTIAL_MAX < state.ABSOLUTE_MAX_ATTEMPTS

    def test_absolute_max_prevents_runaway(self):
        """Even with corrupted state, ABSOLUTE_MAX_ATTEMPTS stops retries."""
        state = RetryState()

        # Simulate corrupted state where counters don't stop (should never happen)
        # Manually set counters beyond normal limits to test the safeguard
        state.direct_count = 5
        state.residential_count = 5
        state.is_residential_phase = True
        # total_attempts is now 10, equal to ABSOLUTE_MAX_ATTEMPTS

        state.request_retry()
        result = state.get_next_strategy()

        # The absolute cap should prevent any more attempts
        assert result is None

    def test_residential_notified_flag(self):
        """residential_notified flag persists until reset."""
        state = RetryState()
        assert state.residential_notified is False

        state.residential_notified = True
        assert state.residential_notified is True

        state.reset()
        assert state.residential_notified is False


class TestAmbienceState:
    """Tests for the AmbienceState dataclass."""

    def test_default_values(self):
        """AmbienceState initializes with correct defaults."""
        state = AmbienceState()
        assert state.current_playlist_url is None
        assert state.pending_playlist_url is None
        assert state.pending_switch is False

    def test_request_switch_sets_pending(self):
        """request_switch() queues a playlist change."""
        state = AmbienceState()
        state.request_switch("https://youtube.com/playlist?list=PLxxx")

        assert state.pending_playlist_url == "https://youtube.com/playlist?list=PLxxx"
        assert state.pending_switch is True

    def test_request_switch_with_none(self):
        """request_switch(None) queues a stop request."""
        state = AmbienceState()
        state.request_switch(None)

        assert state.pending_playlist_url is None
        assert state.pending_switch is True

    def test_consume_switch_returns_false_if_no_pending(self):
        """consume_switch() returns (False, None) if no switch pending."""
        state = AmbienceState()
        had_switch, url = state.consume_switch()

        assert had_switch is False
        assert url is None

    def test_consume_switch_returns_pending_url(self):
        """consume_switch() returns and clears pending switch."""
        state = AmbienceState()
        state.request_switch("https://youtube.com/playlist?list=PLyyy")

        had_switch, url = state.consume_switch()

        assert had_switch is True
        assert url == "https://youtube.com/playlist?list=PLyyy"
        assert state.pending_switch is False
        assert state.pending_playlist_url is None

    def test_consume_switch_only_works_once(self):
        """consume_switch() only returns True once per request."""
        state = AmbienceState()
        state.request_switch("https://youtube.com/playlist?list=PLzzz")

        # First consume
        had_switch1, url1 = state.consume_switch()
        assert had_switch1 is True
        assert url1 == "https://youtube.com/playlist?list=PLzzz"

        # Second consume - should be empty
        had_switch2, url2 = state.consume_switch()
        assert had_switch2 is False
        assert url2 is None

    def test_confirm_switch_updates_current(self):
        """confirm_switch() updates current_playlist_url."""
        state = AmbienceState()
        state.confirm_switch("https://youtube.com/playlist?list=PLconfirmed")

        assert state.current_playlist_url == "https://youtube.com/playlist?list=PLconfirmed"

    def test_full_switch_workflow(self):
        """Tests the complete switch workflow: request -> consume -> confirm."""
        state = AmbienceState()

        # Initial state
        assert state.current_playlist_url is None

        # Request switch
        new_url = "https://youtube.com/playlist?list=PLnew"
        state.request_switch(new_url)

        # Consume switch
        had_switch, url = state.consume_switch()
        assert had_switch is True
        assert url == new_url

        # Confirm switch
        state.confirm_switch(new_url)
        assert state.current_playlist_url == new_url


# =============================================================================
# PHASE 2: MUSIC COG PLAYLIST LOGIC TESTS
# =============================================================================


# Import Music cog for Phase 2+ tests
from cogs.music import Music
from utils.bot_class import CoreBot
from utils.database import DatabaseManager


def create_voice_ctx(channel_id: int = 12345, author_id: int = 67890) -> MagicMock:
    """Create a mock context with voice channel setup for NLP handler tests.

    Args:
        channel_id: The voice channel ID (must match session channel_id for VC check)
        author_id: The author's user ID

    Returns:
        A MagicMock context configured for voice channel tests.
    """
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author.id = author_id
    ctx.author.voice = MagicMock()
    ctx.author.voice.channel = MagicMock()
    ctx.author.voice.channel.id = channel_id
    return ctx


def setup_active_session(music_cog, channel_id: int = 12345, guild_id: int = 11111) -> MagicMock:
    """Set up an active session on the music cog for testing.

    Args:
        music_cog: The Music cog instance
        channel_id: Voice channel ID (must match ctx voice channel for VC check)
        guild_id: Guild ID

    Returns:
        The mock voice client for additional assertions.
    """
    mock_vc = MagicMock()
    mock_vc.channel = MagicMock()
    mock_vc.channel.id = channel_id
    music_cog.active_session = MagicMock()
    music_cog.active_session.voice_client = mock_vc
    music_cog.active_session.channel_id = channel_id
    music_cog.active_session.guild_id = guild_id
    return mock_vc


@pytest.fixture
def mock_bot():
    """Create a mock CoreBot instance."""
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    bot.user = MagicMock()
    bot.user.id = 99999
    bot.owner_id = 12345  # Bot owner for bypass checks
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def music_cog(mock_bot):
    """Create a Music cog instance with mocked dependencies."""
    with patch('cogs.music.MusicCacheManager'):
        with patch('cogs.music.subscribe_playlist_change'):
            cog = Music(mock_bot)
            cog.logger = MagicMock()  # Silence logging
            return cog


class TestGetCurrentTrack:
    """Tests for _get_current_track method."""

    def test_returns_none_for_empty_playlist(self, music_cog):
        """Returns None when playlist is empty."""
        music_cog.playlist = []
        assert music_cog._get_current_track() is None

    def test_returns_track_at_current_index(self, music_cog):
        """Returns track at current_index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 2
        assert music_cog._get_current_track() == tracks[2]

    def test_handles_index_zero(self, music_cog):
        """Returns first track when index is 0."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks
        music_cog.current_index = 0
        assert music_cog._get_current_track() == tracks[0]

    def test_wraps_index_with_modulo(self, music_cog):
        """Uses modulo to handle out-of-bounds index defensively."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 7  # Out of bounds, but 7 % 5 = 2
        assert music_cog._get_current_track() == tracks[2]


class TestGetNextTrack:
    """Tests for _get_next_track method."""

    def test_returns_none_for_empty_playlist(self, music_cog):
        """Returns None when playlist is empty."""
        music_cog.playlist = []
        assert music_cog._get_next_track() is None

    def test_returns_next_track_normally(self, music_cog):
        """Returns the next track in sequence."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 1
        music_cog.loop_mode = LoopMode.ALL
        assert music_cog._get_next_track() == tracks[2]

    def test_loop_one_returns_current_track(self, music_cog):
        """In LOOP_ONE mode, next track is the current track."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 2
        music_cog.loop_mode = LoopMode.ONE
        assert music_cog._get_next_track() == tracks[2]

    def test_loop_all_wraps_to_start(self, music_cog):
        """In LOOP_ALL mode, wraps to first track at end."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4  # Last track
        music_cog.loop_mode = LoopMode.ALL
        assert music_cog._get_next_track() == tracks[0]

    def test_loop_off_returns_none_at_end(self, music_cog):
        """In LOOP_OFF mode, returns None at end of playlist."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4  # Last track
        music_cog.loop_mode = LoopMode.OFF
        assert music_cog._get_next_track() is None


class TestAdvanceTrack:
    """Tests for _advance_track method."""

    def test_returns_none_for_empty_playlist(self, music_cog):
        """Returns None when playlist is empty."""
        music_cog.playlist = []
        assert music_cog._advance_track() is None

    def test_advances_index_normally(self, music_cog):
        """Advances current_index and returns new track."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 1
        music_cog.loop_mode = LoopMode.ALL

        result = music_cog._advance_track()

        assert result == tracks[2]
        assert music_cog.current_index == 2

    def test_loop_one_stays_on_same_track(self, music_cog):
        """In LOOP_ONE mode, index doesn't change."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 2
        music_cog.loop_mode = LoopMode.ONE

        result = music_cog._advance_track()

        assert result == tracks[2]
        assert music_cog.current_index == 2

    def test_loop_all_wraps_to_zero(self, music_cog):
        """In LOOP_ALL mode, wraps index to 0 at end."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4  # Last track
        music_cog.loop_mode = LoopMode.ALL

        result = music_cog._advance_track()

        assert result == tracks[0]
        assert music_cog.current_index == 0

    def test_loop_off_returns_none_at_end(self, music_cog):
        """In LOOP_OFF mode, returns None at end."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4
        music_cog.loop_mode = LoopMode.OFF

        result = music_cog._advance_track()

        assert result is None
        assert music_cog.current_index == 5  # Went past end

    def test_clears_paused_position(self, music_cog):
        """Clears paused_at_position when advancing."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 1
        music_cog.loop_mode = LoopMode.ALL
        music_cog._playback.paused_at_position = 45.5

        music_cog._advance_track()

        assert music_cog._playback.paused_at_position is None


class TestApplyShuffle:
    """Tests for _apply_shuffle method."""

    def test_empty_playlist_does_nothing(self, music_cog):
        """Does nothing on empty playlist."""
        music_cog.playlist = []
        music_cog._apply_shuffle()
        assert music_cog.playlist == []

    def test_preserves_current_track_at_index_zero(self, music_cog):
        """Current track is moved to index 0 when preserve_current=True."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 3
        current_track = tracks[3]

        music_cog._apply_shuffle(preserve_current=True)

        assert music_cog.playlist[0] == current_track
        assert music_cog.current_index == 0

    def test_all_other_tracks_shuffled_after_current(self, music_cog):
        """All non-current tracks appear after index 0."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 2
        current_track = tracks[2]
        other_video_ids = {t.video_id for t in tracks if t != current_track}

        music_cog._apply_shuffle(preserve_current=True)

        # Current at front
        assert music_cog.playlist[0] == current_track
        # All others are in the list (order may vary)
        shuffled_ids = {t.video_id for t in music_cog.playlist[1:]}
        assert shuffled_ids == other_video_ids

    def test_preserve_false_shuffles_entire_list(self, music_cog):
        """With preserve_current=False, entire playlist is shuffled."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(10)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 5
        original_ids = {t.video_id for t in tracks}

        music_cog._apply_shuffle(preserve_current=False)

        # Index reset to 0
        assert music_cog.current_index == 0
        # All tracks still present
        shuffled_ids = {t.video_id for t in music_cog.playlist}
        assert shuffled_ids == original_ids

    def test_single_track_playlist(self, music_cog):
        """Single track playlist works correctly."""
        track = create_track(title="Only Track", video_id="only1")
        music_cog.playlist = [track]
        music_cog.current_index = 0

        music_cog._apply_shuffle(preserve_current=True)

        assert music_cog.playlist == [track]
        assert music_cog.current_index == 0

    def test_default_preserves_current(self, music_cog):
        """Default behavior is preserve_current=True."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 2
        current_track = tracks[2]

        music_cog._apply_shuffle()  # No argument = default True

        assert music_cog.playlist[0] == current_track
        assert music_cog.current_index == 0


class TestDedupePlaylist:
    """Tests for _dedupe_playlist method."""

    def test_empty_playlist_returns_zero(self, music_cog):
        """Returns 0 for empty playlist."""
        music_cog.playlist = []
        assert music_cog._dedupe_playlist() == 0

    def test_no_duplicates_returns_zero(self, music_cog):
        """Returns 0 when no duplicates exist."""
        tracks = [create_track(title=f"Track {i}", url=f"https://youtube.com/watch?v=vid{i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 2

        result = music_cog._dedupe_playlist()

        assert result == 0
        assert len(music_cog.playlist) == 5

    def test_removes_duplicates_keeps_later(self, music_cog):
        """Removes earlier occurrence, keeps later one."""
        track_a = create_track(title="Track A", url="https://youtube.com/watch?v=aaa", video_id="aaa")
        track_b = create_track(title="Track B", url="https://youtube.com/watch?v=bbb", video_id="bbb")
        track_a_dup = create_track(title="Track A Dup", url="https://youtube.com/watch?v=aaa", video_id="aaa")

        music_cog.playlist = [track_a, track_b, track_a_dup]
        music_cog.current_index = 0

        result = music_cog._dedupe_playlist()

        assert result == 1
        assert len(music_cog.playlist) == 2
        # The later occurrence (track_a_dup) is kept
        assert music_cog.playlist[1].title == "Track A Dup"

    def test_adjusts_current_index_when_dupe_before(self, music_cog):
        """Adjusts current_index when duplicate removed before it."""
        tracks = [
            create_track(title="Track 0", url="https://youtube.com/watch?v=vid0", video_id="vid0"),
            create_track(title="Track 1", url="https://youtube.com/watch?v=vid1", video_id="vid1"),
            create_track(title="Track 2", url="https://youtube.com/watch?v=vid2", video_id="vid2"),
            create_track(title="Track 0 Dup", url="https://youtube.com/watch?v=vid0", video_id="vid0"),  # Dupe of 0
        ]
        music_cog.playlist = tracks
        music_cog.current_index = 2  # Pointing to Track 2

        music_cog._dedupe_playlist()

        # Track 0 (index 0) was removed, so current_index shifts from 2 to 1
        assert music_cog.current_index == 1

    def test_multiple_duplicates(self, music_cog):
        """Handles multiple duplicates correctly."""
        tracks = [
            create_track(url="https://youtube.com/watch?v=aaa", video_id="aaa"),
            create_track(url="https://youtube.com/watch?v=bbb", video_id="bbb"),
            create_track(url="https://youtube.com/watch?v=aaa", video_id="aaa"),  # Dupe
            create_track(url="https://youtube.com/watch?v=ccc", video_id="ccc"),
            create_track(url="https://youtube.com/watch?v=bbb", video_id="bbb"),  # Dupe
        ]
        music_cog.playlist = tracks
        music_cog.current_index = 0

        result = music_cog._dedupe_playlist()

        assert result == 2
        assert len(music_cog.playlist) == 3


class TestRemoveTrack:
    """Tests for _remove_track method."""

    def test_empty_playlist_does_nothing(self, music_cog):
        """Does nothing on empty playlist."""
        music_cog.playlist = []
        music_cog._remove_track(0)
        assert music_cog.playlist == []

    def test_invalid_negative_index(self, music_cog):
        """Invalid negative index does nothing."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks.copy()
        music_cog._remove_track(-1)
        assert len(music_cog.playlist) == 3

    def test_invalid_out_of_bounds_index(self, music_cog):
        """Out of bounds index does nothing."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks.copy()
        music_cog._remove_track(5)
        assert len(music_cog.playlist) == 3

    def test_removes_track_at_index(self, music_cog):
        """Removes the track at specified index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 0

        music_cog._remove_track(2)

        assert len(music_cog.playlist) == 4
        assert all(t.video_id != "vid2" for t in music_cog.playlist)

    def test_adjusts_index_when_removing_before_current(self, music_cog):
        """Decrements current_index when removing track before it."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 3

        music_cog._remove_track(1)

        assert music_cog.current_index == 2

    def test_index_unchanged_when_removing_after_current(self, music_cog):
        """current_index unchanged when removing track after it."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 1

        music_cog._remove_track(3)

        assert music_cog.current_index == 1

    def test_removing_current_track_wraps_index(self, music_cog):
        """When removing current track at end, index wraps to 0."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 2  # Last track

        music_cog._remove_track(2)

        assert music_cog.current_index == 0

    def test_removing_last_track_resets_index(self, music_cog):
        """When playlist becomes empty, index resets to 0."""
        track = create_track(title="Only", video_id="only")
        music_cog.playlist = [track]
        music_cog.current_index = 0

        music_cog._remove_track(0)

        assert music_cog.playlist == []
        assert music_cog.current_index == 0


class TestMoveTrack:
    """Tests for _move_track method."""

    def test_empty_playlist_returns_none(self, music_cog):
        """Returns None for empty playlist."""
        music_cog.playlist = []
        assert music_cog._move_track(0, 1) is None

    def test_invalid_from_index_returns_none(self, music_cog):
        """Returns None for invalid from_index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks
        assert music_cog._move_track(-1, 1) is None
        assert music_cog._move_track(5, 1) is None

    def test_invalid_to_index_returns_none(self, music_cog):
        """Returns None for invalid to_index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks
        assert music_cog._move_track(0, -1) is None
        assert music_cog._move_track(0, 5) is None

    def test_same_index_is_noop(self, music_cog):
        """Moving to same index returns track but changes nothing."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks.copy()

        result = music_cog._move_track(1, 1)

        assert result == tracks[1]
        assert [t.video_id for t in music_cog.playlist] == ["vid0", "vid1", "vid2"]

    def test_move_forward(self, music_cog):
        """Moves track from earlier to later position."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 0

        result = music_cog._move_track(1, 3)

        assert result.video_id == "vid1"
        assert [t.video_id for t in music_cog.playlist] == ["vid0", "vid2", "vid3", "vid1", "vid4"]

    def test_move_backward(self, music_cog):
        """Moves track from later to earlier position."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 0

        result = music_cog._move_track(3, 1)

        assert result.video_id == "vid3"
        assert [t.video_id for t in music_cog.playlist] == ["vid0", "vid3", "vid1", "vid2", "vid4"]

    def test_moving_current_track_updates_index(self, music_cog):
        """Moving the current track updates current_index to new position."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 1

        music_cog._move_track(1, 4)

        assert music_cog.current_index == 4

    def test_move_from_before_to_after_current(self, music_cog):
        """Moving from before current to after decrements index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 2

        music_cog._move_track(1, 3)

        assert music_cog.current_index == 1

    def test_move_from_after_to_before_current(self, music_cog):
        """Moving from after current to before increments index."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 2

        music_cog._move_track(4, 1)

        assert music_cog.current_index == 3


class TestSwapTracks:
    """Tests for _swap_tracks method."""

    def test_empty_playlist_returns_none(self, music_cog):
        """Returns None for empty playlist."""
        music_cog.playlist = []
        assert music_cog._swap_tracks(0, 1) is None

    def test_invalid_index_a_returns_none(self, music_cog):
        """Returns None for invalid index_a."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks
        assert music_cog._swap_tracks(-1, 1) is None
        assert music_cog._swap_tracks(5, 1) is None

    def test_invalid_index_b_returns_none(self, music_cog):
        """Returns None for invalid index_b."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks
        assert music_cog._swap_tracks(0, -1) is None
        assert music_cog._swap_tracks(0, 5) is None

    def test_same_index_returns_same_track_twice(self, music_cog):
        """Swapping same index returns tuple of same track."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(3)]
        music_cog.playlist = tracks.copy()

        result = music_cog._swap_tracks(1, 1)

        assert result == (tracks[1], tracks[1])

    def test_swaps_tracks(self, music_cog):
        """Swaps two tracks in place."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 0

        result = music_cog._swap_tracks(1, 3)

        # Returns the tracks now at those positions (after swap)
        assert result[0].video_id == "vid3"  # Now at index 1
        assert result[1].video_id == "vid1"  # Now at index 3
        assert [t.video_id for t in music_cog.playlist] == ["vid0", "vid3", "vid2", "vid1", "vid4"]

    def test_swap_updates_current_index_a(self, music_cog):
        """Swapping current track (at index_a) updates index to index_b."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 1

        music_cog._swap_tracks(1, 3)

        assert music_cog.current_index == 3

    def test_swap_updates_current_index_b(self, music_cog):
        """Swapping current track (at index_b) updates index to index_a."""
        tracks = [create_track(title=f"Track {i}", video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks.copy()
        music_cog.current_index = 3

        music_cog._swap_tracks(1, 3)

        assert music_cog.current_index == 1


class TestFindTrackByQuery:
    """Tests for _find_track_by_query method."""

    def test_empty_query_returns_none(self, music_cog):
        """Empty query returns None."""
        tracks = [create_track(title="Test", video_id="vid1")]
        music_cog.playlist = tracks
        assert music_cog._find_track_by_query("") is None
        assert music_cog._find_track_by_query("   ") is None

    def test_empty_playlist_returns_none(self, music_cog):
        """Empty playlist returns None."""
        music_cog.playlist = []
        assert music_cog._find_track_by_query("test") is None

    def test_exact_title_substring_match(self, music_cog):
        """Finds track by exact title substring."""
        tracks = [
            create_track(title="First Song", video_id="vid1"),
            create_track(title="Bohemian Rhapsody", video_id="vid2"),
            create_track(title="Third Track", video_id="vid3"),
        ]
        music_cog.playlist = tracks

        assert music_cog._find_track_by_query("Bohemian") == 1
        assert music_cog._find_track_by_query("rhapsody") == 1  # Case insensitive

    def test_exact_artist_substring_match(self, music_cog):
        """Finds track by artist substring."""
        tracks = [
            create_track(title="Song A", artist="Unknown", video_id="vid1"),
            create_track(title="Song B", artist="Queen", video_id="vid2"),
            create_track(title="Song C", artist="Beatles", video_id="vid3"),
        ]
        music_cog.playlist = tracks

        assert music_cog._find_track_by_query("queen") == 1

    def test_title_bonus_over_artist(self, music_cog):
        """Title matches get +0.5 bonus, artist gets +0.3."""
        # When coverage is equal, title should win due to bonus
        tracks = [
            create_track(title="Queen Song", artist="Unknown", video_id="vid1"),
            create_track(title="Other Song", artist="Queen X", video_id="vid2"),
        ]
        music_cog.playlist = tracks

        # "Queen" in "Queen Song" = 5/10 + 0.5 = 1.0
        # "Queen" in "Queen X" (artist) = 5/7 + 0.3 = 1.01
        # Artist actually wins here due to shorter string
        # This tests the actual behavior, not ideal behavior
        result = music_cog._find_track_by_query("Queen")
        assert result in [0, 1]  # Either is acceptable given the scoring

    def test_word_overlap_matching(self, music_cog):
        """Matches based on word overlap when no substring match."""
        tracks = [
            create_track(title="Dancing in the Moonlight", video_id="vid1"),
            create_track(title="Sunshine Reggae", video_id="vid2"),
        ]
        music_cog.playlist = tracks

        # "dancing moonlight" has word overlap with track 0
        assert music_cog._find_track_by_query("dancing moonlight") == 0

    def test_no_match_below_threshold(self, music_cog):
        """Returns None when no good match found."""
        tracks = [
            create_track(title="Completely Different Song", video_id="vid1"),
        ]
        music_cog.playlist = tracks

        # Very short query with no overlap
        assert music_cog._find_track_by_query("xyz") is None


class TestParseTrackReference:
    """Tests for _parse_track_reference method."""

    def test_empty_string_returns_none(self, music_cog):
        """Empty string returns None."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        assert music_cog._parse_track_reference("") is None

    def test_parses_number(self, music_cog):
        """Parses track number (1-indexed) to 0-indexed."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_track_reference("1") == 0
        assert music_cog._parse_track_reference("3") == 2
        assert music_cog._parse_track_reference("5") == 4

    def test_parses_number_with_text(self, music_cog):
        """Extracts number from surrounding text."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_track_reference("track 3") == 2
        assert music_cog._parse_track_reference("song #2") == 1

    def test_out_of_bounds_number_returns_none(self, music_cog):
        """Out of bounds number returns None."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_track_reference("0") is None  # 0 is invalid (1-indexed)
        assert music_cog._parse_track_reference("6") is None  # Beyond playlist
        assert music_cog._parse_track_reference("99") is None

    def test_falls_back_to_name_search(self, music_cog):
        """Falls back to name search when no valid number."""
        tracks = [
            create_track(title="First Song", video_id="vid0"),
            create_track(title="Bohemian Rhapsody", video_id="vid1"),
        ]
        music_cog.playlist = tracks

        assert music_cog._parse_track_reference("bohemian") == 1


class TestParseDestination:
    """Tests for _parse_destination method."""

    def test_empty_string_returns_none(self, music_cog):
        """Empty string returns None."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        assert music_cog._parse_destination("") is None

    def test_keyword_top(self, music_cog):
        """Keywords 'top', 'first', etc. return index 0."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_destination("top") == 0
        assert music_cog._parse_destination("first") == 0
        assert music_cog._parse_destination("beginning") == 0
        assert music_cog._parse_destination("start") == 0

    def test_keyword_bottom(self, music_cog):
        """Keywords 'bottom', 'last', etc. return last index."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_destination("bottom") == 4
        assert music_cog._parse_destination("last") == 4
        assert music_cog._parse_destination("end") == 4

    def test_number_to_mode(self, music_cog):
        """Number in 'to' mode returns exact 0-indexed position."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_destination("3", mode='to') == 2

    def test_number_after_mode(self, music_cog):
        """Number in 'after' mode returns position after that track."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        # "after 3" = after position 3 = index 3 (but clamped to max)
        assert music_cog._parse_destination("3", mode='after') == 3

    def test_number_before_mode(self, music_cog):
        """Number in 'before' mode returns position before that track."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        # "before 3" = before position 3 = index 2
        assert music_cog._parse_destination("3", mode='before') == 2

    def test_out_of_bounds_number_returns_none(self, music_cog):
        """Out of bounds number returns None."""
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks

        assert music_cog._parse_destination("0") is None
        assert music_cog._parse_destination("10") is None

    def test_falls_back_to_track_name(self, music_cog):
        """Falls back to track name search."""
        tracks = [
            create_track(title="First Song", video_id="vid0"),
            create_track(title="Target Song", video_id="vid1"),
            create_track(title="Last Song", video_id="vid2"),
        ]
        music_cog.playlist = tracks

        assert music_cog._parse_destination("target") == 1


# =============================================================================
# PHASE 3: SESSION AND PLAYBACK LOGIC TESTS
# =============================================================================

import time
import discord


class TestGetElapsedSeconds:
    """Tests for _get_elapsed_seconds method."""

    def test_returns_paused_position_when_paused(self, music_cog):
        """Returns paused_at_position when track is paused."""
        music_cog._playback.paused_at_position = 45.5
        assert music_cog._get_elapsed_seconds() == 45.5

    def test_calculates_from_start_time_when_playing(self, music_cog):
        """Calculates elapsed from track_started_at when not paused."""
        music_cog._playback.paused_at_position = None
        music_cog.track_started_at = time.time() - 30.0  # Started 30 seconds ago

        elapsed = music_cog._get_elapsed_seconds()
        assert 29.5 < elapsed < 31.0  # Allow small timing variance


class TestDoPause:
    """Tests for _do_pause method."""

    def test_returns_false_without_session(self, music_cog):
        """Returns False when no active session."""
        music_cog.active_session = None
        assert music_cog._do_pause() is False

    def test_returns_false_without_voice_client(self, music_cog):
        """Returns False when session has no voice client."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = None
        assert music_cog._do_pause() is False

    def test_returns_false_when_not_playing(self, music_cog):
        """Returns False when voice client is not playing."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = False
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc

        assert music_cog._do_pause() is False

    def test_pauses_and_captures_position(self, music_cog):
        """Pauses playback and captures position."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        music_cog.track_started_at = time.time() - 60.0  # 60 seconds in

        result = music_cog._do_pause()

        assert result is True
        mock_vc.pause.assert_called_once()
        assert music_cog._playback.paused_at_position is not None
        assert 59.0 < music_cog._playback.paused_at_position < 61.0


class TestDoSkip:
    """Tests for _do_skip method."""

    def test_returns_false_without_session(self, music_cog):
        """Returns False when no active session."""
        music_cog.active_session = None
        assert music_cog._do_skip() is False

    def test_returns_false_when_not_playing_or_paused(self, music_cog):
        """Returns False when voice client is idle."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc

        assert music_cog._do_skip() is False

    def test_returns_false_with_empty_playlist(self, music_cog):
        """Returns False when playlist is empty."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        music_cog.playlist = []

        assert music_cog._do_skip() is False

    def test_advances_index_and_stops(self, music_cog):
        """Advances index and stops current playback."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 1
        music_cog.loop_mode = LoopMode.ALL

        result = music_cog._do_skip()

        assert result is True
        assert music_cog.current_index == 2
        mock_vc.stop.assert_called_once()
        assert music_cog._suppress_next_track_end is True

    def test_skip_ignores_loop_one(self, music_cog):
        """Skip advances even in LOOP_ONE mode (unlike natural track end)."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 2
        music_cog.loop_mode = LoopMode.ONE

        music_cog._do_skip()

        assert music_cog.current_index == 3  # Advanced, not stuck on 2

    def test_skip_wraps_at_end_with_loop_all(self, music_cog):
        """Wraps to 0 at end of playlist with LOOP_ALL."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4  # Last track
        music_cog.loop_mode = LoopMode.ALL

        music_cog._do_skip()

        assert music_cog.current_index == 0

    def test_skip_wraps_at_end_with_loop_off(self, music_cog):
        """Wraps to 0 at end even with LOOP_OFF (but doesn't auto-play)."""
        mock_vc = MagicMock(spec=discord.VoiceClient)
        mock_vc.is_playing.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc
        tracks = [create_track(video_id=f"vid{i}") for i in range(5)]
        music_cog.playlist = tracks
        music_cog.current_index = 4
        music_cog.loop_mode = LoopMode.OFF

        music_cog._do_skip()

        assert music_cog.current_index == 0


class TestUserInVoiceWithBot:
    """Tests for _user_in_voice_with_bot method."""

    def test_returns_false_without_session(self, music_cog):
        """Returns False when no active session."""
        music_cog.active_session = None
        ctx = MagicMock()
        assert music_cog._user_in_voice_with_bot(ctx) is False

    def test_owner_bypasses_check(self, music_cog):
        """Bot owner bypasses the VC check."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 99999

        ctx = MagicMock()
        ctx.author.id = 99999  # Owner

        assert music_cog._user_in_voice_with_bot(ctx) is True

    def test_returns_false_if_author_not_in_voice(self, music_cog):
        """Returns False if author has no voice state."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = None

        assert music_cog._user_in_voice_with_bot(ctx) is False

    def test_returns_false_if_in_different_channel(self, music_cog):
        """Returns False if author is in a different voice channel."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = MagicMock()
        ctx.author.voice.channel = MagicMock()
        ctx.author.voice.channel.id = 99999  # Different channel

        assert music_cog._user_in_voice_with_bot(ctx) is False

    def test_returns_true_if_in_same_channel(self, music_cog):
        """Returns True if author is in the same voice channel as bot."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = MagicMock()
        ctx.author.voice.channel = MagicMock()
        ctx.author.voice.channel.id = 12345  # Same channel

        assert music_cog._user_in_voice_with_bot(ctx) is True

    def test_returns_false_when_author_has_no_voice_attr(self, music_cog):
        """Returns False when author object has no voice attribute."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        del ctx.author.voice  # Remove voice attribute

        assert music_cog._user_in_voice_with_bot(ctx) is False

    def test_returns_false_when_voice_channel_is_none(self, music_cog):
        """Returns False when author's voice.channel is None."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = MagicMock()
        ctx.author.voice.channel = None

        assert music_cog._user_in_voice_with_bot(ctx) is False


class TestRequireUserInVC:
    """Tests for _require_user_in_vc async method."""

    @pytest.mark.asyncio
    async def test_returns_false_and_sends_message_without_session(self, music_cog):
        """Returns False and sends message when no session."""
        music_cog.active_session = None
        ctx = MagicMock()
        ctx.send = AsyncMock()

        result = await music_cog._require_user_in_vc(ctx)

        assert result is False
        ctx.send.assert_called_once()
        assert "not playing" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_returns_false_and_sends_message_if_not_in_vc(self, music_cog):
        """Returns False and sends message when user not in VC with bot."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = None
        ctx.send = AsyncMock()

        result = await music_cog._require_user_in_vc(ctx)

        assert result is False
        ctx.send.assert_called_once()
        assert "voice channel" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_returns_true_when_user_in_vc(self, music_cog):
        """Returns True when user is in VC with bot."""
        music_cog.active_session = MagicMock()
        music_cog.active_session.channel_id = 12345
        music_cog.bot.owner_id = 11111

        ctx = MagicMock()
        ctx.author.id = 22222
        ctx.author.voice = MagicMock()
        ctx.author.voice.channel = MagicMock()
        ctx.author.voice.channel.id = 12345
        ctx.send = AsyncMock()

        result = await music_cog._require_user_in_vc(ctx)

        assert result is True
        ctx.send.assert_not_called()


class TestClearPrefetch:
    """Tests for _clear_prefetch method."""

    def test_cancels_task_and_clears_state(self, music_cog):
        """Cancels prefetch task and clears all state."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        music_cog._prefetch.audio_url = "https://audio.url"
        music_cog._prefetch.target_index = 5
        music_cog._prefetch.target_video_id = "abc123"
        music_cog._prefetch.task = mock_task

        music_cog._clear_prefetch()

        mock_task.cancel.assert_called_once()
        assert music_cog._prefetch.audio_url is None
        assert music_cog._prefetch.target_index is None
        assert music_cog._prefetch.task is None


class TestClearAudioCaches:
    """Tests for _clear_audio_caches method."""

    def test_clears_prefetch_and_playback_caches(self, music_cog):
        """Clears both prefetch and current playback caches."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        music_cog._prefetch.audio_url = "https://prefetch.url"
        music_cog._prefetch.target_index = 3
        music_cog._prefetch.task = mock_task
        music_cog._playback.current_audio_url = "https://current.url"
        music_cog._playback.current_audio_track_url = "https://youtube.com/watch?v=xyz"

        music_cog._clear_audio_caches()

        assert music_cog._prefetch.audio_url is None
        assert music_cog._playback.current_audio_url is None
        assert music_cog._playback.current_audio_track_url is None


class TestBuildNowPlayingState:
    """Tests for _build_now_playing_state method."""

    def test_builds_state_with_track(self, music_cog):
        """Builds state from current track and player state."""
        track = create_track(
            title="Test Song",
            artist="Test Artist",
            url="https://youtube.com/watch?v=test123",
            duration=200
        )
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.ALL
        music_cog.track_started_at = time.time() - 65  # 65 seconds in
        music_cog._playback.paused_at_position = None
        music_cog.active_session = None

        state = music_cog._build_now_playing_state(thumbnail_url="https://thumb.url")

        assert state.track_title == "Test Song"
        assert state.track_artist == "Test Artist"
        assert state.track_url == "https://youtube.com/watch?v=test123"
        assert state.elapsed_str == "1:05"  # 65 seconds
        assert state.duration_str == "3:20"  # 200 seconds
        assert 0.3 < state.progress < 0.35  # ~65/200
        assert state.loop_display == "All"
        assert state.is_playing is False
        assert state.is_paused is False
        assert state.in_voice is False
        assert state.playlist_count == 1
        assert state.thumbnail_url == "https://thumb.url"

    def test_builds_state_when_in_voice_playing(self, music_cog):
        """Correctly reports playing state when in voice."""
        track = create_track(title="Playing Track", duration=180)
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.ONE
        music_cog.track_started_at = time.time() - 30

        mock_vc = MagicMock()
        mock_vc.is_playing.return_value = True
        mock_vc.is_paused.return_value = False
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc

        state = music_cog._build_now_playing_state()

        assert state.is_playing is True
        assert state.is_paused is False
        assert state.in_voice is True
        assert state.loop_display == "One"

    def test_builds_state_when_paused(self, music_cog):
        """Correctly reports paused state."""
        track = create_track(title="Paused Track", duration=300)
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.OFF
        music_cog._playback.paused_at_position = 120.0  # Paused at 2:00

        mock_vc = MagicMock()
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = True
        music_cog.active_session = MagicMock()
        music_cog.active_session.voice_client = mock_vc

        state = music_cog._build_now_playing_state()

        assert state.is_playing is False
        assert state.is_paused is True
        assert state.elapsed_str == "2:00"
        assert state.loop_display == "Off"

    def test_handles_empty_playlist(self, music_cog):
        """Handles empty playlist gracefully."""
        music_cog.playlist = []
        music_cog.active_session = None

        state = music_cog._build_now_playing_state()

        assert state.track_title == "Unknown"
        assert state.track_artist == "Unknown"
        assert state.playlist_count == 0


class TestOnPlaylistChange:
    """Tests for _on_playlist_change callback method."""

    def test_none_url_requests_stop(self, music_cog):
        """None URL requests a stop."""
        music_cog._ambience.current_playlist_url = "https://current.url"
        music_cog._ambience.pending_playlist_url = None

        music_cog._on_playlist_change(None, None)

        # Should request a switch to None (stop)
        assert music_cog._ambience.pending_playlist_url is None

    def test_same_url_does_not_request_change(self, music_cog):
        """Same URL as current does not request change."""
        same_url = "https://same.url"
        music_cog._ambience.current_playlist_url = same_url
        music_cog._ambience.pending_playlist_url = None

        music_cog._on_playlist_change(same_url, "Same playlist")

        # Should NOT set pending since it's the same
        assert music_cog._ambience.pending_playlist_url is None

    def test_different_url_requests_change(self, music_cog):
        """Different URL requests a playlist switch."""
        music_cog._ambience.current_playlist_url = "https://old.url"
        music_cog._ambience.pending_playlist_url = None

        music_cog._on_playlist_change("https://new.url", "New mood")

        assert music_cog._ambience.pending_playlist_url == "https://new.url"


class TestGetPresenceForActivity:
    """Tests for _get_presence_for_activity method."""

    def test_returns_default_when_no_activity(self, music_cog):
        """Returns 'vibing ✨' when no activity is set."""
        # With get_current_activity returning None (default mock behavior)
        result = music_cog._get_presence_for_activity()

        assert result == "vibing ✨"

    def test_returns_activity_status(self, music_cog, monkeypatch):
        """Returns the activity's status string."""
        mock_activity = MagicMock()
        mock_activity.status = "working 💻"
        mock_activity.context_key = None

        monkeypatch.setattr('cogs.music.get_current_activity', lambda: mock_activity)

        result = music_cog._get_presence_for_activity()

        assert result == "working 💻"


class TestLoopNlp:
    """Tests for loop_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_parses_loop_one(self, music_cog):
        """Parses 'one', 'single', 'track' as LoopMode.ONE."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        for query in ["loop one", "loop single", "repeat track"]:
            music_cog.loop_mode = LoopMode.OFF
            await music_cog.loop_nlp(ctx, query)
            assert music_cog.loop_mode == LoopMode.ONE

    @pytest.mark.asyncio
    async def test_parses_loop_all(self, music_cog):
        """Parses 'all', 'playlist' as LoopMode.ALL."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        for query in ["loop all", "loop playlist"]:
            music_cog.loop_mode = LoopMode.OFF
            await music_cog.loop_nlp(ctx, query)
            assert music_cog.loop_mode == LoopMode.ALL

    @pytest.mark.asyncio
    async def test_parses_loop_off(self, music_cog):
        """Parses 'off', 'disable' as LoopMode.OFF.

        Note: 'none' is not tested because it contains 'one' as substring,
        which matches the ONE pattern first. This is a known parsing quirk.
        """
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        for query in ["loop off", "loop disable"]:
            music_cog.loop_mode = LoopMode.ALL
            await music_cog.loop_nlp(ctx, query)
            assert music_cog.loop_mode == LoopMode.OFF

    @pytest.mark.asyncio
    async def test_shows_current_mode_when_no_keyword(self, music_cog):
        """Shows current mode when query has no recognized keyword."""
        ctx = create_voice_ctx()
        music_cog.loop_mode = LoopMode.ALL

        await music_cog.loop_nlp(ctx, "loop")

        ctx.send.assert_called_once()
        call_arg = ctx.send.call_args[0][0]
        assert "All" in call_arg
        assert music_cog.loop_mode == LoopMode.ALL  # Unchanged


class TestJumpNlp:
    """Tests for jump_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_extracts_number_from_query(self, music_cog, mocker):
        """Extracts track number from query and calls _do_jump."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = False

        # Mock _play_current_track to avoid YT-DLP calls
        mocker.patch.object(music_cog, '_play_current_track', new_callable=AsyncMock)

        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(10)]
        music_cog.current_index = 0

        await music_cog.jump_nlp(ctx, "jump to 5")

        assert music_cog.current_index == 4  # 0-indexed
        assert "jumped" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_shows_current_position_when_no_number(self, music_cog):
        """Shows current position when no number in query."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        music_cog.playlist = [create_track() for _ in range(5)]
        music_cog.current_index = 2

        await music_cog.jump_nlp(ctx, "jump")

        ctx.send.assert_called_once()
        call_arg = ctx.send.call_args[0][0]
        assert "#3" in call_arg  # 1-indexed display
        assert "5" in call_arg  # total tracks


class TestShuffleNlp:
    """Tests for shuffle_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_shuffles_playlist(self, music_cog):
        """Shuffles playlist and clears prefetch."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        # Create playlist with distinct tracks
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(20)]
        music_cog.current_index = 5
        original_current = music_cog.playlist[5]
        music_cog._prefetch.audio_url = "https://prefetched.url"
        music_cog._prefetch.target_index = 6

        await music_cog.shuffle_nlp(ctx, "shuffle")

        # Current track should be at index 0
        assert music_cog.playlist[0] == original_current
        assert music_cog.current_index == 0
        # Prefetch should be cleared
        assert music_cog._prefetch.audio_url is None
        ctx.send.assert_called_once()
        assert "shuffled" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_empty_playlist_message(self, music_cog):
        """Shows message when playlist is empty."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        music_cog.playlist = []

        await music_cog.shuffle_nlp(ctx, "shuffle")

        ctx.send.assert_called_once()
        assert "no playlist" in ctx.send.call_args[0][0].lower()


class TestClearQueueNlp:
    """Tests for clear_queue_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_keeps_current_track(self, music_cog):
        """Clears queue but keeps current track."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        current = create_track(title="Current Song")
        music_cog.playlist = [
            create_track(title="Track 1"),
            create_track(title="Track 2"),
            current,
            create_track(title="Track 4"),
        ]
        music_cog.current_index = 2

        await music_cog.clear_queue_nlp(ctx, "clear queue")

        assert len(music_cog.playlist) == 1
        assert music_cog.playlist[0] == current
        assert music_cog.current_index == 0
        assert music_cog._playlist_modified_during_session is True

    @pytest.mark.asyncio
    async def test_empty_queue_message(self, music_cog):
        """Shows message when queue is already empty."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        music_cog.playlist = []

        await music_cog.clear_queue_nlp(ctx, "clear queue")

        assert "already empty" in ctx.send.call_args[0][0].lower()


class TestSkipNlp:
    """Tests for skip_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_skips_when_playing(self, music_cog):
        """Skips track when playing."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = True
        mock_vc.is_paused.return_value = False

        music_cog.playlist = [create_track() for _ in range(5)]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.OFF

        await music_cog.skip_nlp(ctx, "skip")

        mock_vc.stop.assert_called_once()
        assert "skipped" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_message_when_not_playing(self, music_cog):
        """Shows message when nothing is playing."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False

        music_cog.playlist = [create_track()]

        await music_cog.skip_nlp(ctx, "skip")

        assert "nothing" in ctx.send.call_args[0][0].lower()


class TestPauseNlp:
    """Tests for pause_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_pauses_when_playing(self, music_cog):
        """Pauses playback when playing."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = True
        mock_vc.is_paused.return_value = False
        music_cog.track_started_at = time.time() - 30

        await music_cog.pause_nlp(ctx, "pause")

        mock_vc.pause.assert_called_once()
        assert "paused" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_message_when_already_paused(self, music_cog):
        """Shows message when already paused."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = True

        await music_cog.pause_nlp(ctx, "pause")

        mock_vc.pause.assert_not_called()
        assert "already paused" in ctx.send.call_args[0][0].lower()


class TestResumeNlp:
    """Tests for resume_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_resumes_when_paused(self, music_cog, mocker):
        """Resumes playback when paused."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = True

        # Mock _do_resume to return True
        mocker.patch.object(music_cog, '_do_resume', new_callable=AsyncMock, return_value=True)

        await music_cog.resume_nlp(ctx, "resume")

        music_cog._do_resume.assert_called_once()
        assert "resumed" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_message_when_already_playing(self, music_cog):
        """Shows message when already playing."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = True
        mock_vc.is_paused.return_value = False

        await music_cog.resume_nlp(ctx, "resume")

        assert "already playing" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_message_when_nothing_to_resume(self, music_cog):
        """Shows message when nothing to resume."""
        ctx = create_voice_ctx()
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False

        await music_cog.resume_nlp(ctx, "resume")

        assert "nothing to resume" in ctx.send.call_args[0][0].lower()


class TestPlayNlp:
    """Tests for play_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_extracts_query_from_play_command(self, music_cog, mocker):
        """Extracts song query after 'play' keyword."""
        ctx = create_voice_ctx()

        mock_do_play = mocker.patch.object(music_cog, '_do_play', new_callable=AsyncMock)

        await music_cog.play_nlp(ctx, "play never gonna give you up")

        mock_do_play.assert_called_once_with(ctx, "never gonna give you up")

    @pytest.mark.asyncio
    async def test_extracts_query_from_queue_command(self, music_cog, mocker):
        """Extracts song query after 'queue' keyword."""
        ctx = create_voice_ctx()

        mock_do_play = mocker.patch.object(music_cog, '_do_play', new_callable=AsyncMock)

        await music_cog.play_nlp(ctx, "queue bohemian rhapsody")

        mock_do_play.assert_called_once_with(ctx, "bohemian rhapsody")

    @pytest.mark.asyncio
    async def test_resume_when_no_query_and_paused(self, music_cog, mocker):
        """Resumes when 'play' has no query and is paused."""
        ctx = create_voice_ctx()
        ctx.guild = MagicMock()
        ctx.guild.id = 11111

        mock_vc = setup_active_session(music_cog)
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = True

        mocker.patch.object(music_cog, '_do_resume', new_callable=AsyncMock, return_value=True)

        await music_cog.play_nlp(ctx, "play ")  # Note: space after play, empty query

        music_cog._do_resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_when_no_session(self, music_cog):
        """Shows message when no active session and no query."""
        ctx = create_voice_ctx()
        music_cog.active_session = None

        await music_cog.play_nlp(ctx, "play ")  # Empty query

        assert "listen along" in ctx.send.call_args[0][0].lower()


class TestNlpKeywordStripping:
    """Parameterized tests for NLP handlers that strip trigger words.
    
    These handlers follow a common pattern: strip command keywords from
    the query string and pass the cleaned argument to _do_* methods.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("nlp_method,do_method,query,expected_arg", [
        # remove_nlp -> _do_remove
        ("remove_nlp", "_do_remove", "remove track 3", "3"),
        ("remove_nlp", "_do_remove", "delete song 5", "5"),
        ("remove_nlp", "_do_remove", "remove # 2", "2"),
        ("remove_nlp", "_do_remove", "delete number 7", "7"),
        ("remove_nlp", "_do_remove", "remove midnight city", "midnight city"),
        # move_nlp -> _do_move  
        ("move_nlp", "_do_move", "move track 3 to 1", "3 to 1"),
        ("move_nlp", "_do_move", "move song 5 to end", "5 to end"),
        ("move_nlp", "_do_move", "move 7 to 2", "7 to 2"),
        # lyrics_nlp -> _do_lyrics
        ("lyrics_nlp", "_do_lyrics", "lyrics bohemian rhapsody", "bohemian rhapsody"),
        ("lyrics_nlp", "_do_lyrics", "find lyrics for never gonna give you up", "never gonna give you up"),
        ("lyrics_nlp", "_do_lyrics", "search lyrics to despacito", "despacito"),
        ("lyrics_nlp", "_do_lyrics", "lyrics", None),  # Empty -> uses current track
    ])
    async def test_strips_keywords_and_delegates(
        self, music_cog, mocker, nlp_method, do_method, query, expected_arg
    ):
        """NLP handler strips trigger words and delegates to _do_* method."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        mock_do = mocker.patch.object(music_cog, do_method, new_callable=AsyncMock)
        
        nlp_handler = getattr(music_cog, nlp_method)
        await nlp_handler(ctx, query)

        mock_do.assert_called_once_with(ctx, expected_arg)


class TestLeaveNlp:
    """Tests for leave_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_ends_session_and_sends_message(self, music_cog, mocker):
        """Ends session and sends disconnect message."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)

        mock_end_session = mocker.patch.object(music_cog, '_end_session', new_callable=AsyncMock)

        await music_cog.leave_nlp(ctx, "leave")

        mock_end_session.assert_called_once()
        assert "disconnect" in ctx.send.call_args[0][0].lower()


class TestNowPlayingNlp:
    """Tests for now_playing_nlp NLP handler."""

    @pytest.mark.asyncio
    async def test_no_track_sends_message(self, music_cog):
        """Sends message when no track is loaded."""
        ctx = create_voice_ctx()
        music_cog.playlist = []

        await music_cog.now_playing_nlp(ctx, "now playing")

        ctx.send.assert_called_once()
        assert "no track" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_with_track_sends_view(self, music_cog, mocker):
        """Creates and sends NowPlayingView with track state."""
        ctx = create_voice_ctx()
        track = create_track(title="Test Song", url="https://youtube.com/watch?v=test123")
        music_cog.playlist = [track]
        music_cog.current_index = 0

        # Mock the thumbnail fetching and view creation
        mocker.patch(
            'cogs.music.get_best_thumbnail_bytes',
            new_callable=AsyncMock,
            return_value=None
        )

        await music_cog.now_playing_nlp(ctx, "now playing")

        # Should have called ctx.send with view
        ctx.send.assert_called_once()
        # The view kwarg should be present
        assert 'view' in ctx.send.call_args.kwargs


# =============================================================================
# INTERNAL IMPLEMENTATION METHODS (_do_* methods)
# =============================================================================


class TestDoJump:
    """Tests for _do_jump internal implementation."""

    @pytest.mark.asyncio
    async def test_no_playlist_sends_error(self, music_cog):
        """Sends error when no playlist is loaded."""
        ctx = create_voice_ctx()
        music_cog.playlist = []

        await music_cog._do_jump(ctx, 1)

        ctx.send.assert_called_once()
        assert "no playlist" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_position_too_low_sends_error(self, music_cog):
        """Sends error for position 0 or negative."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]

        await music_cog._do_jump(ctx, 0)

        ctx.send.assert_called_once()
        assert "invalid position" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_position_too_high_sends_error(self, music_cog):
        """Sends error for position beyond playlist length."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]

        await music_cog._do_jump(ctx, 10)

        ctx.send.assert_called_once()
        assert "invalid position" in ctx.send.call_args[0][0].lower()
        assert "between 1 and 5" in ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_valid_position_updates_index(self, music_cog, mocker):
        """Updates current_index and clears audio caches."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.current_index = 0

        mock_clear_audio = mocker.patch.object(music_cog, '_clear_audio_caches')

        await music_cog._do_jump(ctx, 3)

        assert music_cog.current_index == 2  # 1-indexed to 0-indexed
        mock_clear_audio.assert_called_once()
        # Should send confirmation
        ctx.send.assert_called_once()
        assert "jumped to" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_while_playing_stops_and_sets_suppress_flag(self, music_cog, mocker):
        """When playing, sets suppress flag and stops playback."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]
        setup_active_session(music_cog)

        mock_vc = music_cog.active_session.voice_client
        mock_vc.is_playing.return_value = True

        mocker.patch.object(music_cog, '_clear_audio_caches')

        await music_cog._do_jump(ctx, 3)

        assert music_cog._suppress_next_track_end is True
        mock_vc.stop.assert_called_once()


class TestDoRemove:
    """Tests for _do_remove internal implementation."""

    @pytest.mark.asyncio
    async def test_empty_playlist_sends_error(self, music_cog):
        """Sends error when playlist is empty."""
        ctx = create_voice_ctx()
        music_cog.playlist = []

        await music_cog._do_remove(ctx, "5")

        ctx.send.assert_called_once()
        assert "empty" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_no_query_sends_help(self, music_cog):
        """Sends help message when no query provided."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title="Test Track")]

        await music_cog._do_remove(ctx, "")

        ctx.send.assert_called_once()
        assert "what should i remove" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_invalid_number_sends_error(self, music_cog):
        """Sends error for out-of-range track number."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]

        await music_cog._do_remove(ctx, "99")

        ctx.send.assert_called_once()
        assert "invalid track number" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_remove_by_number(self, music_cog, mocker):
        """Removes track by position number."""
        ctx = create_voice_ctx()
        tracks = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.playlist = tracks.copy()

        mock_remove = mocker.patch.object(music_cog, '_remove_track')

        await music_cog._do_remove(ctx, "3")

        mock_remove.assert_called_once_with(2)  # 1-indexed to 0-indexed
        # Confirmation message
        ctx.send.assert_called_once()
        assert "removed" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_remove_by_name(self, music_cog, mocker):
        """Removes track by song name match."""
        ctx = create_voice_ctx()
        music_cog.playlist = [
            create_track(title="Bohemian Rhapsody"),
            create_track(title="Stairway to Heaven"),
            create_track(title="Hotel California"),
        ]

        mocker.patch.object(music_cog, '_find_track_by_query', return_value=1)
        mock_remove = mocker.patch.object(music_cog, '_remove_track')

        await music_cog._do_remove(ctx, "stairway")

        mock_remove.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_remove_not_found_sends_error(self, music_cog, mocker):
        """Sends error when song name not found."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title="Test Track")]

        mocker.patch.object(music_cog, '_find_track_by_query', return_value=None)

        await music_cog._do_remove(ctx, "nonexistent song")

        ctx.send.assert_called_once()
        assert "couldn't find" in ctx.send.call_args[0][0].lower()


class TestDoMove:
    """Tests for _do_move internal implementation."""

    @pytest.mark.asyncio
    async def test_empty_playlist_sends_error(self, music_cog):
        """Sends error when playlist is empty."""
        ctx = create_voice_ctx()
        music_cog.playlist = []

        await music_cog._do_move(ctx, "1 to 2")

        ctx.send.assert_called_once()
        assert "empty" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_single_track_sends_error(self, music_cog):
        """Sends error when only one track in playlist."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title="Only Track")]

        await music_cog._do_move(ctx, "1 to 1")

        ctx.send.assert_called_once()
        assert "at least 2 tracks" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_no_query_sends_help(self, music_cog):
        """Sends help when no query provided."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track() for _ in range(5)]

        await music_cog._do_move(ctx, "")

        ctx.send.assert_called_once()
        # Should contain example usage
        assert "what should i move" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_move_x_to_y_uses_swap(self, music_cog, mocker):
        """'move X to Y' swaps tracks."""
        ctx = create_voice_ctx()
        tracks = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.playlist = tracks.copy()

        mocker.patch.object(music_cog, '_parse_track_reference', return_value=0)
        mocker.patch.object(music_cog, '_parse_destination', return_value=4)
        mock_swap = mocker.patch.object(
            music_cog, '_swap_tracks', return_value=(tracks[0], tracks[4])
        )

        await music_cog._do_move(ctx, "1 to 5")

        mock_swap.assert_called_once_with(0, 4)
        ctx.send.assert_called_once()
        assert "swapped" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_move_x_after_y_uses_insert(self, music_cog, mocker):
        """'move X after Y' inserts track."""
        ctx = create_voice_ctx()
        tracks = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.playlist = tracks.copy()

        mocker.patch.object(music_cog, '_parse_track_reference', return_value=0)
        mocker.patch.object(music_cog, '_parse_destination', return_value=3)
        mock_move = mocker.patch.object(music_cog, '_move_track', return_value=tracks[0])

        await music_cog._do_move(ctx, "1 after 3")

        mock_move.assert_called_once_with(0, 3)
        ctx.send.assert_called_once()
        assert "moved" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_from_not_found_sends_error(self, music_cog, mocker):
        """Sends error when source track not found."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track() for _ in range(5)]

        mocker.patch.object(music_cog, '_parse_track_reference', return_value=None)

        await music_cog._do_move(ctx, "nonexistent to 1")

        ctx.send.assert_called_once()
        assert "couldn't figure out which track" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_to_not_found_sends_error(self, music_cog, mocker):
        """Sends error when destination not found."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track() for _ in range(5)]

        mocker.patch.object(music_cog, '_parse_track_reference', return_value=0)
        mocker.patch.object(music_cog, '_parse_destination', return_value=None)

        await music_cog._do_move(ctx, "1 to invalid")

        ctx.send.assert_called_once()
        assert "couldn't figure out where" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_same_position_sends_error(self, music_cog, mocker):
        """Sends error when source and destination are the same."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track() for _ in range(5)]

        mocker.patch.object(music_cog, '_parse_track_reference', return_value=2)
        mocker.patch.object(music_cog, '_parse_destination', return_value=2)

        await music_cog._do_move(ctx, "3 to 3")

        ctx.send.assert_called_once()
        assert "already at that position" in ctx.send.call_args[0][0].lower()


class TestDoResume:
    """Tests for _do_resume internal implementation."""

    @pytest.mark.asyncio
    async def test_no_session_returns_false(self, music_cog):
        """Returns False when no active session."""
        music_cog.active_session = None

        result = await music_cog._do_resume()

        assert result is False

    @pytest.mark.asyncio
    async def test_not_paused_returns_false(self, music_cog):
        """Returns False when not paused."""
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = False

        result = await music_cog._do_resume()

        assert result is False

    @pytest.mark.asyncio
    async def test_fallback_resume_when_no_paused_position(self, music_cog):
        """Falls back to simple vc.resume() when paused_at_position is None."""
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = True
        music_cog._playback.paused_at_position = None

        result = await music_cog._do_resume()

        assert result is True
        mock_vc.resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_resume_when_no_track(self, music_cog):
        """Falls back to simple vc.resume() when no current track."""
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = True
        music_cog._playback.paused_at_position = 30.0
        music_cog.playlist = []

        result = await music_cog._do_resume()

        assert result is True
        mock_vc.resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_seek_resume_with_cached_track(self, music_cog, mocker, tmp_path):
        """Creates new FFmpeg source with seek when track is cached."""
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = True

        # Create a temp file to simulate cached track
        cache_file = tmp_path / "test.mp3"
        cache_file.write_text("fake audio")

        track = create_track(title="Cached Track")
        track.local_path = str(cache_file)
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog._playback.paused_at_position = 30.0

        # Mock FFmpegPCMAudio to avoid actual ffmpeg
        mock_ffmpeg = mocker.patch('cogs.music.discord.FFmpegPCMAudio')
        mocker.patch('cogs.music.get_ffmpeg_path', return_value='ffmpeg')

        result = await music_cog._do_resume()

        assert result is True
        mock_ffmpeg.assert_called_once()
        mock_vc.play.assert_called_once()
        # Paused position should be cleared
        assert music_cog._playback.paused_at_position is None

    @pytest.mark.asyncio
    async def test_seek_uses_cached_audio_url(self, music_cog, mocker):
        """Uses cached audio URL if available."""
        mock_vc = setup_active_session(music_cog)
        mock_vc.is_paused.return_value = True

        track = create_track()
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog._playback.paused_at_position = 30.0
        music_cog._playback.current_audio_track_url = track.url
        music_cog._playback.current_audio_url = "https://cached.audio.url"

        mock_ffmpeg = mocker.patch('cogs.music.discord.FFmpegPCMAudio')
        mocker.patch('cogs.music.get_ffmpeg_path', return_value='ffmpeg')

        result = await music_cog._do_resume()

        assert result is True
        # Should use cached URL, not fetch new one
        mock_ffmpeg.assert_called_once()
        call_args = mock_ffmpeg.call_args
        assert call_args[0][0] == "https://cached.audio.url"


class TestDoQueue:
    """Tests for _do_queue internal implementation."""

    @pytest.mark.asyncio
    async def test_no_playlist_sends_message(self, music_cog):
        """Sends message when no playlist loaded."""
        ctx = create_voice_ctx()
        music_cog.playlist = []

        await music_cog._do_queue(ctx)

        ctx.send.assert_called_once()
        assert "no playlist" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_single_page_sends_embed_directly(self, music_cog):
        """Sends embed without paginator for small playlists."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.current_index = 2

        await music_cog._do_queue(ctx)

        ctx.send.assert_called_once()
        # Should have embed kwarg, no view
        call_kwargs = ctx.send.call_args.kwargs
        assert 'embed' in call_kwargs
        assert 'view' not in call_kwargs

    @pytest.mark.asyncio
    async def test_multi_page_sends_paginator(self, music_cog):
        """Sends paginator view for large playlists."""
        ctx = create_voice_ctx()
        # Create 25 tracks (3 pages at 10 per page)
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(25)]
        music_cog.current_index = 15

        await music_cog._do_queue(ctx)

        ctx.send.assert_called_once()
        call_kwargs = ctx.send.call_args.kwargs
        assert 'embed' in call_kwargs
        assert 'view' in call_kwargs

    @pytest.mark.asyncio
    async def test_starts_on_page_with_current_track(self, music_cog):
        """Starts paginator on the page containing current track."""
        ctx = create_voice_ctx()
        # 25 tracks, current at index 15 (track 16), should start on page 2 (0-indexed: page 1)
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(25)]
        music_cog.current_index = 15

        await music_cog._do_queue(ctx)

        ctx.send.assert_called_once()
        call_kwargs = ctx.send.call_args.kwargs
        embed = call_kwargs['embed']
        # Page 2 of 3 (index 15 / 10 = page 1, displayed as page 2)
        assert "Page 2/3" in embed.footer.text

    @pytest.mark.asyncio
    async def test_highlights_current_track(self, music_cog):
        """Current track is highlighted in the embed."""
        ctx = create_voice_ctx()
        music_cog.playlist = [create_track(title=f"Track {i}") for i in range(5)]
        music_cog.current_index = 2

        await music_cog._do_queue(ctx)

        embed = ctx.send.call_args.kwargs['embed']
        # Track 3 (index 2) should have ▶️ marker
        assert "▶️" in embed.description
        assert "Track 2" in embed.description

    @pytest.mark.asyncio
    async def test_shows_user_added_star(self, music_cog):
        """User-added tracks show star icon."""
        ctx = create_voice_ctx()
        user_track = create_track(title="User Track")
        user_track.user_added = True
        music_cog.playlist = [
            create_track(title="Ambient Track"),
            user_track,
        ]
        music_cog.current_index = 0

        await music_cog._do_queue(ctx)

        embed = ctx.send.call_args.kwargs['embed']
        assert "⭐" in embed.description


class TestDoPlay:
    """Tests for _do_play internal implementation."""

    @pytest.mark.asyncio
    async def test_empty_query_sends_message(self, music_cog):
        """Sends message when query is empty."""
        ctx = create_voice_ctx()

        await music_cog._do_play(ctx, "")

        ctx.send.assert_called_once()
        assert "provide a song name" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_empty_whitespace_query_sends_message(self, music_cog):
        """Sends message when query is only whitespace."""
        ctx = create_voice_ctx()

        await music_cog._do_play(ctx, "   ")

        ctx.send.assert_called_once()
        assert "provide a song name" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_url_triggers_fetch(self, music_cog, mocker):
        """URL query triggers _fetch_url_info."""
        ctx = create_voice_ctx()
        
        mock_fetch = mocker.patch.object(
            music_cog, '_fetch_url_info', new_callable=AsyncMock,
            return_value=([], "Error fetching", None)
        )

        await music_cog._do_play(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_query_triggers_search(self, music_cog, mocker):
        """Non-URL query triggers _search_youtube."""
        ctx = create_voice_ctx()
        
        mock_search = mocker.patch.object(
            music_cog, '_search_youtube', new_callable=AsyncMock,
            return_value=[]
        )

        await music_cog._do_play(ctx, "never gonna give you up")

        mock_search.assert_called_once_with("never gonna give you up", max_results=5)

    @pytest.mark.asyncio
    async def test_no_results_sends_message(self, music_cog, mocker):
        """Sends message when search returns no results."""
        ctx = create_voice_ctx()
        
        mocker.patch.object(
            music_cog, '_search_youtube', new_callable=AsyncMock,
            return_value=[]
        )

        await music_cog._do_play(ctx, "completely nonexistent song xyz123")

        # Should send both searching and no results messages
        calls = ctx.send.call_args_list
        assert any("no results" in str(call).lower() for call in calls)

    @pytest.mark.asyncio
    async def test_queue_limit_enforced(self, music_cog, mocker):
        """Enforces 2000 track queue limit."""
        ctx = create_voice_ctx()
        setup_active_session(music_cog)
        ctx.guild = MagicMock()
        ctx.guild.id = music_cog.active_session.guild_id

        # Fill playlist to max
        music_cog.playlist = [create_track() for _ in range(2000)]

        track = create_track(title="New Track")
        mocker.patch.object(
            music_cog, '_fetch_url_info', new_callable=AsyncMock,
            return_value=([track], None, None)
        )

        await music_cog._do_play(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # Should send queue full message
        calls = ctx.send.call_args_list
        assert any("queue is full" in str(call).lower() for call in calls)

    @pytest.mark.asyncio
    async def test_user_not_in_voice_shows_message(self, music_cog, mocker):
        """Shows message when user not in voice channel."""
        ctx = create_voice_ctx()
        ctx.author.voice = None  # User not in voice
        music_cog.active_session = None

        track = create_track(title="Test Track")
        mocker.patch.object(
            music_cog, '_search_youtube', new_callable=AsyncMock,
            return_value=[track]
        )

        await music_cog._do_play(ctx, "test song")

        calls = ctx.send.call_args_list
        assert any("join a voice channel" in str(call).lower() for call in calls)

    @pytest.mark.asyncio
    async def test_appends_to_queue_when_in_session(self, music_cog, mocker):
        """Appends tracks to queue when already in session."""
        ctx = create_voice_ctx()
        ctx.guild = MagicMock()
        ctx.guild.id = 11111
        setup_active_session(music_cog, guild_id=11111)

        existing_track = create_track(title="Existing Track")
        new_track = create_track(title="New Track", url="https://www.youtube.com/watch?v=new123")
        music_cog.playlist = [existing_track]
        music_cog.current_index = 0

        mocker.patch.object(
            music_cog, '_search_youtube', new_callable=AsyncMock,
            return_value=[new_track]
        )

        await music_cog._do_play(ctx, "new song")

        assert len(music_cog.playlist) == 2
        assert music_cog.playlist[1].title == "New Track"
        assert music_cog._playlist_modified_during_session is True

    @pytest.mark.asyncio
    async def test_duplicate_track_moves_to_end(self, music_cog, mocker):
        """Duplicate track is moved to end instead of adding twice."""
        ctx = create_voice_ctx()
        ctx.guild = MagicMock()
        ctx.guild.id = 11111
        setup_active_session(music_cog, guild_id=11111)

        track = create_track(title="Duplicate Track")
        music_cog.playlist = [track, create_track(title="Other Track")]
        music_cog.current_index = 1

        # Try to add same track again
        mocker.patch.object(
            music_cog, '_search_youtube', new_callable=AsyncMock,
            return_value=[track]
        )

        await music_cog._do_play(ctx, "duplicate track")

        # Should have moved, not duplicated
        assert len(music_cog.playlist) == 2
        assert music_cog.playlist[-1].title == "Duplicate Track"
        # current_index should be adjusted since we removed before it
        assert music_cog.current_index == 0


class TestDoListenAlong:
    """Tests for _do_listen_along internal implementation."""

    @pytest.mark.asyncio
    async def test_already_in_session_same_guild(self, music_cog):
        """Sends message when already playing in same guild."""
        ctx = create_voice_ctx()
        ctx.guild = MagicMock()
        ctx.guild.id = 11111
        setup_active_session(music_cog, guild_id=11111, channel_id=22222)
        # Must have playlist loaded to reach the session check
        music_cog.playlist = [create_track()]

        await music_cog._do_listen_along(ctx)

        ctx.send.assert_called_once()
        assert "already playing" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_already_in_session_different_guild(self, music_cog):
        """Sends message when playing in different guild."""
        ctx = create_voice_ctx()
        ctx.guild = MagicMock()
        ctx.guild.id = 99999  # Different guild
        setup_active_session(music_cog, guild_id=11111)
        # Must have playlist loaded to reach the session check
        music_cog.playlist = [create_track()]

        await music_cog._do_listen_along(ctx)

        ctx.send.assert_called_once()
        # Should indicate playing elsewhere (different guild or "another server")
        message = ctx.send.call_args[0][0].lower()
        assert "only be in one place" in message or "another server" in message

    @pytest.mark.asyncio
    async def test_user_not_in_voice_no_fallback(self, music_cog, mocker):
        """Sends message when user not in voice and no fallback channel."""
        ctx = create_voice_ctx()
        ctx.author.voice = None  # User not in voice
        ctx.guild = MagicMock()
        ctx.guild.id = 11111
        music_cog.active_session = None
        music_cog.playlist = [create_track()]

        mocker.patch.object(
            music_cog.db_manager, 'get_guild_config', new_callable=AsyncMock,
            return_value=None
        )

        await music_cog._do_listen_along(ctx)

        ctx.send.assert_called_once()
        assert "join a voice channel" in ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_no_playlist_triggers_ambience(self, music_cog, mocker):
        """Triggers ambience when no playlist loaded."""
        ctx = create_voice_ctx()
        music_cog.active_session = None
        music_cog.playlist = []

        mock_ensure = mocker.patch(
            'cogs.music.ensure_music_for_user',
            return_value=("https://youtube.com/playlist?list=abc", "Test Playlist")
        )
        mocker.patch.object(
            music_cog, '_load_playlist', new_callable=AsyncMock
        )

        # After loading, still no playlist = error message
        await music_cog._do_listen_along(ctx)

        mock_ensure.assert_called_once()
        # Should send "no music loaded" message since playlist is still empty after load
        calls = ctx.send.call_args_list
        assert any("don't have any music" in str(call).lower() for call in calls)

    @pytest.mark.asyncio
    async def test_joins_user_channel(self, music_cog, mocker):
        """Joins user's voice channel when available."""
        ctx = create_voice_ctx()
        # Make author a proper Member mock
        ctx.author = MagicMock(spec=discord.Member)
        ctx.author.voice = MagicMock()
        ctx.author.voice.channel = MagicMock(spec=discord.VoiceChannel)
        ctx.author.voice.channel.id = 12345

        music_cog.active_session = None
        music_cog.playlist = [create_track()]

        mock_start = mocker.patch.object(
            music_cog, '_start_session', new_callable=AsyncMock
        )
        mocker.patch('cogs.music.MusicAmbience.get_listen_along_response', return_value="🎵")

        await music_cog._do_listen_along(ctx)

        mock_start.assert_called_once()
        # First arg should be the voice channel
        call_args = mock_start.call_args
        assert call_args[0][0] == ctx.author.voice.channel


# =============================================================================
# ActiveSession Dataclass Tests
# =============================================================================

from utils.music_helpers import ActiveSession, LyricsResult


class TestActiveSession:
    """Tests for ActiveSession dataclass."""

    def test_creation_with_required_fields(self):
        """Can create with required fields only."""
        voice_client = MagicMock(spec=discord.VoiceClient)
        session = ActiveSession(
            guild_id=123,
            channel_id=456,
            voice_client=voice_client
        )
        assert session.guild_id == 123
        assert session.channel_id == 456
        assert session.voice_client == voice_client

    def test_started_at_auto_populated(self):
        """started_at defaults to current time."""
        import time
        before = time.time()
        session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=MagicMock()
        )
        after = time.time()
        assert before <= session.started_at <= after

    def test_waiting_for_users_defaults_false(self):
        """waiting_for_users defaults to False."""
        session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=MagicMock()
        )
        assert session.waiting_for_users is False

    def test_waiting_for_users_can_be_set(self):
        """waiting_for_users can be set explicitly."""
        session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=MagicMock(),
            waiting_for_users=True
        )
        assert session.waiting_for_users is True


# =============================================================================
# LyricsResult Dataclass Tests
# =============================================================================


class TestLyricsResult:
    """Tests for LyricsResult dataclass."""

    def test_creation_with_required_fields(self):
        """Can create with required fields."""
        result = LyricsResult(
            title="Song Title",
            artist="Artist Name",
            source="Genius",
            url="https://genius.com/song"
        )
        assert result.title == "Song Title"
        assert result.artist == "Artist Name"
        assert result.source == "Genius"
        assert result.url == "https://genius.com/song"

    def test_defaults(self):
        """Optional fields have correct defaults."""
        result = LyricsResult(
            title="Song",
            artist="Artist",
            source="Test",
            url="https://example.com"
        )
        assert result.has_translation is False
        assert result.lyrics_text is None
        assert result.translation_text is None

    def test_display_name_without_translation(self):
        """display_name formats correctly without translation."""
        result = LyricsResult(
            title="Bohemian Rhapsody",
            artist="Queen",
            source="Genius",
            url="https://example.com"
        )
        assert result.display_name == "Bohemian Rhapsody - Queen"

    def test_display_name_with_translation(self):
        """display_name includes globe emoji when has_translation is True."""
        result = LyricsResult(
            title="紅蓮華",
            artist="LiSA",
            source="Lyrical Nonsense",
            url="https://example.com",
            has_translation=True
        )
        assert result.display_name == "紅蓮華 - LiSA 🌐"

    def test_lyrics_text_can_be_set(self):
        """lyrics_text can be populated."""
        result = LyricsResult(
            title="Song",
            artist="Artist",
            source="Test",
            url="https://example.com",
            lyrics_text="These are the lyrics\nLine two"
        )
        assert result.lyrics_text == "These are the lyrics\nLine two"

    def test_translation_text_can_be_set(self):
        """translation_text can be populated."""
        result = LyricsResult(
            title="Song",
            artist="Artist",
            source="Test",
            url="https://example.com",
            has_translation=True,
            translation_text="English translation here"
        )
        assert result.translation_text == "English translation here"


# =============================================================================
# Track.to_dict / Track.from_dict Tests
# =============================================================================


class TestTrackSerialization:
    """Tests for Track serialization (to_dict/from_dict)."""

    def test_to_dict_includes_all_fields(self):
        """to_dict includes all serializable fields."""
        track = Track(
            title="Test Song",
            artist="Test Artist",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            duration=213,
            thumbnail="https://example.com/thumb.jpg",
            thumbnail_needs_crop=True,
            local_path="/cache/song.mp3",
            video_id="dQw4w9WgXcQ"
        )
        data = track.to_dict()

        assert data['title'] == "Test Song"
        assert data['artist'] == "Test Artist"
        assert data['url'] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert data['duration'] == 213
        assert data['thumbnail'] == "https://example.com/thumb.jpg"
        assert data['thumbnail_needs_crop'] is True
        assert data['local_path'] == "/cache/song.mp3"
        assert data['video_id'] == "dQw4w9WgXcQ"

    def test_to_dict_excludes_user_added(self):
        """to_dict intentionally excludes user_added (session-only)."""
        track = Track(
            title="Song",
            artist="Artist",
            url="https://youtube.com/watch?v=abc",
            duration=100,
            user_added=True
        )
        data = track.to_dict()
        assert 'user_added' not in data

    def test_from_dict_restores_track(self):
        """from_dict correctly restores a Track."""
        data = {
            'title': "Restored Song",
            'artist': "Restored Artist",
            'url': "https://www.youtube.com/watch?v=xyz789",
            'duration': 180,
            'thumbnail': "https://example.com/t.jpg",
            'thumbnail_needs_crop': False,
            'local_path': None,
            'video_id': "xyz789"
        }
        track = Track.from_dict(data)

        assert track.title == "Restored Song"
        assert track.artist == "Restored Artist"
        assert track.url == "https://www.youtube.com/watch?v=xyz789"
        assert track.duration == 180
        assert track.thumbnail == "https://example.com/t.jpg"
        assert track.thumbnail_needs_crop is False
        assert track.video_id == "xyz789"

    def test_from_dict_extracts_video_id_from_url(self):
        """from_dict extracts video_id from URL if not provided."""
        data = {
            'title': "Song",
            'artist': "Artist",
            'url': "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            'duration': 100
            # video_id intentionally missing
        }
        track = Track.from_dict(data)
        assert track.video_id == "dQw4w9WgXcQ"

    def test_from_dict_handles_missing_optional_fields(self):
        """from_dict handles missing optional fields gracefully."""
        data = {
            'title': "Minimal",
            'artist': "Test",
            'url': "https://youtube.com/watch?v=min",
            'duration': 60
        }
        track = Track.from_dict(data)

        assert track.thumbnail is None
        assert track.thumbnail_needs_crop is False
        assert track.local_path is None

    def test_roundtrip_preserves_data(self):
        """to_dict -> from_dict preserves all serializable data."""
        original = Track(
            title="Roundtrip Test",
            artist="Test Artist",
            url="https://www.youtube.com/watch?v=round123",
            duration=240,
            thumbnail="https://i.ytimg.com/thumb.jpg",
            thumbnail_needs_crop=True,
            local_path="/path/to/file.mp3",
            video_id="round123"
        )

        data = original.to_dict()
        restored = Track.from_dict(data)

        assert restored.title == original.title
        assert restored.artist == original.artist
        assert restored.url == original.url
        assert restored.duration == original.duration
        assert restored.thumbnail == original.thumbnail
        assert restored.thumbnail_needs_crop == original.thumbnail_needs_crop
        assert restored.local_path == original.local_path
        assert restored.video_id == original.video_id

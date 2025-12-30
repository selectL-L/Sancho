"""Tests for data classes and enums with logic in music_data.py.

Most dataclasses are just data containers (no logic to test), but some have
meaningful behavior that needs testing:
- LoopMode.next() - cycle logic
- LoopMode.convert() - parsing
- Track.to_dict() / from_dict() - serialization
- PrefetchState.is_valid_for() - validation logic
- PrefetchState.invalidate_if_affected() - conditional invalidation
- AmbienceState.request_switch() / consume_switch() - state machine
"""

import time
from unittest.mock import patch, MagicMock
import pytest

from utils.musicutils.music_data import (
    LoopMode,
    Track,
    PrefetchState,
    AmbienceState,
    FetchContext,
)


class TestLoopModeNext:
    """Tests for LoopMode.next() - cycle through loop modes."""

    def test_off_to_one(self):
        """OFF -> ONE."""
        assert LoopMode.OFF.next() == LoopMode.ONE

    def test_one_to_all(self):
        """ONE -> ALL."""
        assert LoopMode.ONE.next() == LoopMode.ALL

    def test_all_to_off(self):
        """ALL -> OFF (wraps around)."""
        assert LoopMode.ALL.next() == LoopMode.OFF

    def test_full_cycle(self):
        """Complete cycle returns to start."""
        mode = LoopMode.OFF
        mode = mode.next()  # ONE
        mode = mode.next()  # ALL
        mode = mode.next()  # OFF
        assert mode == LoopMode.OFF


class TestLoopModeConvert:
    """Tests for LoopMode.convert() - case-insensitive parsing."""

    @pytest.mark.asyncio
    async def test_lowercase(self):
        """Lowercase 'off' parses correctly."""
        ctx = MagicMock()
        result = await LoopMode.convert(ctx, "off")
        assert result == LoopMode.OFF

    @pytest.mark.asyncio
    async def test_uppercase(self):
        """Uppercase 'ALL' parses correctly."""
        ctx = MagicMock()
        result = await LoopMode.convert(ctx, "ALL")
        assert result == LoopMode.ALL

    @pytest.mark.asyncio
    async def test_mixed_case(self):
        """Mixed case 'One' parses correctly."""
        ctx = MagicMock()
        result = await LoopMode.convert(ctx, "One")
        assert result == LoopMode.ONE

    @pytest.mark.asyncio
    async def test_invalid_raises(self):
        """Invalid value raises BadArgument or ValueError."""
        ctx = MagicMock()
        with pytest.raises((ValueError, Exception)):  # BadArgument is a subclass of Exception
            await LoopMode.convert(ctx, "invalid")


class TestLoopModeDisplay:
    """Tests for LoopMode.display and emoji properties."""

    def test_display_names(self):
        """Each mode has a display name."""
        assert LoopMode.OFF.display == "Off"
        assert LoopMode.ONE.display == "One"
        assert LoopMode.ALL.display == "All"

    def test_emojis(self):
        """Each mode has an emoji."""
        assert LoopMode.OFF.emoji == "➡️"
        assert LoopMode.ONE.emoji == "🔂"
        assert LoopMode.ALL.emoji == "🔁"


class TestTrackSerialization:
    """Tests for Track.to_dict() and from_dict() round-trip."""

    def test_basic_round_trip(self):
        """Track survives serialization round-trip."""
        track = Track(
            title="Test Song",
            artist="Test Artist",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            duration=212,
            thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg",
            video_id="dQw4w9WgXcQ"
        )
        data = track.to_dict()
        restored = Track.from_dict(data)

        assert restored.title == track.title
        assert restored.artist == track.artist
        assert restored.url == track.url
        assert restored.duration == track.duration
        assert restored.thumbnail == track.thumbnail
        assert restored.video_id == track.video_id

    def test_user_added_not_serialized(self):
        """user_added flag is intentionally NOT serialized."""
        track = Track(
            title="User Added Song",
            artist="Artist",
            url="https://www.youtube.com/watch?v=abc123def45",
            duration=180,
            user_added=True  # Should NOT appear in dict
        )
        data = track.to_dict()
        assert 'user_added' not in data

        restored = Track.from_dict(data)
        # Default is False when not in dict
        assert restored.user_added is False

    def test_extracts_video_id_if_missing(self):
        """from_dict extracts video_id from URL if not in data."""
        data = {
            'title': 'Song',
            'artist': 'Artist',
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'duration': 100
            # video_id NOT provided
        }
        track = Track.from_dict(data)
        assert track.video_id == "dQw4w9WgXcQ"

    def test_uses_provided_video_id(self):
        """from_dict uses provided video_id over extraction."""
        data = {
            'title': 'Song',
            'artist': 'Artist',
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'duration': 100,
            'video_id': 'explicit_id'  # Provided explicitly
        }
        track = Track.from_dict(data)
        assert track.video_id == "explicit_id"

    def test_optional_fields_default(self):
        """Optional fields have sensible defaults."""
        data = {
            'title': 'Minimal Song',
            'artist': 'Artist',
            'url': 'https://youtu.be/abc123def45',
            'duration': 60
        }
        track = Track.from_dict(data)
        assert track.thumbnail is None
        assert track.thumbnail_needs_crop is False


class TestPrefetchStateIsValidFor:
    """Tests for PrefetchState.is_valid_for() - critical validation logic."""

    def test_no_audio_url(self):
        """Invalid when audio_url is None."""
        state = PrefetchState()
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0
        ) is False

    def test_no_target_index(self):
        """Invalid when target_index is None."""
        state = PrefetchState(audio_url="http://audio.url")
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0
        ) is False

    def test_valid_sequential_advance(self):
        """Valid when advancing to next track sequentially."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            target_video_id="abc123",
            fetched_at=time.time()
        )
        # current=0, next=1, playlist_len=5
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0, video_id="abc123"
        ) is True

    def test_valid_wrap_around(self):
        """Valid when wrapping from last to first track."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=0,  # First track
            fetched_at=time.time()
        )
        # current=4 (last), next=0 (wrap), playlist_len=5
        assert state.is_valid_for(
            playlist_index=0, playlist_len=5, current_index=4
        ) is True

    def test_invalid_index_mismatch(self):
        """Invalid when target_index doesn't match expected next."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            fetched_at=time.time()
        )
        # Prefetch was for index 1, but now we want index 3
        assert state.is_valid_for(
            playlist_index=3, playlist_len=5, current_index=2
        ) is False

    def test_invalid_video_id_mismatch(self):
        """Invalid when video_id at index changed (playlist mutated)."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            target_video_id="original_video",
            fetched_at=time.time()
        )
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0,
            video_id="different_video"
        ) is False

    def test_expired_url(self):
        """Invalid when URL has expired (>5 hours old)."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            fetched_at=time.time() - (6 * 60 * 60)  # 6 hours ago
        )
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0
        ) is False

    def test_fresh_url_valid(self):
        """Valid when URL is fresh (<5 hours old)."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            fetched_at=time.time() - (4 * 60 * 60)  # 4 hours ago
        )
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0
        ) is True

    def test_video_id_none_skips_check(self):
        """When video_id not provided, skip that check."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            target_video_id="stored_id",
            fetched_at=time.time()
        )
        # No video_id passed - should skip the check
        assert state.is_valid_for(
            playlist_index=1, playlist_len=5, current_index=0, video_id=None
        ) is True


class TestPrefetchStateInvalidateIfAffected:
    """Tests for PrefetchState.invalidate_if_affected()."""

    def test_no_target_index(self):
        """Does nothing when target_index is None."""
        state = PrefetchState()
        result = state.invalidate_if_affected({1, 2}, new_playlist_len=5)
        assert result is False

    def test_target_in_affected_set(self):
        """Invalidates when target_index is in affected set."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=2,
            fetched_at=time.time()
        )
        result = state.invalidate_if_affected({2, 3}, new_playlist_len=5)
        assert result is True
        assert state.audio_url is None
        assert state.target_index is None

    def test_target_not_in_affected_set(self):
        """Does NOT invalidate when target_index is NOT affected."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=1,
            fetched_at=time.time()
        )
        result = state.invalidate_if_affected({3, 4}, new_playlist_len=5)
        assert result is False
        assert state.audio_url == "http://audio.url"

    def test_target_out_of_bounds(self):
        """Invalidates when target_index >= new playlist length."""
        state = PrefetchState(
            audio_url="http://audio.url",
            target_index=5,  # Was valid, now out of bounds
            fetched_at=time.time()
        )
        result = state.invalidate_if_affected({0}, new_playlist_len=3)
        assert result is True
        assert state.audio_url is None


class TestPrefetchStateClear:
    """Tests for PrefetchState.clear()."""

    def test_clears_all_fields(self):
        """clear() resets all data fields."""
        state = PrefetchState(
            audio_url="http://audio.url",
            http_headers={"Authorization": "token"},
            thumbnail_bytes=b"image_data",
            target_index=3,
            target_video_id="video123",
            fetched_at=12345.0
        )
        state.clear()
        assert state.audio_url is None
        assert state.http_headers is None
        assert state.thumbnail_bytes is None
        assert state.target_index is None
        assert state.target_video_id is None
        assert state.fetched_at == 0.0


class TestAmbienceStateSwitch:
    """Tests for AmbienceState.request_switch() / consume_switch()."""

    def test_request_switch(self):
        """request_switch sets pending state."""
        state = AmbienceState()
        state.request_switch("https://youtube.com/playlist?list=ABC")

        assert state.pending_switch is True
        assert state.pending_playlist_url == "https://youtube.com/playlist?list=ABC"

    def test_consume_switch_when_pending(self):
        """consume_switch returns pending URL and clears state."""
        state = AmbienceState()
        state.request_switch("https://youtube.com/playlist?list=ABC")

        had_switch, url = state.consume_switch()

        assert had_switch is True
        assert url == "https://youtube.com/playlist?list=ABC"
        assert state.pending_switch is False
        assert state.pending_playlist_url is None

    def test_consume_switch_when_not_pending(self):
        """consume_switch returns False when nothing pending."""
        state = AmbienceState()

        had_switch, url = state.consume_switch()

        assert had_switch is False
        assert url is None

    def test_double_request_overwrites(self):
        """Second request_switch overwrites first."""
        state = AmbienceState()
        state.request_switch("https://playlist/1")
        state.request_switch("https://playlist/2")

        _had_switch, url = state.consume_switch()

        assert url == "https://playlist/2"

    def test_confirm_switch(self):
        """confirm_switch updates current_playlist_url."""
        state = AmbienceState()
        state.confirm_switch("https://youtube.com/playlist?list=XYZ")

        assert state.current_playlist_url == "https://youtube.com/playlist?list=XYZ"

    def test_full_switch_flow(self):
        """Complete flow: request → consume → confirm."""
        state = AmbienceState()
        state.current_playlist_url = "old_playlist"

        # Ambience requests switch
        state.request_switch("new_playlist")
        assert state.pending_switch is True

        # Main loop consumes
        had_switch, url = state.consume_switch()
        assert had_switch is True
        assert url == "new_playlist"

        # After loading, confirm
        state.confirm_switch("new_playlist")
        assert state.current_playlist_url == "new_playlist"

    def test_request_none_clears_playlist(self):
        """request_switch(None) signals playlist shutdown."""
        state = AmbienceState(current_playlist_url="some_playlist")
        state.request_switch(None)

        had_switch, url = state.consume_switch()

        assert had_switch is True
        assert url is None


class TestFetchContext:
    """Basic tests for FetchContext enum (mostly existence checks)."""

    def test_values_exist(self):
        """All expected values exist."""
        assert FetchContext.PREFETCH.value == "prefetch"
        assert FetchContext.LIVE.value == "live"
        assert FetchContext.RETRY.value == "retry"

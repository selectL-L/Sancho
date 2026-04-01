"""Tests for ManagedPlayer playback state machine.

ManagedPlayer owns:
- Playback state (STOPPED/PLAYING/PAUSED)
- Generation-based callback filtering (stale callbacks ignored)
- Single callback path (only natural track end triggers callback)

Tests mock VoiceClient and SeekableAudioSource to test state logic in isolation.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from utils.musicutils.managed_player import ManagedPlayer, PlayerState
from utils.musicutils.music_data import AudioErrorType, FFmpegHealth, FFmpegResponseAction


class MockTrack:
    """Mock track that satisfies TrackInfo protocol."""

    def __init__(
        self,
        title: str = "Test Track",
        url: str = "https://youtube.com/watch?v=test",
        duration: int = 180
    ):
        self._title = title
        self._url = url
        self._duration = duration

    @property
    def title(self) -> str:
        return self._title

    @property
    def url(self) -> str:
        return self._url

    @property
    def duration(self) -> int:
        return self._duration


@pytest.fixture
def mock_voice_client():
    """Create a mock VoiceClient."""
    vc = MagicMock()
    vc.is_playing.return_value = False
    vc.stop = MagicMock()
    vc.play = MagicMock()
    return vc


@pytest.fixture
def callback_tracker():
    """Track callback invocations."""
    class CallbackTracker:
        def __init__(self):
            self.calls = []

        def __call__(self, report) -> None:
            self.calls.append(report)

        @property
        def called(self) -> bool:
            return len(self.calls) > 0

        @property
        def call_count(self) -> int:
            return len(self.calls)

        @property
        def last_error(self) -> Exception | None:
            if self.calls:
                return self.calls[-1].error
            return None

        @property
        def last_report(self):
            if self.calls:
                return self.calls[-1]
            return None

    return CallbackTracker()


@pytest.fixture
def mock_source():
    """Create a mock SeekableAudioSource."""
    source = MagicMock()
    source.position = 0.0
    source.pause = MagicMock()
    source.resume = MagicMock()
    source.seek = MagicMock()
    source.cleanup = MagicMock()
    source.build_ffmpeg_health = MagicMock(return_value=FFmpegHealth())
    return source


@pytest.fixture
def player(mock_voice_client, callback_tracker):
    """Create a ManagedPlayer with mocked dependencies."""
    loop = asyncio.new_event_loop()
    with patch('utils.musicutils.managed_player.asyncio.get_running_loop', return_value=loop):
        p = ManagedPlayer(mock_voice_client, callback_tracker)
    yield p
    loop.close()


class TestPlayerStateInit:
    """Tests for initial player state."""

    def test_starts_stopped(self, player):
        """Player starts in STOPPED state."""
        assert player.state == PlayerState.STOPPED
        assert player.is_stopped is True
        assert player.is_playing is False
        assert player.is_paused is False

    def test_no_current_track(self, player):
        """No track loaded initially."""
        assert player.current_track is None

    def test_position_zero(self, player):
        """Position is 0 when no source."""
        assert player.position == 0.0


class TestPlayerPlay:
    """Tests for play() method."""

    def test_play_changes_state(self, player, mock_voice_client):
        """play() transitions to PLAYING state."""
        track = MockTrack()

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(track, "http://audio.url")

        assert player.state == PlayerState.PLAYING
        assert player.is_playing is True
        assert player.current_track is track

    def test_play_calls_voice_client(self, player, mock_voice_client):
        """play() calls VoiceClient.play()."""
        track = MockTrack()

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(track, "http://audio.url")

        mock_voice_client.play.assert_called_once()

    def test_play_increments_generation(self, player):
        """play() increments generation counter."""
        track = MockTrack()
        initial_gen = player._generation

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(track, "http://audio.url")

        assert player._generation == initial_gen + 1

    def test_play_stops_current_if_playing(self, player, mock_voice_client):
        """play() stops current playback before starting new."""
        mock_voice_client.is_playing.return_value = True
        track = MockTrack()

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(track, "http://audio.url")

        mock_voice_client.stop.assert_called()

    def test_play_cleans_up_old_source(self, player, mock_source):
        """play() cleans up previous source."""
        player._source = mock_source
        track = MockTrack()

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(track, "http://audio.url")

        mock_source.cleanup.assert_called_once()

    def test_play_applies_repeat_one_setting_to_new_source(self, player):
        """New sources inherit the current repeat-one setting."""
        track = MockTrack()
        player.set_repeat_one(True)

        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            created_source = MagicMock(position=0.0)
            MockSource.return_value = created_source
            player.play(track, "http://audio.url")

        created_source.set_repeat_one.assert_called_once_with(True)


class TestPlayerPause:
    """Tests for pause() method."""

    def test_pause_when_playing(self, player, mock_source):
        """pause() transitions PLAYING -> PAUSED."""
        player._state = PlayerState.PLAYING
        player._source = mock_source

        result = player.pause()

        assert result is True
        assert player.state == PlayerState.PAUSED
        mock_source.pause.assert_called_once()

    def test_pause_when_not_playing(self, player):
        """pause() returns False when not playing."""
        player._state = PlayerState.STOPPED

        result = player.pause()

        assert result is False
        assert player.state == PlayerState.STOPPED

    def test_pause_when_already_paused(self, player, mock_source):
        """pause() returns False when already paused."""
        player._state = PlayerState.PAUSED
        player._source = mock_source

        result = player.pause()

        assert result is False


class TestPlayerResume:
    """Tests for resume() method."""

    def test_resume_when_paused(self, player, mock_source):
        """resume() transitions PAUSED -> PLAYING."""
        player._state = PlayerState.PAUSED
        player._source = mock_source

        result = player.resume()

        assert result is True
        assert player.state == PlayerState.PLAYING
        mock_source.resume.assert_called_once()

    def test_resume_when_not_paused(self, player):
        """resume() returns False when not paused."""
        player._state = PlayerState.STOPPED

        result = player.resume()

        assert result is False

    def test_resume_when_playing(self, player, mock_source):
        """resume() returns False when already playing."""
        player._state = PlayerState.PLAYING
        player._source = mock_source

        result = player.resume()

        assert result is False


class TestPlayerSeek:
    """Tests for seek() method."""

    def test_seek_with_source(self, player, mock_source):
        """seek() delegates to source."""
        player._source = mock_source

        result = player.seek(30.0)

        assert result is True
        mock_source.seek.assert_called_once_with(30.0)

    def test_seek_without_source(self, player):
        """seek() returns False when no source."""
        result = player.seek(30.0)

        assert result is False


class TestPlayerRewind:
    """Tests for archive-preserving rewind()."""

    def test_rewind_with_source(self, player, mock_source):
        """rewind() delegates to source without rebuilding it."""
        mock_source.rewind = MagicMock(return_value=True)
        player._source = mock_source

        result = player.rewind(1.0)

        assert result is True
        mock_source.rewind.assert_called_once_with(1.0)

    def test_rewind_without_source(self, player):
        """rewind() returns False when no source is loaded."""
        result = player.rewind(1.0)

        assert result is False


class TestPlayerStop:
    """Tests for stop() method."""

    def test_stop_changes_state(self, player, mock_voice_client, mock_source):
        """stop() transitions to STOPPED state."""
        player._state = PlayerState.PLAYING
        player._source = mock_source
        player._current_track = MockTrack()
        mock_voice_client.is_playing.return_value = True

        player.stop()

        assert player.state == PlayerState.STOPPED
        assert player.current_track is None

    def test_stop_increments_generation(self, player):
        """stop() increments generation (invalidates pending callbacks)."""
        initial_gen = player._generation

        player.stop()

        assert player._generation == initial_gen + 1

    def test_stop_cleans_up_source(self, player, mock_source):
        """stop() cleans up source."""
        player._source = mock_source

        player.stop()

        mock_source.cleanup.assert_called_once()
        assert player._source is None

    def test_stop_no_callback(self, player, callback_tracker, mock_source):
        """stop() does NOT trigger callback."""
        player._state = PlayerState.PLAYING
        player._source = mock_source

        player.stop()

        assert callback_tracker.called is False


class TestPlayerRepeatOne:
    """Tests for repeat-one configuration propagation."""

    def test_set_repeat_one_updates_active_source(self, player, mock_source):
        player._source = mock_source

        player.set_repeat_one(True)

        mock_source.set_repeat_one.assert_called_once_with(True)


class TestGenerationFiltering:
    """Tests for generation-based callback filtering."""

    def test_stale_callback_ignored(self, player, callback_tracker):
        """Callback from old generation is ignored."""
        # Simulate callback from generation 0 when current is 1
        player._generation = 1

        # Call _after_callback directly with stale generation
        player._after_callback(None, callback_generation=0)

        # Callback should NOT have been invoked
        assert callback_tracker.called is False

    def test_current_generation_callback_processed(self, player, callback_tracker):
        """Callback from current generation is processed."""
        player._generation = 5
        player._play_started_at = 0  # Long time ago so elapsed check passes
        player._source = MagicMock()
        player._source.cleanup = MagicMock()
        player._source.build_ffmpeg_health = MagicMock(return_value=FFmpegHealth())

        # Call _after_callback with matching generation
        player._after_callback(None, callback_generation=5)

        # Callback should have been invoked
        # Note: Since _handle_track_end is async and scheduled on event loop,
        # we need to run the loop
        player._loop.run_until_complete(asyncio.sleep(0.1))

        assert callback_tracker.called is True
        assert callback_tracker.last_report.ffmpeg.response_action == FFmpegResponseAction.NONE

    def test_play_invalidates_pending_callbacks(self, player, mock_voice_client, callback_tracker):
        """play() increments generation before stopping, so pending callbacks are invalidated."""
        # Setup: simulate a track playing
        player._state = PlayerState.PLAYING
        player._generation = 1
        old_source = MagicMock()
        player._source = old_source
        mock_voice_client.is_playing.return_value = True

        # Start new track (simulates user skip)
        with patch('utils.musicutils.managed_player.SeekableAudioSource') as MockSource:
            MockSource.return_value = MagicMock(position=0.0)
            player.play(MockTrack(), "http://new.url")

        # Generation should have incremented
        assert player._generation == 2

        # Old callback arrives (simulates the stopped source triggering callback)
        player._after_callback(None, callback_generation=1)

        # Should be ignored because generation doesn't match
        assert callback_tracker.called is False


class TestPlayerPosition:
    """Tests for position property."""

    def test_position_from_source(self, player, mock_source):
        """position returns source.position."""
        mock_source.position = 42.5
        player._source = mock_source

        assert player.position == 42.5

    def test_position_zero_no_source(self, player):
        """position returns 0 when no source."""
        assert player.position == 0.0


class TestPlayerUpdateVoiceClient:
    """Tests for update_voice_client() method."""

    def test_updates_reference(self, player):
        """update_voice_client() replaces the voice client."""
        new_vc = MagicMock()

        player.update_voice_client(new_vc)

        assert player._vc is new_vc


class TestPlayerStateEnum:
    """Basic tests for PlayerState enum."""

    def test_values_exist(self):
        """All expected states exist."""
        assert PlayerState.STOPPED.value == "stopped"
        assert PlayerState.PLAYING.value == "playing"
        assert PlayerState.PAUSED.value == "paused"


class TestPlayerFailureReports:
    """Tests for the typed playback report produced on failures."""

    def test_retryable_ffmpeg_failure_reaches_callback(self, player, callback_tracker):
        """ManagedPlayer forwards parsed retry actions to its callback."""
        player._generation = 7
        player._play_started_at = time.time()
        player._current_track = MockTrack(duration=180)
        player._source = MagicMock()
        player._source.cleanup = MagicMock()
        health = FFmpegHealth()
        health.error_type = AudioErrorType.HTTP_403
        health.response_action = FFmpegResponseAction.RETRY_NEW_URL
        health.summary = "The remote server rejected the current signed stream URL (HTTP 403)."
        health.error_detail = "[error] [https @ 0x1] HTTP error 403 Forbidden"
        health.stderr_lines = ["[error] [https @ 0x1] HTTP error 403 Forbidden"]
        player._source.build_ffmpeg_health = MagicMock(
            return_value=health
        )

        player._after_callback(Exception("ffmpeg died"), callback_generation=7)
        player._loop.run_until_complete(asyncio.sleep(0.1))

        assert callback_tracker.called is True
        assert callback_tracker.last_report.ffmpeg.error_type == AudioErrorType.HTTP_403
        assert callback_tracker.last_report.ffmpeg.response_action == FFmpegResponseAction.RETRY_NEW_URL

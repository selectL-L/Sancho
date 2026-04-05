"""Unit tests for the Music cog.

Tests here verify individual cog methods in isolation — one event, one
expected outcome.  Multi-step session flows belong in tests/test_music_*.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.music import Music
from utils.musicutils import (
    ActiveSession,
    AudioErrorType,
    FFmpegBucket,
    FFmpegHealth,
    LoopMode,
    PlaybackEndReport,
    PlaybackState,
    Track,
    TrackIssueKind,
)
from utils.views import TrackFailureAction


# =============================================================================
# Helpers
# =============================================================================


def create_track(title: str, video_id: str) -> Track:
    """Create a deterministic test track."""
    return Track(
        title=title,
        artist="Artist",
        url=f"https://www.youtube.com/watch?v={video_id}",
        duration=180,
        video_id=video_id,
        user_added=True,
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_bot():
    """Create a mock bot instance."""
    bot = MagicMock()
    bot.owner_ids = {99999}
    bot.user = MagicMock()
    bot.user.id = 12345
    bot.loop = MagicMock()
    bot.db_manager = MagicMock()
    return bot


@pytest.fixture
def mock_voice_client():
    """Create a mock voice client."""
    vc = MagicMock(spec=discord.VoiceClient)
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.is_paused.return_value = False
    vc.play = MagicMock()
    vc.stop = MagicMock()
    return vc


@pytest.fixture
def music_cog(mock_bot):
    """Create a Music cog instance with minimal mocked dependencies."""
    with patch('cogs.music.MusicAmbience'):
        cog = Music(mock_bot)
        cog.playlist = []
        cog.current_index = 0
        cog.loop_mode = LoopMode.OFF
        cog.active_session = None
        cog._playback = PlaybackState()
        cog.db_manager = MagicMock()
        cog.db_manager.increment_proxy_usage = AsyncMock()
        return cog


# =============================================================================
# Startup
# =============================================================================


class TestMusicStartup:
    """Tests for non-blocking POT startup behavior."""

    @pytest.mark.asyncio
    async def test_cog_ready_schedules_pot_startup_in_background(self, music_cog):
        """cog_ready should not wait for POT startup readiness checks."""
        music_cog.bot.loop = asyncio.get_running_loop()
        pot_start_entered = asyncio.Event()
        release_pot_start = asyncio.Event()

        async def delayed_start() -> bool:
            pot_start_entered.set()
            await release_pot_start.wait()
            return True

        async def run_to_thread(func, *args):
            return func(*args)

        with (
            patch('cogs.music.YTDLP_AVAILABLE', True),
            patch.object(music_cog, '_start_pot_server', new=AsyncMock(side_effect=delayed_start)) as start_mock,
            patch.object(music_cog.cache_manager, 'initialize', new=AsyncMock()),
            patch.object(music_cog, '_presence_loop', new=AsyncMock()),
            patch.object(music_cog, '_start_cache_background_tasks'),
            patch.object(music_cog, '_pot_health_watchdog', new=AsyncMock()),
            patch('cogs.music.subscribe_playlist_change'),
            patch('cogs.music.start_music', return_value=(None, None)),
            patch('cogs.music.ambience.initialize'),
            patch('cogs.music.asyncio.to_thread', new=AsyncMock(side_effect=run_to_thread)),
            patch('utils.musicutils.music_auth.detect_youtube_auth') as detect_mock,
        ):
            await asyncio.wait_for(music_cog.cog_ready(), timeout=0.2)

            await asyncio.wait_for(pot_start_entered.wait(), timeout=0.2)
            assert music_cog._pot_start_task is not None
            assert not music_cog._pot_start_task.done()
            assert start_mock.await_count == 1

            pot_task = music_cog._pot_start_task
            release_pot_start.set()
            await asyncio.wait_for(pot_task, timeout=0.2)
            await asyncio.sleep(0)

            detect_mock.assert_called_once_with('startup')
            assert music_cog._pot_start_task is None

    @pytest.mark.asyncio
    async def test_stop_pot_server_cancels_pending_background_startup(self, music_cog):
        """Stopping POT should cancel any in-flight background startup task."""
        startup_cancelled = asyncio.Event()

        async def pending_startup() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                startup_cancelled.set()
                raise

        music_cog._pot_start_task = asyncio.create_task(pending_startup())
        await asyncio.sleep(0)

        await music_cog._stop_pot_server()

        assert startup_cancelled.is_set()
        assert music_cog._pot_start_task is None


# =============================================================================
# Bucket-driven playback decisions (_on_track_end)
# =============================================================================


class TestOnTrackEnd:
    """Tests for how the cog reacts to FFmpegBucket values."""

    @pytest.mark.asyncio
    async def test_retry_bucket_triggers_play_current_track(self, music_cog, mock_voice_client):
        """RETRY bucket should cause _on_track_end to retry via _play_current_track."""
        track = create_track("Retry Me", "dQw4w9WgXcQ")
        music_cog.playlist = [track]
        music_cog.active_session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=mock_voice_client,
            origin_channel_id=3,
        )

        report = PlaybackEndReport(
            error=Exception("ffmpeg failed"),
            ffmpeg=FFmpegHealth(
                error_type=AudioErrorType.HTTP_403,
                bucket=FFmpegBucket.RETRY,
                summary="The remote server rejected the current signed stream URL (HTTP 403).",
            ),
            elapsed=2.0,
        )

        with patch.object(music_cog, '_play_current_track', new=AsyncMock()) as play_mock:
            await music_cog._on_track_end(report)

        play_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_replay_bucket_triggers_play_current_track(self, music_cog, mock_voice_client):
        """REPLAY bucket should also retry (same as RETRY for now)."""
        track = create_track("Replay Me", "abc123def45")
        music_cog.playlist = [track]
        music_cog.active_session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=mock_voice_client,
            origin_channel_id=3,
        )

        report = PlaybackEndReport(
            error=None,
            ffmpeg=FFmpegHealth(
                error_type=AudioErrorType.CONNECTION,
                bucket=FFmpegBucket.REPLAY,
                summary="FFmpeg lost the network connection while reading the stream.",
            ),
            elapsed=10.0,
        )

        with patch.object(music_cog, '_play_current_track', new=AsyncMock()) as play_mock:
            await music_cog._on_track_end(report)

        play_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_bucket_prompts_user(self, music_cog, mock_voice_client):
        """SKIP bucket should show a prompt defaulting to SKIP."""
        track = create_track("Broken", "J---aiyznGQ")
        music_cog.playlist = [track]
        music_cog.active_session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=mock_voice_client,
            origin_channel_id=3,
        )

        report = PlaybackEndReport(
            error=None,
            ffmpeg=FFmpegHealth(
                error_type=AudioErrorType.HTTP_416,
                bucket=FFmpegBucket.SKIP,
                summary="FFmpeg requested an invalid byte range for the stream (HTTP 416).",
            ),
            elapsed=1.5,
        )

        with patch.object(music_cog, '_handle_track_failure', new=AsyncMock()) as failure_mock:
            await music_cog._on_track_end(report)

        failure_mock.assert_awaited_once()
        await_args = failure_mock.await_args
        assert await_args is not None
        assert await_args.args == (track,)
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.SKIP

    @pytest.mark.asyncio
    async def test_remove_bucket_prompts_user(self, music_cog, mock_voice_client):
        """REMOVE bucket should show a prompt defaulting to REMOVE."""
        track = create_track("Gone", "xvFZjo5PgG0")
        music_cog.playlist = [track]
        music_cog.active_session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=mock_voice_client,
            origin_channel_id=3,
        )

        report = PlaybackEndReport(
            error=None,
            ffmpeg=FFmpegHealth(
                bucket=FFmpegBucket.REMOVE,
                summary="Test: track should be removed.",
            ),
            elapsed=12.0,
        )

        with patch.object(music_cog, '_handle_track_failure', new=AsyncMock()) as failure_mock:
            await music_cog._on_track_end(report)

        failure_mock.assert_awaited_once()
        await_args = failure_mock.await_args
        assert await_args is not None
        assert await_args.args == (track,)
        assert await_args.kwargs['issue_kind'] == TrackIssueKind.UNAVAILABLE
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.REMOVE


# =============================================================================
# Source acquisition → cog decisions
# =============================================================================


class TestPlayCurrentTrack:
    """Tests for _play_current_track decision points."""

    @pytest.mark.asyncio
    async def test_acquire_returns_none_prompts_user(self, music_cog, mock_voice_client):
        """When _acquire_source returns None, _play_current_track shows a prompt."""
        track = create_track("Exhausted", "9bZkp7q19f0")
        music_cog.playlist = [track]
        music_cog.active_session = ActiveSession(
            guild_id=1,
            channel_id=2,
            voice_client=mock_voice_client,
            origin_channel_id=3,
        )
        music_cog._player = MagicMock()

        with patch.object(music_cog, '_acquire_source', new=AsyncMock(return_value=None)):
            with patch.object(music_cog, '_handle_track_failure', new=AsyncMock()) as failure_mock:
                await music_cog._play_current_track()

        failure_mock.assert_awaited_once()
        await_args = failure_mock.await_args
        assert await_args is not None
        assert await_args.args == (track,)
        assert await_args.kwargs['issue_kind'] == TrackIssueKind.TRANSIENT
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.SKIP


# =============================================================================
# Playlist navigation
# =============================================================================


class TestPlaylistNavigation:
    """Tests for skip/advance edge cases."""

    def test_skip_to_different_track_returns_none_for_single_track(self, music_cog):
        """Single-track playlist: skip should return None (no alternate)."""
        track = create_track("Lonely Track", "lonely001")
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.ONE

        assert music_cog._skip_to_different_track() is None

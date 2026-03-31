"""Focused tests for FFmpeg-driven music playback decisions.

These tests verify the cog's behavior when ManagedPlayer reports playback
failures via PlaybackEndReport.  The cog uses classify_failure() to decide
what to do, then either retries or prompts the user.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.music import Music
from utils.musicutils import (
    ActiveSession,
    AudioErrorType,
    FFmpegHealth,
    FFmpegResponseAction,
    LoopMode,
    PlaybackEndReport,
    PlaybackState,
    TrackIssueKind,
    TrackIssuePromptPreference,
    Track,
)
from utils.views import TrackFailureAction


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


@pytest.fixture
def mock_bot():
    """Create a mock bot instance."""
    bot = MagicMock()
    bot.owner_ids = {99999}
    bot.user = MagicMock()
    bot.user.id = 12345
    bot.loop = MagicMock()
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


class TestMusicFFmpegHandling:
    """Tests for classify_failure-driven playback decisions in the Music cog."""

    @pytest.mark.asyncio
    async def test_retryable_report_triggers_play_current_track(self, music_cog, mock_voice_client):
        """REFRESH_URL should cause _on_track_end to retry via _play_current_track."""
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
                response_action=FFmpegResponseAction.REFRESH_URL,
                summary="The remote server rejected the current signed stream URL (HTTP 403).",
            ),
            elapsed=2.0,
        )

        with patch.object(music_cog, '_play_current_track', new=AsyncMock()) as play_mock:
            await music_cog._on_track_end(report)

        play_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_track_report_prompts_user(self, music_cog, mock_voice_client):
        """REMOVE_TRACK should show a prompt defaulting to REMOVE."""
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
                error_type=AudioErrorType.HTTP_404,
                response_action=FFmpegResponseAction.REMOVE_TRACK,
                prompt_preference=TrackIssuePromptPreference.PREFER_REMOVE,
                summary="The remote stream no longer exists (HTTP 404).",
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

    @pytest.mark.asyncio
    async def test_fail_track_report_prompts_skip(self, music_cog, mock_voice_client):
        """FAIL_TRACK should show a prompt defaulting to SKIP."""
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
                response_action=FFmpegResponseAction.FAIL_TRACK,
                prompt_preference=TrackIssuePromptPreference.PREFER_SKIP,
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
        assert await_args.kwargs['issue_kind'] == TrackIssueKind.INTERNAL
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.SKIP

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
        # Default is TRANSIENT/SKIP when not unavailable
        assert await_args.kwargs['issue_kind'] == TrackIssueKind.TRANSIENT
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.SKIP

    @pytest.mark.asyncio
    async def test_unsupported_codec_prompts_skip(self, music_cog, mock_voice_client):
        """SKIP_TRACK with UNSUPPORTED_CODEC should prompt SKIP with INTERNAL kind."""
        track = create_track("Odd Codec", "codectrack1")
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
                error_type=AudioErrorType.UNSUPPORTED_CODEC,
                response_action=FFmpegResponseAction.SKIP_TRACK,
                prompt_preference=TrackIssuePromptPreference.PREFER_SKIP,
                summary="FFmpeg could not find a supported codec for this track.",
            ),
            elapsed=1.0,
        )

        with patch.object(music_cog, '_handle_track_failure', new=AsyncMock()) as failure_mock:
            await music_cog._on_track_end(report)

        failure_mock.assert_awaited_once()
        await_args = failure_mock.await_args
        assert await_args is not None
        assert await_args.args == (track,)
        assert await_args.kwargs['issue_kind'] == TrackIssueKind.INTERNAL
        assert await_args.kwargs['timeout_action'] == TrackFailureAction.SKIP

    def test_advance_track_after_skip_returns_none_for_single_track(self, music_cog):
        """Single-track playlist on loop ONE: skip should return None (no alternate)."""
        track = create_track("Lonely Track", "lonely001")
        music_cog.playlist = [track]
        music_cog.current_index = 0
        music_cog.loop_mode = LoopMode.ONE

        assert music_cog._advance_track_after_skip() is None

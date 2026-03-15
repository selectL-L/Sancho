"""Tests for AudioFetcher retry orchestration logic.

AudioFetcher owns the retry strategy:
- Tracks attempts per video_id
- Respects FetchContext (PREFETCH=conservative, LIVE/RETRY=aggressive)
- Escalates to residential proxy for LIVE/RETRY contexts

Tests mock the underlying get_audio_url() and cache_manager to test
the orchestration logic in isolation.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from utils.musicutils.music_data import AudioUrlResult, FetchContext, Track
from utils.musicutils.music_auth import (
    AudioFetcher,
    AudioFetchResult,
    YouTubeAuthStatus,
)


@pytest.fixture
def mock_track():
    """Create a mock Track object."""
    return Track(
        title="Test Song",
        artist="Test Artist",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        duration=212,
        video_id="dQw4w9WgXcQ"
    )


@pytest.fixture
def mock_cache_manager():
    """Create a mock MusicCacheManager."""
    manager = MagicMock()
    manager.get_any_local_path = MagicMock(return_value=None)  # No ambient cache by default
    manager.download_live_residential = AsyncMock(return_value=(False, "Not configured", 0, None))
    return manager


@pytest.fixture
def fetcher(mock_cache_manager):
    """Create an AudioFetcher with mocked dependencies."""
    return AudioFetcher(
        cache_manager=mock_cache_manager,
    )


class TestAudioFetcherInit:
    """Tests for AudioFetcher initialization."""

    def test_initial_state_empty(self, fetcher):
        """New fetcher has no tracked states."""
        assert fetcher.active_tracks == 0

    def test_constants_defined(self):
        """Class constants are reasonable."""
        assert AudioFetcher.DIRECT_MAX >= 1
        assert AudioFetcher.RESIDENTIAL_MAX >= 1
        assert AudioFetcher.RESIDENTIAL_MIN_DELAY >= 0


class TestAudioFetcherStateManagement:
    """Tests for state tracking per video_id."""

    def test_get_state_creates_new(self, fetcher):
        """_get_state creates new state if not exists."""
        state = fetcher._get_state("video123")
        assert state.video_id == "video123"
        assert state.direct_attempts == 0
        assert state.residential_attempts == 0

    def test_get_state_returns_existing(self, fetcher):
        """_get_state returns existing state."""
        state1 = fetcher._get_state("video123")
        state1.direct_attempts = 5
        state2 = fetcher._get_state("video123")
        assert state2.direct_attempts == 5
        assert state1 is state2

    def test_clear_state_single(self, fetcher):
        """clear_state removes specific video_id."""
        fetcher._get_state("video1")
        fetcher._get_state("video2")
        assert fetcher.active_tracks == 2

        fetcher.clear_state("video1")
        assert fetcher.active_tracks == 1
        assert fetcher.get_state_info("video1") is None
        assert fetcher.get_state_info("video2") is not None

    def test_clear_state_all(self, fetcher):
        """clear_state(None) clears all states."""
        fetcher._get_state("video1")
        fetcher._get_state("video2")
        fetcher.clear_state()
        assert fetcher.active_tracks == 0

    def test_reset_alias(self, fetcher):
        """reset() is alias for clear_state()."""
        fetcher._get_state("video1")
        fetcher.reset()
        assert fetcher.active_tracks == 0


class TestAudioFetcherResidentialCacheHit:
    """Tests for residential cache hit path (after direct fails)."""

    @pytest.mark.asyncio
    async def test_returns_cached_path(self, fetcher, mock_cache_manager, mock_track):
        """Returns local_path when residential cache hits after direct fails."""
        mock_cache_manager.get_any_local_path.side_effect = [
            None,
            "/cache/video.mp3",
        ]

        # Direct must fail for us to reach residential cache check
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(url=None, error="Blocked")
            result = await fetcher.fetch(mock_track, FetchContext.LIVE)

            # Verify direct was attempted
            mock_get.assert_called_once()

        assert result.success is True
        assert result.local_path == "/cache/video.mp3"
        # State is cleared on success, so no point checking direct_attempts
        mock_cache_manager.get_any_local_path.assert_any_call(
            mock_track.video_id,
            residential_allowed=True,
        )


class TestAudioFetcherAmbientCacheHit:
    """Tests for ambient cache lookup via get_any_local_path."""

    @pytest.mark.asyncio
    async def test_uses_ambient_cache(self, fetcher, mock_cache_manager, mock_track):
        """Returns cached file from ambient playlists.

        This covers the case where a user adds a song via /play that happens
        to exist in the ambient cache - we should use the cached file.
        """
        mock_cache_manager.get_any_local_path.return_value = "/playlists/abc123/dQw4w9WgXcQ.mp3"

        result = await fetcher.fetch(mock_track, FetchContext.LIVE)

        assert result.success is True
        assert result.local_path == "/playlists/abc123/dQw4w9WgXcQ.mp3"
        # Should not attempt direct fetch since cache hit
        assert fetcher._get_state(mock_track.video_id).direct_attempts == 0
        mock_cache_manager.get_any_local_path.assert_called_once_with(
            mock_track.video_id,
            residential_allowed=False,
        )

    @pytest.mark.asyncio
    async def test_direct_attempted_before_residential_cache(
        self, fetcher, mock_cache_manager, mock_track
    ):
        """Direct fetch is attempted before falling back to residential cache.

        Even if a residential cached file exists, we try direct first in case
        the 403 has cleared - direct gives higher quality than residential.
        """
        mock_cache_manager.get_any_local_path.side_effect = [
            None,
            "/residential/dQw4w9WgXcQ.mp3",
        ]

        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            # Direct fails, so we fall back to residential cache
            mock_get.return_value = AudioUrlResult(error="403 Forbidden")

            result = await fetcher.fetch(mock_track, FetchContext.LIVE)

        assert result.success is True
        assert result.local_path == "/residential/dQw4w9WgXcQ.mp3"
        # Direct was attempted first (before checking residential cache)
        mock_get.assert_called_once()
        mock_cache_manager.get_any_local_path.assert_any_call(
            mock_track.video_id,
            residential_allowed=True,
        )


class TestAudioFetcherDirectFetch:
    """Tests for direct yt-dlp fetch attempts."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, fetcher, mock_track):
        """Succeeds when get_audio_url returns URL."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(
                url="https://audio.url/stream",
                thumbnail="https://thumb.jpg",
                http_headers={"Authorization": "token"}
            )

            result = await fetcher.fetch(mock_track, FetchContext.PREFETCH)

            assert result.success is True
            assert result.url == "https://audio.url/stream"
            assert result.http_headers == {"Authorization": "token"}
            # State is cleared on success, so we verify via the mock call instead
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_increments_attempts_on_failure(self, fetcher, mock_track):
        """Direct attempts increment on failure."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            # First attempt fails
            await fetcher.fetch(mock_track, FetchContext.PREFETCH)
            assert fetcher._get_state(mock_track.video_id).direct_attempts == 1

            # Second attempt also fails
            await fetcher.fetch(mock_track, FetchContext.PREFETCH)
            assert fetcher._get_state(mock_track.video_id).direct_attempts == 2

    @pytest.mark.asyncio
    async def test_stops_at_direct_max(self, fetcher, mock_track):
        """Stops attempting direct after DIRECT_MAX."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            # Exhaust direct attempts
            for _ in range(AudioFetcher.DIRECT_MAX + 1):
                await fetcher.fetch(mock_track, FetchContext.PREFETCH)

            # Should not exceed max
            assert fetcher._get_state(mock_track.video_id).direct_attempts == AudioFetcher.DIRECT_MAX


class TestAudioFetcherPrefetchContext:
    """Tests for PREFETCH context (conservative mode)."""

    @pytest.mark.asyncio
    async def test_prefetch_no_residential_escalation(self, fetcher, mock_cache_manager, mock_track):
        """PREFETCH does NOT escalate to residential on failure."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            result = await fetcher.fetch(mock_track, FetchContext.PREFETCH)

            assert result.success is False
            # Should not have tried residential
            mock_cache_manager.download_live_residential.assert_not_called()


class TestAudioFetcherLiveContext:
    """Tests for LIVE context (aggressive mode)."""

    @pytest.mark.asyncio
    async def test_live_escalates_to_residential(self, fetcher, mock_cache_manager, mock_track):
        """LIVE escalates to residential on direct failure."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            with patch('utils.musicutils.music_auth.get_residential_proxy_url') as mock_proxy:
                mock_proxy.return_value = "http://proxy:8080"
                mock_cache_manager.download_live_residential.return_value = (
                    True, None, 5000, "/residential/video.mp3"
                )

                result = await fetcher.fetch(mock_track, FetchContext.LIVE)

                assert result.success is True
                assert result.local_path == "/residential/video.mp3"
                assert result.residential_used is True
                mock_cache_manager.download_live_residential.assert_called_once()


class TestAudioFetcherRetryContext:
    """Tests for RETRY context (aggressive mode after FFmpeg failure)."""

    @pytest.mark.asyncio
    async def test_retry_escalates_to_residential(self, fetcher, mock_cache_manager, mock_track):
        """RETRY escalates to residential on direct failure."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            with patch('utils.musicutils.music_auth.get_residential_proxy_url') as mock_proxy:
                mock_proxy.return_value = "http://proxy:8080"
                mock_cache_manager.download_live_residential.return_value = (
                    True, None, 5000, "/residential/video.mp3"
                )

                result = await fetcher.fetch(mock_track, FetchContext.RETRY)

                assert result.success is True
                assert result.residential_used is True


class TestAudioFetcherResidentialEscalation:
    """Tests for residential proxy escalation logic."""

    @pytest.mark.asyncio
    async def test_no_proxy_configured(self, fetcher, mock_cache_manager, mock_track):
        """Fails gracefully when proxy not configured."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            with patch('utils.musicutils.music_auth.get_residential_proxy_url') as mock_proxy:
                mock_proxy.return_value = None  # No proxy

                result = await fetcher.fetch(mock_track, FetchContext.LIVE)

                assert result.success is False
                assert "not configured" in result.error.lower()

    @pytest.mark.asyncio
    async def test_residential_attempt_limit(self, fetcher, mock_cache_manager, mock_track):
        """Stops at RESIDENTIAL_MAX attempts."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            with patch('utils.musicutils.music_auth.get_residential_proxy_url') as mock_proxy:
                mock_proxy.return_value = "http://proxy:8080"
                mock_cache_manager.download_live_residential.return_value = (False, "Failed", 0, None)

                # Exhaust residential attempts
                for _ in range(AudioFetcher.RESIDENTIAL_MAX + 2):
                    await fetcher.fetch(mock_track, FetchContext.LIVE)

                state = fetcher._get_state(mock_track.video_id)
                assert state.residential_attempts == AudioFetcher.RESIDENTIAL_MAX

    @pytest.mark.asyncio
    async def test_residential_tracks_bytes(self, fetcher, mock_cache_manager, mock_track):
        """Residential downloads track bytes for cost accounting."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(error="Failed")

            with patch('utils.musicutils.music_auth.get_residential_proxy_url') as mock_proxy:
                mock_proxy.return_value = "http://proxy:8080"
                mock_cache_manager.download_live_residential.return_value = (
                    True, None, 1_000_000, "/path.mp3"
                )

                result = await fetcher.fetch(mock_track, FetchContext.LIVE)

                assert result.residential_bytes == 1_000_000


class TestAudioFetcherAuthFailureTracking:
    """Tests for 403 / auth failure tracking."""

    @pytest.mark.asyncio
    async def test_auth_failure_recorded(self, fetcher, mock_track):
        """Auth failure (403) sets state flag."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            # Simulate 403 response
            mock_get.side_effect = Exception("HTTP Error 403: Forbidden")

            with patch('utils.musicutils.music_auth.is_403_error', return_value=True):
                with patch('utils.musicutils.music_auth.is_video_unavailable', return_value=False):
                    result = await fetcher.fetch(mock_track, FetchContext.PREFETCH)

                    assert result.is_auth_failure is True
                    assert fetcher._get_state(mock_track.video_id).auth_failed is True


class TestAudioFetcherUnavailable:
    """Tests for permanently unavailable tracks."""

    @pytest.mark.asyncio
    async def test_unavailable_marked_prefetch(self, fetcher, mock_track):
        """Unavailable tracks are flagged for removal (PREFETCH context)."""
        with patch('utils.musicutils.music_auth.get_audio_url') as mock_get:
            mock_get.return_value = AudioUrlResult(is_unavailable=True)

            # Use PREFETCH - won't escalate to residential
            result = await fetcher.fetch(mock_track, FetchContext.PREFETCH)

            assert result.is_unavailable is True


class TestAudioFetcherNoVideoId:
    """Tests for tracks without video_id."""

    @pytest.mark.asyncio
    async def test_no_video_id_fails(self, fetcher, mock_cache_manager):
        """Track with no video_id fails immediately."""
        track = Track(
            title="No ID",
            artist="Artist",
            url="https://example.com",
            duration=100,
            video_id=None
        )

        result = await fetcher.fetch(track, FetchContext.LIVE)

        assert result.success is False
        assert "video_id" in result.error.lower()


class TestAudioFetchResult:
    """Tests for AudioFetchResult dataclass."""

    def test_bool_success(self):
        """__bool__ returns True for success."""
        result = AudioFetchResult(success=True, url="http://audio.url")
        assert bool(result) is True
        assert result  # if result: should work

    def test_bool_failure(self):
        """__bool__ returns False for failure."""
        result = AudioFetchResult(success=False, error="Failed")
        assert bool(result) is False


class TestYouTubeAuthStatus:
    """Tests for YouTubeAuthStatus 403 tracking."""

    def test_record_403_under_threshold(self):
        """record_403 returns False under threshold."""
        status = YouTubeAuthStatus()
        result = False
        for _ in range(status._403_threshold - 1):
            result = status.record_403()
        # Should not alert yet
        assert result is False

    def test_record_403_at_threshold(self):
        """record_403 returns True at threshold."""
        status = YouTubeAuthStatus()
        result = False
        for _ in range(status._403_threshold):
            result = status.record_403()

        # Should alert at threshold
        assert result is True

    def test_record_403_cooldown(self):
        """record_403 respects alert cooldown."""
        import time
        status = YouTubeAuthStatus()
        status._alert_cooldown = 10.0  # 10 second cooldown for test

        # First batch triggers alert
        for _ in range(status._403_threshold):
            status.record_403()

        # Reset timestamps but set last_alert recently
        status._403_timestamps.clear()
        status._last_alert = time.time()

        # Second batch should NOT alert (cooldown)
        result = False
        for _ in range(status._403_threshold):
            result = status.record_403()

        # Should not alert due to cooldown
        assert result is False

    def test_get_403_rate(self):
        """get_403_rate returns count in window."""
        status = YouTubeAuthStatus()
        status.record_403()
        status.record_403()
        status.record_403()

        count, window = status.get_403_rate()
        assert count == 3
        assert window == status._403_window

    def test_reset_403_tracking(self):
        """reset_403_tracking clears history."""
        status = YouTubeAuthStatus()
        status.record_403()
        status.record_403()
        status.reset_403_tracking()

        count, _ = status.get_403_rate()
        assert count == 0

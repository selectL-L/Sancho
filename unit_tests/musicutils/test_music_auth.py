"""Focused tests for surviving auth-state helpers in music_auth.py."""

import time

from utils.musicutils.music_auth import YouTubeAuthStatus


class TestYouTubeAuthStatus:
    """Tests for YouTubeAuthStatus 403 tracking."""

    def test_record_403_under_threshold(self):
        """record_403 returns False under threshold."""
        status = YouTubeAuthStatus()
        result = False
        for _ in range(status._403_threshold - 1):
            result = status.record_403()
        assert result is False

    def test_record_403_at_threshold(self):
        """record_403 returns True at threshold."""
        status = YouTubeAuthStatus()
        result = False
        for _ in range(status._403_threshold):
            result = status.record_403()

        assert result is True

    def test_record_403_cooldown(self):
        """record_403 respects alert cooldown."""
        status = YouTubeAuthStatus()
        status._alert_cooldown = 10.0

        for _ in range(status._403_threshold):
            status.record_403()

        status._403_timestamps.clear()
        status._last_alert = time.time()

        result = False
        for _ in range(status._403_threshold):
            result = status.record_403()

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

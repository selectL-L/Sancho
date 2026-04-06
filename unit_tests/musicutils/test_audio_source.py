"""Tests for the source-owned loop-one archive behavior."""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from utils.musicutils.audio_source import SeekableAudioSource


def _prime_archive(source: SeekableAudioSource, *packets: bytes) -> None:
    """Inject pre-encoded Opus packets directly into the archive for testing.

    Each entry in *packets represents one 20ms Opus packet (variable size).
    In production these come from the producer thread's Opus encoder; in
    tests we use arbitrary byte strings to verify cursor/looping behavior.
    """
    with source._archive_condition:
        source._archive_packets = list(packets)
        source._archive_total_frames = len(packets)
        source._archive_complete = True
        source._play_frame_index = 0
        source._archive_condition.notify_all()


@pytest.fixture
def source() -> Generator[SeekableAudioSource, None, None]:
    with patch.object(SeekableAudioSource, '_spawn_ffmpeg', lambda self, start_position: None):
        src = SeekableAudioSource('https://example.com/audio')
    yield src
    src.cleanup()


class TestSeekableAudioSourceLooping:
    """Tests for source-level loop-one rewind behavior."""

    def test_read_eofs_when_repeat_one_disabled(self, source: SeekableAudioSource):
        pkt_a = b'opus-packet-a'
        pkt_b = b'opus-packet-b'
        _prime_archive(source, pkt_a, pkt_b)

        source.set_repeat_one(False)

        assert source.read() == pkt_a
        assert source.read() == pkt_b
        assert source.read() == b''

    def test_read_rewinds_when_repeat_one_enabled(self, source: SeekableAudioSource):
        pkt_a = b'opus-packet-a'
        pkt_b = b'opus-packet-b'
        _prime_archive(source, pkt_a, pkt_b)

        source.set_repeat_one(True)

        assert source.read() == pkt_a
        assert source.read() == pkt_b
        assert source.read() == pkt_a
        assert source.read() == pkt_b

    def test_mid_playback_toggle_on_loops_at_boundary(self, source: SeekableAudioSource):
        pkt_a = b'opus-packet-a'
        pkt_b = b'opus-packet-b'
        _prime_archive(source, pkt_a, pkt_b)

        source.set_repeat_one(False)
        assert source.read() == pkt_a

        source.set_repeat_one(True)
        assert source.read() == pkt_b
        assert source.read() == pkt_a

    def test_mid_playback_toggle_off_eofs_on_next_boundary(self, source: SeekableAudioSource):
        pkt_a = b'opus-packet-a'
        pkt_b = b'opus-packet-b'
        _prime_archive(source, pkt_a, pkt_b)

        source.set_repeat_one(True)
        assert source.read() == pkt_a
        assert source.read() == pkt_b
        assert source.read() == pkt_a

        source.set_repeat_one(False)
        assert source.read() == pkt_b
        assert source.read() == b''

    def test_rewind_does_not_change_loop_one_restart_point(self, source: SeekableAudioSource):
        # 60 distinct packets, each a unique byte pattern
        packets = [bytes([index]) * 10 for index in range(60)]
        _prime_archive(source, *packets)
        source.set_repeat_one(True)

        # Consume 55 packets (1.1 seconds at 50 packets/sec)
        for index in range(55):
            assert source.read() == packets[index]

        # Rewind 1 second = 50 frames, lands at frame index 5
        assert source.rewind(1.0) is True
        assert source.read() == packets[5]

        for index in range(6, len(packets)):
            assert source.read() == packets[index]

        # Loop one should still restart from the real beginning, not frame 5.
        assert source.read() == packets[0]

    def test_rewind_at_start_is_a_safe_no_op(self, source: SeekableAudioSource):
        pkt_a = b'opus-packet-a'
        pkt_b = b'opus-packet-b'
        _prime_archive(source, pkt_a, pkt_b)

        # Simulates pausing immediately after playback starts, before we have
        # advanced beyond the beginning of the archive.
        assert source.rewind(1.0) is True
        assert source.read() == pkt_a
        assert source.read() == pkt_b

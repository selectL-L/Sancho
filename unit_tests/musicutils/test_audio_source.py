"""Tests for the source-owned loop-one archive behavior."""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from utils.musicutils.audio_source import FRAME_SIZE, SeekableAudioSource


def _prime_archive(source: SeekableAudioSource, *frames: bytes) -> None:
    with source._archive_condition:
        source._archive_chunks = list(frames)
        source._archive_total_bytes = sum(len(frame) for frame in frames)
        source._archive_complete = True
        source._play_chunk_index = 0
        source._play_chunk_offset = 0
        source._play_absolute_bytes = 0
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
        frame_a = b'A' * FRAME_SIZE
        frame_b = b'B' * FRAME_SIZE
        _prime_archive(source, frame_a, frame_b)

        source.set_repeat_one(False)

        assert source.read() == frame_a
        assert source.read() == frame_b
        assert source.read() == b''

    def test_read_rewinds_when_repeat_one_enabled(self, source: SeekableAudioSource):
        frame_a = b'A' * FRAME_SIZE
        frame_b = b'B' * FRAME_SIZE
        _prime_archive(source, frame_a, frame_b)

        source.set_repeat_one(True)

        assert source.read() == frame_a
        assert source.read() == frame_b
        assert source.read() == frame_a
        assert source.read() == frame_b

    def test_mid_playback_toggle_on_loops_at_boundary(self, source: SeekableAudioSource):
        frame_a = b'A' * FRAME_SIZE
        frame_b = b'B' * FRAME_SIZE
        _prime_archive(source, frame_a, frame_b)

        source.set_repeat_one(False)
        assert source.read() == frame_a

        source.set_repeat_one(True)
        assert source.read() == frame_b
        assert source.read() == frame_a

    def test_mid_playback_toggle_off_eofs_on_next_boundary(self, source: SeekableAudioSource):
        frame_a = b'A' * FRAME_SIZE
        frame_b = b'B' * FRAME_SIZE
        _prime_archive(source, frame_a, frame_b)

        source.set_repeat_one(True)
        assert source.read() == frame_a
        assert source.read() == frame_b
        assert source.read() == frame_a

        source.set_repeat_one(False)
        assert source.read() == frame_b
        assert source.read() == b''

    def test_rewind_does_not_change_loop_one_restart_point(self, source: SeekableAudioSource):
        frames = [bytes([index]) * FRAME_SIZE for index in range(60)]
        _prime_archive(source, *frames)
        source.set_repeat_one(True)

        for index in range(55):
            assert source.read() == frames[index]

        assert source.rewind(1.0) is True
        assert source.read() == frames[5]

        for index in range(6, len(frames)):
            assert source.read() == frames[index]

        # Loop one should still restart from the real beginning, not frame 5.
        assert source.read() == frames[0]

    def test_rewind_at_start_is_a_safe_no_op(self, source: SeekableAudioSource):
        frame_a = b'A' * FRAME_SIZE
        frame_b = b'B' * FRAME_SIZE
        _prime_archive(source, frame_a, frame_b)

        # Simulates pausing immediately after playback starts, before we have
        # advanced beyond the beginning of the archive.
        assert source.rewind(1.0) is True
        assert source.read() == frame_a
        assert source.read() == frame_b

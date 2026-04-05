"""Tests for FFmpeg stderr transcript parsing."""

from utils.musicutils.ffmpeg_parser import FFmpegStderrParser
from utils.musicutils.music_data import AudioErrorType, FFmpegBucket


class TestFFmpegStderrParser:
    """Transcript-driven tests for FFmpeg stderr reduction."""

    def test_http_403_midstream_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] HTTP error 403 Forbidden")
        parser.consume_line("[error] [matroska,webm @ 0x1] Read error")

        report = parser.finalize(
            frames_read=1200,
            process_returncode=1,
            context='playback',
            elapsed=25.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_403
        assert report.bucket == FFmpegBucket.RETRY
        assert '403' in (report.summary or '')

    def test_http_404_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] HTTP error 404 Not Found")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=1.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_404
        assert report.bucket == FFmpegBucket.RETRY

    def test_http_416_maps_to_skip(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] HTTP error 416 Requested Range Not Satisfiable")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=3.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_416
        assert report.bucket == FFmpegBucket.SKIP

    def test_rate_limit_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] HTTP error 429 Too Many Requests")

        report = parser.finalize(
            frames_read=50,
            process_returncode=1,
            context='playback',
            elapsed=4.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_429
        assert report.bucket == FFmpegBucket.RETRY

    def test_final_stats_mark_normal_completion(self):
        parser = FFmpegStderrParser()
        parser.consume_line(
            "video:0kB audio:12345kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: 0.000000%"
        )

        report = parser.finalize(
            frames_read=9000,
            process_returncode=0,
            context='playback',
            elapsed=180.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.NONE
        assert report.bucket == FFmpegBucket.DONE
        assert report.summary == 'FFmpeg finished streaming normally.'

    def test_prefetch_unknown_failure_defaults_to_retry(self):
        parser = FFmpegStderrParser()

        report = parser.finalize(
            frames_read=200,
            process_returncode=1,
            context='prefetch',
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.UNKNOWN
        assert report.bucket == FFmpegBucket.RETRY
        assert report.used_heuristic is True

    def test_broken_pipe_is_done(self):
        parser = FFmpegStderrParser()
        parser.consume_line("Broken pipe")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=0.5,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.BROKEN_PIPE
        assert report.bucket == FFmpegBucket.DONE

    def test_connection_failure_maps_to_replay(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] Connection reset by peer")

        report = parser.finalize(
            frames_read=500,
            process_returncode=1,
            context='playback',
            elapsed=10.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.CONNECTION
        assert report.bucket == FFmpegBucket.REPLAY

    def test_tls_failure_maps_to_replay(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] TLS connection was non-properly terminated")

        report = parser.finalize(
            frames_read=300,
            process_returncode=1,
            context='playback',
            elapsed=6.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.TLS
        assert report.bucket == FFmpegBucket.REPLAY

    def test_reconnect_failed_maps_to_replay(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[info] Reconnecting to stream...")
        parser.consume_line("[error] Reconnect failed, max retries reached")

        report = parser.finalize(
            frames_read=1000,
            process_returncode=1,
            context='playback',
            elapsed=20.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.CONNECTION
        assert report.bucket == FFmpegBucket.REPLAY
        assert report.reconnect_count == 1

    def test_format_error_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] moov atom not found")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=1.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.FORMAT
        assert report.bucket == FFmpegBucket.RETRY

    def test_fast_fail_heuristic_maps_to_retry(self):
        parser = FFmpegStderrParser()

        report = parser.finalize(
            frames_read=10,
            process_returncode=1,
            context='playback',
            elapsed=1.5,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_403
        assert report.bucket == FFmpegBucket.RETRY
        assert report.used_heuristic is True

    def test_unknown_terminal_failure_maps_to_skip(self):
        parser = FFmpegStderrParser()

        report = parser.finalize(
            frames_read=10,
            process_returncode=1,
            context='playback',
            elapsed=30.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.UNKNOWN
        assert report.bucket == FFmpegBucket.SKIP
        assert report.used_heuristic is True

    def test_server_error_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] Server returned 503 Service Unavailable")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=2.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_OTHER
        assert report.bucket == FFmpegBucket.RETRY

    def test_http_410_maps_to_retry(self):
        parser = FFmpegStderrParser()
        parser.consume_line("[error] [https @ 0x1] HTTP error 410 Gone")

        report = parser.finalize(
            frames_read=0,
            process_returncode=1,
            context='playback',
            elapsed=1.0,
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.HTTP_410
        assert report.bucket == FFmpegBucket.RETRY

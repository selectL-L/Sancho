"""Tests for FFmpeg stderr transcript parsing."""

from utils.musicutils.ffmpeg_parser import FFmpegStderrParser
from utils.musicutils.music_data import AudioErrorType, FFmpegResponseAction, TrackIssuePromptPreference


class TestFFmpegStderrParser:
    """Transcript-driven tests for FFmpeg stderr reduction."""

    def test_http_403_midstream_maps_to_refresh(self):
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
        assert report.response_action == FFmpegResponseAction.REFRESH_URL
        assert '403' in (report.summary or '')

    def test_http_404_maps_to_remove_preferred_prompt(self):
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
        assert report.response_action == FFmpegResponseAction.REMOVE_TRACK
        assert report.prompt_preference == TrackIssuePromptPreference.PREFER_REMOVE

    def test_http_416_maps_to_skip_preferred_prompt(self):
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
        assert report.response_action == FFmpegResponseAction.FAIL_TRACK
        assert report.prompt_preference == TrackIssuePromptPreference.PREFER_SKIP

    def test_rate_limit_maps_to_backoff_retry(self):
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
        assert report.response_action == FFmpegResponseAction.BACKOFF_RETRY

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
        assert report.response_action == FFmpegResponseAction.NONE
        assert report.summary == 'FFmpeg finished streaming normally.'

    def test_prefetch_unknown_failure_defaults_to_refresh(self):
        parser = FFmpegStderrParser()

        report = parser.finalize(
            frames_read=200,
            process_returncode=1,
            context='prefetch',
            expected_duration=180,
        )

        assert report.error_type == AudioErrorType.UNKNOWN
        assert report.response_action == FFmpegResponseAction.REFRESH_URL
        assert report.used_heuristic is True

    def test_broken_pipe_is_ignored(self):
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
        assert report.response_action == FFmpegResponseAction.IGNORE

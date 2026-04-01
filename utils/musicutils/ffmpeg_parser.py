"""Stateful FFmpeg stderr parsing for music playback."""

import re
from typing import Optional

from utils.musicutils.music_data import AudioErrorType, FFmpegHealth, FFmpegResponseAction, TrackIssuePromptPreference


RE_HTTP_STATUS = re.compile(r'(?:HTTP error|Server returned)\s+(\d{3})\s+(.+)', re.IGNORECASE)
RE_RECONNECT_ATTEMPT = re.compile(r'Reconnecting to|reconnect.*attempt|Will reconnect', re.IGNORECASE)
RE_RECONNECT_SUCCESS = re.compile(r'Reconnect(?:ing)? successful', re.IGNORECASE)
RE_RECONNECT_FAILED = re.compile(r'Reconnect(?:ing)? failed|max.*retries? reached', re.IGNORECASE)
RE_FINAL_STATS = re.compile(r'video:\d+kB audio:\d+kB', re.IGNORECASE)
RE_BROKEN_PIPE = re.compile(r'Broken pipe', re.IGNORECASE)
RE_END_OF_FILE = re.compile(r'End of file', re.IGNORECASE)

CONNECTION_PATTERNS = (
    'connection reset by peer',
    'connection refused',
    'connection timed out',
    'network is unreachable',
    'no route to host',
    'name or service not known',
    'could not resolve host',
    'i/o error',
    'read error',
)

TLS_PATTERNS = (
    'tls connection was non-properly terminated',
    'ssl routines',
    'handshake failed',
    'specified data could not be decrypted',
    'error in the pull function',
)

UNSUPPORTED_CODEC_PATTERNS = (
    'could not find codec',
    'unable to find a suitable codec',
    'audio: none',
)

FORMAT_PATTERNS = (
    'moov atom not found',
    'invalid data found when processing input',
    'invalid stream',
)

FILTER_PATTERNS = (
    'error initializing filter',
    'filter failed',
)


class FFmpegStderrParser:
    """Incrementally parses FFmpeg stderr into a structured report."""

    def __init__(self) -> None:
        self._report = FFmpegHealth()
        self._root_cause_locked = False

    @property
    def report(self) -> FFmpegHealth:
        """Current parser state without finalization heuristics."""
        return self._report

    def consume_line(self, line: str) -> None:
        """Consume one stderr line and update parser state."""
        stripped = line.strip()
        if not stripped:
            return

        self._report.stderr_lines.append(stripped)

        if RE_RECONNECT_ATTEMPT.search(stripped):
            self._report.reconnect_count += 1

        if RE_RECONNECT_SUCCESS.search(stripped):
            self._report.summary = 'FFmpeg reconnected to the remote stream.'

        if RE_RECONNECT_FAILED.search(stripped):
            self._set_root_cause(
                AudioErrorType.CONNECTION,
                FFmpegResponseAction.RETRY_NEW_URL,
                'FFmpeg exhausted its reconnect attempts for the current stream.',
                stripped,
            )
            return

        if RE_FINAL_STATS.search(stripped):
            self._report.saw_final_stats = True

        if 'exiting normally' in stripped.lower():
            self._report.saw_normal_exit = True

        if RE_END_OF_FILE.search(stripped):
            self._report.saw_end_of_file = True

        if RE_BROKEN_PIPE.search(stripped):
            self._report.saw_broken_pipe = True
            self._set_root_cause(
                AudioErrorType.BROKEN_PIPE,
                FFmpegResponseAction.IGNORE,
                'FFmpeg output pipe was closed by the caller.',
                stripped,
            )
            return

        status_match = RE_HTTP_STATUS.search(stripped)
        if status_match:
            status_code = int(status_match.group(1))
            self._handle_http_status(status_code, stripped)
            return

        lowered = stripped.lower()

        if any(pattern in lowered for pattern in CONNECTION_PATTERNS):
            self._set_root_cause(
                AudioErrorType.CONNECTION,
                FFmpegResponseAction.RETRY_SAME_URL,
                'FFmpeg lost the network connection while reading the stream.',
                stripped,
            )
            return

        if any(pattern in lowered for pattern in TLS_PATTERNS):
            self._set_root_cause(
                AudioErrorType.TLS,
                FFmpegResponseAction.RETRY_SAME_URL,
                'FFmpeg hit a TLS or socket-layer read failure.',
                stripped,
            )
            return

        if any(pattern in lowered for pattern in UNSUPPORTED_CODEC_PATTERNS):
            self._set_root_cause(
                AudioErrorType.UNSUPPORTED_CODEC,
                FFmpegResponseAction.SKIP,
                'FFmpeg could not find a supported codec for this track.',
                stripped,
                prompt_preference=TrackIssuePromptPreference.PREFER_SKIP,
            )
            return

        if any(pattern in lowered for pattern in FILTER_PATTERNS):
            self._set_root_cause(
                AudioErrorType.FILTER,
                FFmpegResponseAction.SKIP,
                'FFmpeg failed while initializing the audio filter chain.',
                stripped,
                prompt_preference=TrackIssuePromptPreference.PREFER_SKIP,
            )
            return

        if any(pattern in lowered for pattern in FORMAT_PATTERNS):
            self._set_root_cause(
                AudioErrorType.FORMAT,
                FFmpegResponseAction.RETRY_NEW_URL,
                'FFmpeg reported malformed or incomplete stream data.',
                stripped,
            )

    def finalize(
        self,
        *,
        frames_read: int,
        process_returncode: Optional[int],
        context: str,
        elapsed: Optional[float] = None,
        expected_duration: Optional[int] = None,
        explicit_error: bool = False,
    ) -> FFmpegHealth:
        """Finalize parser state into a report with fallback heuristics."""
        self._report.frames_read = frames_read
        self._report.process_returncode = process_returncode

        if self._report.response_action == FFmpegResponseAction.NONE:
            if context == 'prefetch':
                self._apply_unknown_prefetch_failure()
            elif explicit_error:
                self._apply_unknown_playback_failure()
            elif self._should_use_fast_fail_heuristic(elapsed, expected_duration):
                self._report.used_heuristic = True
                self._report.error_type = AudioErrorType.HTTP_403
                self._report.response_action = FFmpegResponseAction.RETRY_NEW_URL
                self._report.summary = (
                    'Playback ended far too quickly for the track length; '
                    'the signed stream URL likely went stale.'
                )
            elif self._looks_like_normal_completion(expected_duration, frames_read, process_returncode):
                self._report.summary = 'FFmpeg finished streaming normally.'
            else:
                self._apply_unknown_terminal_failure()

        return self._report

    def _handle_http_status(self, status_code: int, line: str) -> None:
        if status_code == 403:
            # yt-dlp already resolved the watch page into a playable media URL.
            # A 403 here usually means that specific signed URL went stale or
            # was rejected by the edge serving the bytes, not that the track is
            # permanently gone from YouTube.
            self._set_root_cause(
                AudioErrorType.HTTP_403,
                FFmpegResponseAction.RETRY_NEW_URL,
                'The remote server rejected the current signed stream URL (HTTP 403).',
                line,
            )
        elif status_code == 404:
            # This is a failure on the resolved media URL, not necessarily on
            # the YouTube watch page itself. Treat it as serious enough to skip
            # for now, but not strong enough on its own to mutate the queue.
            self._set_root_cause(
                AudioErrorType.HTTP_404,
                FFmpegResponseAction.REMOVE,
                'The remote stream no longer exists (HTTP 404).',
                line,
                prompt_preference=TrackIssuePromptPreference.PREFER_REMOVE,
            )
        elif status_code == 410:
            # Stronger than 404 on the media URL layer, but it is still FFmpeg
            # talking to the extracted audio resource. We keep the track and
            # surface the issue instead of assuming the queue entry is dead.
            self._set_root_cause(
                AudioErrorType.HTTP_410,
                FFmpegResponseAction.REMOVE,
                'The remote stream is permanently gone (HTTP 410).',
                line,
                prompt_preference=TrackIssuePromptPreference.PREFER_REMOVE,
            )
        elif status_code == 416:
            # Requested Range Not Satisfiable usually points to a mismatch
            # between FFmpeg's byte-range request and the current media object.
            # This is a playback/pathology issue, not a strong unavailable
            # signal, so it stays in the explicit failure bucket.
            self._set_root_cause(
                AudioErrorType.HTTP_416,
                FFmpegResponseAction.FAIL,
                'FFmpeg requested an invalid byte range for the stream (HTTP 416).',
                line,
                prompt_preference=TrackIssuePromptPreference.PREFER_SKIP,
            )
        elif status_code == 429:
            self._set_root_cause(
                AudioErrorType.HTTP_429,
                FFmpegResponseAction.RETRY_WITH_BACKOFF,
                'The remote server rate-limited the stream request (HTTP 429).',
                line,
            )
        else:
            self._set_root_cause(
                AudioErrorType.HTTP_OTHER,
                FFmpegResponseAction.RETRY_WITH_BACKOFF if 500 <= status_code < 600 else FFmpegResponseAction.RETRY_NEW_URL,
                f'FFmpeg received HTTP {status_code} while reading the stream.',
                line,
            )

    def _set_root_cause(
        self,
        error_type: AudioErrorType,
        response_action: FFmpegResponseAction,
        summary: str,
        detail: str,
        prompt_preference: TrackIssuePromptPreference = TrackIssuePromptPreference.NONE,
    ) -> None:
        if self._root_cause_locked:
            return

        self._report.error_type = error_type
        self._report.response_action = response_action
        self._report.prompt_preference = prompt_preference
        self._report.summary = summary
        self._report.error_detail = detail
        self._root_cause_locked = True

    def _apply_unknown_prefetch_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.response_action = FFmpegResponseAction.RETRY_NEW_URL
        self._report.summary = (
            'FFmpeg stopped before the prefetched stream proved stable; '
            'a fresh URL should be fetched at play time.'
        )

    def _apply_unknown_playback_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.response_action = FFmpegResponseAction.RETRY_NEW_URL
        self._report.prompt_preference = TrackIssuePromptPreference.NONE
        self._report.summary = (
            'FFmpeg reported a playback failure without a recognized stderr signature; '
            'retrying with a fresh stream URL is the safest fallback.'
        )

    def _apply_unknown_terminal_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.response_action = FFmpegResponseAction.FAIL
        self._report.prompt_preference = TrackIssuePromptPreference.PREFER_SKIP
        self._report.summary = 'FFmpeg stopped unexpectedly without a recognizable error signature.'

    def _looks_like_normal_completion(
        self,
        expected_duration: Optional[int],
        frames_read: int,
        process_returncode: Optional[int],
    ) -> bool:
        if self._report.saw_final_stats or self._report.saw_normal_exit:
            return True

        if process_returncode == 0:
            return True

        if expected_duration is None or expected_duration <= 0:
            return False

        expected_frames = max(expected_duration * 50, 1)
        return (frames_read / expected_frames) >= 0.9

    def _should_use_fast_fail_heuristic(
        self,
        elapsed: Optional[float],
        expected_duration: Optional[int],
    ) -> bool:
        if elapsed is None or expected_duration is None:
            return False
        return elapsed < 3.0 and expected_duration > 10

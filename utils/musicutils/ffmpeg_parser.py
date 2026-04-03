"""Stateful FFmpeg stderr parsing for music playback."""

import re
from typing import Optional

from utils.musicutils.music_data import AudioErrorType, FFmpegBucket, FFmpegHealth


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

FORMAT_PATTERNS = (
    'moov atom not found',
    'invalid data found when processing input',
    'invalid stream',
)


class FFmpegStderrParser:
    """Incrementally parses FFmpeg stderr into a structured report.

    Each stderr line is consumed and pattern-matched against known FFmpeg
    errors.  The first match locks in a root cause (error_type) and an
    action bucket.  ``finalize()`` applies heuristics when no pattern
    matched.

    The parser assigns one of five buckets:

    - DONE   — Normal completion or intentional cancellation.
    - RETRY  — Get a fresh URL and try again.
    - REPLAY — URL is probably fine, play it again.
    - SKIP   — Prompt user, default to skip.
    - REMOVE — Prompt user, default to remove (nothing currently maps here).
    """

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
            # FFmpeg exhausted its own internal reconnects.  The URL is
            # probably still valid — the network just died.
            self._set_root_cause(
                AudioErrorType.CONNECTION,
                FFmpegBucket.REPLAY,
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
                FFmpegBucket.DONE,
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
                FFmpegBucket.REPLAY,
                'FFmpeg lost the network connection while reading the stream.',
                stripped,
            )
            return

        if any(pattern in lowered for pattern in TLS_PATTERNS):
            self._set_root_cause(
                AudioErrorType.TLS,
                FFmpegBucket.REPLAY,
                'FFmpeg hit a TLS or socket-layer read failure.',
                stripped,
            )
            return

        if any(pattern in lowered for pattern in FORMAT_PATTERNS):
            self._set_root_cause(
                AudioErrorType.FORMAT,
                FFmpegBucket.RETRY,
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

        if self._root_cause_locked:
            return self._report

        if context == 'prefetch':
            self._apply_unknown_prefetch_failure()
        elif explicit_error:
            self._apply_unknown_playback_failure()
        elif self._should_use_fast_fail_heuristic(elapsed, expected_duration):
            self._report.used_heuristic = True
            self._report.error_type = AudioErrorType.HTTP_403
            self._report.bucket = FFmpegBucket.RETRY
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
        match status_code:
            case 403:
                self._set_root_cause(
                    AudioErrorType.HTTP_403,
                    FFmpegBucket.RETRY,
                    'The remote server rejected the current signed stream URL (HTTP 403).',
                    line,
                )
            case 404 | 410:
                # CDN media URL, not the YouTube watch page.  The track
                # itself is probably fine — yt-dlp will confirm if the
                # watch page is actually dead during the retry.
                self._set_root_cause(
                    AudioErrorType.HTTP_404 if status_code == 404 else AudioErrorType.HTTP_410,
                    FFmpegBucket.RETRY,
                    f'The CDN stream returned HTTP {status_code}.',
                    line,
                )
            case 416:
                self._set_root_cause(
                    AudioErrorType.HTTP_416,
                    FFmpegBucket.SKIP,
                    'FFmpeg requested an invalid byte range for the stream (HTTP 416).',
                    line,
                )
            case 429:
                self._set_root_cause(
                    AudioErrorType.HTTP_429,
                    FFmpegBucket.RETRY,
                    'The remote server rate-limited the stream request (HTTP 429).',
                    line,
                )
            case _:
                self._set_root_cause(
                    AudioErrorType.HTTP_OTHER,
                    FFmpegBucket.RETRY,
                    f'FFmpeg received HTTP {status_code} while reading the stream.',
                    line,
            )

    def _set_root_cause(
        self,
        error_type: AudioErrorType,
        bucket: FFmpegBucket,
        summary: str,
        detail: str,
    ) -> None:
        if self._root_cause_locked:
            return

        self._report.error_type = error_type
        self._report.bucket = bucket
        self._report.summary = summary
        self._report.error_detail = detail
        self._root_cause_locked = True

    def _apply_unknown_prefetch_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.bucket = FFmpegBucket.RETRY
        self._report.summary = (
            'FFmpeg stopped before the prefetched stream proved stable; '
            'a fresh URL should be fetched at play time.'
        )

    def _apply_unknown_playback_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.bucket = FFmpegBucket.RETRY
        self._report.summary = (
            'FFmpeg reported a playback failure without a recognized stderr signature; '
            'retrying with a fresh stream URL is the safest fallback.'
        )

    def _apply_unknown_terminal_failure(self) -> None:
        self._report.used_heuristic = True
        self._report.error_type = AudioErrorType.UNKNOWN
        self._report.bucket = FFmpegBucket.SKIP
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

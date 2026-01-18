"""
logging_config.py

This module configures the logging for the entire application.
It sets up a structured logging format that includes a timestamp, log level,
logger name, and the message. It also configures file-based logging with
log rotation to manage file sizes.

Additionally, this module provides the ResourceTracker class for monitoring
CPU and RAM usage over time, integrated with the logging system.
"""
import asyncio
import logging
import os
import queue
import re
import sys
import time
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Any, Dict, List, Literal, Optional

import psutil

import config


# Module-level listener reference for proper cleanup
_queue_listener: Optional[QueueListener] = None


def stop_queue_listener() -> None:
    """Stops the queue listener thread without closing file handlers.

    Call this before module purge on soft restart to prevent orphaned threads.
    The listener will be recreated when setup_logging() is called again.
    """
    global _queue_listener
    if _queue_listener is not None:
        _queue_listener.stop()
        _queue_listener = None


class CustomFormatter(logging.Formatter):
    """
    A custom log formatter that adds color codes to log levels for console output,
    making it easier to distinguish between different levels of severity.
    """
    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)"

    FORMATS = {
        logging.DEBUG: grey + format_str + reset,
        logging.INFO: grey + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


class NoisyAsyncioFilter(logging.Filter):
    """
    Filters out noisy asyncio errors that occur during shutdown on Windows,
    specifically 'Unclosed connector'. These are harmless side effects.
    """

    def filter(self, record):
        # Filter out the specific known noise
        if record.levelno == logging.ERROR and ('Unclosed connector' in record.getMessage() or 'Unclosed client session' in record.getMessage()):
            return False
        return True


class RegionStrippingFormatter(logging.Formatter):
    """Formatter that strips #region/#endregion markers while preserving content.

    These markers are useful for log file folding in VS Code/Notepad++ but add
    visual noise to console/journald output. This formatter removes the marker
    prefixes while keeping any meaningful content (like phase headers).

    Wraps another formatter (e.g., CustomFormatter) to handle the actual formatting
    after the message has been cleaned.
    """

    # Pattern matches #region or #endregion at line start, with optional trailing content
    _REGION_PATTERN = re.compile(r'^#(?:end)?region\s*', re.MULTILINE)

    def __init__(self, wrapped_formatter: logging.Formatter):
        """Initialize with a wrapped formatter for final output formatting.

        Args:
            wrapped_formatter: The formatter to use after stripping markers.
        """
        super().__init__()
        self._wrapped = wrapped_formatter

    def format(self, record: logging.LogRecord) -> str:
        """Strips region markers from the message, then delegates to wrapped formatter.

        Args:
            record: The log record to format.

        Returns:
            Formatted log string with region markers stripped from the message.
        """
        # Work on a copy to avoid affecting other handlers (like file handler)
        # that need the original message with markers intact
        record_copy = logging.makeLogRecord(record.__dict__)

        if isinstance(record_copy.msg, str):
            # Strip #region and #endregion prefixes from all lines
            cleaned = self._REGION_PATTERN.sub('', record_copy.msg)
            # Remove lines that became empty/whitespace-only after stripping
            lines = [line for line in cleaned.split('\n') if line.strip()]
            record_copy.msg = '\n'.join(lines)

        return self._wrapped.format(record_copy)


class ResourceTracker:
    """Tracks CPU and RAM usage over time, integrated with the logging system.

    This class monitors system resources at regular intervals and maintains
    an in-memory history for the current session. On shutdown, the history
    is appended to the log file.

    For snapshot history, CPU and RAM usage are averaged over each interval
    period (sampled every SAMPLE_INTERVAL seconds). Peak values are also
    tracked to detect spikes - if the peak exceeds the average by more than
    SPIKE_THRESHOLD_PERCENT, it's flagged in the snapshot.

    Attributes:
        interval_minutes: Minutes between automatic snapshots.
        usage_history: List of recorded usage snapshots.
        start_time: Timestamp when tracking began.
    """

    # How often to sample CPU/RAM for averaging (in seconds)
    SAMPLE_INTERVAL = 10

    # If peak exceeds average by this percentage, flag as a spike
    SPIKE_THRESHOLD_PERCENT = 50

    def __init__(self, interval_minutes: int = 15):
        """Initializes the ResourceTracker.

        Args:
            interval_minutes: Minutes between automatic snapshots.
        """
        self.process = psutil.Process()
        self.process.cpu_percent()  # Prime the first reading for accuracy
        self._cpu_count = psutil.cpu_count() or 1  # For normalizing CPU % to 0-100 scale
        self.usage_history: List[Dict[str, Any]] = []
        self.interval_minutes = interval_minutes
        self._tracking_task: Optional[asyncio.Task] = None
        self._sampling_task: Optional[asyncio.Task] = None
        # Accumulated samples for averaging
        self._cpu_samples: List[float] = []
        self._ram_samples: List[float] = []
        self._ram_private_samples: List[float] = []
        self._ram_swap_samples: List[float] = []
        # Peak tracking for spike detection
        self._cpu_peak: float = 0.0
        self._ram_peak: float = 0.0
        self._samples_lock = asyncio.Lock()
        self.start_time: float = time.time()
        self._logger = logging.getLogger("logging")

    def _get_instantaneous_cpu(self) -> float:
        """Gets an instantaneous CPU reading (blocking, ~0.5s).

        This method blocks for a short interval to measure actual CPU usage.
        Should be called via asyncio.to_thread() from async contexts.
        Uses 0.5s interval for reliable cross-platform readings (0.1s seems too short on Windows).

        Returns:
            CPU usage percentage (normalized to 0-100% scale).
        """
        # Normalize to 0-100% scale (Linux reports per-core summed, e.g., 400% on 4 cores)
        return self.process.cpu_percent(interval=0.5) / self._cpu_count
    async def _get_memory_stats_async(self) -> Dict[str, float]:
        """Gets current memory statistics (async-safe).

        Runs memory_full_info() in a thread pool since it reads /proc on Linux.

        Returns:
            Dict with 'ram' (RSS in MB), 'ram_private' (USS in MB),
            and 'ram_swap' (swap in MB, Linux only).
        """
        memory_info = await asyncio.to_thread(self.process.memory_full_info)
        return {
            'ram': memory_info.rss / (1024 * 1024),
            'ram_private': memory_info.uss / (1024 * 1024),
            'ram_swap': getattr(memory_info, 'swap', 0) / (1024 * 1024),
        }
    async def get_current_usage_async(self) -> Dict[str, float]:
        """Returns LIVE, accurate CPU and RAM usage (async-safe).

        This method measures CPU usage over a 0.5 second interval in a thread pool
        to avoid blocking the event loop while providing accurate readings.

        Returns:
            Dict containing 'cpu' (percentage), 'ram' (RSS in MB),
            'ram_private' (USS in MB), and 'ram_swap' (swap in MB, Linux only).
        """
        try:
            # Run both in parallel since they're independent
            cpu_task = asyncio.to_thread(self._get_instantaneous_cpu)
            mem_task = self._get_memory_stats_async()
            cpu_usage, mem_stats = await asyncio.gather(cpu_task, mem_task)

            return {
                'cpu': cpu_usage,
                **mem_stats,
            }
        except Exception as e:
            self._logger.error(f"Error getting current usage (async): {e}")
            return {'cpu': 0.0, 'ram': 0.0, 'ram_private': 0.0, 'ram_swap': 0.0}

    async def _sample_resources(self) -> None:
        """Takes a single CPU and RAM sample and adds them to the accumulators."""
        try:
            cpu_task = asyncio.to_thread(self._get_instantaneous_cpu)
            mem_task = self._get_memory_stats_async()
            cpu, mem_stats = await asyncio.gather(cpu_task, mem_task)

            async with self._samples_lock:
                self._cpu_samples.append(cpu)
                self._ram_samples.append(mem_stats['ram'])
                self._ram_private_samples.append(mem_stats['ram_private'])
                self._ram_swap_samples.append(mem_stats['ram_swap'])
                # Track peaks for spike detection
                self._cpu_peak = max(self._cpu_peak, cpu)
                self._ram_peak = max(self._ram_peak, mem_stats['ram'])
        except Exception as e:
            self._logger.debug(f"Error sampling resources: {e}")

    async def _get_averaged_stats(self) -> tuple[float, Dict[str, float], float, float]:
        """Returns averaged CPU and RAM stats from accumulated samples and clears them.

        If no samples are available, takes instantaneous readings.

        Returns:
            Tuple of (cpu_average, memory_stats_dict, cpu_peak, ram_peak).
        """
        async with self._samples_lock:
            if self._cpu_samples:
                cpu_avg = sum(self._cpu_samples) / len(self._cpu_samples)
                ram_avg = sum(self._ram_samples) / len(self._ram_samples)
                ram_private_avg = sum(self._ram_private_samples) / len(self._ram_private_samples)
                ram_swap_avg = sum(self._ram_swap_samples) / len(self._ram_swap_samples)
                cpu_peak = self._cpu_peak
                ram_peak = self._ram_peak

                self._cpu_samples.clear()
                self._ram_samples.clear()
                self._ram_private_samples.clear()
                self._ram_swap_samples.clear()
                self._cpu_peak = 0.0
                self._ram_peak = 0.0

                return cpu_avg, {
                    'ram': ram_avg,
                    'ram_private': ram_private_avg,
                    'ram_swap': ram_swap_avg,
                }, cpu_peak, ram_peak

        # Fallback to instantaneous if no samples (no peaks to report)
        cpu = await asyncio.to_thread(self._get_instantaneous_cpu)
        mem_stats = await self._get_memory_stats_async()
        return cpu, mem_stats, cpu, mem_stats['ram']

    async def take_snapshot_async(self, label: Optional[str] = None) -> None:
        """Records a snapshot of current resource usage to history (async).

        For regular snapshots, uses averaged CPU/RAM from accumulated samples.
        For labeled snapshots (Startup/Shutdown), uses instantaneous readings.

        Reports both RSS (resident in physical RAM) and USS (unique private memory).
        RSS can drop when memory is paged out under pressure, while USS stays
        constant - useful for detecting paging vs actual leaks.

        Args:
            label: Optional label for the snapshot (e.g., 'Startup', 'Shutdown').
        """
        try:
            # For labeled snapshots (startup/shutdown), use instantaneous reading
            # For regular interval snapshots, use averaged values with peak tracking
            cpu_peak: Optional[float] = None
            ram_peak: Optional[float] = None

            if label:
                cpu_usage = await asyncio.to_thread(self._get_instantaneous_cpu)
                mem_stats = await self._get_memory_stats_async()
            else:
                cpu_usage, mem_stats, cpu_peak, ram_peak = await self._get_averaged_stats()

            ram_rss = mem_stats['ram']
            ram_private = mem_stats['ram_private']
            ram_swap = mem_stats['ram_swap']

            # Detect spikes (peak significantly exceeds average)
            threshold = self.SPIKE_THRESHOLD_PERCENT / 100
            cpu_spike = cpu_peak is not None and cpu_usage > 0 and (cpu_peak - cpu_usage) / cpu_usage > threshold
            ram_spike = ram_peak is not None and ram_rss > 0 and (ram_peak - ram_rss) / ram_rss > threshold

            timestamp = datetime.utcnow()
            self.usage_history.append({
                'timestamp': timestamp,
                'cpu': cpu_usage,
                'ram': ram_rss,
                'ram_private': ram_private,
                'ram_swap': ram_swap,
                'cpu_peak': cpu_peak,
                'ram_peak': ram_peak,
                'cpu_spike': cpu_spike,
                'ram_spike': ram_spike,
                'label': label
            })

            # Build log message with spike indicators
            spike_info = []
            if cpu_spike:
                spike_info.append(f"CPU spike: {cpu_peak:.1f}%")
            if ram_spike:
                spike_info.append(f"RAM spike: {ram_peak:.1f}MB")
            spike_str = f" ⚠️ {', '.join(spike_info)}" if spike_info else ""

            # Platform-aware logging:
            # - Windows: Show private bytes (useful for paging detection)
            # - Linux: Show swap if any (RSS vs USS gap is just shared libs)
            if sys.platform == 'win32':
                self._logger.debug(f"Resource snapshot: CPU={cpu_usage:.1f}%, RAM={ram_rss:.2f}MB (private: {ram_private:.2f}MB), Label={label}{spike_str}")
            elif ram_swap > 0:
                self._logger.debug(f"Resource snapshot: CPU={cpu_usage:.1f}%, RAM={ram_rss:.2f}MB (paged: {ram_swap:.2f}MB), Label={label}{spike_str}")
            else:
                self._logger.debug(f"Resource snapshot: CPU={cpu_usage:.1f}%, RAM={ram_rss:.2f}MB, Label={label}{spike_str}")
        except Exception as e:
            self._logger.error(f"Error recording usage snapshot: {e}")

    def get_history(self) -> List[Dict[str, Any]]:
        """Returns the usage history list.

        Returns:
            List of snapshot dictionaries with timestamp, cpu, ram, and label.
        """
        return self.usage_history.copy()

    async def _sampling_loop(self) -> None:
        """Background task that samples CPU and RAM at regular intervals for averaging."""
        try:
            while True:
                await self._sample_resources()
                await asyncio.sleep(self.SAMPLE_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _tracking_loop(self) -> None:
        """Background task that takes snapshots at regular intervals."""
        try:
            # Wait 5 minutes before starting regular tracking
            await asyncio.sleep(300)
            while True:
                await self.take_snapshot_async()
                await asyncio.sleep(self.interval_minutes * 60)
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        """Starts the background tracking and sampling tasks.

        Should be called after the bot is ready. Takes an initial 'Startup' snapshot.
        Starts two background tasks:
        - Sampling task: collects CPU samples every CPU_SAMPLE_INTERVAL seconds
        - Tracking task: takes averaged snapshots every interval_minutes
        """
        await self.take_snapshot_async(label="Startup")
        self._sampling_task = asyncio.create_task(self._sampling_loop())
        self._tracking_task = asyncio.create_task(self._tracking_loop())
        self._logger.info(f"ResourceTracker started (interval: {self.interval_minutes} min, sampling: {self.SAMPLE_INTERVAL}s)")

    async def stop(self) -> None:
        """Stops tracking and takes a final 'Shutdown' snapshot.

        Also logs the full session history to the log file.
        """
        # Stop sampling task
        if self._sampling_task:
            self._sampling_task.cancel()
            try:
                await self._sampling_task
            except asyncio.CancelledError:
                pass
            self._sampling_task = None

        # Stop tracking task
        if self._tracking_task:
            self._tracking_task.cancel()
            try:
                await self._tracking_task
            except asyncio.CancelledError:
                pass
            self._tracking_task = None

        await self.take_snapshot_async(label="Shutdown")
        self._log_history_summary()
        self._logger.info("ResourceTracker stopped")

    def _log_history_summary(self) -> None:
        """Logs a compact session summary with detailed snapshot history."""
        if not self.usage_history:
            return

        # Extract key metrics
        startup = self.usage_history[0] if self.usage_history else None
        shutdown = self.usage_history[-1] if len(self.usage_history) > 1 else None

        # Calculate peak values across all snapshots (use per-interval peaks if available)
        peak_cpu = max(
            e.get('cpu_peak') or e['cpu']
            for e in self.usage_history
        )
        peak_ram = max(
            e.get('ram_peak') or e['ram']
            for e in self.usage_history
        )

        # Count spikes
        cpu_spikes = sum(1 for e in self.usage_history if e.get('cpu_spike'))
        ram_spikes = sum(1 for e in self.usage_history if e.get('ram_spike'))

        # Build compact summary
        parts = [f"Snapshots: {len(self.usage_history)}"]
        if startup:
            parts.append(f"Start: {startup['cpu']:.1f}% CPU, {startup['ram']:.1f}MB RAM")
        if shutdown and shutdown != startup:
            parts.append(f"End: {shutdown['cpu']:.1f}% CPU, {shutdown['ram']:.1f}MB RAM")
        parts.append(f"Peak: {peak_cpu:.1f}% CPU, {peak_ram:.1f}MB RAM")
        if cpu_spikes or ram_spikes:
            parts.append(f"Spikes: {cpu_spikes} CPU, {ram_spikes} RAM")

        # Build detailed snapshot table with fold markers
        snapshot_lines = [
            "",
            "#region ─── ResourceTracker Snapshots ───────────────────",
            f"  {'Timestamp':<19} | {'CPU %':>6} | {'Peak':>6} | {'RAM MB':>8} | {'Peak':>8} | Flags",
            "  " + "-" * 75
        ]
        for entry in self.usage_history:
            ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            cpu_peak_str = f"{entry['cpu_peak']:.1f}" if entry.get('cpu_peak') is not None else "-"
            ram_peak_str = f"{entry['ram_peak']:.1f}" if entry.get('ram_peak') is not None else "-"

            flags = []
            if entry.get('label'):
                flags.append(entry['label'])
            if entry.get('cpu_spike'):
                flags.append("⚠CPU")
            if entry.get('ram_spike'):
                flags.append("⚠RAM")
            flags_str = " ".join(flags)

            snapshot_lines.append(
                f"  {ts:<19} | {entry['cpu']:>6.1f} | {cpu_peak_str:>6} | {entry['ram']:>8.1f} | {ram_peak_str:>8} | {flags_str}"
            )
        snapshot_lines.append("#endregion ResourceTracker Snapshots")
        snapshot_lines.append("")  # Trailing blank line for consistency

        self._logger.info(f"Session summary: {' | '.join(parts)}\n" + "\n".join(snapshot_lines))

    def format_history_for_export(self) -> str:
        """Formats history as a string for file export.

        Returns:
            Formatted string representation of the usage history.
        """
        if not self.usage_history:
            return "No historical data recorded."

        lines = [f"{'Timestamp':<25} | {'CPU (%)':>8} | {'CPU Peak':>9} | {'RAM (MB)':>9} | {'RAM Peak':>9} | Flags"]
        lines.append("-" * 95)

        for entry in self.usage_history:
            ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            cpu_peak_str = f"{entry['cpu_peak']:.1f}" if entry.get('cpu_peak') is not None else "-"
            ram_peak_str = f"{entry['ram_peak']:.1f}" if entry.get('ram_peak') is not None else "-"

            flags = []
            if entry.get('label'):
                flags.append(entry['label'])
            if entry.get('cpu_spike'):
                flags.append("⚠CPU")
            if entry.get('ram_spike'):
                flags.append("⚠RAM")
            flags_str = " ".join(flags)

            lines.append(
                f"{ts:<25} | {entry['cpu']:>8.1f} | {cpu_peak_str:>9} | {entry['ram']:>9.2f} | {ram_peak_str:>9} | {flags_str}"
            )

        return "\n".join(lines)


def cleanup_old_logs(logs_dir: str, retention_count: int) -> None:
    """Removes oldest log files beyond the retention count.

    Only removes completed logs (those with 'runtime' in filename).
    The current active log (without runtime) is preserved.

    Args:
        logs_dir: Path to the logs directory.
        retention_count: Number of completed logs to keep.
    """
    if not os.path.exists(logs_dir):
        return

    try:
        # Find completed logs (those with runtime in filename)
        completed_logs = [
            f for f in os.listdir(logs_dir)
            if f.endswith('.log') and 'runtime' in f
        ]

        # Sort by modification time (oldest first)
        completed_logs.sort(key=lambda x: os.path.getmtime(os.path.join(logs_dir, x)))

        # Remove oldest logs beyond retention count
        while len(completed_logs) > retention_count:
            file_to_remove = completed_logs.pop(0)
            os.remove(os.path.join(logs_dir, file_to_remove))
            logging.info(f"Removed old log file: {file_to_remove}")

    except Exception as e:
        logging.error(f"Error cleaning up old logs: {e}")


def _close_file_handlers() -> None:
    """Closes and removes all file handlers from the root logger.

    This must be called before renaming the log file to release the file lock.
    Also stops the QueueListener to ensure all queued log records are flushed.
    """
    global _queue_listener

    # Stop the queue listener first to flush any pending records
    if _queue_listener is not None:
        _queue_listener.stop()
        _queue_listener = None

    root_logger = logging.getLogger()
    handlers_to_remove = []

    for handler in root_logger.handlers[:]:
        if isinstance(handler, QueueHandler):
            handlers_to_remove.append(handler)
        elif isinstance(handler, logging.FileHandler):
            handler.close()
            handlers_to_remove.append(handler)

    for handler in handlers_to_remove:
        root_logger.removeHandler(handler)


def finalize_log(log_path: str, runtime_seconds: float) -> Optional[str]:
    """Renames the log file to include runtime in the filename.

    Args:
        log_path: Current path to the log file.
        runtime_seconds: Total runtime in seconds.

    Returns:
        The new log file path, or None if renaming failed.
    """
    if not os.path.exists(log_path):
        logging.warning(f"Log file not found for finalization: {log_path}")
        return None

    # Close file handlers to release the file lock before renaming
    _close_file_handlers()

    try:
        # Format runtime as Xh-Ym-Zs
        hours, remainder = divmod(int(runtime_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{hours}h-{minutes}m-{seconds}s"

        # Build new filename by inserting runtime before .log extension
        base, ext = os.path.splitext(log_path)
        new_path = f"{base}_runtime-{runtime_str}{ext}"

        os.rename(log_path, new_path)
        logging.info(f"Log file finalized: {os.path.basename(new_path)}")
        return new_path

    except Exception as e:
        logging.error(f"Error finalizing log file: {e}")
        return None


def setup_logging(
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    log_to_file: bool = True,
    logs_dir: Optional[str] = None,
    bot_name: Optional[str] = None,
    retention_count: int = 10,
    existing_log_path: Optional[str] = None
) -> Optional[str]:
    """Sets up logging for the entire application.

    This function configures:
    - A console handler with colored output for immediate feedback.
    - An asynchronous, rotating file handler to save logs to a timestamped file
      without blocking the bot's operations.
    - Clears any existing handlers to prevent duplicate log entries.
    - Sets the log levels for noisy libraries like discord.py to a higher
      threshold to reduce spam.
    - Cleans up old log files beyond the retention count.

    Args:
        level: The logging level.
        log_to_file: Whether to log to a file.
        logs_dir: Directory to store log files.
        bot_name: Bot name for log filename prefix.
        retention_count: Number of completed logs to retain.
        existing_log_path: Path to an existing log file to continue writing to
            (used on soft restarts to keep logs in a single file).

    Returns:
        The path to the created log file, or None if file logging is disabled.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()  # Prevent duplicate logs if called multiple times.

    # Stop any existing queue listener from a previous setup call
    global _queue_listener
    if _queue_listener is not None:
        _queue_listener.stop()
        _queue_listener = None

    # Console Handler
    # Use RegionStrippingFormatter to remove fold markers while keeping headers
    # This applies to both interactive terminals AND journald/pipes
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(RegionStrippingFormatter(CustomFormatter()))
    root_logger.addHandler(console_handler)

    log_file_path: Optional[str] = None

    # Queue-based File Handler (preserves log ordering while being non-blocking)
    if log_to_file:
        if not logs_dir:
            raise ValueError("logs_dir must be provided when log_to_file is True.")
        if not bot_name:
            raise ValueError("bot_name must be provided when log_to_file is True.")

        # Create logs directory if it doesn't exist
        os.makedirs(logs_dir, exist_ok=True)

        # Reuse existing log file on restart, or create a new one
        if existing_log_path and os.path.exists(existing_log_path):
            log_file_path = existing_log_path
        else:
            # Clean up old logs before creating new one (only on fresh start)
            cleanup_old_logs(logs_dir, retention_count)

            # Create timestamped log filename
            timestamp_str = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
            log_filename = f"{bot_name}_{timestamp_str}.log"
            log_file_path = os.path.join(logs_dir, log_filename)

        # Create the actual file handler (runs in background thread via QueueListener)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=2,        # Keep 2 backup files
            encoding='utf-8'
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(funcName)s:%(lineno)d] - %(message)s'
        ))

        # Use QueueHandler + QueueListener pattern:
        # - QueueHandler puts records into a queue (fast, non-blocking)
        # - QueueListener runs in a background thread, consuming records in order
        # This preserves log ordering while not blocking the asyncio event loop.
        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)  # Unbounded queue
        queue_handler = QueueHandler(log_queue)
        root_logger.addHandler(queue_handler)

        # Start the listener thread that processes the queue
        _queue_listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
        _queue_listener.start()

    # Reduce noise from third-party libraries.
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)

    # Filter out harmless asyncio noise
    logging.getLogger('asyncio').addFilter(NoisyAsyncioFilter())

    root_logger.info("Logging configured with console and rotating file handlers.")

    return log_file_path

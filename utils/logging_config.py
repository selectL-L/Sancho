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
import sys
import time
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Any, Dict, List, Literal, Optional

import psutil


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


class ResourceTracker:
    """Tracks CPU and RAM usage over time, integrated with the logging system.

    This class monitors system resources at regular intervals and maintains
    an in-memory history for the current session. On shutdown, the history
    is appended to the log file.

    For snapshot history, CPU usage is averaged over each interval period
    (sampled every 30 seconds) to provide a representative view of resource
    consumption, rather than a single point-in-time measurement.

    Attributes:
        interval_minutes: Minutes between automatic snapshots.
        usage_history: List of recorded usage snapshots.
        start_time: Timestamp when tracking began.
    """

    # How often to sample CPU for averaging (in seconds)
    CPU_SAMPLE_INTERVAL = 30

    def __init__(self, interval_minutes: int = 15):
        """Initializes the ResourceTracker.

        Args:
            interval_minutes: Minutes between automatic snapshots.
        """
        self.process = psutil.Process()
        self.process.cpu_percent()  # Prime the first reading for accuracy
        self.usage_history: List[Dict[str, Any]] = []
        self.interval_minutes = interval_minutes
        self._tracking_task: Optional[asyncio.Task] = None
        self._sampling_task: Optional[asyncio.Task] = None
        self._cpu_samples: List[float] = []  # Accumulated CPU samples for averaging
        self._cpu_samples_lock = asyncio.Lock()
        self.start_time: float = time.time()
        self._logger = logging.getLogger("logging")

    def _get_instantaneous_cpu(self) -> float:
        """Gets an instantaneous CPU reading (blocking, ~0.1s).

        This method blocks for a short interval to measure actual CPU usage.
        Should be called via asyncio.to_thread() from async contexts.

        Returns:
            CPU usage percentage.
        """
        return self.process.cpu_percent(interval=0.1)

    def _get_non_blocking_cpu(self) -> float:
        """Gets CPU usage since last call (non-blocking).

        Returns the CPU percentage since the previous call to any cpu_percent method.
        Used for sampling/averaging purposes.

        Returns:
            CPU usage percentage since last measurement.
        """
        return self.process.cpu_percent(interval=None)

    def get_current_usage(self) -> Dict[str, float]:
        """Returns current RAM usage and a non-blocking CPU sample.

        Note: For accurate instantaneous CPU readings, use get_current_usage_async().
        This method is intended for internal sampling where blocking is not acceptable.

        Returns:
            Dict containing 'cpu' (percentage since last call) and 'ram' (MB) keys.
        """
        try:
            memory_info = self.process.memory_info()
            cpu_usage = self._get_non_blocking_cpu()
            ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB
            return {'cpu': cpu_usage, 'ram': ram_usage}
        except Exception as e:
            self._logger.error(f"Error getting current usage: {e}")
            return {'cpu': 0.0, 'ram': 0.0}

    async def get_current_usage_async(self) -> Dict[str, float]:
        """Returns LIVE, accurate CPU and RAM usage (async-safe).

        This method measures CPU usage over a 0.1 second interval in a thread pool
        to avoid blocking the event loop while providing accurate readings.

        Returns:
            Dict containing 'cpu' (percentage) and 'ram' (MB) keys.
        """
        try:
            memory_info = self.process.memory_info()
            # Run blocking CPU measurement in thread pool
            cpu_usage = await asyncio.to_thread(self._get_instantaneous_cpu)
            ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB
            return {'cpu': cpu_usage, 'ram': ram_usage}
        except Exception as e:
            self._logger.error(f"Error getting current usage (async): {e}")
            return {'cpu': 0.0, 'ram': 0.0}

    async def _sample_cpu(self) -> None:
        """Takes a single CPU sample and adds it to the accumulator."""
        try:
            cpu = await asyncio.to_thread(self._get_instantaneous_cpu)
            async with self._cpu_samples_lock:
                self._cpu_samples.append(cpu)
        except Exception as e:
            self._logger.debug(f"Error sampling CPU: {e}")

    async def _get_averaged_cpu(self) -> float:
        """Returns the average of accumulated CPU samples and clears them.

        If no samples are available, takes an instantaneous reading.

        Returns:
            Average CPU usage percentage.
        """
        async with self._cpu_samples_lock:
            if self._cpu_samples:
                avg = sum(self._cpu_samples) / len(self._cpu_samples)
                self._cpu_samples.clear()
                return avg
        # Fallback to instantaneous if no samples
        return await asyncio.to_thread(self._get_instantaneous_cpu)

    async def take_snapshot_async(self, label: Optional[str] = None) -> None:
        """Records a snapshot of current resource usage to history (async).

        For regular snapshots, uses averaged CPU from accumulated samples.
        For labeled snapshots (Startup/Shutdown), uses instantaneous readings.

        Args:
            label: Optional label for the snapshot (e.g., 'Startup', 'Shutdown').
        """
        try:
            memory_info = self.process.memory_info()
            ram_usage = memory_info.rss / (1024 * 1024)

            # For labeled snapshots (startup/shutdown), use instantaneous reading
            # For regular interval snapshots, use averaged CPU
            if label:
                cpu_usage = await asyncio.to_thread(self._get_instantaneous_cpu)
            else:
                cpu_usage = await self._get_averaged_cpu()

            timestamp = datetime.utcnow()
            self.usage_history.append({
                'timestamp': timestamp,
                'cpu': cpu_usage,
                'ram': ram_usage,
                'label': label
            })
            self._logger.debug(f"Resource snapshot: CPU={cpu_usage:.1f}%, RAM={ram_usage:.2f}MB, Label={label}")
        except Exception as e:
            self._logger.error(f"Error recording usage snapshot: {e}")

    def get_history(self) -> List[Dict[str, Any]]:
        """Returns the usage history list.

        Returns:
            List of snapshot dictionaries with timestamp, cpu, ram, and label.
        """
        return self.usage_history.copy()

    async def _sampling_loop(self) -> None:
        """Background task that samples CPU at regular intervals for averaging."""
        try:
            while True:
                await self._sample_cpu()
                await asyncio.sleep(self.CPU_SAMPLE_INTERVAL)
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
        self._logger.info(f"ResourceTracker started (interval: {self.interval_minutes} min, sampling: {self.CPU_SAMPLE_INTERVAL}s)")

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
        """Logs a compact session summary."""
        if not self.usage_history:
            return

        # Extract key metrics
        startup = self.usage_history[0] if self.usage_history else None
        shutdown = self.usage_history[-1] if len(self.usage_history) > 1 else None

        # Calculate peak values across all snapshots
        peak_cpu = max(e['cpu'] for e in self.usage_history)
        peak_ram = max(e['ram'] for e in self.usage_history)

        # Build compact summary
        parts = [f"Snapshots: {len(self.usage_history)}"]
        if startup:
            parts.append(f"Start: {startup['cpu']:.1f}% CPU, {startup['ram']:.1f}MB RAM")
        if shutdown and shutdown != startup:
            parts.append(f"End: {shutdown['cpu']:.1f}% CPU, {shutdown['ram']:.1f}MB RAM")
        parts.append(f"Peak: {peak_cpu:.1f}% CPU, {peak_ram:.1f}MB RAM")

        self._logger.info(f"Session summary: {' | '.join(parts)}")

    def format_history_for_export(self) -> str:
        """Formats history as a string for file export.

        Returns:
            Formatted string representation of the usage history.
        """
        if not self.usage_history:
            return "No historical data recorded."

        lines = [f"{'Timestamp':<25} | {'CPU (%)':<10} | {'RAM (MB)':<10} | {'Label':<15}"]
        lines.append("-" * 70)

        for entry in self.usage_history:
            ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            label = entry.get('label') or ""
            lines.append(f"{ts:<25} | {entry['cpu']:<10.1f} | {entry['ram']:<10.2f} | {label:<15}")

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
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(CustomFormatter())
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
            maxBytes=5*1024*1024,  # 5 MB per file
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

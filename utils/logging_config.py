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
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Literal, Optional

import psutil


class AsyncFileHandler(logging.Handler):
    """
    A logging handler that writes to a file asynchronously in a separate thread,
    preventing it from blocking the asyncio event loop.
    """

    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0, encoding=None, delay=False):
        super().__init__()
        # The underlying handler is the synchronous one that does the actual file I/O.
        self._handler = RotatingFileHandler(filename, mode, maxBytes, backupCount, encoding='utf-8', delay=delay)

    def setFormatter(self, fmt):
        """Set the formatter for this handler."""
        super().setFormatter(fmt)
        self._handler.setFormatter(fmt)

    def emit(self, record):
        """
        Emit a record by scheduling the write operation in a separate thread
        to avoid blocking the main asyncio event loop.
        """
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(asyncio.to_thread(self._handler.emit, record))
            else:
                # Fallback to synchronous logging if no event loop is running.
                self._handler.emit(record)
        except RuntimeError:
            # This occurs if there's no running event loop.
            self._handler.emit(record)


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

    Attributes:
        interval_minutes: Minutes between automatic snapshots.
        usage_history: List of recorded usage snapshots.
        start_time: Timestamp when tracking began.
    """

    def __init__(self, interval_minutes: int = 15):
        """Initializes the ResourceTracker.

        Args:
            interval_minutes: Minutes between automatic snapshots.
        """
        self.process = psutil.Process()
        self.process.cpu_percent()  # Prime the first reading for accuracy
        self.usage_history: List[Dict[str, Any]] = []
        self.interval_minutes = interval_minutes
        self._task: Optional[asyncio.Task] = None
        self.start_time: float = time.time()
        self._logger = logging.getLogger("logging")

    def get_current_usage(self) -> Dict[str, float]:
        """Returns LIVE CPU and RAM usage (not cached).

        This method always fetches fresh values from the system.

        Returns:
            Dict containing 'cpu' (percentage) and 'ram' (MB) keys.
        """
        try:
            memory_info = self.process.memory_info()
            cpu_usage = self.process.cpu_percent(interval=None)
            ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB
            return {'cpu': cpu_usage, 'ram': ram_usage}
        except Exception as e:
            self._logger.error(f"Error getting current usage: {e}")
            return {'cpu': 0.0, 'ram': 0.0}

    def take_snapshot(self, label: Optional[str] = None) -> None:
        """Records a snapshot of current resource usage to history.

        Args:
            label: Optional label for the snapshot (e.g., 'Startup', 'Shutdown').
        """
        try:
            usage = self.get_current_usage()
            timestamp = datetime.utcnow()
            self.usage_history.append({
                'timestamp': timestamp,
                'cpu': usage['cpu'],
                'ram': usage['ram'],
                'label': label
            })
            self._logger.debug(f"Resource snapshot: CPU={usage['cpu']:.1f}%, RAM={usage['ram']:.2f}MB, Label={label}")
        except Exception as e:
            self._logger.error(f"Error recording usage snapshot: {e}")

    def get_history(self) -> List[Dict[str, Any]]:
        """Returns the usage history list.

        Returns:
            List of snapshot dictionaries with timestamp, cpu, ram, and label.
        """
        return self.usage_history.copy()

    async def _tracking_loop(self) -> None:
        """Background task that takes snapshots at regular intervals."""
        try:
            # Wait 5 minutes before starting regular tracking
            await asyncio.sleep(300)
            while True:
                self.take_snapshot()
                await asyncio.sleep(self.interval_minutes * 60)
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        """Starts the background tracking task.

        Should be called after the bot is ready. Takes an initial 'Startup' snapshot.
        """
        self.take_snapshot(label="Startup")
        self._task = asyncio.create_task(self._tracking_loop())
        self._logger.info(f"ResourceTracker started (interval: {self.interval_minutes} minutes)")

    async def stop(self) -> None:
        """Stops tracking and takes a final 'Shutdown' snapshot.

        Also logs the full session history to the log file.
        """
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self.take_snapshot(label="Shutdown")
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
    Handles both standard FileHandlers and AsyncFileHandler (which wraps a
    RotatingFileHandler internally).
    """
    root_logger = logging.getLogger()
    handlers_to_remove = []

    for handler in root_logger.handlers[:]:
        # Check for our custom AsyncFileHandler
        if isinstance(handler, AsyncFileHandler):
            # Close the internal RotatingFileHandler
            handler._handler.close()
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
    retention_count: int = 10
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

    Returns:
        The path to the created log file, or None if file logging is disabled.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()  # Prevent duplicate logs if called multiple times.

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(CustomFormatter())
    root_logger.addHandler(console_handler)

    log_file_path: Optional[str] = None

    # Asynchronous File Handler
    if log_to_file:
        if not logs_dir:
            raise ValueError("logs_dir must be provided when log_to_file is True.")
        if not bot_name:
            raise ValueError("bot_name must be provided when log_to_file is True.")

        # Create logs directory if it doesn't exist
        os.makedirs(logs_dir, exist_ok=True)

        # Clean up old logs before creating new one
        cleanup_old_logs(logs_dir, retention_count)

        # Create timestamped log filename
        timestamp_str = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
        log_filename = f"{bot_name}_{timestamp_str}.log"
        log_file_path = os.path.join(logs_dir, log_filename)

        # Use the async file handler to prevent I/O from blocking the event loop.
        file_handler = AsyncFileHandler(
            log_file_path,
            maxBytes=5*1024*1024,  # 5 MB per file
            backupCount=2         # Keep 2 backup files
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(funcName)s:%(lineno)d] - %(message)s'
        ))
        root_logger.addHandler(file_handler)

    # Reduce noise from third-party libraries.
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)

    # Filter out harmless asyncio noise
    logging.getLogger('asyncio').addFilter(NoisyAsyncioFilter())

    root_logger.info("Logging configured with console and rotating file handlers.")

    return log_file_path

"""
logging_config.py

This module configures the logging for the entire application.
It sets up a structured logging format that includes a timestamp, log level,
logger name, and the message. It also configures file-based logging with
log rotation to manage file sizes.
"""
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import asyncio
from typing import Literal

class AsyncFileHandler(logging.Handler):
    """
    A logging handler that writes to a file asynchronously in a separate thread,
    preventing it from blocking the asyncio event loop.
    """
    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0, encoding=None, delay=False):
        super().__init__()
        # The underlying handler is the synchronous one that does the actual file I/O.
        self._handler = RotatingFileHandler(filename, mode, maxBytes, backupCount, encoding, delay)

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

def setup_logging(
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    log_to_file: bool = True
):
    """
    Sets up logging for the entire application.

    This function configures:
    - A console handler with colored output for immediate feedback.
    - An asynchronous, rotating file handler to save logs to `sancho.log`
      without blocking the bot's operations.
    - Clears any existing handlers to prevent duplicate log entries.
    - Sets the log levels for noisy libraries like discord.py to a higher
      threshold to reduce spam.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear() # Prevent duplicate logs if called multiple times.

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(CustomFormatter())
    root_logger.addHandler(console_handler)

    # --- Asynchronous File Handler ---
    if log_to_file:
        # Use the async file handler to prevent I/O from blocking the event loop.
        file_handler = AsyncFileHandler(
            'sancho.log', 
            maxBytes=5*1024*1024, # 5 MB per file
            backupCount=2         # Keep 2 backup files
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(funcName)s:%(lineno)d] - %(message)s'
        ))
        root_logger.addHandler(file_handler)

    # Reduce noise from third-party libraries.
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)

    root_logger.info("Logging configured with console and rotating file handlers.")
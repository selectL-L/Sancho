"""
Module for setting up centralized logging for the application.
"""
import logging
import sys
import asyncio
from logging.handlers import RotatingFileHandler
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
        Emit a record.
        If an asyncio event loop is running, it schedules the log to be written
        in a separate thread. Otherwise, it writes the log synchronously.
        """
        try:
            # Check if an event loop is running
            loop = asyncio.get_running_loop()
            if loop.is_running():
                # If the loop is running, use the async approach
                loop.create_task(asyncio.to_thread(self._handler.emit, record))
            else:
                # If the loop is not running, fall back to synchronous logging
                self._handler.emit(record)
        except RuntimeError:
            # This exception is raised if there's no running event loop
            # Fall back to synchronous logging
            self._handler.emit(record)

# Custom formatter
class CustomFormatter(logging.Formatter):
    """
    A custom log formatter that adds color to log levels.
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
    Set up logging for the application.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear() # Clear existing handlers

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(CustomFormatter())
    root_logger.addHandler(console_handler)

    # File handler
    if log_to_file:
        # Use the async file handler to prevent blocking the event loop.
        file_handler = AsyncFileHandler(
            'sancho.log', 
            maxBytes=5*1024*1024, # 5 MB
            backupCount=2
        )
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(funcName)s:%(lineno)d] - %(message)s'))
        root_logger.addHandler(file_handler)

    # Set discord.py logger to a higher level to avoid spam
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)

    root_logger.info("Logging configured successfully with console and rotating file handlers.")
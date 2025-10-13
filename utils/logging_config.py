"""
Module for setting up centralized logging for the application.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from config import LOG_PATH, LOG_LEVEL, LOG_FORMAT, LOG_MAX_BYTES, LOG_BACKUP_COUNT

def setup_logging():
    """
    Configures the root logger for the application with console and rotating file handlers.
    This setup is designed to be called once at application startup.
    """
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    # Prevent duplicate handlers by clearing any existing ones
    if logger.hasHandlers():
        logger.handlers.clear()

    # Create a formatter
    formatter = logging.Formatter(LOG_FORMAT)

    # --- Console Handler ---
    # Logs messages to the console (stdout).
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # --- Rotating File Handler ---
    # Logs messages to a file, with automatic rotation when the file gets too large.
    # This prevents log files from growing indefinitely.
    try:
        file_handler = RotatingFileHandler(
            filename=LOG_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (IOError, FileNotFoundError) as e:
        # If file logging fails, log the error to the console and continue.
        logging.error(f"Failed to set up file logging at {LOG_PATH}: {e}", exc_info=True)

    # Log that logging has been successfully configured
    logging.info("Logging configured successfully with console and rotating file handlers.")

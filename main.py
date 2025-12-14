"""main.py

This is the primary entry point for the Bot. Its responsibilities are:
- Performing initial setup: logging, configuration validation from `info.env`.
- Instantiating the custom `CoreBot` class from `utils.bot_class`.
- Defining console and signal handlers for graceful startup and shutdown.
- Orchestrating the bot's asynchronous startup sequence via the `main()` function,
  which initializes the database, loads cogs, and connects to Discord.

This script acts as the "launcher" for the bot; the core logic, event handlers,
and command processing are defined within the `CoreBot` class itself.

Lifecycle Phases handled here:
    INIT: Logging, config validation, database setup
    LOAD: Cog module loading
"""

import asyncio
import importlib
import logging
import signal
import sys
import threading
from typing import Any

# Global queue for console input, shared across restarts
CONSOLE_QUEUE: asyncio.Queue = asyncio.Queue()


def console_reader(loop: asyncio.AbstractEventLoop) -> None:
    """Reads from stdin in a separate thread and puts lines into the queue.

    This runs forever to avoid blocking the main loop or creating zombie threads
    on restart (especially on Windows where stdin.readline is blocking).

    Args:
        loop (asyncio.AbstractEventLoop): The main event loop.
    """
    while True:
        try:
            line = sys.stdin.readline()
            if line:
                # Thread-safe put into the asyncio queue
                loop.call_soon_threadsafe(CONSOLE_QUEUE.put_nowait, line.strip())
        except Exception as e:
            # If stdin closes or errors, log it (though we can't do much from this thread)
            print(f"Console reader error: {e}", file=sys.stderr)
            break


async def console_consumer(bot: Any, shutdown_handler: Any, log_path: Any) -> None:
    """Consumes commands from the global console queue.

    Args:
        bot (CoreBot): The current bot instance.
        shutdown_handler (Callable): The function to call for graceful shutdown.
        log_path (str): Path to the current log file for finalization.
    """
    try:
        while True:
            line = await CONSOLE_QUEUE.get()
            if not line:
                continue

            command = line.lower()
            if command == 'exit':
                logging.info("'exit' command received from console.")
                # Lifecycle handler handles cog unloading, shutdown message, and cleanup
                await shutdown_handler(signal.SIGINT, bot, is_restart=False, log_path=log_path)
                break
            elif command == 'restart':
                logging.info("'restart' command received from console.")
                bot.restart_signal = True
                # Lifecycle handler handles cog unloading, restart message, and cleanup
                await shutdown_handler(signal.SIGINT, bot, is_restart=True, log_path=log_path)
                break
            elif command == 'reload':
                logging.info("'reload' command received. Reloading cogs...")
                await bot.reload_all_cogs()
            else:
                print(f"Unknown command: {command}")
    except asyncio.CancelledError:
        pass


async def run_bot_lifecycle() -> None:
    """Orchestrates the bot lifecycle loop, allowing for soft restarts."""

    # 1. Start the global console reader thread (ONCE)
    loop = asyncio.get_running_loop()
    reader_thread = threading.Thread(target=console_reader, args=(loop,), daemon=True)
    reader_thread.start()
    logging.info("Console reader thread started.")

    restart_count = 0

    while True:
        if restart_count > 0:
            logging.info(f"--- Soft Restart #{restart_count} in progress ---")
            # Small delay to ensure sockets close cleanly
            await asyncio.sleep(1)

        # 2. Dynamic Import / Reload
        # We import inside the loop to ensure we get fresh versions of the modules
        # if they were purged from sys.modules by the previous run.
        mod_bot = None
        mod_db = None
        mod_extensions = None
        mod_lifecycle = None
        mod_logging = None

        try:
            # Always ensure config is fresh first
            import config
            if restart_count > 0:
                importlib.reload(config)

            # Import utilities
            # We use import_module to bind them to local names cleanly
            mod_bot = importlib.import_module('utils.bot_class')
            mod_db = importlib.import_module('utils.database')
            mod_extensions = importlib.import_module('utils.extensions')
            mod_lifecycle = importlib.import_module('utils.lifecycle')
            mod_logging = importlib.import_module('utils.logging_config')

        except Exception as e:
            logging.critical(f"Failed to import modules during startup/restart: {e}", exc_info=True)
            # If we can't import code, we must exit to avoid a broken loop
            sys.exit(1)

        # Assertions to satisfy static analysis (Pylance)
        # Since we exit on failure above, these will always be true here.
        assert mod_bot is not None
        assert mod_db is not None
        assert mod_extensions is not None
        assert mod_lifecycle is not None
        assert mod_logging is not None

        # ─── INIT ───
        # Setup Logging (safe to call repeatedly as it clears existing handlers)
        log_level = "DEBUG" if config.DEV_MODE else "INFO"
        log_path = mod_logging.setup_logging(
            level=log_level,
            logs_dir=config.LOGS_DIR,
            bot_name=config.BOT_NAME,
            retention_count=config.LOG_RETENTION_COUNT
        )

        # ─── INIT ───
        mod_lifecycle.log_phase("INIT")

        # Configuration Validation
        if not config.TOKEN:
            logging.critical("DISCORD_TOKEN missing.")
            sys.exit("Critical error: DISCORD_TOKEN not configured.")

        # Initialize Bot (using the class from the potentially reloaded module)
        bot = mod_bot.CoreBot()

        # Initialize and attach resource tracker
        resource_tracker = mod_logging.ResourceTracker(interval_minutes=config.RESOURCE_TRACK_INTERVAL)
        bot.resource_tracker = resource_tracker

        # Attach database manager
        if config.DB_PATH is None:
            raise ValueError("DB_PATH cannot be None.")
        db_manager = await mod_db.DatabaseManager.create(config.DB_PATH)
        bot.db_manager = db_manager
        logging.info("Database connection established")

        # ─── LOAD ───
        mod_lifecycle.log_phase("LOAD")

        try:
            async with bot:
                cogs_to_load = mod_extensions.discover_cogs(config.COGS_PATH)
                logging.info(f"Found {len(cogs_to_load)} cogs to load")
                for extension in cogs_to_load:
                    try:
                        await bot.load_extension(extension)
                        # Extract cog name from extension path (e.g., "cogs.admin" -> "Admin")
                        cog_name = extension.split('.')[-1].title()
                        logging.info(f"  ✓ {cog_name}")
                    except Exception:
                        cog_name = extension.split('.')[-1].title()
                        logging.error(f"  ✗ {cog_name} - Failed to load", exc_info=True)

                # Start Console Consumer
                consumer_task = loop.create_task(console_consumer(bot, mod_lifecycle.shutdown_handler, log_path))

                # Setup signal handlers for this iteration
                # Windows doesn't support add_signal_handler fully, but we try for graceful SIGINT
                if sys.platform != "win32":
                    for s in (signal.SIGINT, signal.SIGTERM):
                        try:
                            loop.remove_signal_handler(s)  # Clear old handlers
                            loop.add_signal_handler(
                                s,
                                lambda s=s: asyncio.create_task(
                                    mod_lifecycle.shutdown_handler(s, bot, is_restart=False, log_path=log_path)
                                )
                            )
                        except NotImplementedError:
                            pass

                # Connect to Discord (CONNECT/READY/POST-READY phases handled in lifecycle.startup_handler)
                try:
                    logging.info(f"Connecting to Discord as {config.BOT_NAME}...")
                    await bot.start(config.TOKEN)
                except KeyboardInterrupt:
                    # Handle Ctrl+C directly if signal handler didn't catch it
                    pass
                finally:
                    # Cancel consumer so it stops looking at the queue for THIS bot instance
                    consumer_task.cancel()
                    try:
                        await consumer_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            logging.info("Bot task cancelled.")
        except Exception as e:
            logging.error(f"Bot encountered an error: {e}", exc_info=True)

        # Check for Restart Signal
        if bot.restart_signal:
            logging.info("Restart signal received. Purging modules...")
            mod_lifecycle.purge_modules()
            restart_count += 1
            # Loop continues -> Modules re-imported -> New Bot made
        else:
            logging.info("No restart signal. Exiting.")
            break

if __name__ == '__main__':
    try:
        # Check if we are potentially masking an existing event loop (e.g. in some IDEs)
        # But for authorized usage `python main.py`, asyncio.run is correct.
        asyncio.run(run_bot_lifecycle())
    except KeyboardInterrupt:
        # Catch hard exits
        pass
    finally:
        logging.info("Process terminated.")

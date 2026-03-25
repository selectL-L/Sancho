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
import time
from typing import Any


def _suppress_post_shutdown_exceptions(args: Any) -> None:
    """Suppresses aiohttp cleanup exceptions that occur after event loop closure.

    These exceptions are harmless - they occur when aiohttp's garbage collection
    runs after the event loop is closed. We suppress them to keep shutdown clean.
    """
    # Check if this is the specific "Event loop is closed" error from aiohttp cleanup
    if isinstance(args.exc_value, RuntimeError) and "Event loop is closed" in str(args.exc_value):
        return  # Suppress silently
    # For any other unraisable exception, use default behavior
    sys.__unraisablehook__(args)


# Install the hook to suppress post-shutdown aiohttp noise
sys.unraisablehook = _suppress_post_shutdown_exceptions

# Global queue for console input, shared across restarts
CONSOLE_QUEUE: asyncio.Queue = asyncio.Queue()


def console_reader(loop: asyncio.AbstractEventLoop) -> None:
    """Reads from stdin in a separate thread and puts lines into the queue.

    This runs forever to avoid blocking the main loop or creating zombie threads
    on restart (especially on Windows where stdin.readline is blocking).

    Platform behavior:
        - Windows: stdin stays open in interactive consoles; readline() blocks.
        - Linux (systemd): stdin is closed/redirected to /dev/null; readline()
          returns "" (EOF) immediately. We must detect EOF and exit to avoid
          a tight CPU-burning loop.

    Args:
        loop (asyncio.AbstractEventLoop): The main event loop.
    """
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                # EOF - stdin closed (common in background services on Linux)
                # Exit cleanly to avoid a tight CPU-burning loop
                break
            line = line.strip()
            if line:
                # Thread-safe put into the asyncio queue
                loop.call_soon_threadsafe(CONSOLE_QUEUE.put_nowait, line)
        except Exception as e:
            # If stdin closes or errors, log it (though we can't do much from this thread)
            print(f"Console reader error: {e}", file=sys.stderr)
            break


async def process_control_command(
    command: str, bot: Any, shutdown_handler: Any, log_path: Any,
    shutdown_reason_cls: Any = None
) -> str:
    """Processes a control command and returns a response.

    Args:
        command: The command string to process.
        bot: The current bot instance.
        shutdown_handler: The function to call for graceful shutdown.
        log_path: Path to the current log file for finalization.
        shutdown_reason_cls: The ShutdownReason enum class (passed from lifecycle module).

    Returns:
        A response string indicating the result.
    """
    command = command.lower().strip()

    if command == 'exit':
        logging.info("'exit' command received.")
        reason = shutdown_reason_cls.MANUAL_STOP if shutdown_reason_cls else None
        await shutdown_handler(signal.SIGINT, bot, reason=reason, log_path=log_path)
        return "OK: Shutting down"
    elif command == 'restart':
        logging.info("'restart' command received.")
        bot.restart_signal = True
        reason = shutdown_reason_cls.RESTART if shutdown_reason_cls else None
        await shutdown_handler(signal.SIGINT, bot, reason=reason, log_path=log_path)
        return "OK: Restarting"
    elif command == 'reload':
        logging.info("'reload' command received.")
        await bot.reload_all_cogs()
        return "OK: Cogs reloaded"
    elif command == 'status':
        return f"OK: {bot.user.name if bot.user else 'Bot'} is running"
    else:
        logging.warning(f"Unknown control command received: '{command}'")
        return f"ERROR: Unknown command '{command}'"


async def console_consumer(
    bot: Any, shutdown_handler: Any, log_path: Any,
    shutdown_reason_cls: Any = None
) -> None:
    """Consumes commands from the global console queue.

    Args:
        bot (CoreBot): The current bot instance.
        shutdown_handler (Callable): The function to call for graceful shutdown.
        log_path (str): Path to the current log file for finalization.
        shutdown_reason_cls: The ShutdownReason enum class (passed from lifecycle module).
    """
    try:
        while True:
            line = await CONSOLE_QUEUE.get()
            if not line:
                continue

            response = await process_control_command(
                line, bot, shutdown_handler, log_path, shutdown_reason_cls
            )
            # For console, just print error responses (success is logged already)
            if response.startswith("ERROR"):
                print(response)
            # Exit the consumer if we're shutting down or restarting
            if "Shutting down" in response or "Restarting" in response:
                break
    except asyncio.CancelledError:
        pass


async def tcp_control_server(
    bot: Any, shutdown_handler: Any, log_path: Any, port: int,
    shutdown_reason_cls: Any = None
) -> None:
    """Runs a TCP server on localhost for remote control commands.

    Accepts connections on 127.0.0.1 only. Each connection receives one command,
    gets a response, and is closed. Valid commands: exit, restart, reload, status.

    Args:
        bot: The current bot instance.
        shutdown_handler: The function to call for graceful shutdown.
        log_path: Path to the current log file for finalization.
        port: The TCP port to listen on.
        shutdown_reason_cls: The ShutdownReason enum class (passed from lifecycle module).
    """
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handles a single client connection."""
        addr = writer.get_extra_info('peername')
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if data:
                command = data.decode('utf-8').strip()
                logging.info(f"TCP control command from {addr}: {command}")
                response = await process_control_command(
                    command, bot, shutdown_handler, log_path, shutdown_reason_cls
                )
                writer.write((response + "\n").encode('utf-8'))
                await writer.drain()
        except asyncio.TimeoutError:
            writer.write(b"ERROR: Timeout\n")
            await writer.drain()
        except Exception as e:
            logging.warning(f"TCP control error from {addr}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, '127.0.0.1', port)
    logging.info(f"TCP control server listening on 127.0.0.1:{port}")

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        logging.info("TCP control server shutting down.")
        server.close()
        await server.wait_closed()


async def run_bot_lifecycle() -> None:
    """Orchestrates the bot lifecycle loop, allowing for soft restarts."""

    # 1. Start the global console reader thread (ONCE)
    loop = asyncio.get_running_loop()
    reader_thread = threading.Thread(target=console_reader, args=(loop,), daemon=True)
    reader_thread.start()
    logging.info("Console reader thread started.")

    restart_count = 0
    log_path: str | None = None  # Persist log path across restarts
    start_time: float = 0.0  # Track start time for runtime calculation

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
        # On restart, reuse existing log file; on fresh start, create new one
        log_level = "DEBUG" if config.DEV_MODE else "INFO"
        log_path = mod_logging.setup_logging(
            level=log_level,
            logs_dir=config.LOGS_DIR,
            bot_name=config.BOT_NAME,
            retention_count=config.LOG_RETENTION_COUNT,
            existing_log_path=log_path if restart_count > 0 else None
        )

        # Track start time on first run only (persists across restarts)
        if restart_count == 0:
            start_time = time.time()

        # ─── INIT ───
        mod_lifecycle.log_phase("INIT")

        # Configuration Validation
        if not config.TOKEN:
            logging.critical("DISCORD_TOKEN missing.")
            sys.exit("Critical error: DISCORD_TOKEN not configured.")

        # Initialize Bot (using the class from the potentially reloaded module)
        bot = mod_bot.CoreBot()

        # Register signal handlers immediately after bot creation — before anything
        # that could block or fail (database, cog loading, D-Bus, TCP server).
        # This ensures we can catch signals and shut down gracefully even during startup.
        # Platform behavior:
        #   - Linux: Register SIGINT/SIGTERM/SIGUSR1 handlers for graceful shutdown
        #     SIGTERM = stop or system shutdown (reason resolved from D-Bus flags)
        #     SIGUSR1 = restart (via RestartKillSignal in systemd unit file) — process exits
        #     SIGINT  = Ctrl+C (dev convenience, treated as manual stop)
        #   - Windows: Skip (no add_signal_handler support); relies on KeyboardInterrupt for Ctrl+C
        if sys.platform != "win32":
            # SIGINT and SIGTERM: reason resolved at handler time from D-Bus flags
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(s)  # Clear old handlers
                    loop.add_signal_handler(
                        s,
                        lambda s=s, _lifecycle=mod_lifecycle, _bot=bot, _log=log_path: asyncio.create_task(
                            _lifecycle.shutdown_handler(s, _bot, reason=None, log_path=_log)
                        )
                    )
                except NotImplementedError:
                    pass

            # SIGUSR1: deterministic restart signal from systemd RestartKillSignal.
            # Does NOT set restart_signal — systemd expects the process to die so it
            # can spawn a fresh instance. Soft restart (restart_signal=True) is only
            # for the TCP/console "restart" command.
            # Reason is NOT hardcoded — lifecycle auto-resolves SIGUSR1 to either
            # RESTART or UPGRADE_RESTART by checking apt-daily-upgrade.service state.
            try:
                loop.remove_signal_handler(signal.SIGUSR1)

                def _sigusr1_handler(_lifecycle=mod_lifecycle, _bot=bot, _log=log_path) -> None:
                    # Store reference on bot to prevent GC before completion (RUF006)
                    _bot._shutdown_task = asyncio.create_task(
                        _lifecycle.shutdown_handler(
                            signal.SIGUSR1, _bot,
                            reason=None,
                            log_path=_log
                        )
                    )

                loop.add_signal_handler(signal.SIGUSR1, _sigusr1_handler)
            except (NotImplementedError, OSError):
                pass

            logging.info("Registered signal handlers: SIGINT, SIGTERM, SIGUSR1")

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
                consumer_task = loop.create_task(console_consumer(
                    bot, mod_lifecycle.shutdown_handler, log_path, mod_lifecycle.ShutdownReason
                ))

                # Start TCP Control Server (if configured)
                tcp_task = None
                if config.CONTROL_PORT:
                    tcp_task = loop.create_task(tcp_control_server(
                        bot, mod_lifecycle.shutdown_handler, log_path, config.CONTROL_PORT,
                        mod_lifecycle.ShutdownReason
                    ))

                # Setup D-Bus shutdown detection (logind PrepareForShutdown listener).
                # Must happen after event loop is running but before bot.start().
                # Bot reference passed for pre-SIGTERM Discord messaging (upgrade notifications).
                await mod_lifecycle.setup_shutdown_detection(bot)

                # Connect to Discord (CONNECT/READY/POST-READY phases handled in lifecycle.startup_handler)
                try:
                    logging.info(f"Connecting to Discord as {config.BOT_NAME}...")
                    await bot.start(config.TOKEN)
                except KeyboardInterrupt:
                    # Handle Ctrl+C directly if signal handler didn't catch it
                    pass
                finally:
                    # Cancel control tasks so they stop for THIS bot instance
                    consumer_task.cancel()
                    if tcp_task:
                        tcp_task.cancel()
                    try:
                        await consumer_task
                    except asyncio.CancelledError:
                        pass
                    if tcp_task:
                        try:
                            await tcp_task
                        except asyncio.CancelledError:
                            pass
        except asyncio.CancelledError:
            logging.info("Bot task cancelled.")
        except Exception as e:
            logging.error(f"Bot encountered an error: {e}", exc_info=True)

        # Check for Restart Signal
        if bot.restart_signal:
            logging.info("Restart signal received. Purging modules...")
            mod_logging.stop_queue_listener()  # Clean up before purge to avoid orphaned thread
            # teardown_shutdown_detection already called in shutdown_handler,
            # but call again defensively in case shutdown_handler didn't run
            # (e.g. crash path). Safe to call multiple times.
            await mod_lifecycle.teardown_shutdown_detection()
            mod_lifecycle.purge_modules()
            restart_count += 1
            # Loop continues -> Modules re-imported -> New Bot made
        else:
            logging.info("No restart signal. Exiting.")
            # Finalize log file (rename with runtime) only on full exit, not restart
            if log_path:
                runtime_seconds = time.time() - start_time
                mod_logging.finalize_log(log_path, runtime_seconds)
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

"""main.py

This is the primary entry point for the Bot. Its responsibilities are:
- Performing initial setup: logging, configuration validation from `info.env`.
- Instantiating the custom `CoreBot` class from `utils.bot_class`.
- Defining console and signal handlers for graceful startup and shutdown.
- Orchestrating the bot's asynchronous startup sequence via the `main()` function,
  which initializes the database, loads cogs, and connects to Discord.

This script acts as the "launcher" for the bot; the core logic, event handlers,
and command processing are defined within the `CoreBot` class itself.
"""

import asyncio
import logging
import os
import signal
import sys

import discord
from discord.ext import commands

# Setup and Configuration
# Import necessary configurations and utility functions.
import config
from utils.bot_class import CoreBot
from utils.database import DatabaseManager
from utils.extensions import discover_cogs
from utils.lifecycle import shutdown_handler
from utils.logging_config import setup_logging

# Set up logging immediately to capture any issues during startup.
log_level = "DEBUG" if config.DEV_MODE else "INFO"
setup_logging(level=log_level, log_file=config.LOG_PATH)

# Configuration Validation
# Ensure the bot's token is present, as it's impossible to run without it.
if not config.TOKEN:
    logging.critical(
        f"DISCORD_TOKEN is missing from '{os.path.basename(config.ENV_PATH)}'. "
        "This is required for the bot to run."
    )
    print(f"Error: DISCORD_TOKEN not found in {config.ENV_PATH}.")
    print("Please add your bot's token to the file.")
    sys.exit("Critical error: DISCORD_TOKEN not configured.")

# Warn if the owner ID is missing, as owner-only commands will fail.
if not config.OWNER_ID:
    logging.warning(
        f"OWNER_ID not found or invalid in '{os.path.basename(config.ENV_PATH)}'. "
        "The bot will run, but owner-specific commands will not be available."
    )

# Warn if the system channel ID is missing.
if not config.SYSTEM_CHANNEL_ID:
    logging.warning(
        f"SYSTEM_CHANNEL_ID not found in '{os.path.basename(config.ENV_PATH)}'. "
        "The bot will run, but startup/shutdown messages will not be sent."
    )

# Bot Initialization

# Define the bot's intents. `message_content` is required for reading messages
# for NLP commands.
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

# Create the custom bot instance.
# The prefix logic is now handled inside the CoreBot class.
bot = CoreBot()

logging.info(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
print(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
# The db_manager will be attached in main() after async initialization.


# Core Bot Commands

@bot.command(name="ping", help="Check if the bot is responsive.")
async def ping(ctx: commands.Context) -> None:
    """Simple ping command to check bot responsiveness.

    Args:
        ctx (commands.Context): The command context.
    """
    await ctx.send(f"Pong! Latency: {round(bot.latency * 1000)}ms")
    logging.info(f"Ping command used by {ctx.author}.")


# Main Bot Execution

async def console_input_handler(bot: CoreBot) -> None:
    """Listens for console input and triggers a graceful shutdown if 'exit' is typed.

    This implementation uses a platform-specific approach for compatibility.

    Args:
        bot (CoreBot): The bot instance to control.
    """
    loop = asyncio.get_running_loop()
    try:
        if sys.platform == "win32":
            # On Windows, run_in_executor is a reliable way to read from stdin.
            # This is a blocking call in a separate thread, so cancellation is
            # not immediate but will occur after the next input.
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line.strip().lower() == 'exit':
                    logging.info("'exit' command received from console. Initiating shutdown.")
                    loop.create_task(shutdown_handler(signal.SIGINT, bot))
                    break
                elif line.strip().lower() == 'reload':
                    logging.info("'reload' command received from console. Reloading cogs...")
                    # Create a task to run the reload concurrently.
                    loop.create_task(bot.reload_all_cogs())
        else:
            # On Linux/macOS, use a non-blocking StreamReader for stdin.
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            while True:
                line_bytes = await reader.readline()
                if not line_bytes:  # Reached EOF
                    break
                line = line_bytes.decode().strip()
                if line.lower() == 'exit':
                    logging.info("'exit' command received from console. Initiating shutdown.")
                    loop.create_task(shutdown_handler(signal.SIGINT, bot))
                    break
                elif line.lower() == 'reload':
                    logging.info("'reload' command received from console. Reloading cogs...")
                    # Create a task to run the reload concurrently.
                    loop.create_task(bot.reload_all_cogs())

    except asyncio.CancelledError:
        logging.info("Console input handler cancelled.")
    except Exception as e:
        # Log other potential errors, e.g., if stdin is closed unexpectedly.
        logging.error(f"Error in console input handler: {e}", exc_info=False)


async def main() -> None:
    """The main asynchronous entry point for initializing and running the bot.

    This function orchestrates the entire startup process.

    Raises:
        ValueError: If DB_PATH or TOKEN is not configured.
    """
    logging.info(f"{config.BOT_NAME} is starting...")

    # Asynchronously initialize the database manager and attach it to the bot.
    # This ensures the database is ready before the bot logs in.
    if config.DB_PATH is None:
        raise ValueError("DB_PATH cannot be None.")
    db_manager = await DatabaseManager.create(config.DB_PATH)
    bot.db_manager = db_manager

    async with bot:
        # Load all cogs (extensions) specified in the configuration file.
        cogs_to_load = discover_cogs(config.COGS_PATH)
        logging.info(f"Found {len(cogs_to_load)} cogs to load.")
        for extension in cogs_to_load:
            try:
                await bot.load_extension(extension)
                logging.info(f"Successfully loaded extension: {extension}")
            except Exception:
                logging.error(f'Failed to load extension {extension}.', exc_info=True)

        if config.TOKEN is None:
            # This check is technically redundant due to the earlier validation,
            # but it satisfies type checkers that TOKEN is not None.
            raise ValueError("TOKEN cannot be None.")

        # Start the bot and connect to Discord.
        await bot.start(config.TOKEN)


async def run_bot_with_handlers() -> None:
    """Wraps the main bot logic with signal and console handlers for graceful shutdown."""
    loop = asyncio.get_running_loop()

    # Add signal handlers for SIGINT/SIGTERM on Linux for systemd integration.
    if sys.platform != "win32":
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                s, lambda s=s: asyncio.create_task(shutdown_handler(s, bot))
            )

    # Start the console listener for the 'exit' command.
    if sys.stdin and sys.stdin.isatty():
        bot.console_task = loop.create_task(console_input_handler(bot))

    await main()

if __name__ == '__main__':
    try:
        asyncio.run(run_bot_with_handlers())
    finally:
        # This message logs after the asyncio event loop has closed, ensuring
        # it's the final log entry upon termination.
        logging.info(f"{config.BOT_NAME} has shutdown properly!")

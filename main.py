"""
main.py

This is the primary entry point for the Sancho-Bot. Its responsibilities are:

1.  Performing initial setup: logging, configuration validation from `info.env`.
2.  Instantiating the custom `SanchoBot` class from `utils.bot_class`.
3.  Defining console and signal handlers for graceful startup and shutdown.
4.  Orchestrating the bot's asynchronous startup sequence via the `main()` function,
    which initializes the database, loads cogs, and connects to Discord.

This script acts as the "launcher" for the bot; the core logic, event handlers,
and command processing are defined within the `SanchoBot` class itself.
"""
import discord
from discord.ext import commands
import logging
import asyncio
import os
import signal
import sys
import time
import psutil
from datetime import timedelta

# --- 1. Setup and Configuration ---
# Import necessary configurations and utility functions.
import config
from utils.logging_config import setup_logging
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
from utils.lifecycle import shutdown_handler
from utils.extensions import discover_cogs

# Set up logging immediately to capture any issues during startup.
setup_logging()

# --- Configuration Validation ---
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

# --- 2. Bot Initialization ---

# Define the bot's intents. `message_content` is required for reading messages
# for NLP commands.
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

# Create the custom bot instance.
# The prefix logic is now handled inside the SanchoBot class.
bot = SanchoBot()

logging.info(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
print(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
# The db_manager will be attached in main() after async initialization.


# --- 3. Core Bot Commands ---
    
@bot.command(name="ping", help="Provides a comprehensive health and status check for the bot.", hidden=True)
@commands.is_owner()
async def ping(ctx: commands.Context) -> None:
    """
    Provides a comprehensive health and status check for the bot, including
    latency, uptime, cog status, database health, and resource usage.
    """
    # 1. Initial "Pinging..." message
    start_time = time.monotonic()
    message = await ctx.send("Pinging for status...")
    end_time = time.monotonic()

    # 2. Gather all metrics
    # Latencies
    roundtrip_latency = (end_time - start_time) * 1000
    gateway_latency = bot.latency * 1000
    db_latency = await bot.db_manager.ping() if bot.db_manager else -1

    # Uptime & Start Time
    start_timestamp = int(bot.start_time)
    uptime_delta = timedelta(seconds=time.time() - bot.start_time)
    days, remainder = divmod(uptime_delta.total_seconds(), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"

    # Cogs
    loaded_cogs = bot.extensions.keys()
    total_cogs = len(discover_cogs(config.COGS_PATH))
    cogs_status = f"{len(loaded_cogs)}/{total_cogs}"
    
    # Resource Usage
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    cpu_usage = psutil.cpu_percent(interval=None) # Use interval=None for non-blocking call
    ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB

    # 3. Create Embed
    embed = discord.Embed(
        title="Sancho Status Report",
        color=discord.Color.green() if gateway_latency < 200 else discord.Color.orange()
    )
    if bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="Timings",
        value=f"**Gateway:** `{gateway_latency:.2f}ms`\n"
              f"**Roundtrip:** `{roundtrip_latency:.2f}ms`\n"
              f"**Database:** `{db_latency:.2f}ms`",
        inline=True
    )

    embed.add_field(
        name="Status",
        value=f"**Uptime:** `{uptime_str}`\n"
              f"**Started:** <t:{start_timestamp}:f>\n"
              f"**Cogs Loaded:** `{cogs_status}`",
        inline=True
    )
    
    embed.add_field(
        name="Resource Usage",
        value=f"**CPU:** `{cpu_usage:.1f}%`\n"
              f"**RAM:** `{ram_usage:.2f} MB`",
        inline=True
    )

    # Add a field for loaded cogs, formatted nicely
    if loaded_cogs:
        # Format cog names by removing 'cogs.' prefix and joining them
        cog_list_str = ", ".join([cog.replace('cogs.', '') for cog in sorted(loaded_cogs)])
        embed.add_field(
            name="Loaded Cogs",
            value=f"```{cog_list_str}```",
            inline=False
        )

    embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()

    # 4. Edit the original message with the embed
    await message.edit(content=None, embed=embed)
    logging.info(f"Ping command used by {ctx.author}.")

# --- 4. Main Bot Execution ---

async def console_input_handler(bot: SanchoBot):
    """
    Listens for console input and triggers a graceful shutdown if 'exit' is typed.
    This implementation uses a platform-specific approach for compatibility.
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
                if not line_bytes: # Reached EOF
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
    """
    The main asynchronous entry point for initializing and running the bot.
    This function orchestrates the entire startup process.
    """
    logging.info("Sancho is starting...")
    
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

async def run_bot_with_handlers():
    """
    Wraps the main bot logic with signal and console handlers for graceful shutdown.
    """
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
        logging.info("Sancho has shutdown properly!")
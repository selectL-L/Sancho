"""
main.py

This is the primary entry point for the Sancho-Bot, which is responsible for:

1.  Setting up logging and validating critical configurations from the .env file.
2.  Initializing the Discord bot instance (`SanchoBot`) with necessary intents and
    the database manager.
3.  Defining core bot event handlers, including `on_ready`, `on_command_error`,
    and the crucial `on_message` for NLP-based command dispatching.
4.  A `main` asynchronous function that orchestrates the bot's startup sequence:
    initializing the database, loading all cogs (extensions), and connecting to Discord.

(Note to self, adding descriptions to other files might be a smart idea.)
"""
import discord
from discord.ext import commands
import logging
import asyncio
import os
import re
import signal
import sys
import time
from typing import Optional, Any
from collections.abc import Callable

# --- 1. Setup and Configuration ---
# Import necessary configurations and utility functions.
import config
from utils.logging_config import setup_logging
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
from utils.lifecycle import startup_handler, shutdown_handler
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
bot = SanchoBot(
    intents=intents,
    case_insensitive=True  # This makes the command name (e.g., 'ping') case-insensitive
)
logging.info(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
print(f"Bot initialized with prefixes: {config.BOT_PREFIX}")
# The db_manager will be attached in main() after async initialization.


# --- 3. Core Bot Events ---
@bot.event
async def on_ready():
    """Called when the bot is ready; triggers the startup handler."""
    await startup_handler(bot)
    
@bot.command()
async def ping(ctx: commands.Context) -> None:
    """
    A command to check the bot's latency, differentiating bot vs. gateway.
    This Commands main purpose is to check for basic functionality and responsiveness.
    """
    # Gateway latency (from Discord's heartbeat)
    gateway_latency = bot.latency * 1000

    # Measure message round-trip time
    start_time = time.monotonic()
    message = await ctx.send("Pinging...")
    end_time = time.monotonic()
    
    # This is the time it took to send the message and get a confirmation.
    # It includes network latency to Discord, processing time on Discord's end,
    # and network latency back to the bot.
    roundtrip_latency = (end_time - start_time) * 1000

    await message.edit(
        content=f"Pong! 🏓\n"
                f"Gateway Latency: `{gateway_latency:.2f}ms`\n"
                f"Roundtrip Latency: `{roundtrip_latency:.2f}ms`"
    )
    logging.info(f"Ping command used by {ctx.author}.")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    """
    Global error handler for all standard `discord.ext.commands`.
    This catches errors from commands defined with `@bot.command()`.
    """
    # Ignore `CommandNotFound` errors, as the `on_message` handler will treat
    # these as potential NLP commands. This prevents duplicate error messages.
    if isinstance(error, commands.CommandNotFound):
        return

    # For user input errors (e.g., missing arguments), show the command's help message
    # to guide the user on correct usage.
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.send_help(ctx.command)
        return

    # For all other errors, log the full traceback for debugging purposes.
    logging.error(f"Unhandled error in command '{ctx.command}'", exc_info=error)

    # Notify the user that a generic, unexpected error occurred.
    try:
        await ctx.send("Sorry, an unexpected error occurred. The issue has been logged.")
    except discord.HTTPException:
        logging.error(f"Failed to send error message to channel {ctx.channel.id}")


@bot.event
async def on_message(message: discord.Message) -> None:
    """
    The main event handler for processing all incoming messages.
    This function serves as the core dispatcher for NLP-based commands.
    """
    # Ignore messages from the bot itself to prevent loops.
    if message.author.bot:
        return

    # If in developer mode, only respond to the owner.
    if config.DEV_MODE and message.author.id != config.OWNER_ID:
        return

    # First, allow `discord.py` to process the message to see if it's a
    # standard, decorator-based command (like `.ping`).
    await bot.process_commands(message)

    # If the message was a standard command, we don't need to process it for NLP.
    # `ctx.valid` will be True if a valid command was found and invoked.
    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # --- NLP Processing Logic ---
    # Check if the message starts with one of the recognized bot prefixes (case-insensitive).
    prefix_used = None
    content_lower = message.content.lower()
    for p in config.BOT_PREFIX:
        if content_lower.startswith(p.lower()):
            # Find the actual prefix used from the original message content
            # to correctly slice it off later.
            prefix_used = message.content[:len(p)]
            break

    # If no valid prefix was found, it's not an NLP command, so we ignore it.
    if not prefix_used:
        return

    # Extract the user's query by removing the prefix and any leading/trailing whitespace.
    query = message.content[len(prefix_used):].strip()
    if not query:
        return  # Ignore messages that are just the prefix.

    query_lower = query.lower()
    logging.info(f"NLP query from '{message.author}': '{query}'")

    # --- NLP Command Matching Logic ---
    group_winners = []
    # Step 1: Find a "winner" from each command group based on definition order.
    for group in config.NLP_COMMANDS:
        for keywords, cog_name, method_name in group:
            for keyword in keywords:
                match = re.search(keyword, query_lower)
                if match:
                    # Found a match. This is the winner for its group.
                    # Store it with its match position and break to the next group.
                    group_winners.append((match.start(), cog_name, method_name))
                    break  # Move to the next group
            else:
                # This 'else' belongs to the inner 'for' loop.
                # If the inner loop completes without a 'break', continue to the next command.
                continue
            # This 'break' belongs to the outer 'for' loop.
            # It executes if the inner loop was broken (i.e., a match was found).
            break

    # If no commands matched at all, do nothing.
    if not group_winners:
        return

    # Step 2: Sort the group winners by their keyword's position in the query.
    # The command that appeared earliest in the string wins overall.
    group_winners.sort(key=lambda x: x[0])
    
    # Get the final winning command.
    _, cog_name, method_name = group_winners[0]

    cog = bot.get_cog(cog_name)
    if not cog:
        logging.error(f"NLP dispatcher: Winning cog '{cog_name}' is not loaded.")
        return

    method: Optional[Callable[..., Any]] = getattr(cog, method_name, None)
    if not (method and asyncio.iscoroutinefunction(method)):
        logging.error(f"NLP dispatcher: Winning method '{method_name}' in '{cog_name}' is not an awaitable coroutine.")
        return

    try:
        # Call the winning NLP handler.
        await method(ctx, query=query)
    except Exception as e:
        logging.error(f"Error in NLP command '{cog_name}.{method_name}': {e}", exc_info=True)
        await ctx.send("Sorry, an internal error occurred. The issue has been logged.")


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
                    loop.create_task(reload_all_cogs(bot))
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
                    loop.create_task(reload_all_cogs(bot))

    except asyncio.CancelledError:
        logging.info("Console input handler cancelled.")
    except Exception as e:
        # Log other potential errors, e.g., if stdin is closed unexpectedly.
        logging.error(f"Error in console input handler: {e}", exc_info=False)

async def reload_all_cogs(bot: SanchoBot):
    """
    Asynchronously discovers and reloads all cogs, handling new, removed,
    and updated extensions.
    """
    logging.info("Starting cog reload process...")

    # Get the set of currently loaded extension names (e.g., {'cogs.fun', 'cogs.math'})
    loaded_cogs = set(bot.extensions.keys())
    logging.info(f"Currently loaded cogs: {loaded_cogs or 'None'}")

    # Discover the cogs currently present in the filesystem.
    try:
        discovered_cogs = set(discover_cogs(config.COGS_PATH))
        logging.info(f"Discovered cogs in filesystem: {discovered_cogs or 'None'}")
    except Exception as e:
        logging.error(f"Failed to discover cogs: {e}", exc_info=True)
        return

    # --- Determine which cogs to load, unload, and reload ---
    cogs_to_load = discovered_cogs - loaded_cogs
    cogs_to_unload = loaded_cogs - discovered_cogs
    cogs_to_reload = loaded_cogs.intersection(discovered_cogs)

    # --- Perform actions ---
    # 1. Unload cogs that have been removed.
    for extension in cogs_to_unload:
        try:
            await bot.unload_extension(extension)
            logging.info(f"Successfully unloaded removed extension: {extension}")
        except Exception:
            logging.error(f'Failed to unload extension {extension}.', exc_info=True)

    # 2. Load new cogs that have been added.
    for extension in cogs_to_load:
        try:
            await bot.load_extension(extension)
            logging.info(f"Successfully loaded new extension: {extension}")
        except Exception:
            logging.error(f'Failed to load new extension {extension}.', exc_info=True)

    # 3. Reload existing cogs to apply any changes.
    for extension in cogs_to_reload:
        try:
            await bot.reload_extension(extension)
            logging.info(f"Successfully reloaded extension: {extension}")
        except Exception:
            logging.error(f'Failed to reload extension {extension}.', exc_info=True)

    logging.info("Finished reloading cogs.")

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
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
import sys
from typing import Optional, Any
from collections.abc import Callable

# --- 1. Setup and Configuration ---
# Import necessary configurations and utility functions.
import config
from utils.logging_config import setup_logging
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager

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

# --- 2. Bot Initialization ---

# Define the bot's intents. `message_content` is required for reading messages
# for NLP commands.
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

# Create the custom bot instance.
bot = SanchoBot(command_prefix=config.BOT_PREFIX, intents=intents)
# Attach database path and manager to the bot instance for easy access across cogs.
bot.db_path = config.DB_PATH
bot.db_manager = DatabaseManager(bot.db_path)


# --- 3. Core Bot Events ---

@bot.event
async def on_ready() -> None:
    """
    Called when the bot has successfully connected to Discord and is ready to operate.
    This is typically used to log the bot's user information.
    """
    if bot.user:
        logging.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    else:
        logging.error("Bot user information not available on ready.")

@bot.command()
async def ping(ctx: commands.Context) -> None:
    """A simple command to check the bot's latency."""
    latency = bot.latency * 1000
    await ctx.send(f'Pong! Latency: {latency:.2f}ms')
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

    # First, allow `discord.py` to process the message to see if it's a
    # standard, decorator-based command (like `.ping`).
    await bot.process_commands(message)

    # If the message was a standard command, we don't need to process it for NLP.
    # `ctx.valid` will be True if a valid command was found and invoked.
    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # --- NLP Processing Logic ---
    # Check if the message starts with one of the recognized bot prefixes.
    prefix_used = None
    for p in config.BOT_PREFIX:
        if message.content.startswith(p):
            prefix_used = p
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

    # Iterate through the NLP command mappings defined in `config.py`.
    for keywords, cog_name, method_name in config.NLP_COMMANDS:
        # Check if any of the keywords for a command are present in the query.
        if any(re.search(keyword, query_lower) for keyword in keywords):
            cog = bot.get_cog(cog_name)
            if not cog:
                logging.error(f"NLP dispatcher: Cog '{cog_name}' is registered but not loaded.")
                continue

            # Get the method from the cog and ensure it's a callable coroutine.
            method: Optional[Callable[..., Any]] = getattr(cog, method_name, None)
            if not (method and asyncio.iscoroutinefunction(method)):
                logging.error(f"NLP dispatcher: Method '{method_name}' in '{cog_name}' is not an awaitable coroutine.")
                continue

            try:
                # Call the NLP handler method in the cog. All NLP handlers are
                # expected to have the signature `(self, ctx, *, query)`.
                await method(ctx, query=query)
            except Exception as e:
                # This is a fallback for unhandled errors within the NLP command itself.
                logging.error(f"Error in NLP command '{cog_name}.{method_name}': {e}", exc_info=True)
                await ctx.send("Sorry, an internal error occurred. The issue has been logged.")
            
            # Stop after the first match to prevent multiple commands from firing.
            return

# --- 4. Main Bot Execution ---

async def main() -> None:
    """
    The main asynchronous entry point for initializing and running the bot.
    This function orchestrates the entire startup process.
    """
    logging.info("Sancho is starting...")
    async with bot:
        # Initialize the database, creating tables if they don't exist.
        await bot.db_manager.setup_databases()
        # Load any dynamic configurations from the database.
        await bot.db_manager.load_skill_limit()

        # Load all cogs (extensions) specified in the configuration file.
        for extension in config.COGS_TO_LOAD:
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

if __name__ == '__main__':
    try:
        # Run the main asynchronous function.
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # Gracefully handle shutdown signals (e.g., Ctrl+C).
        logging.info("Sancho is shutting down.")
import discord
from discord.ext import commands
import logging
import asyncio
import os
import re
import sys
from dotenv import load_dotenv
from typing import Optional, Any
from collections.abc import Callable

# --- 1. Setup and Configuration ---
import config
from utils.logging_config import setup_logging
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager

# Set up logging BEFORE anything else
setup_logging()

# Load environment variables from the .env file
load_dotenv(dotenv_path=config.ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    logging.critical(f"DISCORD_TOKEN not found in {config.ENV_PATH}. The bot cannot start.")
    sys.exit("Critical error: DISCORD_TOKEN not found.")

# --- 2. Bot Initialization ---

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = SanchoBot(command_prefix=config.BOT_PREFIX, intents=intents)
bot.db_path = config.DB_PATH  # Attach db_path to the bot instance
bot.db_manager = DatabaseManager(bot.db_path) # Attach the database manager


# --- 3. Core Bot Events ---

@bot.event
async def on_ready() -> None:
    """Called when the bot is successfully connected and ready."""
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
    """Global error handler for all standard commands."""
    # Ignore CommandNotFound, as it's not a "real" error.
    # The on_message handler will process it for NLP commands.
    if isinstance(error, commands.CommandNotFound):
        return

    # For command input errors, show the command's help.
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.send_help(ctx.command)
        return

    # Log the full error traceback for debugging.
    logging.error(f"Unhandled error in command '{ctx.command}'", exc_info=error)

    # Notify the user that something went wrong.
    try:
        await ctx.send("Sorry, an unexpected error occurred. The issue has been logged.")
    except discord.HTTPException:
        logging.error(f"Failed to send error message to channel {ctx.channel.id}")


@bot.event
async def on_message(message: discord.Message) -> None:
    """Main event for processing all messages for NLP commands."""
    if message.author.bot:
        return

    # First, let discord.py process any standard commands.
    await bot.process_commands(message)

    # If the message was a standard command, don't also process it for NLP.
    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    # NLP processing starts here.
    if not message.content.startswith(config.BOT_PREFIX):
        return

    query = message.content[len(config.BOT_PREFIX):].strip()
    if not query:
        return # Ignore empty commands

    query_lower = query.lower()
    logging.info(f"NLP query from '{message.author}': '{query}'")

    for keywords, cog_name, method_name in config.NLP_COMMANDS:
        if any(re.search(keyword, query_lower) for keyword in keywords):
            cog = bot.get_cog(cog_name)
            if not cog:
                logging.error(f"NLP dispatcher: Cog '{cog_name}' is registered but not loaded.")
                continue

            # Ensure the method exists and is a coroutine
            method: Optional[Callable[..., Any]] = getattr(cog, method_name, None)
            if not (method and asyncio.iscoroutinefunction(method)):
                logging.error(f"NLP dispatcher: Method '{method_name}' in '{cog_name}' is not an awaitable coroutine.")
                continue

            try:
                # This single call works for any cog following the standard signature.
                await method(ctx, query=query)
            except Exception as e:
                # This is a fallback for errors within the NLP command itself.
                logging.error(f"Error in NLP command '{cog_name}.{method_name}': {e}", exc_info=True)
                await ctx.send("Sorry, an internal error occurred. The issue has been logged.")
            
            return # Stop after the first match.

# --- 4. Main Bot Execution ---

async def main() -> None:
    """The main entry point for starting the bot."""
    logging.info("Sancho is starting...")
    async with bot:
        # Setup the database tables and load configs
        await bot.db_manager.setup_databases()
        await bot.db_manager.load_skill_limit()

        # Load cogs from the static list in the config file.
        for extension in config.COGS_TO_LOAD:
            try:
                await bot.load_extension(extension)
                logging.info(f"Successfully loaded extension: {extension}")
            except Exception:
                logging.error(f'Failed to load extension {extension}.', exc_info=True)
        
        if TOKEN is None:
            raise ValueError("TOKEN cannot be None.")
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Sancho is shutting down.")
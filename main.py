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

# --- 1. Pathing and Bot Configuration ---

def get_application_path() -> str:
    """Returns the base path for the application, whether running from source or bundled."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running as a bundled executable
        return os.path.dirname(sys.executable)
    # Running from a normal .py script
    return os.path.dirname(os.path.abspath(__file__))

# Define all application paths and the bot prefix as constants
APP_PATH = get_application_path()
ENV_PATH = os.path.join(APP_PATH, 'info.env')
LOG_PATH = os.path.join(APP_PATH, 'sancho.log')
DB_PATH = os.path.join(APP_PATH, 'reminders.db')
COGS_PATH = os.path.join(APP_PATH, 'cogs')
BOT_PREFIX = ".sancho "

# --- 2. Logging and Environment Setup ---

load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

logger = logging.getLogger('')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

file_handler = logging.FileHandler(filename=LOG_PATH, encoding='utf-8', mode='w')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

if not TOKEN:
    logger.critical(f"DISCORD_TOKEN not found in {ENV_PATH}. The bot cannot start.")
    sys.exit("Critical error: DISCORD_TOKEN not found.")

# --- 3. Bot Initialization ---

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# --- 4. NLP Command Registry ---

# To add a new command, add a tuple to this list.
# Format: ( (keywords), 'CogClassName', 'method_name' )
NLP_COMMANDS: list[tuple[tuple[str, ...], str, str]] = [
    ((r'\broll\b', r'\bdice\b'), 'DiceRoller', 'roll'),
    ((r'\bremind\b', r'\breminder\b', r'\bremember\b'), 'Reminders', 'remind'),
]

# --- 5. Core Bot Events ---

@bot.event
async def on_ready() -> None:
    """Called when the bot is successfully connected and ready."""
    if bot.user:
        logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    else:
        logger.error("Bot user information not available on ready.")

@bot.command()
async def ping(ctx: commands.Context) -> None:
    """A simple command to check the bot's latency."""
    latency = bot.latency * 1000
    await ctx.send(f'Pong! Latency: {latency:.2f}ms')
    logger.info(f"Ping command used by {ctx.author}.")

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
    if message.content.startswith(BOT_PREFIX):
        query = message.content[len(BOT_PREFIX):].strip()
        query_lower = query.lower()
        logger.info(f"NLP query from '{message.author}': '{query}'")

        for keywords, cog_name, method_name in NLP_COMMANDS:
            if any(re.search(keyword, query_lower) for keyword in keywords):
                cog: Optional[commands.Cog] = bot.get_cog(cog_name)
                if not cog:
                    logger.error(f"NLP dispatcher: Cog '{cog_name}' is registered but not loaded.")
                    continue

                method: Optional[Callable[..., Any]] = getattr(cog, method_name, None)
                if not (method and asyncio.iscoroutinefunction(method)):
                    logger.error(f"NLP dispatcher: Method '{method_name}' in '{cog_name}' is not an awaitable coroutine.")
                    continue

                try:
                    # This single call works for any cog following the standard signature.
                    await method(ctx, query=query)
                except Exception:
                    logger.error(f"Error in NLP command '{cog_name}.{method_name}'", exc_info=True)
                    await ctx.send("Sorry, an internal error occurred. The issue has been logged.")
                return # Stop after the first match.

# --- 6. Main Bot Execution ---

async def main() -> None:
    """The main entry point for starting the bot."""
    logger.info("Sancho is starting...")
    async with bot:
        # Dynamically discover and load cogs from the 'cogs' directory.
        for filename in os.listdir(COGS_PATH):
            if filename.endswith('.py') and not filename.startswith('__'):
                extension = f'cogs.{filename[:-3]}'
                try:
                    # Pass dependencies to specific cogs if needed.
                    if extension == 'cogs.reminders':
                        await bot.load_extension(extension, package=DB_PATH)
                    else:
                        await bot.load_extension(extension)
                    logger.info(f"Successfully loaded extension: {extension}")
                except Exception:
                    logger.error(f'Failed to load extension {extension}.', exc_info=True)
        
        if TOKEN is None:
            raise ValueError("TOKEN cannot be None.")
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Sancho is shutting down.")
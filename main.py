import discord
from discord.ext import commands
import logging
import asyncio
import os
import re
import sys
from dotenv import load_dotenv

# --- Setup and Configuration ---

load_dotenv(dotenv_path='info.env')
TOKEN = os.getenv('DISCORD_TOKEN')

logger = logging.getLogger('')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

file_handler = logging.FileHandler(filename='sancho.log', encoding='utf-8', mode='w')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

if not TOKEN:
    logger.critical("DISCORD_TOKEN not found in info.env file.")
    sys.exit("DISCORD_TOKEN not found. Please set it in info.env file.")

intent = discord.Intents.default()
intent.messages = True
intent.message_content = True

bot = commands.Bot(command_prefix='.sancho ', intents=intent, log_handler=None)

# To add a new NLP command, add a new entry to this list.
# The format is a tuple with four values:
# 1. Keywords (tuple of strings): Words that trigger this command. Use word boundaries (\b) to avoid false positives.
# 2. Cog Name (string): The name of the Cog class that contains the method.
# 3. Method Name (string): The name of the method to call within the Cog.
# 4. Requires Attachment (boolean): Currently unused, but for future features.
#
# Example to add a new command:
#   ( (r'\bweather\b', r'\bforecast\b'), 'WeatherCog', 'get_weather', False ),
#
NLP_COMMANDS = [
    ((r'\broll\b', r'\bdice\b'), 'DiceRoller', 'roll', False),
    ((r'\bremind\b', r'\breminder\b'), 'Reminders', 'remind', False),
]

# --- Main Bot Logic ---

@bot.event
async def on_ready():
    """Event handler for when the bot is connected to discord."""
    if bot.user:
        logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
        logger.info('------')
        print(f'{bot.user} is ready!')
        print('------')

@bot.command()
async def ping(ctx):
    """Checks latency, responds with Pong! and latency in ms."""
    latency = bot.latency * 1000
    await ctx.send(f'Pong! Latency: {latency:.2f}ms')
    logger.info(f'Ping command used by {ctx.author} in {ctx.guild}/{ctx.channel}')

@bot.event
async def on_command_error(ctx, error):
    """Gracefully handles errors for standard commands."""
    if isinstance(error, commands.CommandNotFound):
        logger.warning(f"Standard command not found, passing to NLP: '{ctx.message.content}'")
        return
    logger.error(f"An error occurred with command '{ctx.command}': {error}", exc_info=True)
    raise error

@bot.event
async def on_message(message):
    """Event handler for ALL incoming messages."""
    if message.author == bot.user:
        return
    
    await bot.process_commands(message)

    if (await bot.get_context(message)).valid:
        return
    
    if message.content.startswith(bot.command_prefix):
        ctx = await bot.get_context(message)
        prefix = str(bot.command_prefix)
        query = message.content[len(prefix):].lower().strip()
        logger.info(f"User '{ctx.author}' in guild '{ctx.guild}' used NLP with query: '{query}'")

        command_matched = False
        for keywords, cog_name, method_name, requires_attachment in NLP_COMMANDS:
            # Use regex search for more accurate keyword matching
            if any(re.search(keyword, query) for keyword in keywords):
                command_matched = True
                
                cog = bot.get_cog(cog_name)
                if not cog:
                    logger.error(f"NLP dispatcher: Cog '{cog_name}' is not loaded or does not exist.")
                    continue
                
                method = getattr(cog, method_name, None)
                if not method:
                    logger.error(f"NLP dispatcher: Method '{method_name}' not found in cog '{cog_name}'.")
                    continue

                try:
                    # Dynamically call the method from the cog
                    if cog_name == 'DiceRoller':
                        await method(ctx, roll_string=query)
                    elif cog_name == 'Reminders':
                        await method(ctx, query=query)
                    # Add future cog/method calls here
                except Exception as e:
                    # Log the full error with traceback for debugging
                    logger.error(f"An error occurred in NLP command '{cog_name}.{method_name}' for query: '{query}'", exc_info=True)
                    # Inform the user that something went wrong
                    await ctx.send("Sorry, I encountered an error while trying to process your request. The issue has been logged!")
                
                break
            
        if not command_matched:
            # This message is now only sent if no keywords were matched at all.
            # It will NOT be sent if a keyword matched but the command failed.
            pass # We can choose to be silent on non-matches to reduce spam.

# --- Main Bot Execution ---

async def main():
    """Main entry point to load cogs and start the bot."""
    logger.info("Sancho starting...")

    for filename in os.listdir('./cogs'):
        if filename.startswith('__'):
            continue
        if filename.endswith('.py'):
            extension = f'cogs.{filename[:-3]}'
            try:
                await bot.load_extension(extension)
            except Exception:
                logger.error(f'Failed to load extension {extension}.', exc_info=True)
        
    try:
        assert TOKEN is not None
        await bot.start(TOKEN)
    except Exception:
        logger.critical(f"Sancho encountered a critical error and could not start.", exc_info=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Sancho is shutting down due to keyboard interrupt.")
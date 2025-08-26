import discord
from discord.ext import commands
import logging
import asyncio
import os
import re
import sys
from dotenv import load_dotenv

# Setup and Config (Expandable later)

# Load environment variables from info.env file
load_dotenv(dotenv_path='info.env')
TOKEN = os.getenv('DISCORD_TOKEN')

# Logging setup
# Configuring OUR logger to avoid using discords default logger
logger = logging.getLogger('')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

# File handler
file_handler = logging.FileHandler(filename='sancho.log', encoding='utf-8', mode='w')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Bot initialization

# Check if the token is loaded successfully exit otherwise
if not TOKEN:
    logger.critical("DISCORD_TOKEN not found in info.env file.")
    sys.exit("DISCORD_TOKEN not found in environment variables. Please set it in info.env file.")

# Intents setup
intent = discord.Intents.default()
intent.messages = True
intent.message_content = True

# Flexible prefix
bot = commands.Bot(command_prefix='.sancho ', intents=intent, log_handler=None) # Disable default logging to avoid duplicate logs

# NLP command handling
# Stores the regex patterns and their corresponding responses, making expansion easy
# Format: ( (Keywords_tuple), 'CogName', 'Method_to_call', requires_attachment )
NLP_COMMANDS = [
    (('roll', 'dice', 'advantage', 'disadvantage'), 'DiceRoller', 'roll', False),
    (('remind', 'reminder'), 'Reminders', 'reminder', False),
    (('convert', 'compress', 'resize'), 'ImageTools', 'none', True), # Image tools are special and have no defined method
]

# Main logic (general event handling and command routing)

@bot.event
async def on_ready():
    """Event handler for when the bot is connected to discord."""
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')
    print(f'{bot.user} is ready!')
    print('------')

# Simple ping command to check if the bot is responsive, only command that should not be using NLP
@bot.command()
async def ping(ctx):
    """Checks latency, responds with Pong! and latency in ms."""
    latency = bot.latency * 1000  # Convert to milliseconds
    await ctx.send(f'Pong! Latency: {latency:.2f}ms')
    logger.info(f'Ping command used by {ctx.author} in {ctx.guild}/{ctx.channel}')

# Add an event to catch errors
@bot.event
async def on_command_error(ctx, error):
    """Gracefully handles errors, no gurantee it will catch everything."""
    # We check first for commandnotfound to suppress errors from standard commands
    if isinstance(error, commands.CommandNotFound):
        # the on_message event will handle this as an NLP query so we can log it quietly
        logger.warning(f"Standard command not found, passing to NLP: '{ctx.message.content}' from '{ctx.author}' in '{ctx.guild}/{ctx.channel}'")
        return # No need to keep processing this error, it's expected
    
    # For other errors, we need to log them and inform the user
    logger.error(f"An error occurred with command '{ctx.command}': {error}", exc_info=True)
    raise error  # Re-raise the error for further handling if needed

@bot.event
async def on_message(message):
    """Event handler for ALL incoming messages."""
    if message.author == bot.user:
    # ignore messages from the bot itself
        return
    
    # Prioritize commands over NLP
    await bot.process_commands(message)

    # If standard command was processed, skip NLP
    if (await bot.get_context(message)).valid:
        return
    
    # if it's not a command, check for NLP patterns
    if message.content.startswith(bot.command_prefix):
        ctx = await bot.get_context(message)
        query = message.content[len(bot.command_prefix):].lower().strip() # pyright: ignore[reportArgumentType]

        logger.info(f"user '{ctx.author}' in guild '{ctx.guild}' used NLP with query: '{query}'")

        command_matched = False  # Flag to track if any command matched, declared here to avoid infinite loop

        # Now we check for each NLP command pattern
        for keywords, cog_name, method_name, requires_attachment in NLP_COMMANDS:
            if any(keyword in query for keyword in keywords):
                command_matched = True # Set flag to true if any command matches, which prevents spamming the no match message

                # handle image tools first since they require attachments
                if requires_attachment:
                    if not message.attachments:
                        await ctx.send("Please attach an image to use this command.")
                        return
                    await ctx.send("For image tools, please use the specific command such as `.sancho convert filetype`, `.sancho compress`, or `.sancho resize`.")
                    return
                
                cog = bot.get_cog(cog_name)
                if not cog:
                    logger.error(f"NLP dispatcher: Cog '{cog_name}' is not loaded or does not exist.")
                
                method = getattr(cog, method_name, None) # This bothers me, fix later
                if not method:
                    logger.error(f"NLP dispatcher: Method '{method_name}' not found in cog '{cog_name}'.")
                    continue

                # Call method from the cog, preparing context for each of them.
                if cog_name == 'DiceRoller':
                    await method(ctx, roll_string=query)
                elif cog_name == 'Reminders':
                    # This one needs much more clean up and parsing
                    clean_query = query.replace('remind me', '').replace('remind', '').replace('reminder','').strip()
                    await method(ctx, time_and_reminder=clean_query)

                # more commands can be added here in the future

                break  # Prevent multiple matches
            
        # If no patterns matched
        if not command_matched:
            await ctx.send("Sorry, I didn't understand your query, please use .help to help format your query, if the problem persists, please contact the author.")
            logger.warning(f"NLP dispatcher: No matching patterns found for '{message.content}' from '{ctx.author}.")

# Main routine to load cogs and start the bot
# Each cog is in it's own file in the cogs/ directory

async def main():
    """Main entry point to load cogs and start the bot."""
    logger.info("Sancho starting...")

    # Load all cogs from the cogs directory
    for filename in os.listdir('./cogs'):
        # Do not load __init__.py or non-python files
        if filename.startswith('__'):
            continue

        if filename.endswith('.py'):
            extension = f'cogs.{filename[:-3]}'
            try:
                await bot.load_extension(extension)
                logger.info(f'Loaded extension: {extension}')
            except Exception as e:
                logger.error(f'Failed to load extension {extension}.', exc_info=True)
        
    # Start the bot
    try:
        await bot.start(TOKEN)
    except Exception as e:
        logger.critical(f"Sancho encountered a critical error: {e}", exc_info=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Sancho is shutting down due to keyboard interrupt.")
import discord
from discord.ext import commands
import logging
import os
import re
from dotenv import load_dotenv

# Load environment variables from info.env file
load_dotenv(dotenv_path='info.env')
TOKEN = os.getenv('DISCORD_TOKEN')

# Check if the token is loaded successfully
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing in environment variables.")
    print("please create an info.env file containing your bot token as DISCORD_TOKEN=your_token_here")
    exit()

# Create cogs directory if it doesn't exist
if not os.path.exists('cogs'):
    os.makedirs('cogs')
    print("Created 'cogs' directory. Please add your cog files there.")

intent = discord.Intents.default()
intent.messages = True
intent.message_content = True

# Flexible prefix

bot = commands.Bot(command_prefix='.sancho ', intents=intent)

# logging setup
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = logging.FileHandler(filename='sancho.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

def log_command(ctx):
    """Logs who used a command, and what it was."""
    # Don't log the bot's own messages or messages with nothing in them
    if not ctx.author.bot and ctx.message.content:
        logger.info(f"User: '{ctx.author}' in guild: '{ctx.guild}' used command: '{ctx.message.content}'")

# Cogs category
# Loading cogs
initial_extensions = [
    'cogs.dice_roller',
    'cogs.reminders',
    'cogs.image_tools',
]

if __name__ == '__main__':
    for extension in initial_extensions:
        try:
            bot.load_extension(extension)
            print(f'Loaded extension: {extension}')
        except Exception as e:
            print(f'Failed to load extension {extension}: {e}')
            logger.error(f'Failed to load extension {extension}: {e}')

# Adding case specific commands
@bot.command()
async def ping(ctx):
    """Checks the ping of the bot, acts as a basic connectivity test, notably does NOT use NLP to check what the message means."""
    latency = bot.latency * 1000  # Convert to milliseconds
    await ctx.send(f'Pong! Latency: {latency:.2f}ms')
    # logging this specific command usage
    logger.info(f"User: '{ctx.author}' in guild: '{ctx.guild}' used command: 'ping'")

# NLP processing category
@bot.event
async def on_message(message):
    # This should tell the bot to check if the message is a standard command first.
    await bot.process_commands(message)

    # If it is a standard command, we want to stop here.
    # Coincidentally, this also stops the bot from responding to its own messages.
    if message.author == bot.user or (await bot.get_context(message)).valid:
        return
    
    # If the command isn't standard, then we can check if it is a natural language command.
    if message.content.startswith(bot.command_prefix):
        log_command(await bot.get_context(message))
        query = message.content[len(bot.command_prefix):].lower().strip()

        # interpretation for dice rolling
        if any(keyword in query for keyword in ['roll', 'dice', 'advantage', 'disadvantage']):
            dice_cog = bot.get_cog('DiceRoller')
            if dice_cog:
                await dice_cog.roll(await bot.get_context(message), roll_string=query)
            return
        
        # interpretation for reminders
        if 'remind' in query or 'reminder' in query:
            reminder_cog = bot.get_cog('Reminders')
            if reminder_cog:
                await reminder_cog.remind(await bot.get_context(message), time_and_reminder=query.replace('remind', '').replace('reminder', '').strip())
            return
        
        # interpretation for image tools
        if any(keyword in query for keyword in ['convert', 'compress', 'resize']):
            await message.channel.send("Please use the `.sancho convert`, `.sancho compress`, or `.sancho resize` with an immage attached.")
            return
        
        await message.channel.send("Sorry, I didn't understand that command. Please use bother the author to check this error.")
        logger.info(f"User: '{message.author}' in guild: '{message.guild}' sent an unrecognized command: '{message.content}'")
        return

# Run the bot
async def on_ready():
    print(f'Bot connected as {bot.user} (ID: {bot.user.id})')
    logger.info(f'Bot connected as {bot.user} (ID: {bot.user.id})')

bot.run(TOKEN)
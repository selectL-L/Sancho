import logging
import signal
import subprocess
import sys
import os
from typing import TYPE_CHECKING
import discord
import config

if TYPE_CHECKING:
    from .bot_class import SanchoBot


def is_system_rebooting():
    """Checks if the system is in the process of rebooting or shutting down."""
    # This check is only relevant on Linux systems with systemd.
    if not sys.platform.startswith('linux'):
        return False

    try:
        result = subprocess.run(
            ['systemctl', 'list-jobs'],
            capture_output=True, text=True, check=False
        )
        output = result.stdout
        # If a reboot or shutdown job is running, we consider it a system reboot.
        if 'reboot.target' in output or 'shutdown.target' in output:
            logging.info("System reboot or shutdown detected via systemctl.")
            return True
    except FileNotFoundError:
        # This will be triggered if systemctl is not found on a Linux system.
        logging.warning("Running on Linux, but 'systemctl' command not found. Assuming not a systemd reboot.")
        return False
    return False


async def startup_handler(bot: "SanchoBot"):
    """
    Handles the bot's startup sequence, including logging and sending a startup message.
    """
    if bot.user:
        logging.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    else:
        logging.error("Bot user information not available on ready.")

    logging.info("Connected to the following guilds:")
    for guild in bot.guilds:
        logging.info(f"- {guild.name} (ID: {guild.id})")

    if config.STARTUP_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.STARTUP_CHANNEL_ID)
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(title="Good morning, sancho is awake!")
                
                startup_gif_path = os.path.join(config.ASSETS_PATH, "startup.gif")
                if os.path.exists(startup_gif_path):
                    file = discord.File(startup_gif_path, filename="startup.gif")
                    embed.set_image(url="attachment://startup.gif")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Startup message sent to channel ID: {config.STARTUP_CHANNEL_ID}")
            else:
                logging.warning(
                    f"Startup channel ID {config.STARTUP_CHANNEL_ID} is not a valid text channel or could not be found."
                )
        except discord.HTTPException as e:
            logging.error(f"Failed to send startup message: {e}")


async def shutdown_handler(sig: signal.Signals, bot: "SanchoBot"):
    """
    Handles the graceful shutdown of the bot when a signal is received.
    """
    logging.info(f"Received exit signal {sig.name}...")

    # Determine the shutdown reason and prepare the message.
    rebooting = is_system_rebooting()
    if rebooting:
        logging.info("Shutdown initiated by a system reboot. Service should be back shortly...")
        embed = discord.Embed(
            title="Sancho is taking a small nap, Sancho will be back shortly!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "reboot.gif")
        attachment_name = "reboot.gif"
    else:
        logging.info("Shutdown initiated by a manual stop or exit.")
        embed = discord.Embed(
            title="Sancho is heading to bed. Goodnight!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "shutdown.gif")
        attachment_name = "shutdown.gif"

    # Send the shutdown message to the configured channel.
    if config.SHUTDOWN_CHANNEL_ID:
        channel = bot.get_channel(config.SHUTDOWN_CHANNEL_ID)
        if channel and isinstance(channel, discord.TextChannel):
            try:
                if os.path.exists(gif_path):
                    file = discord.File(gif_path, filename=attachment_name)
                    embed.set_image(url=f"attachment://{attachment_name}")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Shutdown message sent to channel ID: {config.SHUTDOWN_CHANNEL_ID}")
            except discord.HTTPException as e:
                logging.error(f"Failed to send shutdown message to channel {config.SHUTDOWN_CHANNEL_ID}: {e}")
        else:
            logging.warning(f"Shutdown channel ID {config.SHUTDOWN_CHANNEL_ID} configured but not found or not a text channel.")

    # Perform the graceful shutdown of the bot.
    logging.info("Closing connections...")
    await bot.close()
    logging.info("Discord connection has been shut down gracefully.")

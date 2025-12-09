"""utils/lifecycle.py

Handles the bot's startup and shutdown sequences.

This includes sending startup/shutdown messages to a configured channel and
detecting system reboots on Linux systems.
"""

import logging
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

import discord

import config

if TYPE_CHECKING:
    from .bot_class import CoreBot


def is_system_rebooting() -> bool:
    """Checks if the system is in the process of rebooting or shutting down.

    This check is only relevant on Linux systems with systemd.

    Returns:
        bool: True if a reboot/shutdown is detected, False otherwise.
    """
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


async def startup_handler(bot: "CoreBot") -> None:
    """Handles the bot's startup sequence.

    Includes logging and sending a startup message.

    Args:
        bot (CoreBot): The bot instance.
    """
    if bot.user:
        logging.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    else:
        logging.error("Bot user information not available on ready.")

    logging.info("Connected to the following guilds:")
    for guild in bot.guilds:
        logging.info(f"- {guild.name} (ID: {guild.id})")

    if config.SYSTEM_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(title=f"Good morning, {config.BOT_NAME} is awake!")

                startup_gif_path = os.path.join(config.ASSETS_PATH, "startup.gif")
                if os.path.exists(startup_gif_path):
                    file = discord.File(startup_gif_path, filename="startup.gif")
                    embed.set_image(url="attachment://startup.gif")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Startup message sent to channel ID: {config.SYSTEM_CHANNEL_ID}")
            else:
                logging.warning(
                    f"System channel ID {config.SYSTEM_CHANNEL_ID} is not a valid text channel or could not be found."
                )
        except discord.HTTPException as e:
            logging.error(f"Failed to send startup message: {e}")


async def shutdown_handler(sig: signal.Signals, bot: "CoreBot") -> None:
    """Handles the graceful shutdown of the bot when a signal is received.

    Args:
        sig (signal.Signals): The signal received.
        bot (CoreBot): The bot instance.
    """
    logging.info(f"Received exit signal {sig.name}...")

    # Determine the shutdown reason and prepare the message.
    rebooting = is_system_rebooting()
    if rebooting:
        logging.info("Shutdown initiated by a system reboot. Service should be back shortly...")
        embed = discord.Embed(
            title=f"{config.BOT_NAME} is taking a small nap, {config.BOT_NAME} will be back shortly!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "reboot.gif")
        attachment_name = "reboot.gif"
    else:
        logging.info("Shutdown initiated by a manual stop or exit.")
        embed = discord.Embed(
            title=f"{config.BOT_NAME} is heading to bed. Goodnight!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "shutdown.gif")
        attachment_name = "shutdown.gif"

    # Send the shutdown message to the configured channel.
    if config.SYSTEM_CHANNEL_ID:
        channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
        if channel and isinstance(channel, discord.TextChannel):
            try:
                if os.path.exists(gif_path):
                    file = discord.File(gif_path, filename=attachment_name)
                    embed.set_image(url=f"attachment://{attachment_name}")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Shutdown message sent to channel ID: {config.SYSTEM_CHANNEL_ID}")
            except discord.HTTPException as e:
                logging.error(f"Failed to send shutdown message to channel {config.SYSTEM_CHANNEL_ID}: {e}")
        else:
            logging.warning(f"System channel ID {config.SYSTEM_CHANNEL_ID} configured but not found or not a text channel.")

    # Perform the graceful shutdown of the bot.
    logging.info("Closing connections...")
    await bot.close()
    logging.info("Discord connection has been shut down gracefully.")

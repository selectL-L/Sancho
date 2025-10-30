"""
Defines the custom bot class, `SanchoBot`, which extends `commands.Bot`.
This allows for custom attributes and methods to be attached directly to the
bot instance, making them easily accessible throughout the application,
especially within cogs.
(Largely because without this, there are a lot of complaints from pylance.)
"""
import discord
from discord.ext import commands
from typing import TYPE_CHECKING
import logging
import os

import config

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """
    A custom bot class that extends `discord.ext.commands.Bot`.
    This class is used to attach custom attributes like the database manager
    to the bot instance, making it accessible from any cog.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These attributes are initialized after the bot object is created in main.py
        # They provide easy access to the database path and manager from anywhere
        # the bot object is available.
        self.db_path: str = ""
        if TYPE_CHECKING:
            self.db_manager: DatabaseManager

    async def on_ready(self) -> None:
        """
        Called when the bot is ready. Handles startup logging and sends a startup message.
        """
        if self.user:
            logging.info(f'Logged in as {self.user} (ID: {self.user.id})')
        else:
            logging.error("Bot user information not available on ready.")

        if config.STARTUP_CHANNEL_ID:
            try:
                channel = self.get_channel(config.STARTUP_CHANNEL_ID)
                if isinstance(channel, discord.TextChannel):
                    embed = discord.Embed(title="Good morning, sancho is awake!")
                    
                    if os.path.exists(config.STARTUP_GIF_PATH):
                        file = discord.File(config.STARTUP_GIF_PATH, filename="startup.gif")
                        embed.set_image(url="attachment://startup.gif")
                        await channel.send(embed=embed, file=file)
                    else:
                        await channel.send(embed=embed)
                else:
                    logging.warning(
                        f"Startup channel ID {config.STARTUP_CHANNEL_ID} is not a valid text channel or could not be found."
                    )
            except discord.HTTPException as e:
                logging.error(f"Failed to send startup message: {e}")

    async def close(self) -> None:
        """
        Handles bot shutdown, preferably gracefully.
        This sends a final message to a designated channel before closing the connection.
        """
        logging.info("Attempting shutdown...")
        if config.SHUTDOWN_CHANNEL_ID:
            try:
                channel = self.get_channel(config.SHUTDOWN_CHANNEL_ID)
                if isinstance(channel, discord.TextChannel):
                    embed = discord.Embed(title="Sancho is heading to bed, goodnight!")

                    if os.path.exists(config.SHUTDOWN_GIF_PATH):
                        file = discord.File(config.SHUTDOWN_GIF_PATH, filename="shutdown.gif")
                        embed.set_image(url="attachment://shutdown.gif")
                        await channel.send(embed=embed, file=file)
                    else:
                        await channel.send(embed=embed)
                else:
                    logging.warning(
                        f"Shutdown channel ID {config.SHUTDOWN_CHANNEL_ID} is not a valid text channel or could not be found."
                    )
            except discord.HTTPException as e:
                logging.error(f"Failed to send shutdown message: {e}")
        
        await super().close()
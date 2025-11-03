"""
Defines the custom bot class, `SanchoBot`, which extends `commands.Bot`.
This allows for custom attributes and methods to be attached directly to the
bot instance, making them easily accessible throughout the application,
especially within cogs.
(Largely because without this, there are a lot of complaints from pylance.)
"""
from __future__ import annotations
import discord
from discord.ext import commands
from typing import Optional, TYPE_CHECKING
import asyncio
import logging

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """
    A custom bot class that extends `discord.ext.commands.Bot` to include
    additional attributes for managing the database and skill limits.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_manager: Optional[DatabaseManager] = None
        self.console_task: Optional[asyncio.Task] = None

    async def close(self) -> None:
        """
        Overrides the default close method to ensure a clean shutdown.
        The actual shutdown message is now handled by the signal handler
        in `shutdown_logic.py`.
        """
        # Cancel the console listener task if it's running
        if self.console_task and not self.console_task.done():
            self.console_task.cancel()

        logging.info("Closing bot connection...")
        await super().close()
        logging.info("Connection closed.")
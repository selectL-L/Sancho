"""utils/base_cog.py

Defines a base class for all cogs to inherit from.
This provides shared functionality, consistent structure, and lifecycle hooks.

Lifecycle Hooks:
    cog_load(): Called when cog module is loaded. Use for minimal registration
                only - NO background tasks, NO network calls.
    cog_ready(): Called after bot is fully connected and ready. Use for starting
                 background tasks, schedulers, and recovery operations.
    cog_unload(): Called during shutdown before disconnect. Use for graceful
                  cleanup - stop tasks, close sessions.
"""
import logging
from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot_class import CoreBot


class BaseCog(commands.Cog):
    """A base cog that all other cogs should inherit from.

    Provides a dedicated logger instance and lifecycle hook stubs.
    """

    def __init__(self, bot: "CoreBot"):
        """Initializes the BaseCog.

        Args:
            bot: The CoreBot instance.
        """
        self.bot: "CoreBot" = bot
        # Create a logger that is specific to the cog's class name
        self.logger = logging.getLogger(self.__class__.__name__)

    async def cog_ready(self) -> None:
        """Called after the bot is fully connected and ready.

        Override this method to start background tasks, schedulers,
        process missed items, or perform any operations that require
        the bot to be connected to Discord.

        This is called AFTER the bot logs "Bot is ready!" and can
        safely send messages to channels/users.
        """
        pass

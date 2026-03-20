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

Readiness Gate:
    Cogs that override cog_ready() start with _cog_is_ready = False.
    The flag is set to True by ready_all_cogs() after cog_ready() completes.
    Cogs that do NOT override cog_ready() are always ready (True from init).
    NLP handlers should check _cog_is_ready before processing commands.
"""
import logging
from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot_class import CoreBot


class BaseCog(commands.Cog):
    """A base cog that all other cogs should inherit from.

    Provides a dedicated logger instance, lifecycle hook stubs, and a
    readiness gate that prevents NLP handlers from running before
    cog_ready() has completed.
    """

    def __init__(self, bot: "CoreBot"):
        """Initializes the BaseCog.

        Args:
            bot: The CoreBot instance.
        """
        self.bot: "CoreBot" = bot
        # Create a logger that is specific to the cog's class name
        self.logger = logging.getLogger(self.__class__.__name__)
        # Cogs that override cog_ready() start as not-ready;
        # cogs that don't override it are immediately ready.
        self._cog_is_ready: bool = type(self).cog_ready is BaseCog.cog_ready

    async def _not_ready_response(self, ctx: commands.Context) -> None:
        """Sends a default response when the cog hasn't finished initializing.

        Override this in a subclass to customize the message.

        Args:
            ctx: The command context.
        """
        await ctx.send(
            f"\u23f3 **{self.__class__.__name__}** is still coming up to speed, "
            f"try again in a bit!"
        )

    async def cog_ready(self) -> None:
        """Called after the bot is fully connected and ready.

        Override this method to start background tasks, schedulers,
        process missed items, or perform any operations that require
        the bot to be connected to Discord.

        This is called AFTER the bot logs "Bot is ready!" and can
        safely send messages to channels/users.

        After this method returns successfully, _cog_is_ready is set
        to True by ready_all_cogs().
        """

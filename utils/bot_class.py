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
import config

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
        # The case_insensitive_prefix callable is now part of the bot.
        # We set it before calling super().__init__ so it's available.
        kwargs['command_prefix'] = self._get_case_insensitive_prefix
        super().__init__(*args, **kwargs)
        self.db_manager: Optional[DatabaseManager] = None
        self.console_task: Optional[asyncio.Task] = None

    def _get_case_insensitive_prefix(self, bot: "SanchoBot", message: discord.Message) -> list[str]:
        """
        A callable that returns a list of prefixes, making them case-insensitive.
        This is a method of the bot class for better encapsulation.
        """
        content_lower = message.content.lower()
        
        # Find all prefixes that match the start of the message.
        matching_prefixes = [p for p in config.BOT_PREFIX if content_lower.startswith(p.lower())]
        
        if matching_prefixes:
            # Sort by length descending to handle overlapping prefixes (e.g., '!' and '!!')
            matching_prefixes.sort(key=len, reverse=True)
            longest_match = matching_prefixes[0]
            # Return the slice of the original message that corresponds to the prefix length.
            return [message.content[:len(longest_match)]]

        # `when_mentioned` will handle mentions if no other prefix matches.
        return commands.when_mentioned(bot, message)

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
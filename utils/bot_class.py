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
import time

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """
    A custom bot class that extends `discord.ext.commands.Bot` to include
    additional attributes and to centralize initialization.
    """
    def __init__(self, **kwargs):
        # Define intents directly within the class for encapsulation.
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True

        # Call super().__init__ with all configuration handled internally.
        # We pass `config.OWNER_ID or 0` to allow for proper testing of
        # owner-only commands when the OWNER_ID is not set in the .env file.
        super().__init__(
            command_prefix=self._get_case_insensitive_prefix,
            intents=intents,
            case_insensitive=True,
            owner_id=config.OWNER_ID,
            **kwargs
        )
        
        self.db_manager: Optional[DatabaseManager] = None
        self.console_task: Optional[asyncio.Task] = None
        self.start_time: float = time.time()

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
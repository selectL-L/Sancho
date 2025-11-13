"""
Defines the custom bot class, `SanchoBot`, which extends `discord.ext.commands.Bot`.

This class is the central hub of the bot's functionality. It is responsible for:
- Storing shared application state (like the database manager).
- Handling core Discord events (`on_ready`, `on_message`, `on_command_error`).
- Processing incoming messages to dispatch both standard and NLP-based commands.
- Encapsulating bot-specific configuration and helper methods.
"""
from __future__ import annotations
import discord
from discord.ext import commands
from typing import Optional, TYPE_CHECKING, Any
from collections.abc import Callable
import asyncio
import logging
import config
import time
import re
from utils.lifecycle import startup_handler
from utils.extensions import discover_cogs

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """
    The main bot class, extending `discord.ext.commands.Bot` to integrate
    custom functionality and centralize event handling.

    This class holds shared resources like the database manager and defines the
    core logic for command processing, including the NLP dispatcher.
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

    async def on_ready(self):
        """Called when the bot is ready; triggers the startup handler."""
        await startup_handler(self)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """
        Global error handler for all standard `discord.ext.commands`.
        This catches errors from commands defined with `@bot.command()`.
        """
        # Ignore `CommandNotFound` errors, as the `on_message` handler will treat
        # these as potential NLP commands. This prevents duplicate error messages.
        if isinstance(error, commands.CommandNotFound):
            return

        # For user input errors (e.g., missing arguments), show the command's help message
        # to guide the user on correct usage.
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send_help(ctx.command)
            return

        # Handle permission errors gracefully. `NotOwner` is a subclass of `CheckFailure`.
        if isinstance(error, commands.CheckFailure):
            logging.warning(f"User '{ctx.author}' failed check for command '{ctx.command}': {error}")
            # Send a silent or ephemeral message if possible, or just a simple public one.
            try:
                await ctx.send("Sorry, you don't have permission to use this command.", delete_after=8)
            except discord.HTTPException:
                pass # Ignore if we can't send the message
            return

        # For all other errors, log the full traceback for debugging purposes.
        logging.error(f"Unhandled error in command '{ctx.command}'", exc_info=error)

        # Notify the user that a generic, unexpected error occurred.
        try:
            await ctx.send("Sorry, an unexpected error occurred. The issue has been logged.")
        except discord.HTTPException:
            logging.error(f"Failed to send error message to channel {ctx.channel.id}")

    async def on_message(self, message: discord.Message) -> None:
        """
        The main event handler for processing all incoming messages.
        This function serves as the core dispatcher for NLP-based commands.
        """
        # Ignore messages from the bot itself to prevent loops.
        if message.author.bot:
            return

        # If in developer mode, only respond to the owner.
        if config.DEV_MODE and message.author.id != config.OWNER_ID:
            return

        # First, allow `discord.py` to process the message to see if it's a
        # standard, decorator-based command (like `.ping`).
        await self.process_commands(message)

        # If the message was a standard command, we don't need to process it for NLP.
        # `ctx.valid` will be True if a valid command was found and invoked.
        ctx = await self.get_context(message)
        if ctx.valid:
            return

        # --- NLP Processing Logic ---
        # Check if the message starts with one of the recognized bot prefixes (case-insensitive).
        prefix_used = None
        content_lower = message.content.lower()
        for p in config.BOT_PREFIX:
            if content_lower.startswith(p.lower()):
                # Find the actual prefix used from the original message content
                # to correctly slice it off later.
                prefix_used = message.content[:len(p)]
                break

        # If no valid prefix was found, it's not an NLP command, so we ignore it.
        if not prefix_used:
            return

        # Extract the user's query by removing the prefix and any leading/trailing whitespace.
        query = message.content[len(prefix_used):].strip()
        if not query:
            return  # Ignore messages that are just the prefix.

        query_lower = query.lower()
        logging.info(f"NLP query from '{message.author}': '{query}'")

        # --- NLP Command Matching Logic ---
        
        # Step 1: Find the best candidate from each command group.
        # A candidate is the first command in a group that matches the query.
        candidate_commands = []
        for group in config.NLP_COMMANDS:
            for keywords, cog_name, method_name in group:
                for keyword in keywords:
                    match = re.search(keyword, query_lower)
                    if match:
                        # Found a winner for this group. Store it and move to the next group.
                        candidate_commands.append({'match_pos': match.start(), 'cog': cog_name, 'method': method_name})
                        break  # Stop searching this group
                else:
                    # If the inner loop (keywords) completes without a match, continue to the next command.
                    continue
                # If the inner loop was broken (a match was found), break the outer loop to move to the next group.
                break
        
        # If no commands matched at all, do nothing.
        if not candidate_commands:
            return

        # Step 2: From the candidates, find the one that appears earliest in the query.
        best_command = min(candidate_commands, key=lambda x: x['match_pos'])
        
        cog_name = best_command['cog']
        method_name = best_command['method']

        cog = self.get_cog(cog_name)
        if not cog:
            logging.error(f"NLP dispatcher: Winning cog '{cog_name}' is not loaded.")
            return

        method: Optional[Callable[..., Any]] = getattr(cog, method_name, None)
        if not (method and asyncio.iscoroutinefunction(method)):
            logging.error(f"NLP dispatcher: Winning method '{method_name}' in '{cog_name}' is not an awaitable coroutine.")
            return

        try:
            # Call the winning NLP handler.
            await method(ctx, query=query)
        except Exception as e:
            logging.error(f"Error in NLP command '{cog_name}.{method_name}': {e}", exc_info=True)
            await ctx.send("Sorry, an internal error occurred. The issue has been logged.")

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

    async def reload_all_cogs(self):
        """
        Asynchronously discovers and reloads all cogs, handling new, removed,
        and updated extensions.
        """
        logging.info("Starting cog reload process...")

        # Get the set of currently loaded extension names (e.g., {'cogs.fun', 'cogs.math'})
        loaded_cogs = set(self.extensions.keys())
        logging.info(f"Currently loaded cogs: {loaded_cogs or 'None'}")

        # Discover the cogs currently present in the filesystem.
        try:
            discovered_cogs = set(discover_cogs(config.COGS_PATH))
            logging.info(f"Discovered cogs in filesystem: {discovered_cogs or 'None'}")
        except Exception as e:
            logging.error(f"Failed to discover cogs: {e}", exc_info=True)
            return

        # --- Determine which cogs to load, unload, and reload ---
        cogs_to_load = discovered_cogs - loaded_cogs
        cogs_to_unload = loaded_cogs - discovered_cogs
        cogs_to_reload = loaded_cogs.intersection(discovered_cogs)

        # --- Perform actions ---
        # 1. Unload cogs that have been removed.
        for extension in cogs_to_unload:
            try:
                await self.unload_extension(extension)
                logging.info(f"Successfully unloaded removed extension: {extension}")
            except Exception:
                logging.error(f'Failed to unload extension {extension}.', exc_info=True)

        # 2. Load new cogs that have been added.
        for extension in cogs_to_load:
            try:
                await self.load_extension(extension)
                logging.info(f"Successfully loaded new extension: {extension}")
            except Exception:
                logging.error(f'Failed to load new extension {extension}.', exc_info=True)

        # 3. Reload existing cogs to apply any changes.
        for extension in cogs_to_reload:
            try:
                await self.reload_extension(extension)
                logging.info(f"Successfully reloaded extension: {extension}")
            except Exception:
                logging.error(f'Failed to reload extension {extension}.', exc_info=True)

        logging.info("Finished reloading cogs.")
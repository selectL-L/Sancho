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
from discord import app_commands
from typing import Optional, TYPE_CHECKING, Any, Protocol, runtime_checkable
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

    @runtime_checkable
    class ContextLike(Protocol):
        """A Protocol describing the minimal Context-like object required by NLP handlers."""
        author: Any
        guild: Any
        channel: Any
        async def send(self, *args, **kwargs) -> Any: ...

    async def dispatch_nlp(self, ctx: "SanchoBot.ContextLike", query: str) -> None:
        """Dispatch a natural-language `query` using the same NLP dispatcher logic
        that `on_message` uses. This allows hybrid/slash commands to forward a
        query while preserving the original context (`ctx`).

        This method intentionally mirrors the command-dispatch portion of the
        `on_message` pipeline and will call the matched cog method with
        `await method(ctx, query=query)`.
        """
        try:
            q_lower = query.lower()
            handler = self.find_nlp_handler(q_lower)
            if not handler:
                return

            cog, method, method_name = handler
            if asyncio.iscoroutinefunction(method):
                await method(ctx, query=query)
            else:
                assert method is not None
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: method(ctx, query=query))
        except Exception:
            try:
                logging.getLogger(__name__).exception("Error dispatching NLP query")
            except Exception:
                pass

    def find_nlp_handler(self, query_lower: str) -> Optional[tuple[object, Callable[..., Any], str]]:
        """Find the best matching NLP handler for `query_lower`.

        Returns a tuple `(cog, method, method_name)` or `None` if no handler
        matched. This centralizes the NLP matching logic so both `on_message`
        and `dispatch_nlp` can reuse it.
        """
        # Step 1: Find a candidate per group (first matching command in a group)
        candidate_commands = []
        for group in config.NLP_COMMANDS:
            for keywords, cog_name, method_name in group:
                for keyword in keywords:
                    try:
                        m = re.search(keyword, query_lower)
                    except Exception:
                        m = None

                    if m:
                        candidate_commands.append({'match_pos': m.start(), 'cog': cog_name, 'method': method_name})
                        break
                else:
                    continue
                break

        if not candidate_commands:
            return None

        # Step 2: Pick the earliest match across groups
        best_command = min(candidate_commands, key=lambda x: x['match_pos'])
        cog_name = best_command['cog']
        method_name = best_command['method']

        cog = self.get_cog(cog_name)
        if not cog:
            logging.error(f"NLP dispatcher: Winning cog '{cog_name}' is not loaded.")
            return None

        method = getattr(cog, method_name, None)
        if not method:
            logging.error(f"NLP dispatcher: Winning method '{method_name}' in '{cog_name}' not found.")
            return None

        return cog, method, method_name

    class InteractionContextAdapter:
        """A thin adapter that exposes the subset of `commands.Context` used by
        NLP handlers, backed by a `discord.Interaction`.

        Many NLP handlers expect `ctx.author`, `ctx.guild`, `ctx.channel`, and
        `await ctx.send(...)`. This adapter provides those attributes and maps
        `send` to the interaction response/followup so slash commands can use the
        same handlers without modification.
        """
        def __init__(self, bot: "SanchoBot", interaction: discord.Interaction):
            self.bot = bot
            self.interaction = interaction
            self.author = interaction.user
            self.guild = interaction.guild
            # `interaction.channel` can be None in some contexts; keep reference
            self.channel = interaction.channel

        async def _send_to_channel(self, *args, **kwargs):
            """Helper to attempt sending via the channel if possible and return
            the sent Message when available."""
            try:
                if self.channel and isinstance(self.channel, discord.abc.Messageable):
                    return await self.channel.send(*args, **kwargs)
            except Exception:
                # Channel send failed; fall through to interaction-based sending.
                pass
            return None

        async def send(self, *args, **kwargs):
            # Prefer sending directly to the channel (makes behavior match
            # prefix-based flows). When using slash commands we defer the
            # interaction, so sending to the channel is safe. If channel-based
            # sending fails, fall back to the interaction response/followup.
            try:
                sent = await self._send_to_channel(*args, **kwargs)
                if sent is not None:
                    return sent

                if not self.interaction.response.is_done():
                    await self.interaction.response.send_message(*args, **kwargs)
                    # Try to capture the original response as a Message and update channel.
                    try:
                        msg = await self.interaction.original_response()
                        if msg and hasattr(msg, 'channel') and msg.channel is not None:
                            self.channel = msg.channel
                        return msg
                    except Exception:
                        return None
                else:
                    return await self.interaction.followup.send(*args, **kwargs)
            except Exception:
                # As a final fallback, DM the author.
                try:
                    return await self.author.send(*args, **kwargs)
                except Exception:
                    return None

    async def on_ready(self):
        """Called when the bot is ready; triggers the startup handler."""
        await startup_handler(self)

        # Register an application command for forwarding NLP queries if it isn't already registered.
        # The command lives in the bots core just like NLP does.
        try:
            if not self.tree.get_command('nlp'):
                async def _nlp_app(interaction: discord.Interaction, query: str):
                    # Immediately acknowledge the slash command with a short,
                    # ephemeral message so the user sees the command was received
                    # and there's no persistent "thinking" state. Then hand off
                    # processing to the NLP dispatcher which will post normal
                    # messages into the channel as needed.
                    try:
                        await interaction.response.send_message("Forwarding query to NLP...", ephemeral=True)
                    except Exception:
                        # If sending the ephemeral message fails, try to defer as a fallback. (honest to god, I hate this)
                        try:
                            await interaction.response.defer()
                        except Exception:
                            pass

                    ctx_adapter = SanchoBot.InteractionContextAdapter(self, interaction)
                    # Run the NLP dispatcher; no need to await in a special way —
                    # the user already received the ephemeral message.
                    await self.dispatch_nlp(ctx_adapter, query)

                cmd = app_commands.Command(name='nlp', description='Forward a natural-language query to the NLP dispatcher', callback=_nlp_app)
                self.tree.add_command(cmd)

                # Note: global sync can takeup to an hour to propagate to all guilds.
                try:
                    await self.tree.sync()
                    logging.info("Synced app commands globally")
                except Exception:
                    logging.exception("Failed to sync app commands globally")
        except Exception:
            logging.exception('Failed to register NLP application command')

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
            help_cog = self.get_cog('Help')
            if help_cog:
                # We need to tell Pylance that this cog has the method.
                # In a real scenario, you might define a Protocol for this.
                await getattr(help_cog, "send_command_help")(ctx, ctx.command)
            else:
                # Fallback to default behavior if Help cog isn't available
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
                prefix_used = message.content[:len(p)]
                break

        if not prefix_used:
            return

        query = message.content[len(prefix_used):].strip()
        if not query:
            return

        query_lower = query.lower()
        logging.info(f"NLP query from '{message.author}': '{query}'")

        # Use the centralized NLP matcher to find the handler.
        handler = self.find_nlp_handler(query_lower)
        if not handler:
            return

        cog, method, method_name = handler
        try:
            if asyncio.iscoroutinefunction(method):
                await method(ctx, query=query)
            else:
                assert method is not None
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: method(ctx, query=query))
        except Exception as e:
            logging.error(f"Error in NLP command '{cog.__class__.__name__}.{method_name}': {e}", exc_info=True)
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
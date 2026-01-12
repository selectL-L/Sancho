"""utils/bot_class.py

Defines the custom bot class, `CoreBot`, which extends `discord.ext.commands.Bot`.

This class is the central hub of the bot's functionality. It is responsible for:
- Storing shared application state (like the database manager).
- Handling core Discord events (`on_ready`, `on_message`, `on_command_error`).
- Processing incoming messages to dispatch both standard and NLP-based commands.
- Encapsulating bot-specific configuration and helper methods.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.extensions import discover_cogs
from utils.lifecycle import startup_handler

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager
    from utils.logging_config import ResourceTracker


class CoreBot(commands.Bot):
    """The main bot class, extending `discord.ext.commands.Bot`.

    This class integrates custom functionality and centralizes event handling.
    It holds shared resources like the database manager and defines the
    core logic for command processing, including the NLP dispatcher.
    """

    def __init__(self, **kwargs):
        """Initializes the CoreBot instance."""
        # Define intents directly within the class for encapsulation.
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.members = True  # Required for guild.get_member() to work from cache

        # Call super().__init__ with all configuration handled internally.
        # We pass `owner_ids` to prevent auto-fetching application info.
        # If OWNER_IDS is set, we use it. If not, we pass {0} to ensure no fetch happens.
        owner_ids = config.OWNER_IDS if config.OWNER_IDS else {0}
        super().__init__(
            command_prefix=self._get_case_insensitive_prefix,
            intents=intents,
            case_insensitive=True,
            owner_ids=owner_ids,
            **kwargs
        )

        self.db_manager: Optional[DatabaseManager] = None
        self.resource_tracker: Optional[ResourceTracker] = None
        self.console_task: Optional[asyncio.Task] = None
        self.restart_signal: bool = False
        self.start_time: float = time.time()
        self._dynamic_nlp_groups: list[list[tuple[tuple[str, ...], str, str]]] = []

        # Visibility control - determines Discord presence status
        # Maps string names to discord.Status enum values
        self._visibility_map = {
            'online': discord.Status.online,
            'idle': discord.Status.idle,
            'dnd': discord.Status.dnd,
            'invisible': discord.Status.invisible,
        }
        self._current_visibility: discord.Status = self._visibility_map.get(
            config.DEFAULT_VISIBILITY, discord.Status.online
        )

    # =========================================================================
    # VISIBILITY CONTROL
    # =========================================================================

    @property
    def current_visibility(self) -> discord.Status:
        """The current visibility status for the bot."""
        return self._current_visibility

    @property
    def is_visible(self) -> bool:
        """Whether the bot is currently visible (not invisible)."""
        return self._current_visibility != discord.Status.invisible

    async def set_visibility(self, status: str) -> bool:
        """Set the bot's visibility status.

        Args:
            status: One of 'online', 'idle', 'dnd', 'invisible'.

        Returns:
            True if the status was changed, False if invalid status.
        """
        if status not in self._visibility_map:
            return False

        self._current_visibility = self._visibility_map[status]
        logging.info(f"Visibility changed to: {status}")

        # Apply the new visibility immediately
        # If invisible, clear activity; otherwise preserve current activity
        if self._current_visibility == discord.Status.invisible:
            await self.change_presence(status=self._current_visibility, activity=None)
        else:
            # Re-apply current activity with new status
            # This triggers the presence loop to update if needed
            await self.change_presence(status=self._current_visibility)

        return True

    async def change_presence_safe(
        self,
        *,
        activity: Optional[discord.BaseActivity] = discord.utils.MISSING,
        status: Optional[discord.Status] = None,
    ) -> None:
        """Change presence respecting the current visibility setting.

        Use this instead of `change_presence` when the bot should NOT
        override an invisible status (e.g., music presence cycling).

        Args:
            activity: The activity to set. Use None to clear.
            status: The status to set. If None, uses current visibility.
        """
        # If bot is invisible, skip presence updates entirely
        if self._current_visibility == discord.Status.invisible:
            return

        # Use current visibility if no status specified
        effective_status = status if status is not None else self._current_visibility

        if activity is discord.utils.MISSING:
            await self.change_presence(status=effective_status)
        else:
            await self.change_presence(activity=activity, status=effective_status)

    @runtime_checkable
    class ContextLike(Protocol):
        """A Protocol describing the minimal Context-like object required by NLP handlers."""
        author: Any
        guild: Any
        channel: Any
        async def send(self, *args, **kwargs) -> Any: ...

    async def dispatch_nlp(self, ctx: "CoreBot.ContextLike", query: str) -> None:
        """Dispatch a natural-language `query` using the NLP dispatcher logic.

        This allows hybrid/slash commands to forward a query while preserving
        the original context (`ctx`). This method intentionally mirrors the
        command-dispatch portion of the `on_message` pipeline.

        Args:
            ctx (CoreBot.ContextLike): The context-like object.
            query (str): The natural language query to dispatch.
        """
        try:
            q_lower = query.lower()
            handler = self.find_nlp_handler(q_lower)
            if not handler:
                return

            _cog, method, _method_name = handler
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

        This centralizes the NLP matching logic so both `on_message`
        and `dispatch_nlp` can reuse it.

        Args:
            query_lower (str): The query string in lowercase.

        Returns:
            Optional[tuple[object, Callable[..., Any], str]]: A tuple containing
            (cog, method, method_name) or `None` if no handler matched.
        """
        # Find a candidate per group (first matching command in a group)
        # Check both static (config.py) and dynamic (cog-registered) groups
        candidate_commands = []
        for group in config.NLP_COMMANDS + self._dynamic_nlp_groups:
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

        # Pick the earliest match across groups
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

    def register_nlp_group(self, entries: list[tuple[tuple[str, ...], str, str]]) -> None:
        """Register a group of NLP commands dynamically.

        Cogs can call this to add their own NLP patterns without
        polluting config.py. Dynamic groups are checked after
        static NLP_COMMANDS groups (lower priority).

        Args:
            entries: A list of NLP command tuples, each containing
                (keyword_patterns, cog_name, method_name).
        """
        self._dynamic_nlp_groups.append(entries)

    def register_nlp_command(self) -> None:
        """Registers the /nlp slash command if not already registered.

        This command forwards natural-language queries to the NLP dispatcher,
        allowing slash command users to access the same NLP functionality.
        """
        if self.tree.get_command('nlp'):
            return  # Already registered

        async def _nlp_app(interaction: discord.Interaction, query: str):
            # Log command usage similar to how prefix-based NLP does it.
            logging.info(f"slash NLP query from '{interaction.user}': '{query}'")
            try:
                # Immediately acknowledge the slash command with an ephemeral message, prevents persistent "thinking" state.
                await interaction.response.send_message("Forwarding query to NLP...", ephemeral=True)
            except Exception:
                # If sending the ephemeral message fails, try to defer as a fallback.
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

            ctx_adapter = CoreBot.InteractionContextAdapter(self, interaction)
            # Run the NLP dispatcher; no need to await in a special way —
            # the user already received the ephemeral message.
            await self.dispatch_nlp(ctx_adapter, query)

        cmd = app_commands.Command(name='nlp', description='Forward a natural-language query to the NLP dispatcher', callback=_nlp_app)
        self.tree.add_command(cmd)
        logging.info("Registered /nlp application command")

    class InteractionContextAdapter:
        """A thin adapter that exposes the subset of `commands.Context` used by NLP handlers.

        Backed by a `discord.Interaction`. Many NLP handlers expect `ctx.author`,
        `ctx.guild`, `ctx.channel`, and `await ctx.send(...)`. This adapter
        provides those attributes and maps `send` to the interaction response/followup.
        """

        def __init__(self, bot: "CoreBot", interaction: discord.Interaction):
            """Initializes the InteractionContextAdapter.

            Args:
                bot (CoreBot): The bot instance.
                interaction (discord.Interaction): The interaction to adapt.
            """
            self.bot = bot
            self.interaction = interaction
            self.author = interaction.user
            self.guild = interaction.guild
            # `interaction.channel` can be None in some contexts; keep reference
            self.channel = interaction.channel

        async def _send_to_channel(self, *args, **kwargs):
            """Helper to attempt sending via the channel if possible.

            Returns:
                Optional[discord.Message]: The sent message, or None if failed.
            """
            try:
                if self.channel and isinstance(self.channel, discord.abc.Messageable):
                    return await self.channel.send(*args, **kwargs)
            except Exception:
                # Channel send failed; fall through to interaction-based sending.
                pass
            return None

        async def send(self, *args, **kwargs):
            """Sends a message using the interaction or channel.

            Prefer sending directly to the channel (makes behavior match
            prefix-based flows). When using slash commands we defer the
            interaction, so sending to the channel is safe. If channel-based
            sending fails, fall back to the interaction response/followup.
            """
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

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Global error handler for all standard `discord.ext.commands`.

        This catches errors from commands defined with `@bot.command()`.

        Args:
            ctx (commands.Context): The command context.
            error (commands.CommandError): The error that occurred.
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
                await help_cog.send_command_help(ctx, ctx.command)  # type: ignore[attr-defined]
            else:
                # Fallback to default behavior if Help cog isn't available
                await ctx.send_help(ctx.command)
            return

        # Handle permission errors gracefully. `NotOwner` is a subclass of `CheckFailure`.
        if isinstance(error, commands.CheckFailure):
            logging.warning(f"User '{ctx.author}' failed check for command '{ctx.command}': {error}")
            # Send a silent or ephemeral message if possible, or just a simple public one.
            try:
                await ctx.send("Sorry, you don't have permission to use this command!", delete_after=8)
            except discord.HTTPException:
                pass  # Ignore if we can't send the message
            return

        # For all other errors, log the full traceback for debugging purposes.
        logging.error(f"Unhandled error in command '{ctx.command}'", exc_info=error)

        # Notify the user that a generic, unexpected error occurred.
        try:
            await ctx.send("Sorry, an unexpected error occurred. The issue has been logged. Please contact my author!")
        except discord.HTTPException:
            logging.error(f"Failed to send error message to channel {ctx.channel.id}")

    async def on_message(self, message: discord.Message) -> None:
        """The main event handler for processing all incoming messages.

        This function serves as the core dispatcher for NLP-based commands.

        Args:
            message (discord.Message): The incoming message.
        """
        # Ignore messages from the bot itself to prevent loops.
        if message.author.bot:
            return

        # If in developer mode, only respond to owners.
        if config.DEV_MODE and message.author.id not in config.OWNER_IDS:
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
        logging.info(f"prefix NLP query from '{message.author}': '{query}'")

        # Use the NLP matcher to find the handler.
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

    def _get_case_insensitive_prefix(self, bot: "CoreBot", message: discord.Message) -> list[str]:
        """A callable that returns a list of prefixes, making them case-insensitive.

        This is a method of the bot class for better encapsulation.

        Args:
            bot (CoreBot): The bot instance.
            message (discord.Message): The message to check.

        Returns:
            list[str]: A list of matching prefixes.
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
        """Overrides the default close method to ensure a clean shutdown.

        The actual shutdown message is handled by the signal handler in `shutdown_logic.py`.
        """
        # Cancel the console listener task if it's running
        if self.console_task and not self.console_task.done():
            self.console_task.cancel()

        logging.info("Closing bot connection...")
        await super().close()
        logging.info("Connection closed.")

    async def reload_all_cogs(self):
        """Asynchronously discovers and reloads all cogs.

        Handles new, removed, and updated extensions.
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

        # Unload cogs that have been removed.
        for extension in cogs_to_unload:
            try:
                await self.unload_extension(extension)
                logging.info(f"Successfully unloaded removed extension: {extension}")
            except Exception:
                logging.error(f'Failed to unload extension {extension}.', exc_info=True)

        # Load new cogs that have been added.
        for extension in cogs_to_load:
            try:
                await self.load_extension(extension)
                logging.info(f"Successfully loaded new extension: {extension}")
            except Exception:
                logging.error(f'Failed to load new extension {extension}.', exc_info=True)

        # Reload existing cogs to apply any changes.
        for extension in cogs_to_reload:
            try:
                await self.reload_extension(extension)
                logging.info(f"Successfully reloaded extension: {extension}")
            except Exception:
                logging.error(f'Failed to reload extension {extension}.', exc_info=True)

        logging.info("Finished reloading cogs.")

    async def ready_all_cogs(self) -> None:
        """Calls cog_ready() on all loaded cogs.

        This should be called after the bot is fully connected and ready,
        allowing cogs to start their background tasks and recovery operations.
        """
        for cog in self.cogs.values():
            cog_ready_method = getattr(cog, 'cog_ready', None)
            if cog_ready_method is not None:
                try:
                    await cog_ready_method()
                except Exception as e:
                    logging.error(f"Error in {cog.__class__.__name__}.cog_ready(): {e}", exc_info=True)

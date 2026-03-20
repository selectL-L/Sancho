"""cogs/fun.py

This cog contains miscellaneous "fun" commands that don't fit into other categories.
It includes commands like a magic 8-ball and other simple, interactive features.

Simple commands are defined in FUN_COMMANDS registry and handled by the dispatcher.
Complex commands (BOD, etc.) are implemented as regular methods.
"""

import asyncio
import os
import random
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp
import discord
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot


# =============================================================================
# COG CALL TYPES
# =============================================================================


@dataclass
class CogCallResult:
    """Complete, ready-to-post result from a delegated cog method.

    Target method does ALL processing and returns this.
    Fun dispatcher just posts it verbatim.

    Attributes:
        content: Text message to send.
        file: File attachment to send.
        embed: Embed to send.
    """

    content: Optional[str] = None
    file: Optional[discord.File] = None
    embed: Optional[discord.Embed] = None


class CogCallError(Exception):
    """Base exception for cog_call failures."""


class CogCallNoInput(CogCallError):
    """No valid input provided (no attachment, no reply, no URL)."""


class CogCallInvalidInput(CogCallError):
    """Input was provided but couldn't be processed (wrong format, too large, etc.)."""


class CogCallProcessingFailed(CogCallError):
    """Processing started but failed (ffmpeg error, API timeout, etc.)."""


# =============================================================================
# FUN COMMAND REGISTRY
# =============================================================================
# Define simple commands here. The dispatcher handles all the boilerplate.
# Complex commands (BOD, leaderboard, etc.) are regular methods below.


@dataclass
class FunCommand:
    """Definition for a simple fun command.

    Attributes:
        name: Method name and identifier (used by NLP dispatcher).
        patterns: Tuple of regex patterns that trigger this command.
        error_msg: Message shown when command fails.
        is_image: If True, send as file attachment. If False, send as text.
        content: Literal value - text string OR image filename in ASSETS_PATH.
        file: Read lines from this text file in ASSETS_PATH.
        attr: Read items from self.{attr} at runtime.
        cog_call: Tuple of (CogName, method_name) to delegate processing.
        cog_call_errors: Dict mapping exception types to error messages.
            Keys: 'no_input', 'invalid_input', 'processing_failed', 'unavailable'
        random: If True and source has multiple items, pick randomly.
        require_query: If True, user must provide text after trigger.
        query_error: Message shown when require_query=True but query is empty.
    """

    name: str
    patterns: Tuple[str, ...]
    error_msg: str = "Something went wrong!"

    # Output type
    is_image: bool = False

    # Source - set exactly ONE:
    content: Optional[str] = None
    file: Optional[str] = None
    attr: Optional[str] = None
    cog_call: Optional[Tuple[str, str]] = None
    cog_call_errors: Optional[Dict[str, str]] = None

    # Behavior modifiers
    random: bool = True
    require_query: bool = False
    query_error: str = "You need to provide something!"


# Registry of simple fun commands
FUN_COMMANDS: List[FunCommand] = [
    # Static images
    FunCommand(
        'sanitize', (r'\bsanitize\b', r'\bsanitise\b'),
        is_image=True, content='sanitize.webp',
        error_msg="I couldn't find my sanitizer!"),
    FunCommand(
        'pear_wiggler', (r'\bpear\s?wiggler\b',),
        is_image=True, content='pearwiggler.gif',
        error_msg="I couldn't find the wiggler of the pear variety!"),

    # Static text
    FunCommand(
        'issues', (r'\bissues?\b',),
        content='My issues page is [here](https://github.com/selectL-L/Sancho/issues) '  # Note, fix this to point towards Shiori's repo at some point.
                'please write your suggestions and issues over there!'),

    # Random from file
    FunCommand(
        'eight_ball', (r'\b8\s?-?ball',),
        file='8ball.txt',
        require_query=True,
        query_error="I cannot intuit from nothing!",
        error_msg="I seem to have lost my magic 8-ball..."),

    # Random quote from BOD fate system (treasure hunt - shows ONE quote)
    FunCommand(
        'yujin_quotes', (r'\byujin\s*quotes?\b',),
        attr='bod_quote_display',
        error_msg="No Yujin quotes have been configured yet."),
]

# Build lookup dict for __getattr__
_FUN_COMMAND_LOOKUP: Dict[str, FunCommand] = {cmd.name: cmd for cmd in FUN_COMMANDS}

# Auto-export for config.py - converts registry to NLP_COMMANDS format
FUN_NLP_ENTRIES: List[Tuple[Tuple[str, ...], str, str]] = [
    (cmd.patterns, 'Fun', cmd.name) for cmd in FUN_COMMANDS
]


class Fun(BaseCog):
    """A cog for fun, miscellaneous commands.

    Simple commands are defined in FUN_COMMANDS registry at module level.
    The __getattr__ method routes NLP dispatcher calls to _dispatch_fun_command.
    Complex commands (BOD, leaderboard) are implemented as regular methods.
    """

    BOD_CHAIN_DIALOGUE = [
        "First…", "Second…", "Third…", "Fourth…", "Fifth…",
        "Sixth…", "Seventh…", "Eighth…", "Ninth…", "Tenth…",
        "Eleventh…", "Twelfth…", "Thirteenth…", "Fourteenth…", "Fifteenth…",
        "Sixteenth…", "Seventeenth…", "Eighteenth…", "Nineteenth…",
        "Twentieth, and final… Be not afraid."
    ]

    def __init__(self, bot: CoreBot):
        """Initializes the Fun cog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager = bot.db_manager
        self.bod_timeout_tasks: Dict[int, asyncio.Task] = {}
        self.has_cleaned_up_chains = False
        # BOD Fate System
        self.bod_quote_triggers: Dict[int, List[Dict[str, Any]]] = {}
        self.bod_quote_display: List[str] = []

    def _load_bod_quotes(self) -> None:
        """Load BOD quote triggers from TOML file.

        Populates self.bod_quote_triggers and self.bod_quote_display.
        Logs warning if file is missing or malformed.
        """
        quotes_path = os.path.join(config.ASSETS_PATH, 'bod_quotes.toml')
        try:
            with open(quotes_path, 'rb') as f:
                data = tomllib.load(f)

            # Load display quotes
            self.bod_quote_display = data.get('quotes', {}).get('list', [])

            # Load triggers - convert string keys to int
            raw_triggers = data.get('triggers', {})
            self.bod_quote_triggers = {}
            for chain_pos, trigger_list in raw_triggers.items():
                try:
                    chain_int = int(chain_pos)
                    self.bod_quote_triggers[chain_int] = trigger_list
                except ValueError:
                    self.logger.warning(f"Invalid chain position '{chain_pos}' in bod_quotes.toml - skipping")

            self.logger.info(f"Loaded {len(self.bod_quote_display)} BOD quotes and {len(self.bod_quote_triggers)} trigger positions.")
        except FileNotFoundError:
            self.logger.warning("bod_quotes.toml not found. BOD fate triggers will be disabled.")
        except tomllib.TOMLDecodeError as e:
            self.logger.error(f"Failed to parse bod_quotes.toml: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error loading bod_quotes.toml: {e}", exc_info=True)

    # ==========================================================================
    # Fun Command Registry Dispatcher
    # ==========================================================================

    def __getattr__(self, name: str) -> Any:
        """Dynamic method resolution for registered fun commands.

        When the NLP dispatcher calls getattr(cog, 'sanitize'), this method
        intercepts the lookup, finds the FunCommand entry in the registry,
        and returns a handler that routes through _dispatch_fun_command.

        Complex commands (BOD, bod_leaderboard, etc.) are defined as regular
        methods and take precedence over this lookup.

        Args:
            name: The attribute name being accessed.

        Returns:
            An async handler function for registered commands.

        Raises:
            AttributeError: If name is not a registered command.
        """
        cmd = _FUN_COMMAND_LOOKUP.get(name)
        if cmd is not None:
            async def handler(ctx: commands.Context, query: str) -> None:
                if not self._cog_is_ready:
                    await self._not_ready_response(ctx)
                    return
                await self._dispatch_fun_command(cmd, ctx, query)
            return handler
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    async def _dispatch_fun_command(
        self,
        cmd: FunCommand,
        ctx: commands.Context,
        query: str
    ) -> None:
        """Execute a registered fun command.

        Handles query validation, source resolution, random selection,
        output formatting (text vs image), and error handling.

        Args:
            cmd: The FunCommand definition from the registry.
            ctx: The command context.
            query: The user's full query string.
        """
        # Route cog_call commands to dedicated handler
        if cmd.cog_call is not None:
            await self._handle_cog_call(cmd, ctx)
            return

        # Check query requirement
        if cmd.require_query:
            pattern = '|'.join(cmd.patterns)
            cleaned = re.sub(rf'^.*?({pattern})\s*', '', query, flags=re.IGNORECASE).strip()
            if not cleaned:
                await ctx.reply(cmd.query_error)
                return

        try:
            # Resolve source to list of items
            items = await self._resolve_fun_source(cmd)
            if not items:
                await ctx.reply(cmd.error_msg)
                return

            # Select item
            item = random.choice(items) if cmd.random else items[0]

            # Send output
            if cmd.is_image:
                # item is filename in ASSETS_PATH
                path = os.path.join(config.ASSETS_PATH, item)
                await ctx.reply(file=discord.File(path))
            else:
                await ctx.reply(item)

        except FileNotFoundError:
            await ctx.reply(cmd.error_msg)
            self.logger.error(f"Asset missing for fun command '{cmd.name}'")
        except Exception as e:
            await ctx.reply(cmd.error_msg)
            self.logger.error(f"Error in fun command '{cmd.name}': {e}", exc_info=True)

    async def _resolve_fun_source(self, cmd: FunCommand) -> List[Any]:
        """Resolve a FunCommand's source to a list of items.

        Args:
            cmd: The FunCommand definition.

        Returns:
            List of items (strings, filenames, or bytes depending on source type).
            Empty list if source is unavailable.
        """
        if cmd.content is not None:
            # Literal content - wrap in list
            return [cmd.content]

        elif cmd.file is not None:
            # Read lines from file (offloaded — open() is blocking I/O)
            path = os.path.join(config.ASSETS_PATH, cmd.file)

            def _read_lines() -> List[str]:
                with open(path, 'r', encoding='utf-8') as f:
                    return [line.strip() for line in f if line.strip()]

            return await asyncio.to_thread(_read_lines)

        elif cmd.attr is not None:
            # Read from runtime attribute
            return getattr(self, cmd.attr, [])

        return []

    # ==========================================================================
    # Cog Call Dispatcher
    # ==========================================================================

    async def _resolve_cog_call_input(
        self,
        ctx: commands.Context
    ) -> Optional[discord.Attachment]:
        """Resolve input for cog_call: prefer direct attachment over reply.

        Priority:
        1. Direct attachment on the command message
        2. Attachment on replied-to message
        3. None (target method should raise CogCallNoInput)

        Args:
            ctx: The command context.

        Returns:
            The resolved attachment, or None if no attachment found.
        """
        # 1. Direct attachment
        if ctx.message.attachments:
            return ctx.message.attachments[0]

        # 2. Replied-to message
        if ctx.message.reference and ctx.message.reference.resolved:
            ref_msg = ctx.message.reference.resolved
            if isinstance(ref_msg, discord.Message) and ref_msg.attachments:
                return ref_msg.attachments[0]

        return None

    async def _handle_cog_call(
        self,
        cmd: FunCommand,
        ctx: commands.Context
    ) -> None:
        """Execute a cog_call command by delegating to another cog's method.

        Resolves input, calls the target method, and posts the result.
        Maps CogCallError subclasses to user-friendly error messages.

        Args:
            cmd: The FunCommand definition with cog_call set.
            ctx: The command context.
        """
        assert cmd.cog_call is not None  # Guaranteed by caller
        cog_name, method_name = cmd.cog_call
        errors = cmd.cog_call_errors or {}

        # Get cog and method
        cog = self.bot.get_cog(cog_name)
        if not cog:
            await ctx.reply(errors.get('unavailable', cmd.error_msg))
            self.logger.warning(f"Cog '{cog_name}' not available for cog_call '{cmd.name}'")
            return

        method = getattr(cog, method_name, None)
        if not method:
            await ctx.reply(errors.get('unavailable', cmd.error_msg))
            self.logger.error(f"Cog '{cog_name}' has no method '{method_name}'")
            return

        # Resolve input
        attachment = await self._resolve_cog_call_input(ctx)

        try:
            result: CogCallResult = await method(attachment)

            # Build reply kwargs - only include non-None values
            reply_kwargs: Dict[str, Any] = {}
            if result.content is not None:
                reply_kwargs['content'] = result.content
            if result.file is not None:
                reply_kwargs['file'] = result.file
            if result.embed is not None:
                reply_kwargs['embed'] = result.embed

            await ctx.reply(**reply_kwargs)

        except CogCallNoInput:
            await ctx.reply(errors.get('no_input', "You need to provide something to process!"))
        except CogCallInvalidInput:
            await ctx.reply(errors.get('invalid_input', "I can't process that type of input."))
        except CogCallProcessingFailed:
            await ctx.reply(errors.get('processing_failed', "Something went wrong during processing."))
        except Exception as e:
            await ctx.reply(cmd.error_msg)
            self.logger.error(f"Unexpected error in cog_call '{cmd.name}': {e}", exc_info=True)

    # ==========================================================================
    # BOD Fate System Helpers
    # ==========================================================================

    async def _get_previous_message(
        self,
        channel: discord.TextChannel,
        user: Union[discord.User, discord.Member],
        before: discord.Message
    ) -> Optional[str]:
        """Get user's most recent message in channel before BOD command.

        Only considers messages from the last 10 minutes.

        Args:
            channel: The channel to search.
            user: The user whose message to find.
            before: The BOD command message (search before this).

        Returns:
            Message content if found within 10 minutes, None otherwise.
        """
        ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)

        try:
            async for message in channel.history(limit=50, before=before):
                if message.author.id == user.id:
                    if message.created_at < ten_minutes_ago:
                        # Message is too old
                        return None
                    return message.content
        except discord.Forbidden:
            self.logger.warning(f"Missing permissions to read history in channel {channel.id}")
        except Exception as e:
            self.logger.error(f"Error fetching previous message: {e}", exc_info=True)

        return None

    async def _evaluate_quote_trigger(
        self,
        user_id: int,
        channel: discord.TextChannel,
        before_message: discord.Message,
        current_chain: int
    ) -> None:
        """Check if user's previous message triggers quote fate.

        If a match is found, adds fate to user's bank via database.

        Args:
            user_id: The Discord user ID.
            channel: The channel context.
            before_message: The BOD command message.
            current_chain: User's current chain position.
        """
        # Get triggers for this chain position
        triggers = self.bod_quote_triggers.get(current_chain, [])
        if not triggers:
            return

        # Get user's previous message
        previous_content = await self._get_previous_message(
            channel,
            before_message.author,
            before_message
        )
        if not previous_content:
            return

        # Check against triggers
        for trigger in triggers:
            pattern = trigger.get('pattern', '')
            tier = trigger.get('tier', 'LUCKY')
            count = trigger.get('count', 1)

            try:
                if re.search(pattern, previous_content, re.IGNORECASE):
                    await self.db_manager.add_bod_fate(user_id, tier, count)
                    self.logger.info(
                        f"BOD fate triggered for user {user_id}: {tier} x{count} "
                        f"(chain {current_chain}, pattern '{pattern}')"
                    )
                    return  # Only first match counts
            except re.error as e:
                self.logger.warning(f"Invalid regex pattern in bod_quotes.toml: '{pattern}' - {e}")

    async def _consume_fate_and_get_tier(self, user_id: int) -> str:
        """Consume fate from bank, returning the tier used.

        Checks tiers in order: SILENT > GUARANTEED > BLESSED > LUCKY > NORMAL.

        Args:
            user_id: The Discord user ID.

        Returns:
            Tier string: 'SILENT', 'GUARANTEED', 'BLESSED', 'LUCKY', or 'NORMAL'.
        """
        if self.bot.db_manager is None:
            return "NORMAL"

        # Check in priority order (SILENT first for admin-rigged rolls without flavor text)
        for tier in ('SILENT', 'GUARANTEED', 'BLESSED', 'LUCKY'):
            if await self.db_manager.consume_bod_fate(user_id, tier):
                self.logger.info(f"Consumed {tier} fate for user {user_id}")
                return tier

        return "NORMAL"

    def _fate_roll(self, tier: str) -> int:
        """Roll 1d4 with modified probability based on tier.

        Args:
            tier: One of 'SILENT', 'GUARANTEED', 'BLESSED', 'LUCKY', 'NORMAL'.

        Returns:
            Roll result 1-4.
        """
        if tier == "SILENT":
            return 4
        elif tier == "GUARANTEED":
            return 4
        elif tier == "BLESSED":
            # 75% chance of success
            return 4 if random.random() < 0.75 else random.randint(1, 3)
        elif tier == "LUCKY":
            # 50% chance of success
            return 4 if random.random() < 0.50 else random.randint(1, 3)
        else:
            # NORMAL - standard 25% chance
            return random.randint(1, 4)

    def _get_fate_flavor(self, tier: str) -> str:
        """Get flavor text prefix for a fate tier.

        Args:
            tier: The fate tier that was consumed.

        Returns:
            Flavor text string, or empty string for NORMAL.
        """
        flavors = {
            'LUCKY': "✨ *Favoured by fate...* ",
            'BLESSED': "🌟 *Fabled by fate...* ",
            'GUARANTEED': "⚡ *Divine intervention...* ",
        }
        return flavors.get(tier, "")

    async def _resolve_user_display_name(self, user_id: int, guild: Optional[discord.Guild] = None) -> str:
        """Resolve a user ID to a display name with exponential backoff for API calls.

        Resolution order:
        1. Guild member (if guild provided) - returns server nickname
        2. Bot's user cache - returns global display name
        3. API fetch with exponential backoff - guarantees resolution
        4. Fallback to "User {id}" if all else fails

        Args:
            user_id (int): The Discord user ID to resolve.
            guild (Optional[discord.Guild]): The guild context, if any.

        Returns:
            str: The resolved display name.
        """
        # 1. Try guild member first (fastest, gets server nickname)
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name

        # 2. Try bot's user cache (no API call)
        cached_user = self.bot.get_user(user_id)
        if cached_user:
            return cached_user.display_name

        # 3. API fetch with exponential backoff
        backoff = 1.0
        max_retries = 4
        for attempt in range(max_retries):
            try:
                user = await self.bot.fetch_user(user_id)
                return user.display_name
            except discord.NotFound:
                # User doesn't exist - no point retrying
                break
            except (discord.HTTPException, aiohttp.ClientError) as e:
                self.logger.debug(f"fetch_user({user_id}) attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                continue
            except Exception as e:
                self.logger.warning(f"Unexpected error fetching user {user_id}: {e}")
                break

        # 4. Fallback
        return f"User {user_id}"

    async def cog_unload(self) -> None:
        """Clean up tasks when the cog is unloaded."""
        self.logger.info(f"Unloading Fun cog. Cancelling {len(self.bod_timeout_tasks)} BOD timeout tasks.")

        # Create a list of tasks to cancel
        tasks_to_cancel = list(self.bod_timeout_tasks.values())
        if not tasks_to_cancel:
            return

        # Cancel all tasks
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for all tasks to acknowledge cancellation
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self.logger.info("All BOD timeout tasks have been successfully cancelled and cleaned up.")

    async def _handle_bod_session_timeout(self, user_id: int, channel_id: int) -> None:
        """A background task that waits 20 minutes, ending a user's BOD session.

        Args:
            user_id (int): The user ID.
            channel_id (int): The channel ID to send the timeout message to.
        """
        try:
            await asyncio.sleep(20 * 60)

            player_data = await self.db_manager.get_bod_player(user_id)
            current_chain = player_data.get('current_chain', 0)

            # If the user is no longer in a chain, their session ended naturally (by failing a roll).
            if current_chain == 0:
                self.logger.info(f"BOD session for user {user_id} ended naturally. Timeout task complete.")
                return

            # If they are still in a chain, the session has timed out.
            channel = self.bot.get_channel(channel_id)

            reply_message = f"Your 20-minute `bod` session has ended. Your final chain was {current_chain}."
            user_best = await self.db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await self.db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
                reply_message += "\n**Congratulations! You set a new personal best!**"
            else:
                reply_message += f" Your personal best remains {user_best}."

            # Reset chain, start the 12-hour cooldown from now.
            await self.db_manager.update_bod_player(user_id, int(time.time()), 0, channel_id)

            if channel and isinstance(channel, discord.TextChannel):
                await channel.send(f"<@{user_id}>, {reply_message}")
            else:
                self.logger.error(f"BOD session timeout: Could not find channel {channel_id} to notify user {user_id}.")

            self.logger.info(f"BOD session for user {user_id} timed out with a chain of {current_chain}.")

        except asyncio.CancelledError:
            # This is expected when the cog is reloaded or the user fails a roll.
            self.logger.info(f"BOD session task for user {user_id} was cancelled.")
            # No need to re-raise, as we are handling cleanup explicitly.

        finally:
            # Always remove the task from the tracking dictionary upon completion or cancellation.
            if user_id in self.bod_timeout_tasks:
                self.bod_timeout_tasks.pop(user_id, None)
                self.logger.info(f"Removed BOD task for user {user_id} from tracking.")

    @commands.hybrid_command(name='allquotes', description='Show all Yujin quotes (admin only)')
    @commands.is_owner()
    async def all_quotes(self, ctx: commands.Context) -> None:
        """Display ALL available Yujin quotes for BOD (admin reference).

        Shows quotes in shuffled order. This is an admin-only command
        for managing/reviewing the quote pool.

        Args:
            ctx (commands.Context): The command context.
        """
        if not self.bod_quote_display:
            await ctx.reply("No Yujin quotes have been configured yet.")
            return

        # Shuffle a copy of the quotes
        shuffled_quotes = self.bod_quote_display.copy()
        random.shuffle(shuffled_quotes)

        embed = discord.Embed(
            title="Yujin's Words (All Quotes)",
            description="*Admin reference - all configured quotes*",
            color=discord.Color.purple()
        )

        # Format quotes as a numbered list
        quotes_text = "\n".join(f'• *"{quote}"*' for quote in shuffled_quotes)

        # Discord embed field limit is 1024 chars, split if needed
        if len(quotes_text) <= 1024:
            embed.add_field(name="Known Quotes", value=quotes_text, inline=False)
        else:
            # Split into chunks
            chunks = []
            current_chunk = ""
            for quote in shuffled_quotes:
                line = f'• *"{quote}"*\n'
                if len(current_chunk) + len(line) > 1024:
                    chunks.append(current_chunk.rstrip())
                    current_chunk = line
                else:
                    current_chunk += line
            if current_chunk:
                chunks.append(current_chunk.rstrip())

            for i, chunk in enumerate(chunks):
                field_name = "Known Quotes" if i == 0 else "\u200b"  # invisible char for continuation
                embed.add_field(name=field_name, value=chunk, inline=False)

        await ctx.reply(embed=embed)
        self.logger.info(f"All Yujin quotes displayed for admin {ctx.author}.")

    async def bod(self, ctx: commands.Context, query: str) -> None:
        """A special command that rolls a 1d4.

        On a result of 1-3, it sends a common "fail" image. On a 4, it sends a rare "complete" image.
        This command has a 12-hour cooldown. Once off cooldown, the user has a
        20-minute session to build their chain.

        The Fate System can modify roll probabilities:
        - LUCKY: 50% chance of rolling 4
        - BLESSED: 75% chance of rolling 4
        - GUARANTEED: 100% chance of rolling 4

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query (unused).
        """
        if not self._cog_is_ready:
            await self._not_ready_response(ctx)
            return
        user_id = ctx.author.id
        # Check cooldowns.
        player_data = await self.db_manager.get_bod_player(user_id)
        last_used = player_data.get('last_used_timestamp', 0)
        current_chain = player_data.get('current_chain', 0)
        current_time = time.time()
        time_since_last_use = current_time - last_used

        # Main cooldown (12 hours), only applies if the user is not in an active chain.
        # An active chain means they are within their 20-minute session.
        if current_chain == 0 and time_since_last_use < 12 * 60 * 60 and not (config.DEV_MODE and await self.bot.is_owner(ctx.author)):
            remaining_time = (12 * 60 * 60) - time_since_last_use
            hours, remainder = divmod(remaining_time, 3600)
            minutes, _ = divmod(remainder, 60)
            await ctx.reply(f"Yujin is tired. You can use BOD again in {int(hours)}h {int(minutes)}m.")
            return

        # Start session.
        if user_id not in self.bod_timeout_tasks and current_chain == 0:
            task = asyncio.create_task(self._handle_bod_session_timeout(user_id, ctx.channel.id))
            self.bod_timeout_tasks[user_id] = task
            self.logger.info(f"BOD session started for user {user_id}. Creating timeout task.")

        # Evaluate quote triggers BEFORE rolling (adds to fate bank if matched)
        if isinstance(ctx.channel, discord.TextChannel):
            await self._evaluate_quote_trigger(user_id, ctx.channel, ctx.message, current_chain)

        try:
            # Consume fate and determine roll tier
            fate_tier = await self._consume_fate_and_get_tier(user_id)

            # Owner gets guaranteed success until chain 21 for testing purposes, only in DEV_MODE.
            if config.DEV_MODE and await self.bot.is_owner(ctx.author) and current_chain < 21:
                roll_result = 4
                fate_tier = "NORMAL"  # Don't show fate flavor for dev bypass
            else:
                roll_result = self._fate_roll(fate_tier)

            if roll_result == 4:
                # Successful roll, continue the chain
                new_chain = current_chain + 1
                # Update timestamp, chain, and the last channel used.
                await self.db_manager.update_bod_player(user_id, int(current_time), new_chain, ctx.channel.id)

                dialogue = (self.BOD_CHAIN_DIALOGUE[new_chain - 1] if new_chain <= len(self.BOD_CHAIN_DIALOGUE)
                            else f"You've reached an unheard of chain of {new_chain}! The angels sing your name.")

                # Add fate flavor if consumed fate tier was used
                fate_flavor = self._get_fate_flavor(fate_tier)

                file_path = os.path.join(config.ASSETS_PATH, 'bod_complete.jpg')
                await ctx.reply(
                    f"{fate_flavor}You rolled a 4! **{dialogue}** Your chain is now {new_chain}. Roll again!",
                    file=discord.File(file_path)
                )
            else:
                # Failed roll, break the chain and end the session
                if user_id in self.bod_timeout_tasks:
                    self.bod_timeout_tasks[user_id].cancel()
                    # The task is removed from the dict in the finally block of the task handler

                file_path = os.path.join(config.ASSETS_PATH, 'bod_fail.jpg')

                if current_chain > 0:
                    reply_message = f"You rolled a {roll_result}. Your chain of {current_chain} was broken."
                    self.logger.info(f"BOD chain for user {user_id} broken with a roll of {roll_result}. Final chain: {current_chain}.")

                    user_best = await self.db_manager.get_user_bod_best(user_id)
                    if current_chain > user_best:
                        await self.db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
                        reply_message += f"\n**Congratulations! You set a new personal best with a chain of {current_chain}! Yujin would be proud!**"
                    else:
                        reply_message += f"\nYour personal best is {user_best}. Yujin is now heading to sleep!"
                else:
                    reply_message = f"You rolled a {roll_result}. Yujin has collapsed!"
                    self.logger.info(f"BOD chain for user {user_id} failed at chain 0 with a roll of {roll_result}.")

                # Reset chain and start the 12-hour cooldown from now.
                await self.db_manager.update_bod_player(user_id, int(current_time), 0, ctx.channel.id)
                await ctx.reply(reply_message, file=discord.File(file_path))

        except FileNotFoundError as e:
            await ctx.reply("I couldn't find the right Yujin. Please tell my author to fix it!")
            self.logger.error(f"Image not found for bod roll: {e}")
        except Exception as e:
            await ctx.reply("Something went wrong with the dice roll. Please try again.")
            self.logger.error(f"Error in Fun.bod: {e}", exc_info=True)

    async def cog_ready(self) -> None:
        """Cleans up any active BOD chains that were interrupted by a restart.

        This runs in POST-READY phase after the bot is fully connected,
        ensuring the cache is populated before we check for active chains.
        """
        # On a reload, give the unload of the old cog a moment to finish its cleanup.
        # On a cold start, this just adds a small safety buffer.
        self._load_bod_quotes()
        await asyncio.sleep(2)
        await self._cleanup_bod_chains()

    async def _cleanup_bod_chains(self) -> None:
        """Checks for any BOD chains that were active and notifies participants.

        This runs only once per startup to avoid duplicate notifications.
        """
        if self.has_cleaned_up_chains:
            return

        self.logger.info("Performing one-time check for active BOD chains after restart/reload.")
        active_chains = await self.db_manager.get_all_active_bod_chains()

        if not active_chains:
            self.logger.info("No active BOD chains found to clean up.")
            self.has_cleaned_up_chains = True
            return

        self.logger.warning(f"Found {len(active_chains)} active BOD chains after a restart/reload. Notifying users and resetting.")

        for chain_data in active_chains:
            user_id = chain_data['user_id']
            channel_id = chain_data['last_channel_id']
            current_chain = chain_data['current_chain']

            # Reset the user's chain in the database first.
            await self.db_manager.update_bod_player(user_id, int(time.time()), 0, channel_id)

            channel = self.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                self.logger.error(f"Could not find channel {channel_id} to notify user {user_id} about their broken chain.")
                continue

            reply_message = f"It looks like I had to restart or reload, which has unfortunately broken your chain of {current_chain}."

            user_best = await self.db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await self.db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
                reply_message += "\n**However, you set a new personal best! Congratulations!**"
            else:
                reply_message += f" Your personal best remains {user_best}."

            try:
                await channel.send(f"<@{user_id}>, {reply_message}")
                self.logger.info(f"Notified user {user_id} in channel {channel_id} about their broken chain of {current_chain}.")
            except discord.Forbidden:
                self.logger.error(f"Missing permissions to send message in channel {channel_id}.")
            except Exception as e:
                self.logger.error(f"Failed to notify user {user_id} about broken chain: {e}")

        self.has_cleaned_up_chains = True
        self.logger.info("Finished cleaning up all active BOD chains.")

    async def bod_leaderboard(self, ctx: commands.Context, query: str) -> None:
        """Displays the top 10 BOD chain scores from the leaderboard.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query (unused).
        """
        if not self._cog_is_ready:
            await self._not_ready_response(ctx)
            return
        leaderboard_data = await self.db_manager.get_bod_leaderboard()

        if not leaderboard_data:
            await ctx.reply("The BOD leaderboard is currently empty. Be the first to set a score!")
            return

        embed = discord.Embed(
            title="BOD Chain Leaderboard",
            description="The highest chain achieved by the most dedicated Yujin fans.",
            color=discord.Color.gold()
        )

        # Format the leaderboard string
        board_string = ""
        for i, entry in enumerate(leaderboard_data[:10]):
            rank = i + 1
            entry_user_id = entry['user_id']
            chain = entry['best_chain']

            # Fetch display name dynamically (guild member > cached user > API fetch)
            display_name = await self._resolve_user_display_name(entry_user_id, ctx.guild)

            if rank == 1:
                board_string += f"🥇 **{display_name}** - Chain of **{chain}**\n"
            elif rank == 2:
                board_string += f"🥈 **{display_name}** - Chain of **{chain}**\n"
            elif rank == 3:
                board_string += f"🥉 **{display_name}** - Chain of **{chain}**\n"
            else:
                board_string += f"**{rank}.** {display_name} - Chain of {chain}\n"

        embed.add_field(name="Top 10", value=board_string, inline=False)

        # Add user's rank if they are not in the top 10
        user_id = ctx.author.id
        user_in_top_10 = any(entry['user_id'] == user_id for entry in leaderboard_data[:10])

        if not user_in_top_10:
            for i, entry in enumerate(leaderboard_data):
                if entry['user_id'] == user_id:
                    rank = i + 1
                    chain = entry['best_chain']
                    embed.add_field(
                        name="Your Rank",
                        value=f"You are rank **#{rank}** with a chain of **{chain}**.",
                        inline=False
                    )
                    break

        await ctx.reply(embed=embed)


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    bot.register_nlp_group(FUN_NLP_ENTRIES)
    await bot.add_cog(Fun(bot))

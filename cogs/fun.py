"""cogs/fun.py

This cog contains miscellaneous "fun" commands that don't fit into other categories.
It includes commands like a magic 8-ball and other simple, interactive features.
"""

import asyncio
import os
import random
import re
import time
import tomllib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import aiohttp
import discord
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot


class Fun(BaseCog):
    """A cog for fun, miscellaneous commands."""

    def __init__(self, bot: CoreBot):
        """Initializes the Fun cog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)
        # Load the 8-ball responses from the assets file upon initialization.
        self.responses = self._load_8ball_responses()
        self.fun_commands = {
            'sanitize': {
                'type': 'image',
                'file': 'sanitize.webp',
                'error_message': "I couldn't find my sanitizer!"
            },
            'pear_wiggler': {
                'type': 'image',
                'file': 'pearwiggler.gif',
                'error_message': "I couldn't find the wiggler of the pear variety!"
            },
            'issues': {
                'type': 'text',
                'content': 'My issues page is [here](https://github.com/selectL-L/Sancho/issues) please write your suggestions and issues over there!'
            }
        }
        self.bod_timeout_tasks: Dict[int, asyncio.Task] = {}
        self.has_cleaned_up_chains = False
        # BOD Fate System
        self.bod_quote_triggers: Dict[int, List[Dict[str, Any]]] = {}
        self.bod_quote_display: List[str] = []
        self._load_bod_quotes()

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
            self.bod_quote_display = data.get('quotes', {}).get('display', [])

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

    async def fun_command_handler(self, ctx: commands.Context, command: str) -> None:
        """A generic handler for "fun" commands that post content like images, text, or links.

        Args:
            ctx (commands.Context): The context of the command.
            command (str): The command that was triggered.
        """
        command_details = self.fun_commands.get(command)
        if not command_details:
            self.logger.error(f"Fun command '{command}' has no configuration.")
            return

        command_type = command_details.get('type')

        try:
            if command_type == 'image':
                image_file = command_details.get('file')
                if not image_file:
                    self.logger.error(f"Image command '{command}' is missing 'file' in its configuration.")
                    return

                file_path = os.path.join(config.ASSETS_PATH, image_file)
                await ctx.reply(file=discord.File(file_path))
                self.logger.info(f"Image command '{command}' used by {ctx.author}.")

            elif command_type == 'text':
                content = command_details.get('content')
                if not content:
                    self.logger.error(f"Text command '{command}' is missing 'content' in its configuration.")
                    return

                await ctx.reply(content)
                self.logger.info(f"Text command '{command}' used by {ctx.author}.")

        except FileNotFoundError:
            error_message = command_details.get('error_message', f"Asset is missing for '{command}'. Please contact my author to fix it!")
            await ctx.reply(error_message)
            self.logger.error(f"Asset not found for '{command}' command.")
        except Exception as e:
            await ctx.reply("Something went wrong. Please try again.")
            self.logger.error(f"Error in fun_command_handler for '{command}': {e}", exc_info=True)

    def _load_8ball_responses(self) -> List[str]:
        """Loads the magic 8-ball responses from the `8ball.txt` file.

        Returns:
            List[str]: A list of response strings. Returns a default list
                       if the file is not found or is empty.
        """
        responses_path = os.path.join(config.ASSETS_PATH, '8ball.txt')
        try:
            with open(responses_path, 'r', encoding='utf-8') as f:
                responses = [line.strip() for line in f if line.strip()]
            if not responses:
                self.logger.error("8ball.txt is empty. 8ball command will not work.")
                return ["It seems I am out of answers."]
            return responses
        except FileNotFoundError:
            self.logger.error("8ball.txt not found. 8ball command will not work.")
            return ["I seem to have lost my magic 8-ball..."]

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
        ten_minutes_ago = datetime.now(timezone.utc) - __import__('datetime').timedelta(minutes=10)

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
            self.logger.error(f"Error fetching previous message: {e}")

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
                    db_manager = self.bot.db_manager
                    if db_manager:
                        await db_manager.add_bod_fate(user_id, tier, count)
                        self.logger.info(
                            f"BOD fate triggered for user {user_id}: {tier} x{count} "
                            f"(chain {current_chain}, pattern '{pattern}')"
                        )
                    return  # Only first match counts
            except re.error as e:
                self.logger.warning(f"Invalid regex pattern in bod_quotes.toml: '{pattern}' - {e}")

    async def _consume_fate_and_get_tier(self, user_id: int) -> str:
        """Consume fate from bank, returning the tier used.

        Checks tiers in order: GUARANTEED > BLESSED > LUCKY > NORMAL.

        Args:
            user_id: The Discord user ID.

        Returns:
            Tier string: 'GUARANTEED', 'BLESSED', 'LUCKY', or 'NORMAL'.
        """
        db_manager = self.bot.db_manager
        if not db_manager:
            return "NORMAL"

        # Check in priority order
        for tier in ('GUARANTEED', 'BLESSED', 'LUCKY'):
            if await db_manager.consume_bod_fate(user_id, tier):
                self.logger.info(f"Consumed {tier} fate for user {user_id}")
                return tier

        return "NORMAL"

    def _fate_roll(self, tier: str) -> int:
        """Roll 1d4 with modified probability based on tier.

        Args:
            tier: One of 'GUARANTEED', 'BLESSED', 'LUCKY', 'NORMAL'.

        Returns:
            Roll result 1-4.
        """
        if tier == "GUARANTEED":
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

            db_manager = self.bot.db_manager
            if not db_manager:
                self.logger.error(f"BOD session timeout: DatabaseManager not found for user {user_id}.")
                return

            player_data = await db_manager.get_bod_player(user_id)
            current_chain = player_data.get('current_chain', 0)

            # If the user is no longer in a chain, their session ended naturally (by failing a roll).
            if current_chain == 0:
                self.logger.info(f"BOD session for user {user_id} ended naturally. Timeout task complete.")
                return

            # If they are still in a chain, the session has timed out.
            channel = self.bot.get_channel(channel_id)

            reply_message = f"Your 20-minute `bod` session has ended. Your final chain was {current_chain}."
            user_best = await db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
                reply_message += "\n**Congratulations! You set a new personal best!**"
            else:
                reply_message += f" Your personal best remains {user_best}."

            # Reset chain, start the 12-hour cooldown from now.
            await db_manager.update_bod_player(user_id, int(time.time()), 0, channel_id)

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
        BOD_CHAIN_DIALOGUE = [
            "First…", "Second…", "Third…", "Fourth…", "Fifth…",
            "Sixth…", "Seventh…", "Eighth…", "Ninth…", "Tenth…",
            "Eleventh…", "Twelfth…", "Thirteenth…", "Fourteenth…", "Fifteenth…",
            "Sixteenth…", "Seventeenth…", "Eighteenth…", "Nineteenth…",
            "Twentieth, and final… Be not afraid."
        ]

        user_id = ctx.author.id
        db_manager = self.bot.db_manager
        if not db_manager:
            await ctx.reply("The database is not available at the moment. Please try again later.")
            self.logger.error("DatabaseManager not found in bot instance.")
            return

        # Check cooldowns.
        player_data = await db_manager.get_bod_player(user_id)
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
                await db_manager.update_bod_player(user_id, int(current_time), new_chain, ctx.channel.id)

                dialogue = (BOD_CHAIN_DIALOGUE[new_chain - 1] if new_chain <= len(BOD_CHAIN_DIALOGUE)
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

                    user_best = await db_manager.get_user_bod_best(user_id)
                    if current_chain > user_best:
                        await db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
                        reply_message += f"\n**Congratulations! You set a new personal best with a chain of {current_chain}! Yujin would be proud!**"
                    else:
                        reply_message += f"\nYour personal best is {user_best}. Yujin is now heading to sleep!"
                else:
                    reply_message = f"You rolled a {roll_result}. Yujin has collapsed!"
                    self.logger.info(f"BOD chain for user {user_id} failed at chain 0 with a roll of {roll_result}.")

                # Reset chain and start the 12-hour cooldown from now.
                await db_manager.update_bod_player(user_id, int(current_time), 0, ctx.channel.id)
                await ctx.reply(reply_message, file=discord.File(file_path))

        except FileNotFoundError as e:
            await ctx.reply("I couldn't find the right Yujin. Please tell my author to fix it!")
            self.logger.error(f"Image not found for bod roll: {e}")
        except Exception as e:
            await ctx.reply("Something went wrong with the dice roll. Please try again.")
            self.logger.error(f"Error in Fun.bod: {e}", exc_info=True)

    async def eight_ball(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the 8-ball command.

        It picks a random response from the pre-loaded list and sends it to the channel.

        Args:
            ctx (commands.Context): The context of the command.
            query (str): The user's question for the 8-ball.
        """
        # The NLP dispatcher passes the whole message. We need to strip the trigger phrase.
        # This pattern is the same as the one in config.py
        trigger_pattern = r'8\s?-?ball'
        cleaned_query = re.sub(rf'^\s*{trigger_pattern}\s*', '', query, flags=re.IGNORECASE).strip()

        if not cleaned_query:
            await ctx.reply("I cannot intuit from nothing!")
            self.logger.info(f"8ball command used by {ctx.author} with no actual query.")
            return

        response = random.choice(self.responses)
        await ctx.reply(response)
        self.logger.info(f"8ball command used by {ctx.author} with query '{cleaned_query}'. Response: '{response}'")

    async def sanitize(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the sanitize command.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query.
        """
        await self.fun_command_handler(ctx, 'sanitize')

    async def pear_wiggler(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the pear wiggler command.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query.
        """
        await self.fun_command_handler(ctx, 'pear_wiggler')

    async def issues(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the issues command.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query.
        """
        await self.fun_command_handler(ctx, 'issues')

    async def cog_ready(self) -> None:
        """Cleans up any active BOD chains that were interrupted by a restart.

        This runs in POST-READY phase after the bot is fully connected,
        ensuring the cache is populated before we check for active chains.
        """
        # On a reload, give the unload of the old cog a moment to finish its cleanup.
        # On a cold start, this just adds a small safety buffer.
        await asyncio.sleep(2)
        await self._cleanup_bod_chains()

    async def _cleanup_bod_chains(self) -> None:
        """Checks for any BOD chains that were active and notifies participants.

        This runs only once per startup to avoid duplicate notifications.
        """
        if self.has_cleaned_up_chains:
            return

        self.logger.info("Performing one-time check for active BOD chains after restart/reload.")
        db_manager = self.bot.db_manager
        if not db_manager:
            self.logger.error("Cannot perform BOD chain cleanup: DatabaseManager not found.")
            return

        active_chains = await db_manager.get_all_active_bod_chains()

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
            await db_manager.update_bod_player(user_id, int(time.time()), 0, channel_id)

            channel = self.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                self.logger.error(f"Could not find channel {channel_id} to notify user {user_id} about their broken chain.")
                continue

            reply_message = f"It looks like I had to restart or reload, which has unfortunately broken your chain of {current_chain}."

            user_best = await db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await db_manager.update_bod_leaderboard(user_id, current_chain, int(time.time()))
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
        db_manager = self.bot.db_manager
        if not db_manager:
            await ctx.reply("The database is not available at the moment. Please try again later.")
            self.logger.error("DatabaseManager not found in bot instance.")
            return

        leaderboard_data = await db_manager.get_bod_leaderboard()

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
        self.logger.info(f"BOD leaderboard viewed by {ctx.author}.")

    async def yujin_quotes(self, ctx: commands.Context, query: str) -> None:
        """Display available Yujin quotes for BOD.

        Shows quotes in shuffled order so users can't correlate
        position with chain number.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's query (unused).
        """
        if not self.bod_quote_display:
            await ctx.reply("No Yujin quotes have been configured yet.")
            return

        # Shuffle a copy of the quotes
        shuffled_quotes = self.bod_quote_display.copy()
        random.shuffle(shuffled_quotes)

        embed = discord.Embed(
            title="Yujin's Words",
            description="*Speak her words before the boundary, and fate may smile upon you...*",
            color=discord.Color.purple()
        )

        # Format quotes as a numbered list
        quotes_text = "\n".join(f"• *\"{quote}\"*" for quote in shuffled_quotes)

        # Discord embed field limit is 1024 chars, split if needed
        if len(quotes_text) <= 1024:
            embed.add_field(name="Known Quotes", value=quotes_text, inline=False)
        else:
            # Split into chunks
            chunks = []
            current_chunk = ""
            for quote in shuffled_quotes:
                line = f"• *\"{quote}\"*\n"
                if len(current_chunk) + len(line) > 1024:
                    chunks.append(current_chunk.rstrip())
                    current_chunk = line
                else:
                    current_chunk += line
            if current_chunk:
                chunks.append(current_chunk.rstrip())

            for i, chunk in enumerate(chunks):
                embed.add_field(
                    name=f"Known Quotes {f'(Part {i+1})' if len(chunks) > 1 else ''}",
                    value=chunk,
                    inline=False
                )

        embed.set_footer(text="The right words at the right time may change your fortune...")

        await ctx.reply(embed=embed)
        self.logger.info(f"Yujin quotes viewed by {ctx.author}.")


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(Fun(bot))

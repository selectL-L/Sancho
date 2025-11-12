"""
cogs/fun.py

This cog contains miscellaneous "fun" commands that don't fit into other categories.
It includes commands like a magic 8-ball and other simple, interactive features.
"""
import discord
from discord.ext import commands
import random
import os
import time
import asyncio
from typing import TYPE_CHECKING, cast, Dict

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
import config

if TYPE_CHECKING:
    from cogs.math import Math


class Fun(BaseCog):
    """
    A cog for fun, miscellaneous commands.
    """

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        # Load the 8-ball responses from the assets file upon initialization.
        self.responses = self._load_8ball_responses()
        self.fun_commands = {
            'sanitize': {
                'type': 'image',
                'file': 'sanitize.webp',
                'error_message': "I couldn't find my sanitizer!"
            },
            'issues': {
                'type': 'text',
                'content': 'My issues page is [here](https://github.com/selectL-L/Sancho/issues) please write your suggestions and issues over there!'
            }
        }
        self.bod_timeout_tasks: Dict[int, asyncio.Task] = {}
        self.has_cleaned_up_chains = False

    async def fun_command_handler(self, ctx: commands.Context, command: str):
        """
        A generic handler for "fun" commands that post content like images, text, or links.

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

    def _load_8ball_responses(self) -> list[str]:
        """
        Loads the magic 8-ball responses from the `8ball.txt` file located
        in the assets directory.

        Returns:
            list[str]: A list of response strings. Returns a default list
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

    async def cog_unload(self):
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

    async def _handle_bod_session_timeout(self, user_id: int, channel_id: int):
        """
        A background task that waits 20 minutes, ending a user's BOD session 
        if they are still in an active chain.
        """
        try:
            await asyncio.sleep(20 * 60)
            
            db_manager = self.bot.db_manager
            if not db_manager:
                self.logger.error(f"BOD session timeout: DatabaseManager not found for user {user_id}.")
                return

            usage_data = await db_manager.get_bod_usage(user_id)
            current_chain = usage_data.get('current_chain', 0)

            # If the user is no longer in a chain, their session ended naturally (by failing a roll).
            if current_chain == 0:
                self.logger.info(f"BOD session for user {user_id} ended naturally. Timeout task complete.")
                return

            # If they are still in a chain, the session has timed out.
            channel = self.bot.get_channel(channel_id)
            user = self.bot.get_user(user_id)
            user_name = user.display_name if user else "A user"

            reply_message = f"Your 20-minute `bod` session has ended. Your final chain was {current_chain}."
            user_best = await db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await db_manager.update_bod_leaderboard(user_id, user_name, current_chain)
                reply_message += f"\n**Congratulations! You set a new personal best!**"
            else:
                reply_message += f" Your personal best remains {user_best}."
            
            # Reset chain, start the 12-hour cooldown from now.
            await db_manager.update_bod_usage(user_id, int(time.time()), 0, channel_id) 
            
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

    async def bod(self, ctx: commands.Context, query: str):
        """
        A special command that rolls a 1d4. On a result of 1-3, it sends a
        common "fail" image. On a 4, it sends a rare "complete" image.
        This command has a 12-hour cooldown. Once off cooldown, the user has a
        20-minute session to build their chain.
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

        # --- Check Cooldowns ---
        usage_data = await db_manager.get_bod_usage(user_id)
        last_used = usage_data.get('last_used_timestamp', 0)
        current_chain = usage_data.get('current_chain', 0)
        current_time = time.time()
        time_since_last_use = current_time - last_used

        # Main cooldown (12 hours), only applies if the user is not in an active chain.
        # An active chain means they are within their 20-minute session.
        if current_chain == 0 and time_since_last_use < 12 * 60 * 60 and not await self.bot.is_owner(ctx.author):
            remaining_time = (12 * 60 * 60) - time_since_last_use
            hours, remainder = divmod(remaining_time, 3600)
            minutes, _ = divmod(remainder, 60)
            await ctx.reply(f"BOD is on cooldown. You can use it again in {int(hours)}h {int(minutes)}m.")
            return

        # --- Start a new session if applicable ---
        if user_id not in self.bod_timeout_tasks and current_chain == 0:
            await ctx.reply("Your 20-minute `bod` session has begun. Roll now!")
            task = asyncio.create_task(self._handle_bod_session_timeout(user_id, ctx.channel.id))
            self.bod_timeout_tasks[user_id] = task
            self.logger.info(f"BOD session started for user {user_id}. Creating timeout task.")

        # --- Perform Dice Roll ---
        math_cog = cast("Math", self.bot.get_cog('Math'))
        if not math_cog:
            await ctx.reply("I can't find my dice right now. Please try again later.")
            self.logger.error("Math cog not found, cannot perform bod roll.")
            return

        try:
            # Owner gets guaranteed success until chain 21 for testing purposes.
            if await self.bot.is_owner(ctx.author) and current_chain < 21:
                roll_result = 4
            else:
                roll_result = await math_cog.get_roll_result("1d4")
            
            if roll_result == 4:
                # Successful roll, continue the chain
                new_chain = current_chain + 1
                # Update timestamp, chain, and the last channel used.
                await db_manager.update_bod_usage(user_id, int(current_time), new_chain, ctx.channel.id)

                dialogue = (BOD_CHAIN_DIALOGUE[new_chain - 1] if new_chain <= len(BOD_CHAIN_DIALOGUE)
                            else f"You've reached an unheard of chain of {new_chain}! The angels sing your name.")

                file_path = os.path.join(config.ASSETS_PATH, 'bod_complete.jpg')
                await ctx.reply(
                    f"You rolled a 4! **{dialogue}** Your chain is now {new_chain}. Roll again!",
                    file=discord.File(file_path)
                )
            else:
                # Failed roll, break the chain and end the session
                if user_id in self.bod_timeout_tasks:
                    self.bod_timeout_tasks[user_id].cancel()
                    # The task is removed from the dict in the finally block of the task handler

                file_path = os.path.join(config.ASSETS_PATH, 'bod_fail.jpg')
                reply_message = f"You rolled a {roll_result}. Your chain of {current_chain} was broken."
                self.logger.info(f"BOD chain for user {user_id} broken with a roll of {roll_result}. Final chain: {current_chain}.")

                if current_chain > 0:
                    user_best = await db_manager.get_user_bod_best(user_id)
                    if current_chain > user_best:
                        await db_manager.update_bod_leaderboard(user_id, ctx.author.display_name, current_chain)
                        reply_message += f"\n**Congratulations! You set a new personal best with a chain of {current_chain}!**"
                    else:
                        reply_message += f" Your personal best is {user_best}."
                
                # Reset chain and start the 12-hour cooldown from now.
                await db_manager.update_bod_usage(user_id, int(current_time), 0, ctx.channel.id) 
                await ctx.reply(reply_message, file=discord.File(file_path))

        except FileNotFoundError as e:
            await ctx.reply("I couldn't find the right Yujin. Please tell my author to fix it!")
            self.logger.error(f"Image not found for bod roll: {e}")
        except Exception as e:
            await ctx.reply("Something went wrong with the dice roll. Please try again.")
            self.logger.error(f"Error in Fun.bod: {e}", exc_info=True)


    async def eight_ball(self, ctx: commands.Context, *, query: str) -> None:
        """
        NLP handler for the 8-ball command. It picks a random response from the
        pre-loaded list and sends it to the channel.

        Args:
            ctx (commands.Context): The context of the command.
            query (str): The user's question for the 8-ball.
        """
        response = random.choice(self.responses)
        await ctx.reply(response)
        self.logger.info(f"8ball command used by {ctx.author} with query '{query}'. Response: '{response}'")

    async def sanitize(self, ctx: commands.Context, *, query: str):
        """NLP handler for the sanitize command."""
        await self.fun_command_handler(ctx, 'sanitize')

    async def issues(self, ctx: commands.Context, *, query: str):
        """NLP handler for the issues command."""
        await self.fun_command_handler(ctx, 'issues')

    async def cog_load(self):
        """
        Schedules a one-time task to clean up active BOD chains after a restart/reload.
        This is non-blocking to avoid deadlocking the bot's startup process.
        """
        asyncio.create_task(self._cleanup_chains_task())

    async def _cleanup_chains_task(self):
        """
        Waits for the bot to be ready, then checks for any chains that were active
        before a restart/reload, notifies the users, and resets their state.
        This runs only once per startup.
        """
        # Wait for the bot to be fully ready before proceeding,
        # ensuring that the cache is populated.
        await self.bot.wait_until_ready()

        if self.has_cleaned_up_chains:
            return
        
        # On a reload, give the unload of the old cog a moment to finish its cleanup.
        # On a cold start, this just adds a small safety buffer.
        await asyncio.sleep(2)

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
            await db_manager.update_bod_usage(user_id, int(time.time()), 0, channel_id)

            channel = self.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                self.logger.error(f"Could not find channel {channel_id} to notify user {user_id} about their broken chain.")
                continue

            user = self.bot.get_user(user_id)
            user_name = user.display_name if user else "User"
            
            reply_message = f"It looks like I had to restart or reload, which has unfortunately broken your chain of {current_chain}."
            
            user_best = await db_manager.get_user_bod_best(user_id)
            if current_chain > user_best:
                await db_manager.update_bod_leaderboard(user_id, user_name, current_chain)
                reply_message += f"\n**However, you set a new personal best! Congratulations!**"
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


async def setup(bot: SanchoBot) -> None:
    """Standard setup function to add the cog to the bot."""
    await bot.add_cog(Fun(bot))

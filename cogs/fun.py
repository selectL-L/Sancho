"""
cogs/fun.py

This cog contains miscellaneous "fun" commands that don't fit into other categories.
It includes commands like a magic 8-ball and other simple, interactive features.
"""
import discord
from discord.ext import commands
import random
import os
from typing import TYPE_CHECKING, cast

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
        self.image_commands = {
            'sanitize': {
                'file': 'sanitize.webp',
                'error_message': "I couldn't find my sanitizer!"
            }
        }

    async def image_command_handler(self, ctx: commands.Context, command: str):
        """
        A generic handler for commands that post an image.

        Args:
            ctx (commands.Context): The context of the command.
            command (str): The command that was triggered.
        """
        command_details = self.image_commands.get(command)
        if not command_details:
            self.logger.error(f"Image command '{command}' has no image file mapping.")
            return

        image_file = command_details.get('file')
        if not image_file:
            self.logger.error(f"Image command '{command}' is missing 'file' in its configuration.")
            return

        try:
            file_path = os.path.join(config.ASSETS_PATH, image_file)
            await ctx.reply(file=discord.File(file_path))
            self.logger.info(f"Image command '{command}' used by {ctx.author}.")
        except FileNotFoundError:
            error_message = command_details.get('error_message', f"Image is missing for '{command}'. Please contact my author to fix it!")
            await ctx.reply(error_message)
            self.logger.error(f"{image_file} not found for '{command}' command.")
        except Exception as e:
            await ctx.reply("Something went wrong. Please try again.")
            self.logger.error(f"Error in image_command_handler for '{command}': {e}", exc_info=True)

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

    async def bod(self, ctx: commands.Context, query: str):
        """
        A special command that rolls a 1d4. On a result of 1-3, it sends a
        common "fail" image. On a 4, it sends a rare "complete" image.
        This command depends on the `Math` cog to perform the dice roll.

        Args:
            ctx (commands.Context): The context of the command.
            query (str): The user's query, which is not used in this command.
        """
        # Get the Math cog to perform the dice roll.
        math_cog = cast("Math", self.bot.get_cog('Math'))
        if not math_cog:
            await ctx.reply("I can't find my dice right now. Please try again later.")
            self.logger.error("Math cog not found, cannot perform bod roll.")
            return

        try:
            # Use the Math cog's internal method to get a clean roll result.
            if await self.bot.is_owner(ctx.author):
                roll_result = 4
            else:
                roll_result = await math_cog.get_roll_result("1d4")
            
            # Send a different image based on the roll result.
            if roll_result <= 3:
                file_path = os.path.join(config.ASSETS_PATH, 'bod_fail.jpg')
                await ctx.reply(f"You rolled a {roll_result}", file=discord.File(file_path))
            else:
                file_path = os.path.join(config.ASSETS_PATH, 'bod_complete.jpg')
                await ctx.reply(f"You rolled a {roll_result}!", file=discord.File(file_path))

        except FileNotFoundError as e:
            await ctx.reply("I couldn't find the right Yujin. Please tell my author to fix it!")
            self.logger.error(f"Image not found for bod roll: {e}")
        except Exception as e:
            await ctx.reply("Something went wrong with the dice roll. Please try again.")
            self.logger.error(f"Error calling Math cog from Fun.bod: {e}", exc_info=True)


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
        await self.image_command_handler(ctx, 'sanitize')


async def setup(bot: SanchoBot) -> None:
    """Standard setup function to add the cog to the bot."""
    await bot.add_cog(Fun(bot))

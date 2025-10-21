import discord
from discord.ext import commands
import random
import os

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
import config

class Fun(BaseCog):
    """A cog for fun, miscellaneous commands."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.responses = self._load_8ball_responses()

    def _load_8ball_responses(self) -> list[str]:
        """Loads 8-ball responses from the 8ball.txt file."""
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
        Rolls a 1d4. On 1-3, sends a common image. On 4, sends a rare image.
        """
        math_cog = self.bot.get_cog('Math')
        if not math_cog:
            await ctx.reply("I can't find my dice right now. Please try again later.")
            self.logger.error("Math cog not found, cannot perform bod roll.")
            return

        try:
            roll_result = await math_cog.get_roll_result("1d4")
            
            if roll_result <= 3:
                file_path = os.path.join(config.ASSETS_PATH, 'bod_fail.jpg')
                await ctx.reply(f"You rolled a {roll_result}", file=discord.File(file_path))
            else:
                file_path = os.path.join(config.ASSETS_PATH, 'bod_complete.jpg')
                await ctx.reply(f"You rolled a {roll_result}!", file=discord.File(file_path))

        except FileNotFoundError as e:
            await ctx.reply("I couldn't find the image file for that roll. Please tell my author to fix it!")
            self.logger.error(f"Image not found for bod roll: {e}")
        except Exception as e:
            await ctx.reply("Something went wrong with the dice roll. Please try again.")
            self.logger.error(f"Error calling Math cog from Fun.bod: {e}", exc_info=True)


    async def eight_ball(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the 8-ball command."""
        response = random.choice(self.responses)
        await ctx.reply(response)
        self.logger.info(f"8ball command used by {ctx.author} with query '{query}'. Response: '{response}'")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Fun(bot))

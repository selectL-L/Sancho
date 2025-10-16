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
        responses_path = os.path.join(config.APP_PATH, '8ball.txt')
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

    async def eight_ball(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for the 8-ball command."""
        response = random.choice(self.responses)
        await ctx.reply(response)
        self.logger.info(f"8ball command used by {ctx.author} with query '{query}'. Response: '{response}'")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Fun(bot))

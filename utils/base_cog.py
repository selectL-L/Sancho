"""
Defines a base class for all cogs to inherit from.
This allows for shared functionality and consistent structure.
"""
import logging
from discord.ext import commands

class BaseCog(commands.Cog):
    """
    A base cog that all other cogs should inherit from.
    It provides a dedicated logger instance for the cog.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Create a logger that is specific to the cog's class name
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info(f"Cog '{self.__class__.__name__}' initialized.")

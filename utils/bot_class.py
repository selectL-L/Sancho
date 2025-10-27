"""
Defines the custom bot class, `SanchoBot`, which extends `commands.Bot`.
This allows for custom attributes and methods to be attached directly to the
bot instance, making them easily accessible throughout the application,
especially within cogs.
(Largely because without this, there are a lot of complaints from pylance.)
"""
from discord.ext import commands
from typing import TYPE_CHECKING

# Import the type hint for the database manager, but only for type checking
# to avoid circular imports at runtime.
if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """
    A custom bot class that extends `discord.ext.commands.Bot`.
    This class is used to attach custom attributes like the database manager
    to the bot instance, making it accessible from any cog.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These attributes are initialized after the bot object is created in main.py
        # They provide easy access to the database path and manager from anywhere
        # the bot object is available.
        self.db_path: str = ""
        if TYPE_CHECKING:
            self.db_manager: DatabaseManager

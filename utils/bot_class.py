from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.database import DatabaseManager

class SanchoBot(commands.Bot):
    """A custom bot class that includes a database path."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_path: str = "" # Initialized in main.py after creation
        if TYPE_CHECKING:
            self.db_manager: DatabaseManager

from discord.ext import commands

class SanchoBot(commands.Bot):
    """A custom bot class that includes a database path."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_path: str = "" # Initialized in main.py after creation

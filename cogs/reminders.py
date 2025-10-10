import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
import re
import logging
from typing import Optional

log = logging.getLogger('RemindersCog')

class Reminders(commands.Cog):
    """A cog for setting and checking natural language reminders."""
    def __init__(self, bot: commands.Bot, db_path: str):
        self.bot = bot
        self.db_path = db_path
        self.check_reminders.start()

    async def cog_unload(self) -> None:
        # `Cog.cog_unload` is expected to be asynchronous in newer discord.py
        # type stubs; implement as async so the override's return type
        # matches the base class (a coroutine).
        self.check_reminders.cancel()

    async def _setup_database(self) -> None:
        """Ensures the reminders table exists."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    reminder_time INTEGER NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL
                )''')
            await db.commit()
        log.info("Reminders database initialized.")

    @tasks.loop(seconds=15)
    async def check_reminders(self) -> None:
        """Periodically checks for and sends due reminders."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = int(time.time())
                cursor = await db.execute('SELECT id, user_id, channel_id, message FROM reminders WHERE reminder_time <= ?', (current_time,))
                for rid, uid, cid, msg in await cursor.fetchall():
                    try:
                        user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                        channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                            await channel.send(f"{user.mention}, you asked me to remind you: '{msg}'")
                    except (discord.NotFound, discord.Forbidden) as e:
                        log.warning(f"Failed to send reminder {rid} (user/channel not found or permissions error). Deleting. Error: {e}")
                    
                    await db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
                await db.commit()
        except Exception as e:
            log.error(f"Unexpected error in reminder check loop: {e}", exc_info=True)

    @check_reminders.before_loop
    async def before_check_reminders(self) -> None:
        await self.bot.wait_until_ready()
        await self._setup_database()

    def _parse_reminder(self, query: str) -> tuple[str, str] | None:
        """Parses a query into a (message, time_string) tuple."""
        sanitized = re.sub(r'^(remind me to|remind me|remember to|remember)\s*', '', query, flags=re.IGNORECASE).strip()
        words = sanitized.split()
        if not words: return None

        # Strategy: Find the longest possible valid time phrase at the end of the string.
        for i in range(len(words)):
            time_str = ' '.join(words[i:])
            msg_str = ' '.join(words[:i])
            if dateparser.parse(time_str, settings={'PREFER_DATES_FROM': 'future'}):
                # We found the longest valid time string. The rest is the message.
                return (msg_str.removesuffix(' to').strip(), time_str)
        
        return None

    async def remind(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all reminder requests."""
        parsed = self._parse_reminder(query)
        if not parsed or not parsed[0]:
            await ctx.send("I couldn't understand that. Please tell me *what* to remind you of and *when* (e.g., `...in 1 hour`).")
            return

        reminder_message, time_str = parsed
        dt_object = dateparser.parse(time_str, settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not dt_object:
            await ctx.send("Sorry, I couldn't understand that time. Please try again.")
            return

        timestamp = int(dt_object.timestamp())
        if timestamp <= int(time.time()):
            await ctx.send("You can't set a reminder in the past!")
            return

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO reminders (user_id, channel_id, reminder_time, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()))
                )
                await db.commit()
            await ctx.send(f"Okay, I will remind you on <t:{timestamp}:F> to '{reminder_message}'")
        except aiosqlite.Error as e:
            log.error(f"Database error setting reminder: {e}")
            await ctx.send("Sorry, a database error occurred.")

async def setup(bot: commands.Bot, **kwargs) -> None:
    """Standard setup, receiving the database path via kwargs from main.py."""
    db_path = kwargs.get("package")
    if not db_path:
        raise ValueError("Database path not provided for Reminders cog.")
    await bot.add_cog(Reminders(bot, db_path=db_path))
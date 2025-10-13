import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
import re
import logging
from typing import Optional
from utils.base_cog import BaseCog

class Reminders(BaseCog):
    """A cog for setting and checking natural language reminders."""
    def __init__(self, bot: commands.Bot, db_path: str):
        super().__init__(bot)
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
                        self.logger.warning(f"Failed to send reminder {rid} (user/channel not found or permissions error). Deleting. Error: {e}")
                    
                    await db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
                await db.commit()
        except Exception as e:
            self.logger.error(f"Unexpected error in reminder check loop: {e}", exc_info=True)

    @check_reminders.before_loop
    async def before_check_reminders(self) -> None:
        await self.bot.wait_until_ready()
        await self._setup_database()

    def _parse_reminder(self, query: str) -> tuple[str | None, str] | None:
        """
        Parses a query to separate the reminder message from the time string.
        It looks for common time-related prepositions to make a split.
        """
        # Keywords that typically precede a time description. Ordered by likely precedence.
        time_keywords = [' on ', ' at ', ' in ', ' for ', ' next ', ' tomorrow', ' tonight']
        
        # Sanitize the initial trigger words like "remind me to"
        sanitized_query = re.sub(r'^(remind me to|remind me|remember to|remember)\s*', '', query, flags=re.IGNORECASE).strip()

        # --- Strategy 1: Find a time keyword to split the message and time string ---
        for keyword in time_keywords:
            # Use rpartition to find the last occurrence of the keyword
            message_part, sep, time_part = sanitized_query.rpartition(keyword)
            
            if not sep:  # Keyword not found
                continue

            # Reconstruct the time string with the keyword, as partition removes it
            time_string = sep.strip() + ' ' + time_part.strip()
            
            # Check if the parsed time string is valid
            if dateparser.parse(time_string, settings={'PREFER_DATES_FROM': 'future'}):
                self.logger.info(f"Successfully parsed reminder. Message: '{message_part}', Time: '{time_string}'")
                return (message_part.strip(), time_string)

        # --- Strategy 2: If no keywords, assume the whole string is the time (e.g., "tomorrow 5pm") ---
        # This is a fallback for simple cases like ".sancho remind me tomorrow 5pm to take out trash"
        # (which would fail above) or when the message is at the end.
        # We try parsing the whole sanitized query. If it's a valid date, there's no message part.
        if dateparser.parse(sanitized_query, settings={'PREFER_DATES_FROM': 'future'}):
             self.logger.warning("Query was parsed entirely as a time string. No reminder message found.")
             return (None, sanitized_query) # No message, just time

        # If all strategies fail, return None
        self.logger.warning(f"Failed to parse reminder query: '{query}'")
        return None

    async def remind(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all reminder requests."""
        try:
            parsed = self._parse_reminder(query)
            
            # Case 1: Parsing completely failed
            if not parsed:
                await ctx.send("I couldn't understand that reminder. Please try a different phrasing, like:\n"
                             "• `.sancho remind me to take out the trash in 2 hours`\n"
                             "• `.sancho remind me on Friday at 8pm to call Bob`")
                return

            reminder_message, time_str = parsed

            # Case 2: Parser found a time, but no message
            if not reminder_message:
                await ctx.send("You need to provide a message for the reminder! What should I remind you about?")
                return

            dt_object = dateparser.parse(time_str, settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': True})
            
            # This check is technically redundant if _parse_reminder is correct, but it's a good safeguard.
            if not dt_object:
                self.logger.error(f"Dateparser failed on a string that was previously validated: '{time_str}'")
                await ctx.send("Sorry, something went wrong trying to understand that time. Please try again.")
                return

            timestamp = int(dt_object.timestamp())
            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past!")
                return

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO reminders (user_id, channel_id, reminder_time, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()))
                )
                await db.commit()
            
            await ctx.send(f"Okay, I will remind you on <t:{timestamp}:F> to '{reminder_message}'")
            self.logger.info(f"Reminder set for user {ctx.author.id} at {timestamp}.")

        except aiosqlite.Error as e:
            self.logger.error(f"Database error setting reminder for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, a database error occurred while setting your reminder.")
        # Any other unexpected exceptions will be caught by the global handler in main.py.


async def setup(bot: commands.Bot, **kwargs) -> None:
    """Standard setup, receiving the database path via kwargs from main.py."""
    db_path = kwargs.get("package")
    if not db_path:
        raise ValueError("Database path not provided for Reminders cog.")
    await bot.add_cog(Reminders(bot, db_path=db_path))
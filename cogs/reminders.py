import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
import re
import logging
from typing import Optional

log = logging.getLogger('RemindersCog')
DB_FILE = 'reminders.db'

class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    async def _setup_database(self):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    reminder_time INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            ''')
            await db.commit()
        log.info("Reminders database initialized and table ensured.")

    @tasks.loop(seconds=10)
    async def check_reminders(self):
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                current_time = int(time.time())
                async with db.execute('SELECT id, user_id, channel_id, message FROM reminders WHERE reminder_time <= ?', (current_time,)) as cursor:
                    due_reminders = list(await cursor.fetchall())
                if not due_reminders:
                    return
                log.info(f"Found {len(due_reminders)} due reminders to process.")
                for reminder_id, user_id, channel_id, message in due_reminders:
                    try:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        if user and channel:
                            await channel.send(f"{user.mention}, you asked me to remind you: '{message}'")
                        else:
                            log.warning(f"Could not find user ({user_id}) or channel ({channel_id}) for reminder ID {reminder_id}.")
                        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                    except (discord.NotFound, discord.Forbidden):
                        log.warning(f"User/channel not found or permissions missing for reminder {reminder_id}. Deleting.")
                        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                await db.commit()
        except aiosqlite.Error as e:
            log.error(f"Database error during reminder check: {e}")

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()
        await self._setup_database()

    async def remind(self, ctx, *, query: str):
        """Sets a reminder using a multi-pass regex and validation approach."""
        
        log.info("--- Starting Reminder Parsing ---")
        log.info(f"Initial Query: '{query}'")
        
        # This regex removes common polite words from the start or end of the query.
        sanitized_query = re.sub(r'^\s*(please|thank you|thanks)\s*|\s*(please|thank you|thanks)[.,!?]*\s*$', '', query, flags=re.IGNORECASE).strip()
        log.info(f"Sanitized Query: '{sanitized_query}'")

        # Define the patterns to try, from most specific to most general.
        # Each pattern must have a 'time' and 'message' capture group.
        patterns = [
            # Pattern 1: "remind me <TIME> to <MESSAGE>" (e.g., "remind me in 5 minutes to do laundry")
            re.compile(r'remind me\s+(?P<time>.*?)\s+to\s+(?P<message>.*)', re.IGNORECASE),
            
            # Pattern 2: "remind me to <MESSAGE> <TIME>" (e.g., "remind me to do laundry in 5 minutes")
            # This looks for a message followed by a common time preposition.
            re.compile(r'remind me to\s+(?P<message>.*?)\s+(in|on|at)\s+(?P<time>.*)', re.IGNORECASE),
        ]

        time_str = None
        reminder_message = None
        dt_object = None

        for i, pattern in enumerate(patterns):
            log.info(f"--- Trying Pattern #{i+1} ---")
            match = pattern.match(sanitized_query)
            if not match:
                log.info("Pattern did not match.")
                continue

            groups = match.groupdict()
            potential_time = groups.get('time', '').strip()
            potential_message = groups.get('message', '').strip()
            
            # For pattern 2, the preposition is not part of the time string itself.
            if i == 1: # If we are on the second pattern
                preposition = match.group(2) # The (in|on|at) group
                potential_time = f"{preposition} {potential_time}"

            log.info(f"Potential Time: '{potential_time}' | Potential Message: '{potential_message}'")

            # Validate the extracted time string with dateparser
            parsed_time = dateparser.parse(potential_time, settings={'PREFER_DATES_FROM': 'future'})
            if parsed_time:
                log.info(f"SUCCESS: Dateparser validated '{potential_time}'. This is the correct pattern.")
                time_str = potential_time
                reminder_message = potential_message
                break # Stop searching for patterns
            else:
                log.info("Dateparser could not validate the potential time. Trying next pattern.")

        # If after all patterns, we still haven't found a valid time
        if not time_str or not reminder_message:
            log.warning("Parsing failed: No pattern produced a valid time and message.")
            await ctx.send(
                "I couldn't understand that format. Please try one of these:\n"
                "- `.sancho remind me <message> in <time>`\n"
                "- `.sancho remind me in <time> to <message>`"
            )
            return

        log.info(f"Final Parsed Time String: '{time_str}'")
        log.info(f"Final Parsed Message: '{reminder_message}'")

        # Get the final, timezone-aware datetime object.
        dt_object = dateparser.parse(time_str, settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': True})

        reminder_timestamp = int(dt_object.timestamp()) # type: ignore 
        current_time = int(time.time())

        if reminder_timestamp <= current_time:
            await ctx.send("You can't set a reminder in the past! Please try again with a future time.")
            return

        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "INSERT INTO reminders (user_id, channel_id, reminder_time, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ctx.author.id, ctx.channel.id, reminder_timestamp, reminder_message, current_time)
                )
                await db.commit()
            await ctx.send(f"Okay, I will remind you on <t:{reminder_timestamp}:f> to '{reminder_message}'")
            log.info(f"SUCCESS: Reminder set for user {ctx.author.id} at {reminder_timestamp}.")
        except aiosqlite.Error as e:
            log.error(f"Database error when setting reminder: {e}")
            await ctx.send("Sorry, there was a database error setting your reminder. Please contact the author.")

async def setup(bot):
    await bot.add_cog(Reminders(bot))
    log.info("Reminders cog loaded.")
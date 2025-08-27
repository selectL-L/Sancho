import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
import re
import logging
from typing import Optional, Tuple

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

    @tasks.loop(seconds=15)
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

    def _parse_reminder(self, query: str) -> Optional[Tuple[str, str]]:
        """
        Attempts to parse a query into a (message, time_string) tuple by finding the
        longest possible valid time phrase from the beginning or end of the query.
        Returns None if parsing fails.
        """
        log.info(f"--- Starting New Parser ---")
        log.info(f"Original Query: '{query}'")

        # 1. Pre-processing: Handle Discord timestamps <t:12345:F>
        discord_ts_match = re.search(r'<t:(\d+):[a-zA-Z]>', query)
        if discord_ts_match:
            ts = discord_ts_match.group(1)
            # Replace with a string dateparser understands unambiguously
            query = query.replace(discord_ts_match.group(0), f'at timestamp {ts}')
            log.info(f"Converted Discord timestamp. New Query: '{query}'")

        # 2. Sanitization: Remove trigger words
        sanitized_query = re.sub(r'^(remind me to|remind me|remember to|remember)\s*', '', query, flags=re.IGNORECASE).strip()
        log.info(f"Sanitized Query: '{sanitized_query}'")

        words = sanitized_query.split()
        if not words:
            return None

        # STRATEGY 1: Time phrase is at the END. Find the longest valid phrase.
        # e.g., "do my laundry in 5 minutes"
        log.info("--- Strategy 1: Searching for longest time phrase at the end ---")
        for i in range(len(words)):
            potential_time = ' '.join(words[i:])
            potential_message = ' '.join(words[:i])
            
            log.debug(f"Trying split: MESSAGE='{potential_message}' | TIME='{potential_time}'")
            parsed_time = dateparser.parse(potential_time, settings={'PREFER_DATES_FROM': 'future'})
            if parsed_time:
                log.info(f"SUCCESS: Dateparser validated '{potential_time}'.")
                # Final cleanup on message
                final_message = potential_message.strip()
                if final_message.lower().endswith(' to'):
                    final_message = final_message[:-3].strip()
                return (final_message, potential_time.strip())

        # STRATEGY 2: Time phrase is at the BEGINNING. Find the longest valid phrase.
        # e.g., "in 5 minutes to do my laundry"
        log.info("--- Strategy 2: Searching for longest time phrase at the beginning ---")
        for i in range(len(words), 0, -1):
            potential_time = ' '.join(words[:i])
            potential_message = ' '.join(words[i:])

            log.debug(f"Trying split: TIME='{potential_time}' | MESSAGE='{potential_message}'")
            parsed_time = dateparser.parse(potential_time, settings={'PREFER_DATES_FROM': 'future'})
            if parsed_time:
                log.info(f"SUCCESS: Dateparser validated '{potential_time}'.")
                # Final cleanup on message
                final_message = potential_message.strip()
                if final_message.lower().startswith('to '):
                    final_message = final_message[3:].strip()
                return (final_message, potential_time.strip())
        
        log.warning("Parsing failed: No strategy produced a valid time and message.")
        return None

    async def remind(self, ctx, *, query: str):
        parsed_result = self._parse_reminder(query)

        if not parsed_result:
            await ctx.send(
                "I had trouble understanding that reminder. Please try a different phrasing, like:\n"
                "- `.sancho remind me to do the laundry in 2 hours`\n"
                "- `.sancho remind me on Friday at 8pm to call Bob`"
            )
            return

        reminder_message, time_str = parsed_result
        log.info(f"--- Final Parse Results ---")
        log.info(f"Message: '{reminder_message}'")
        log.info(f"Time String: '{time_str}'")

        if not reminder_message:
            await ctx.send("You need to provide a message for the reminder!")
            return

        dt_object = dateparser.parse(time_str, settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not dt_object:
            log.error(f"Dateparser failed on a string that was previously validated: '{time_str}'")
            await ctx.send("Sorry, something went wrong trying to understand that time. Please try again.")
            return

        reminder_timestamp = int(dt_object.timestamp())
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
import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
from dateparser.search import search_dates
import re
import logging
from typing import Optional

log = logging.getLogger('RemindersCog')
DB_FILE = 'reminders.db'

class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # We no longer hold a persistent connection. We connect when needed.
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    async def _setup_database(self):
        """Initializes the database and table. Called before the loop starts."""
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
        """Background task for checking and sending due reminders asynchronously."""
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
                    except Exception as e:
                        log.error(f"Error processing reminder {reminder_id}: {e}")
                
                await db.commit()
        except aiosqlite.Error as e:
            log.error(f"Database error during reminder check: {e}")

    @check_reminders.before_loop
    async def before_check_reminders(self):
        """Ensures the bot is ready and the database is set up before starting the loop."""
        await self.bot.wait_until_ready()
        await self._setup_database()

    async def remind(self, ctx, *, query: str):
        """Sets a reminder by intelligently finding the time phrase within the query."""
        clean_query = re.sub(r'^\s*(remind me|remind|reminder)\s*', '', query, flags=re.IGNORECASE).strip()
        found_dates = search_dates(clean_query, settings={'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not found_dates:
            await ctx.send("I couldn't find a time or date in your request. Please be more specific (e.g., 'in 10 minutes', 'tomorrow at 5pm').")
            return

        time_phrase, dt_object = found_dates[0]
        log.info(f"Dateparser found time string: '{time_phrase}' -> {dt_object}")
        reminder_message = clean_query.replace(time_phrase, '').strip()
        reminder_message = re.sub(r'^\s*to\s*', '', reminder_message, flags=re.IGNORECASE).strip()

        if not reminder_message:
            await ctx.send("It looks like you set a time but didn't provide a message for the reminder!")
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
            log.info(f"Reminder set for user {ctx.author.id} at {reminder_timestamp}.")
        except aiosqlite.Error as e:
            log.error(f"Database error when setting reminder: {e}")
            await ctx.send("Sorry, there was a database error setting your reminder. Please contact the author.")

async def setup(bot):
    await bot.add_cog(Reminders(bot))
    log.info("Reminders cog loaded.")
import discord
from discord.ext import commands, tasks
import aiosqlite
import time
import dateparser
import re
import logging
from typing import Optional, cast, Any
import pytz
from datetime import datetime
from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

class Reminders(BaseCog):
    """A cog for setting and checking natural language reminders."""
    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db_path = bot.db_path
        self.check_reminders.start()

    async def cog_unload(self) -> None:
        # `Cog.cog_unload` is expected to be asynchronous in newer discord.py
        # type stubs; implement as async so the override's return type
        # matches the base class (a coroutine).
        self.check_reminders.cancel()

    async def _setup_database(self) -> None:
        """Ensures the reminders and user_timezones tables exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # Main table for reminders
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    reminder_time INTEGER NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL
                )''')
            # New table for storing user-specific timezones
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_timezones (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL
                )''')
            await db.commit()
        self.logger.info("Reminders and Timezones database tables initialized.")

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone, defaulting to UTC."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT timezone FROM user_timezones WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                return row[0]
            return "UTC"

    @tasks.loop(seconds=15)
    async def check_reminders(self) -> None:
        """Periodically checks for and sends due reminders."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = int(time.time())
                cursor = await db.execute('SELECT id, user_id, channel_id, message FROM reminders WHERE reminder_time <= ?', (current_time,))
                reminders_to_delete = []
                for rid, uid, cid, msg in await cursor.fetchall():
                    try:
                        user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                        channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                            await channel.send(f"{user.mention}, you asked me to remind you: '{msg}'")
                    except (discord.NotFound, discord.Forbidden) as e:
                        self.logger.warning(f"Failed to send reminder {rid} (user/channel not found or permissions error). Deleting. Error: {e}")
                    
                    reminders_to_delete.append((rid,))
                
                if reminders_to_delete:
                    await db.executemany("DELETE FROM reminders WHERE id = ?", reminders_to_delete)
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

            user_tz = await self._get_user_timezone(ctx.author.id)
            date_settings = {
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': user_tz,
                'RETURN_AS_TIMEZONE_AWARE': True
            }
            dt_object = dateparser.parse(time_str, settings=cast(Any, date_settings))
            
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

    @commands.command(name="remindme", help="Sets a reminder. Usage: .remindme <subject> / <time>")
    async def remindme_command(self, ctx: commands.Context, *, query: str):
        """A structured command for setting a reminder."""
        try:
            if '/' not in query:
                await ctx.send(
                    "The `.remindme` command is not an NLP command. It requires a specific format: \n"
                    "` .remindme <subject> / <time>`\n\n"
                    "If you'd like to use NLP, you can start a message with keywords like `reminder` or `remind me`."
                )
                return

            parts = query.split('/')
            if len(parts) != 2:
                await ctx.send("Invalid format. Please use: `.remindme <subject> / <time>`")
                return

            subject = parts[0].strip()
            time_str = parts[1].strip()

            if not subject:
                await ctx.send("You must provide a subject for the reminder.")
                return
            if not time_str:
                await ctx.send("You must provide a time for the reminder.")
                return

            # --- Time Parsing ---
            timestamp = None
            # Strategy 1: Check for Discord timestamp format
            match = re.match(r'<t:(\d+):[a-zA-Z]>', time_str)
            if match:
                timestamp = int(match.group(1))
                self.logger.info(f"Parsed Discord timestamp: {timestamp}")
            else:
                # Strategy 2: Fallback to dateparser with user's timezone
                user_tz = await self._get_user_timezone(ctx.author.id)
                date_settings = {
                    'PREFER_DATES_FROM': 'future',
                    'TIMEZONE': user_tz,
                    'RETURN_AS_TIMEZONE_AWARE': True
                }
                dt_object = dateparser.parse(time_str, settings=cast(Any, date_settings))
                if dt_object:
                    timestamp = int(dt_object.timestamp())
                    self.logger.info(f"Parsed time string '{time_str}' to timestamp: {timestamp} using timezone {user_tz}")
                else:
                    await ctx.send(f"Sorry, I couldn't understand the time '{time_str}'. Please try a different format.")
                    return

            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past!")
                return

            # --- Database Insertion ---
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO reminders (user_id, channel_id, reminder_time, message, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ctx.author.id, ctx.channel.id, timestamp, subject, int(time.time()))
                )
                await db.commit()

            await ctx.send(f"Okay, I will remind you on <t:{timestamp}:F> to '{subject}'")
            self.logger.info(f"Reminder set via command for user {ctx.author.id} at {timestamp}.")

        except aiosqlite.Error as e:
            self.logger.error(f"Database error in remindme command for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, a database error occurred while setting your reminder.")
        except Exception as e:
            self.logger.error(f"Unexpected error in remindme command: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred. The issue has been logged.")

    async def _check_user_reminders(self, user_id: int) -> str:
        """Helper function to fetch and format a user's reminders."""
        async with aiosqlite.connect(self.db_path) as db:
            # Use a row factory to get dict-like rows
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, reminder_time, message FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
                (user_id,)
            )
            reminders = await cursor.fetchall()

        if not reminders:
            return "You have no pending reminders."

        user_tz_str = await self._get_user_timezone(user_id)
        
        response_lines = [f"Your reminders (Timezone: `{user_tz_str}`):", "```"]
        for i, reminder in enumerate(reminders, 1):
            # Format using Discord's timestamp for dynamic, client-side time display
            response_lines.append(f"#{i} (ID: {reminder['id']}) - \"{reminder['message']}\" - Due: <t:{reminder['reminder_time']}:F>")
        
        response_lines.append("```")
        return "\n".join(response_lines)

    @commands.command(name="checkreminders", help="Checks your pending reminders.")
    async def checkreminders_command(self, ctx: commands.Context):
        """Static command to list a user's reminders."""
        try:
            response = await self._check_user_reminders(ctx.author.id)
            await ctx.send(response)
        except Exception as e:
            self.logger.error(f"Error checking reminders for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An error occurred while fetching your reminders.")

    async def check_reminders_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for checking reminders."""
        # The query is unused here, but required by the dispatcher
        self.logger.info(f"Handling NLP request for checking reminders from user {ctx.author.id}.")
        await self.checkreminders_command(ctx)

    @commands.command(name="reminderdelete", aliases=["remdelete"], help="Deletes reminders by number. Use .checkreminders to see numbers.")
    async def reminderdelete_command(self, ctx: commands.Context, *, numbers_str: str):
        """Deletes one or more reminders by their # number from the list."""
        try:
            # 1. Get the user's current reminders to map # to db ID
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT id FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
                    (ctx.author.id,)
                )
                user_reminders_ids = [row[0] for row in await cursor.fetchall()]

            if not user_reminders_ids:
                await ctx.send("You have no reminders to delete.")
                return

            # 2. Parse the input numbers
            ids_to_delete = []
            invalid_numbers = []
            valid_numbers_deleted = []

            # Split by comma and handle potential spaces
            input_numbers = [num.strip() for num in numbers_str.split(',')]

            for num_str in input_numbers:
                if not num_str.isdigit():
                    invalid_numbers.append(num_str)
                    continue
                
                user_facing_num = int(num_str)
                # User numbers are 1-based, list indices are 0-based
                if 1 <= user_facing_num <= len(user_reminders_ids):
                    db_id = user_reminders_ids[user_facing_num - 1]
                    if db_id not in ids_to_delete:
                        ids_to_delete.append(db_id)
                        valid_numbers_deleted.append(user_facing_num)
                else:
                    invalid_numbers.append(num_str)

            # 3. Perform deletion
            if not ids_to_delete:
                await ctx.send(f"No valid reminder numbers provided. I couldn't find reminders for: {', '.join(invalid_numbers)}.")
                return

            async with aiosqlite.connect(self.db_path) as db:
                # Create a list of tuples for executemany
                await db.executemany("DELETE FROM reminders WHERE id = ?", [(id,) for id in ids_to_delete])
                await db.commit()

            # 4. Report results
            deleted_count = len(ids_to_delete)
            response_parts = [f"Successfully deleted {deleted_count} reminder(s): `#{', #'.join(map(str, sorted(valid_numbers_deleted)))}`"]
            
            if invalid_numbers:
                response_parts.append(f"Could not find reminders for these numbers: `{', '.join(invalid_numbers)}`.")

            await ctx.send("\n".join(response_parts))
            self.logger.info(f"User {ctx.author.id} deleted {deleted_count} reminders. IDs: {ids_to_delete}")

        except aiosqlite.Error as e:
            self.logger.error(f"Database error deleting reminders for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("A database error occurred while deleting reminders.")
        except Exception as e:
            self.logger.error(f"Unexpected error in reminderdelete command: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")

    async def delete_reminders_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for deleting reminders."""
        self.logger.info(f"Handling NLP request for deleting reminders from user {ctx.author.id}: '{query}'")
        
        # Find all numbers in the query string
        numbers_found = re.findall(r'\d+', query)
        
        if not numbers_found:
            await ctx.send("I see you want to delete a reminder, but you didn't specify which one. Please provide the reminder number (e.g., 'delete reminder 1').")
            return
            
        numbers_str = ",".join(numbers_found)
        await self.reminderdelete_command(ctx, numbers_str=numbers_str)

    @commands.command(name="timezone", help="Sets your timezone for reminders. E.g., UTC, EST, PST, GMT+5")
    async def timezone_command(self, ctx: commands.Context, timezone_str: str):
        """Sets the user's preferred timezone for parsing dates."""
        
        # --- Timezone Abbreviation and Offset Mapping ---
        # Provides a mapping from common, non-standard abbreviations to IANA timezones.
        TIMEZONE_ABBREVIATIONS = {
            "bst": "Europe/London",      # British Summer Time
            "ist": "Asia/Kolkata",       # Indian Standard Time
            "cst": "America/Chicago",    # Central Standard Time (US)
            "mst": "America/Denver",     # Mountain Standard Time (US)
            "pst": "America/Los_Angeles",# Pacific Standard Time (US)
            "est": "America/New_York",   # Eastern Standard Time (US)
        }

        tz_to_check = timezone_str.lower()
        final_tz_str = None

        # 1. Check for common abbreviations
        if tz_to_check in TIMEZONE_ABBREVIATIONS:
            final_tz_str = TIMEZONE_ABBREVIATIONS[tz_to_check]
        
        # 2. Check for GMT/UTC offset format (e.g., GMT+5, UTC-8)
        if not final_tz_str:
            match = re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', tz_to_check)
            if match:
                sign = match.group(2)
                offset = int(match.group(3))
                # pytz uses inverted signs for Etc/GMT (e.g., GMT+5 is Etc/GMT-5)
                inverted_sign = '-' if sign == '+' else '+'
                final_tz_str = f"Etc/GMT{inverted_sign}{offset}"

        # 3. If no match yet, use the original string for a direct pytz lookup
        if not final_tz_str:
            final_tz_str = timezone_str

        try:
            # Validate the final timezone string using pytz
            tz = pytz.timezone(final_tz_str)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO user_timezones (user_id, timezone) VALUES (?, ?)",
                    (ctx.author.id, tz.zone)
                )
                await db.commit()
            
            now = datetime.now(tz)
            await ctx.send(
                f"Your timezone has been set to `{tz.zone}`.\n"
                f"The current time in your timezone is `{now.strftime('%Y-%m-%d %H:%M:%S')}`."
            )
            self.logger.info(f"Timezone for user {ctx.author.id} set from '{timezone_str}' to '{tz.zone}'.")

        except pytz.UnknownTimeZoneError:
            self.logger.warning(f"Failed to set timezone for user {ctx.author.id}: Unrecognized timezone '{timezone_str}'.")
            await ctx.send(f"`{timezone_str}` is not a recognized timezone. Please use a standard IANA name (e.g., `US/Eastern`, `Europe/London`), a common abbreviation (e.g., `EST`, `BST`), or a GMT/UTC offset (e.g., `GMT+5`).")
        except aiosqlite.Error as e:
            self.logger.error(f"Database error setting timezone for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("A database error occurred while setting your timezone.")
        except Exception as e:
            self.logger.error(f"Unexpected error in timezone command: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")


async def setup(bot: SanchoBot, **kwargs) -> None:
    """Standard setup, receiving the database path via kwargs from main.py."""
    await bot.add_cog(Reminders(bot))
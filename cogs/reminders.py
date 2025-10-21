import discord
from discord.ext import commands, tasks
import time
import dateparser
import re
from typing import Optional, cast, Any, List
import pytz
from datetime import datetime
import asyncio

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager

class Reminders(BaseCog):
    """A cog for setting and checking natural language reminders."""
    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db: DatabaseManager = bot.db_manager # type: ignore
        self.check_reminders.start()

    async def cog_unload(self) -> None:
        # `Cog.cog_unload` is expected to be asynchronous in newer discord.py
        # type stubs; implement as async so the override's return type
        # matches the base class (a coroutine).
        self.check_reminders.cancel()

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone, defaulting to UTC."""
        tz = await self.db.get_user_timezone(user_id)
        return tz or "UTC"

    @tasks.loop(seconds=15)
    async def check_reminders(self) -> None:
        """Periodically checks for and sends due reminders."""
        try:
            current_time = int(time.time())
            due_reminders = await self.db.get_due_reminders(current_time)
            
            reminders_to_delete_ids: List[int] = []
            for reminder in due_reminders:
                try:
                    user = self.bot.get_user(reminder['user_id']) or await self.bot.fetch_user(reminder['user_id'])
                    channel = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])
                    if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                        await channel.send(f"{user.mention}, you asked me to remind you: '{reminder['message']}'")
                except (discord.NotFound, discord.Forbidden) as e:
                    self.logger.warning(f"Failed to send reminder {reminder['id']} (user/channel not found or permissions error). Deleting. Error: {e}")
                
                reminders_to_delete_ids.append(reminder['id'])
            
            if reminders_to_delete_ids:
                await self.db.delete_reminders(reminders_to_delete_ids)
        except Exception as e:
            self.logger.error(f"Unexpected error in reminder check loop: {e}", exc_info=True)

    @check_reminders.before_loop
    async def before_check_reminders(self) -> None:
        await self.bot.wait_until_ready()
        # No setup needed here anymore

    async def _parse_reminder(self, query: str) -> tuple[str | None, str] | None:
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
            
            # Check if the parsed time string is valid by running it in a thread
            if await asyncio.to_thread(dateparser.parse, time_string, settings={'PREFER_DATES_FROM': 'future'}):
                self.logger.info(f"Successfully parsed reminder. Message: '{message_part}', Time: '{time_string}'")
                return (message_part.strip(), time_string)

        # --- Strategy 2: If no keywords, assume the whole string is the time (e.g., "tomorrow 5pm") ---
        # We try parsing the whole sanitized query. If it's a valid date, there's no message part.
        if await asyncio.to_thread(dateparser.parse, sanitized_query, settings={'PREFER_DATES_FROM': 'future'}):
             self.logger.warning("Query was parsed entirely as a time string. No reminder message found.")
             return (None, sanitized_query) # No message, just time

        # If all strategies fail, return None
        self.logger.warning(f"Failed to parse reminder query: '{query}'")
        return None

    async def _interactive_reminder_flow(self, ctx: commands.Context, initial_message: str = "", initial_time: str = "") -> None:
        """Guides the user through creating a reminder interactively."""
        
        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # 1. Get Reminder Message
            reminder_message = initial_message
            if not reminder_message:
                await ctx.send("What should I remind you about? You can say `exit` to cancel.")
                msg = await self.bot.wait_for('message', check=check, timeout=120.0)
                if msg.content.lower() == 'exit':
                    await ctx.send("Reminder creation cancelled.")
                    return
                reminder_message = msg.content

            # 2. Get Reminder Time
            time_str = initial_time
            dt_object = None
            user_tz = await self._get_user_timezone(ctx.author.id)
            date_settings = {
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': user_tz,
                'RETURN_AS_TIMEZONE_AWARE': True
            }

            while True:
                if not time_str:
                    await ctx.send(f"When should I remind you about '{reminder_message}'? (e.g., 'in 2 hours', 'tomorrow at 5pm')")
                    msg = await self.bot.wait_for('message', check=check, timeout=120.0)
                    if msg.content.lower() == 'exit':
                        await ctx.send("Reminder creation cancelled.")
                        return
                    time_str = msg.content
                
                dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))
                if dt_object and dt_object.timestamp() > time.time():
                    break
                else:
                    await ctx.send(f"I couldn't understand that time or it's in the past. Please try another format. Your timezone is set to `{user_tz}`.")
                    time_str = "" # Reset to re-ask

            # 3. Confirmation
            timestamp = int(dt_object.timestamp())
            await ctx.send(f"Okay, I will remind you on <t:{timestamp}:F> to '{reminder_message}'. Is this correct? (`yes`/`no`)")
            
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            if msg.content.lower() in ['yes', 'y']:
                await self.db.add_reminder(ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()))
                await ctx.send("✅ Reminder saved!")
                self.logger.info(f"Reminder set for user {ctx.author.id} at {timestamp}.")
            else:
                await ctx.send("Reminder cancelled. You can start over if you wish.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Reminder creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error in interactive reminder flow for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while creating the reminder.")

    async def remind(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all reminder requests."""
        try:
            # Naked command check
            sanitized_query = re.sub(r'^(remind me to|remind me|remember to|remember|set reminder)\s*', '', query, flags=re.IGNORECASE).strip()
            if not sanitized_query:
                await self._interactive_reminder_flow(ctx)
                return

            parsed = await self._parse_reminder(query)
            
            if not parsed or not parsed[0] or not parsed[1]:
                self.logger.info(f"Could not fully parse reminder '{query}'. Starting interactive flow.")
                initial_msg = sanitized_query if not parsed or not parsed[1] else parsed[0]
                await self._interactive_reminder_flow(ctx, initial_message=initial_msg or "")
                return

            reminder_message, time_str = parsed

            user_tz = await self._get_user_timezone(ctx.author.id)
            date_settings = {
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': user_tz,
                'RETURN_AS_TIMEZONE_AWARE': True
            }
            dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))
            
            if not dt_object:
                self.logger.error(f"Dateparser failed on a string that was previously validated: '{time_str}'. Starting interactive flow.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "")
                return

            timestamp = int(dt_object.timestamp())
            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past! Please try again.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "")
                return

            # --- Confirmation Step ---
            def check(m: discord.Message) -> bool:
                return m.author == ctx.author and m.channel == ctx.channel

            await ctx.send(
                f"Okay, I have a reminder for you to '{reminder_message}' on <t:{timestamp}:F>.\n"
                "Is this correct? (`yes` to confirm, `edit` to change, or `no` to cancel)"
            )
            
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            
            if msg.content.lower() in ['yes', 'y']:
                if reminder_message:
                    await self.db.add_reminder(ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()))
                    await ctx.send("✅ Reminder saved!")
                    self.logger.info(f"Reminder set for user {ctx.author.id} at {timestamp}.")
                else:
                    # This case should ideally not be hit if parsing and flow are correct
                    await ctx.send("Something went wrong, the reminder message was lost. Please try again.")
            elif msg.content.lower() == 'edit':
                await ctx.send("Let's edit the reminder.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_time=time_str)
            else:
                await ctx.send("Reminder cancelled.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Reminder creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error setting reminder for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, an error occurred while setting your reminder.")

    async def check_reminders_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for checking reminders."""
        self.logger.info(f"Handling NLP request for checking reminders from user {ctx.author.id}.")
        try:
            reminders = await self.db.get_user_reminders(ctx.author.id)

            if not reminders:
                await ctx.send("You have no pending reminders.")
                return

            user_tz_str = await self._get_user_timezone(ctx.author.id)
            
            embed = discord.Embed(
                title=f"{ctx.author.display_name}'s Reminders",
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"Your timezone is set to {user_tz_str}. Use 'delete reminder <#>' to remove one.")

            description_lines = []
            for i, reminder in enumerate(reminders, 1):
                # Format using Discord's timestamp for dynamic, client-side time display
                description_lines.append(
                    f"**#{i} (ID: {reminder['id']})** - \"{reminder['message']}\"\n"
                    f"Due: <t:{reminder['reminder_time']}:F>"
                )
            
            embed.description = "\n\n".join(description_lines)
            await ctx.send(embed=embed)
        except Exception as e:
            self.logger.error(f"Error checking reminders for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An error occurred while fetching your reminders.")

    async def delete_reminders_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for deleting reminders."""
        self.logger.info(f"Handling NLP request for deleting reminders from user {ctx.author.id}: '{query}'")
        
        # Find all numbers in the query string
        numbers_found = re.findall(r'\d+', query)
        
        if not numbers_found:
            await ctx.send("I see you want to delete a reminder, but you didn't specify which one. Please provide the reminder number (e.g., 'delete reminder 1').")
            return
            
        numbers_str = ",".join(numbers_found)
        
        try:
            # 1. Get the user's current reminders to map # to db ID
            user_reminders = await self.db.get_user_reminders(ctx.author.id)
            user_reminders_ids = [r['id'] for r in user_reminders]

            if not user_reminders_ids:
                await ctx.send("You have no reminders to delete.")
                return

            ids_to_delete = []
            invalid_numbers = []
            valid_numbers_deleted = []

            input_numbers = [num.strip() for num in numbers_str.split(',')]

            for num_str in input_numbers:
                if not num_str.isdigit():
                    invalid_numbers.append(num_str)
                    continue
                
                user_facing_num = int(num_str)
                if 1 <= user_facing_num <= len(user_reminders_ids):
                    db_id = user_reminders_ids[user_facing_num - 1]
                    if db_id not in ids_to_delete:
                        ids_to_delete.append(db_id)
                        valid_numbers_deleted.append(user_facing_num)
                else:
                    invalid_numbers.append(num_str)

            if not ids_to_delete:
                await ctx.send(f"No valid reminder numbers provided. I couldn't find reminders for: {', '.join(invalid_numbers)}.")
                return

            await self.db.delete_reminders(ids_to_delete)

            deleted_count = len(ids_to_delete)
            response_parts = [f"Successfully deleted {deleted_count} reminder(s): `#{', #'.join(map(str, sorted(valid_numbers_deleted)))}`"]
            
            if invalid_numbers:
                response_parts.append(f"Could not find reminders for these numbers: `{', '.join(invalid_numbers)}`.")

            await ctx.send("\n".join(response_parts))
            self.logger.info(f"User {ctx.author.id} deleted {deleted_count} reminders. IDs: {ids_to_delete}")

        except Exception as e:
            self.logger.error(f"Unexpected error in reminderdelete NLP: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")

    async def set_timezone_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for setting a user's timezone."""
        # Clean the query to get just the timezone string
        timezone_str = re.sub(r'\b(set|change)\b|\b(timezone|tz)\b', '', query, flags=re.IGNORECASE).strip()

        if not timezone_str:
            await ctx.send("Please provide a timezone to set. For example: `set timezone EST` or `tz US/Eastern`.")
            return

        TIMEZONE_ABBREVIATIONS = {
            "bst": "Europe/London", "ist": "Asia/Kolkata", "cst": "America/Chicago",
            "mst": "America/Denver", "pst": "America/Los_Angeles", "est": "America/New_York",
        }

        tz_to_check = timezone_str.lower()
        final_tz_str = None

        if tz_to_check in TIMEZONE_ABBREVIATIONS:
            final_tz_str = TIMEZONE_ABBREVIATIONS[tz_to_check]
        
        if not final_tz_str:
            match = re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', tz_to_check)
            if match:
                sign = match.group(2)
                offset = int(match.group(3))
                # For pytz, the sign is inverted for Etc/GMT zones.
                # GMT-5 is Etc/GMT+5.
                inverted_sign = '-' if sign == '+' else '+'
                final_tz_str = f"Etc/GMT{inverted_sign}{offset}"
                # But we want to show the user the logical name
                display_tz_str = f"GMT{sign}{offset}"

        if not final_tz_str:
            final_tz_str = timezone_str
            display_tz_str = timezone_str

        try:
            tz = pytz.timezone(final_tz_str)
            
            # Use the display name for storage and confirmation, but the real one for calculation.
            zone_to_store = display_tz_str if 'display_tz_str' in locals() else final_tz_str
            
            await self.db.set_user_timezone(ctx.author.id, zone_to_store)
            
            now = datetime.now(tz)
            await ctx.send(
                f"Your timezone has been set to `{zone_to_store}`.\n"
                f"The current time in your timezone is `{now.strftime('%Y-%m-%d %H:%M:%S')}`."
            )
            self.logger.info(f"Timezone for user {ctx.author.id} set from '{timezone_str}' to '{zone_to_store}'.")

        except pytz.UnknownTimeZoneError:
            self.logger.warning(f"Failed to set timezone for user {ctx.author.id}: Unrecognized timezone '{timezone_str}'.")
            await ctx.send(f"`{timezone_str}` is not a recognized timezone. Please use a standard IANA name (e.g., `US/Eastern`, `Europe/London`), a common abbreviation (e.g., `EST`, `BST`), or a GMT/UTC offset (e.g., `GMT+5`).")
        except Exception as e:
            self.logger.error(f"Unexpected error in timezone NLP: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")


async def setup(bot: SanchoBot, **kwargs) -> None:
    """Standard setup, receiving the database path via kwargs from main.py."""
    await bot.add_cog(Reminders(bot))
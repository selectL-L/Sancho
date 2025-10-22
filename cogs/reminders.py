import discord
from discord.ext import commands, tasks
import time
import dateparser
import re
from typing import Optional, cast, Any, List
import pytz
from datetime import datetime
import asyncio
from dateutil.rrule import rrule, rrulestr, WEEKLY, DAILY, HOURLY, MINUTELY, MONTHLY, YEARLY
from dateutil.parser import parse as dateutil_parse

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager

class Reminders(BaseCog):
    """A cog for setting and checking natural language reminders."""
    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db: DatabaseManager = bot.db_manager # type: ignore
        self.scheduled_tasks: dict[int, asyncio.Task[None]] = {}

    async def cog_load(self) -> None:
        self.logger.info("Scheduling existing reminders from database...")
        self.bot.loop.create_task(self._schedule_existing_reminders())

    async def cog_unload(self) -> None:
        # Cancel all running reminder tasks when the cog is unloaded
        for task in self.scheduled_tasks.values():
            task.cancel()
        self.scheduled_tasks.clear()

    async def _schedule_existing_reminders(self) -> None:
        """Queries the database for all pending reminders and schedules them."""
        try:
            all_reminders = await self.db.get_all_pending_reminders()
            count = 0
            for reminder in all_reminders:
                self._schedule_reminder_task(reminder)
                count += 1
            self.logger.info(f"Scheduled {count} existing reminders.")
        except Exception as e:
            self.logger.error(f"Failed to schedule existing reminders: {e}", exc_info=True)

    def _schedule_reminder_task(self, reminder: dict[str, Any]) -> None:
        """Creates and stores an asyncio.Task for a given reminder."""
        reminder_id = reminder['id']
        
        # If a task for this reminder already exists, cancel it before creating a new one.
        if reminder_id in self.scheduled_tasks:
            self.scheduled_tasks[reminder_id].cancel()

        delay = reminder['reminder_time'] - time.time()
        
        if delay > 0:
            task = self.bot.loop.create_task(self._send_reminder_after_delay(delay, reminder))
            self.scheduled_tasks[reminder_id] = task
            self.logger.info(f"Scheduled reminder {reminder_id} to be sent in {delay:.2f} seconds.")
        else:
            # If the reminder is already due (e.g., bot was offline), send it immediately.
            self.logger.info(f"Reminder {reminder_id} is overdue. Sending immediately.")
            self.bot.loop.create_task(self._send_reminder_after_delay(0, reminder))

    def _format_overdue_time(self, seconds: float) -> str:
        """Formats a duration in seconds into a human-readable string."""
        seconds = abs(seconds)
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        if seconds < 86400:
            hours = int(seconds // 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        days = int(seconds // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"

    async def _send_reminder_after_delay(self, delay: float, reminder: dict[str, Any]) -> None:
        """Waits for a specified delay, then sends the reminder and triggers cleanup/rescheduling."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)

            user = self.bot.get_user(reminder['user_id']) or await self.bot.fetch_user(reminder['user_id'])
            channel = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])

            if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                overdue_message = ""
                if delay <= 0:
                    overdue_seconds = time.time() - reminder['reminder_time']
                    overdue_message = f" (This was due {self._format_overdue_time(overdue_seconds)})"

                await channel.send(f"{user.mention}, you asked me to remind you: '{reminder['message']}'{overdue_message}")
                self.logger.info(f"Sent reminder {reminder['id']} to user {user.id}.")

        except asyncio.CancelledError:
            self.logger.info(f"Reminder task {reminder['id']} was cancelled.")
            # When cancelled, we must ensure it's removed from the database so it doesn't get rescheduled on restart.
            await self.db.delete_reminders([reminder['id']])
        except (discord.NotFound, discord.Forbidden) as e:
            self.logger.warning(f"Failed to send reminder {reminder['id']} (user/channel not found or permissions error). Error: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error in reminder task {reminder['id']}: {e}", exc_info=True)
        finally:
            # This block now only triggers the next step.
            self.scheduled_tasks.pop(reminder['id'], None)
            self.bot.loop.create_task(self._reschedule_or_cleanup(reminder))

    async def _reschedule_or_cleanup(self, reminder: dict[str, Any]) -> None:
        """Handles the logic for rescheduling a recurring reminder or deleting a one-off."""
        reminder_id = reminder['id']
        
        # First, check if the reminder still exists. It might have been deleted while the task was running.
        reminder_data = await self.db.get_reminder_by_id(reminder_id)
        if not reminder_data:
            self.logger.info(f"Reminder {reminder_id} was deleted. Halting recurrence.")
            return

        # If it's a recurring reminder, calculate and schedule the next occurrence.
        if reminder_data.get('is_recurring') and reminder_data.get('recurrence_rule'):
            self.logger.info(f"Reminder {reminder_id} is recurring. Calculating next occurrence.")
            try:
                user_tz_str = await self._get_user_timezone(reminder_data['user_id'])
                user_tz = pytz.timezone(user_tz_str)
                
                start_date = datetime.fromtimestamp(reminder_data['created_at'], tz=user_tz)
                rule = rrulestr(reminder_data['recurrence_rule'], dtstart=start_date)
                
                current_reminder_time_aware = datetime.fromtimestamp(reminder_data['reminder_time'], tz=user_tz)
                next_occurrence = rule.after(current_reminder_time_aware)

                if next_occurrence:
                    next_timestamp = int(next_occurrence.timestamp())
                    await self.db.update_reminder_time(reminder_id, next_timestamp)
                    
                    next_reminder = reminder_data.copy()
                    next_reminder['reminder_time'] = next_timestamp
                    self._schedule_reminder_task(next_reminder)
                    self.logger.info(f"Rescheduled reminder {reminder_id} for {next_occurrence.isoformat()}.")
                else:
                    self.logger.info(f"Recurring reminder {reminder_id} has no more occurrences. Deleting.")
                    await self.db.delete_reminders([reminder_id])
            except Exception as e:
                self.logger.error(f"Failed to reschedule recurring reminder {reminder_id}: {e}", exc_info=True)
                await self.db.delete_reminders([reminder_id]) # Delete if rescheduling fails
        else:
            # If it's not recurring, simply delete it.
            await self.db.delete_reminders([reminder_id])
            self.logger.info(f"Cleaned up non-recurring reminder {reminder_id} from database.")

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone, defaulting to UTC."""
        tz = await self.db.get_user_timezone(user_id)
        return tz or "UTC"

    async def _parse_reminder(self, query: str) -> tuple[str | None, str, str | None] | None:
        """
        Parses a query to separate the reminder message from the time string.
        It looks for common time-related prepositions to make a split.
        Returns a tuple of (message, time_string, recurrence_rule).
        """
        # Keywords that typically precede a time description. Ordered by likely precedence.
        time_keywords = [' on ', ' at ', ' in ', ' for ', ' next ', ' tomorrow', ' tonight']
        
        # Sanitize the initial trigger words like "remind me to"
        sanitized_query = re.sub(r'^(remind me to|remind me|remember to|remember)\s*', '', query, flags=re.IGNORECASE).strip()

        # --- Strategy 1: Detect and parse recurrence ---
        recurrence_rule = None
        # More robust regex to capture "every day", "every 2 weeks", "every weekday", etc.
        recurrence_match = re.search(
            r'\b(every\s+(?:(?P<interval>\d+)\s+)?(?P<freq>second|minute|hour|day|week|month|year)s?|every\s+(?P<weekday>weekday))\b',
            sanitized_query,
            re.IGNORECASE
        )
        if recurrence_match:
            groups = recurrence_match.groupdict()
            interval = int(groups.get('interval') or 1)
            
            if groups.get('weekday'):
                recurrence_rule = f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
            else:
                freq_map = {
                    'second': 'SECONDLY', 'minute': 'MINUTELY', 'hour': 'HOURLY',
                    'day': 'DAILY', 'week': 'WEEKLY', 'month': 'MONTHLY', 'year': 'YEARLY'
                }
                freq_str = groups['freq'].lower()
                freq = freq_map.get(freq_str)
                if freq:
                    recurrence_rule = f"FREQ={freq};INTERVAL={interval}"

            if recurrence_rule:
                self.logger.info(f"Detected recurrence rule: {recurrence_rule}")
                # Remove the recurrence part from the query to not confuse dateparser for the first occurrence
                sanitized_query = sanitized_query.replace(recurrence_match.group(0), '', 1).strip()

        # --- Strategy 2: Find a time keyword to split the message and time string ---
        for keyword in time_keywords:
            message_part, sep, time_part = sanitized_query.rpartition(keyword)
            if not sep: continue

            time_string = sep.strip() + ' ' + time_part.strip()
            if await asyncio.to_thread(dateparser.parse, time_string, settings={'PREFER_DATES_FROM': 'future'}):
                self.logger.info(f"Parsed reminder. Message: '{message_part}', Time: '{time_string}', Recurrence: {recurrence_rule}")
                return (message_part.strip(), time_string, recurrence_rule)

        # --- Strategy 3: If no keywords, assume the whole string is the time ---
        if await asyncio.to_thread(dateparser.parse, sanitized_query, settings={'PREFER_DATES_FROM': 'future'}):
             self.logger.warning("Query was parsed entirely as a time string. No reminder message found.")
             return (None, sanitized_query, recurrence_rule)

        self.logger.warning(f"Failed to parse reminder query: '{query}'")
        return None

    async def _interactive_reminder_flow(self, ctx: commands.Context, initial_message: str = "", initial_time: str = "", initial_recurrence: Optional[str] = None) -> None:
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
            recurrence_rule = initial_recurrence

            while True:
                if not time_str:
                    await ctx.send(f"When should I remind you about '{reminder_message}'? (e.g., 'in 2 hours', 'every day at 5pm')")
                    msg = await self.bot.wait_for('message', check=check, timeout=120.0)
                    if msg.content.lower() == 'exit':
                        await ctx.send("Reminder creation cancelled.")
                        return
                    time_str = msg.content
                
                # Check for recurrence in the time string if not already provided
                if not recurrence_rule:
                    recurrence_match = re.search(
                        r'\b(every\s+(?:(?P<interval>\d+)\s+)?(?P<freq>second|minute|hour|day|week|month|year)s?|every\s+(?P<weekday>weekday))\b',
                        time_str, re.IGNORECASE
                    )
                    if recurrence_match:
                        groups = recurrence_match.groupdict()
                        interval = int(groups.get('interval') or 1)
                        if groups.get('weekday'):
                            recurrence_rule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
                        else:
                            freq_map = {
                                'second': 'SECONDLY', 'minute': 'MINUTELY', 'hour': 'HOURLY',
                                'day': 'DAILY', 'week': 'WEEKLY', 'month': 'MONTHLY', 'year': 'YEARLY'
                            }
                            freq_str = groups['freq'].lower()
                            freq = freq_map.get(freq_str)
                            if freq:
                                recurrence_rule = f"FREQ={freq};INTERVAL={interval}"

                        if recurrence_rule:
                            # Clean the time string for dateparser
                            time_str = time_str.replace(recurrence_match.group(0), '', 1).strip()

                dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))

                # If time_str is empty after stripping recurrence, but we have a rule, calculate the first occurrence.
                if not time_str and recurrence_rule:
                    now = datetime.now(pytz.timezone(user_tz))
                    # We need a start date for rrule to calculate the next occurrence
                    rule = rrulestr(recurrence_rule, dtstart=now)
                    dt_object = rule.after(now)

                if dt_object and dt_object.timestamp() > time.time():
                    break
                else:
                    await ctx.send(f"I couldn't understand that time or it's in the past. Please try another format. Your timezone is set to `{user_tz}`.")
                    time_str = "" # Reset to re-ask
                    recurrence_rule = None # Reset recurrence if time fails

            # 3. Confirmation
            timestamp = int(dt_object.timestamp())
            confirmation_message = f"Okay, I will remind you on <t:{timestamp}:F> to '{reminder_message}'."
            if recurrence_rule:
                confirmation_message += f"\nThis reminder will repeat. Is this correct? (`yes`/`no`)"
            else:
                confirmation_message += " Is this correct? (`yes`/`no`)"

            await ctx.send(confirmation_message)
            
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            if msg.content.lower() in ['yes', 'y']:
                is_recurring = recurrence_rule is not None
                new_reminder_id = await self.db.add_reminder(
                    ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()),
                    is_recurring, recurrence_rule
                )
                
                new_reminder_data = {
                    'id': new_reminder_id, 'user_id': ctx.author.id, 'channel_id': ctx.channel.id,
                    'reminder_time': timestamp, 'message': reminder_message, 'created_at': int(time.time()),
                    'is_recurring': is_recurring, 'recurrence_rule': recurrence_rule
                }
                self._schedule_reminder_task(new_reminder_data)
                
                await ctx.send("✅ Reminder saved and scheduled!")
                self.logger.info(f"Reminder {new_reminder_id} set for user {ctx.author.id} at {timestamp} (Recurring: {is_recurring}).")

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
            sanitized_query = re.sub(r'^(remind me to|remind me|remember to|remember|set reminder)\s*', '', query, flags=re.IGNORECASE).strip()
            if not sanitized_query:
                await self._interactive_reminder_flow(ctx)
                return

            parsed = await self._parse_reminder(query)
            
            if not parsed or not parsed[0] or not parsed[1]:
                self.logger.info(f"Could not fully parse reminder '{query}'. Starting interactive flow.")
                initial_msg = sanitized_query if not parsed or not parsed[1] else parsed[0]
                initial_recurrence = parsed[2] if parsed else None
                await self._interactive_reminder_flow(ctx, initial_message=initial_msg or "", initial_recurrence=initial_recurrence)
                return

            reminder_message, time_str, recurrence_rule = parsed

            user_tz = await self._get_user_timezone(ctx.author.id)
            date_settings = {
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': user_tz,
                'RETURN_AS_TIMEZONE_AWARE': True
            }
            dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))
            
            if not dt_object:
                self.logger.error(f"Dateparser failed on a string that was previously validated: '{time_str}'. Starting interactive flow.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_recurrence=recurrence_rule)
                return

            timestamp = int(dt_object.timestamp())
            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past! Please try again.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_recurrence=recurrence_rule)
                return

            # --- Confirmation Step ---
            def check(m: discord.Message) -> bool:
                return m.author == ctx.author and m.channel == ctx.channel

            confirmation_text = f"Okay, I have a reminder for you to '{reminder_message}' on <t:{timestamp}:F>."
            if recurrence_rule:
                confirmation_text += "\nThis reminder will repeat."
            
            await ctx.send(
                f"{confirmation_text}\n"
                "Is this correct? (`yes` to confirm, `edit` to change, or `no` to cancel)"
            )
            
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            
            if msg.content.lower() in ['yes', 'y']:
                if reminder_message:
                    is_recurring = recurrence_rule is not None
                    new_reminder_id = await self.db.add_reminder(
                        ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()),
                        is_recurring, recurrence_rule
                    )
                    
                    new_reminder_data = {
                        'id': new_reminder_id, 'user_id': ctx.author.id, 'channel_id': ctx.channel.id,
                        'reminder_time': timestamp, 'message': reminder_message, 'created_at': int(time.time()),
                        'is_recurring': is_recurring, 'recurrence_rule': recurrence_rule
                    }
                    self._schedule_reminder_task(new_reminder_data)
                    
                    await ctx.send("✅ Reminder saved and scheduled!")
                    self.logger.info(f"Reminder {new_reminder_id} set for user {ctx.author.id} at {timestamp} (Recurring: {is_recurring}).")
                else:
                    await ctx.send("Something went wrong, the reminder message was lost. Please try again.")
            elif msg.content.lower() == 'edit':
                await ctx.send("Let's edit the reminder.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_time=time_str, initial_recurrence=recurrence_rule)
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
            
            # Create a mapping from user-facing number to reminder ID
            num_to_id_map = {i + 1: r['id'] for i, r in enumerate(user_reminders)}

            if not user_reminders:
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
                db_id = num_to_id_map.get(user_facing_num)

                if db_id:
                    if db_id not in ids_to_delete:
                        ids_to_delete.append(db_id)
                        valid_numbers_deleted.append(user_facing_num)
                        # Also cancel the scheduled task
                        if db_id in self.scheduled_tasks:
                            self.scheduled_tasks[db_id].cancel()
                            # Remove from the dict to prevent memory leaks
                            self.scheduled_tasks.pop(db_id, None)
                            self.logger.info(f"Cancelled and removed scheduled task for deleted reminder {db_id}.")
                else:
                    invalid_numbers.append(num_str)

            if not ids_to_delete:
                await ctx.send(f"No valid reminder numbers provided. I couldn't find reminders for: {', '.join(invalid_numbers)}.")
                return

            # This is the crucial step: delete from the database so it doesn't recur on restart.
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
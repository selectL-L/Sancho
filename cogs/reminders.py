"""
cogs/reminders.py

This cog is responsible for all reminder-related functionality. It allows users
to set, view, and delete reminders using natural language.

Key Features:
- Natural Language Parsing: Uses `dateparser` and custom regex to understand
  time expressions like "in 5 minutes", "tomorrow at 3pm", or "on Friday".
- Recurring Reminders: Supports setting reminders that repeat, such as "every day"
  or "every Tuesday", by generating and storing `rrule` strings.
- Timezone Awareness: Allows users to set their timezone to ensure reminders
  are delivered at the correct local time.
- Persistent Storage: Saves all reminders to the database, ensuring they survive
  bot restarts.
- Dynamic Scheduling: On cog load, it fetches all pending reminders from the
  database and schedules them as `asyncio.Task` instances. This ensures the
  bot can be updated without losing reminders.
- Interactive Flow: If the initial NLP parsing fails, it guides the user
  through a step-by-step process to create a reminder.
(Damn I'm eloquent)
"""
import discord
from discord.ext import commands, tasks
import time
import dateparser
import re
from typing import Optional, cast, Any, List, Callable
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
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager
        # Stores active reminder tasks, mapping reminder ID to the asyncio.Task instance.
        # This allows us to cancel reminders if they are deleted or the cog is reloaded.
        self.scheduled_tasks: dict[int, asyncio.Task[None]] = {}

    async def cog_load(self) -> None:
        """Schedules all pending reminders from the database when the cog is loaded."""
        self.logger.info("Scheduling existing reminders from database...")
        # Use create_task to run this in the background without blocking cog loading.
        self.bot.loop.create_task(self._schedule_existing_reminders())

    async def cog_unload(self) -> None:
        """Cancels all running reminder tasks when the cog is unloaded."""
        # This prevents reminders from firing while the cog is inactive or being reloaded.
        for task in self.scheduled_tasks.values():
            task.cancel()
        self.scheduled_tasks.clear()

    async def _schedule_existing_reminders(self) -> None:
        """Queries the database for all pending reminders and schedules them."""
        try:
            all_reminders = await self.db_manager.get_all_reminders()
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
        # This is important for rescheduling recurring reminders or handling reloads.
        if reminder_id in self.scheduled_tasks:
            self.scheduled_tasks[reminder_id].cancel()

        # Calculate the delay until the reminder is due.
        delay = reminder['reminder_time'] - time.time()
        
        if delay > 0:
            # Create a new asyncio task that will fire after the calculated delay.
            task = self.bot.loop.create_task(self._send_reminder_after_delay(delay, reminder))
            # Add a callback that will handle cleanup/rescheduling ONLY if the task completes normally.
            task.add_done_callback(self._create_done_callback(reminder))
            self.scheduled_tasks[reminder_id] = task
            self.logger.info(f"Scheduled reminder {reminder_id} to be sent in {delay:.2f} seconds.")
        else:
            # If the reminder is already due (e.g., bot was offline), send it immediately.
            self.logger.info(f"Reminder {reminder_id} is overdue. Sending immediately.")
            # We still create a task so the done callback handles cleanup consistently.
            task = self.bot.loop.create_task(self._send_reminder_after_delay(0, reminder))
            task.add_done_callback(self._create_done_callback(reminder))
            self.scheduled_tasks[reminder_id] = task

    def _create_done_callback(self, reminder: dict[str, Any]) -> "Callable[[asyncio.Task[None]], None]":
        """
        Creates a closure for the task's done callback. This captures the reminder
        data and provides a function that checks the task's state before cleanup.
        """
        def done_callback(task: asyncio.Task[None]) -> None:
            # --- This is the core of the fix ---
            # Only proceed with cleanup if the task was NOT cancelled.
            # This prevents the database entry from being deleted on cog reloads.
            if task.cancelled():
                self.logger.info(f"Reminder {reminder['id']} task was cancelled. Skipping cleanup.")
                return
            
            # Also, check for exceptions during task execution.
            if task.exception():
                self.logger.error(f"An exception occurred in reminder task {reminder['id']}: {task.exception()}")
                # Depending on the desired behavior, you might still want to clean up or retry.
                # For now, we'll log it and let it be. It might be rescheduled on next restart.
                return

            # If the task completed successfully, proceed with the cleanup/reschedule logic.
            self.logger.info(f"Reminder task {reminder['id']} finished. Proceeding to cleanup/reschedule.")
            if not self.bot.is_closed():
                self.bot.loop.create_task(self._reschedule_or_cleanup(reminder))

        return done_callback

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
        """
        Waits for a specified delay, then sends the reminder.
        Cleanup and rescheduling are now handled by the task's done callback.
        """
        try:
            # Only sleep if the reminder is in the future. Overdue reminders run immediately.
            if delay > 0:
                await asyncio.sleep(delay)

            # Fetch the user and channel to send the reminder to.
            user = self.bot.get_user(reminder['user_id']) or await self.bot.fetch_user(reminder['user_id'])
            channel = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])

            if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                overdue_message = ""
                # If the reminder was overdue, add a note indicating how long ago it was due.
                if delay <= 0:
                    overdue_seconds = time.time() - reminder['reminder_time']
                    overdue_message = f" (This was due {self._format_overdue_time(overdue_seconds)})"

                await channel.send(f"{user.mention}, you asked me to remind you: '{reminder['message']}'{overdue_message}")
                self.logger.info(f"Sent reminder {reminder['id']} to user {user.id}.")

        except asyncio.CancelledError:
            # This is expected when the cog is unloaded. The done callback will see the
            # cancelled state and prevent cleanup.
            self.logger.info(f"Reminder task {reminder['id']} was cancelled, likely due to cog unload.")
            # Re-raise the error to ensure the task is properly marked as cancelled.
            raise
        except (discord.NotFound, discord.Forbidden) as e:
            self.logger.warning(f"Failed to send reminder {reminder['id']} (user/channel not found or permissions error). Deleting. Error: {e}")
            # If we can't find the user/channel, the reminder is unserviceable. Delete it directly.
            await self.db_manager.delete_reminders([reminder['id']])
        except Exception as e:
            self.logger.error(f"Unexpected error in reminder task {reminder['id']}: {e}", exc_info=True)
        finally:
            # Clean up the completed/cancelled task from our tracking dictionary.
            self.scheduled_tasks.pop(reminder['id'], None)

    async def _reschedule_or_cleanup(self, reminder: dict[str, Any]) -> None:
        """Handles the logic for rescheduling a recurring reminder or deleting a one-off."""
        reminder_id = reminder['id']
        
        # First, check if the reminder still exists. It might have been deleted while the task was running.
        reminder_data = await self.db_manager.get_reminder_by_id(reminder_id)
        if not reminder_data:
            self.logger.info(f"Reminder {reminder_id} was deleted. Halting recurrence.")
            return

        # If it's a recurring reminder, calculate and schedule the next occurrence.
        if reminder_data.get('is_recurring') and reminder_data.get('recurrence_rule'):
            self.logger.info(f"Reminder {reminder_id} is recurring. Calculating next occurrence.")
            try:
                # Get user's timezone to correctly calculate the next occurrence.
                user_tz_str = await self._get_user_timezone(reminder_data['user_id'])
                user_tz = pytz.timezone(user_tz_str)
                
                # --- FIX for unstable timing ---
                # Anchor the recurrence rule to the original creation time.
                # This provides a stable starting point for calculating all future occurrences.
                start_date = datetime.fromtimestamp(reminder_data['created_at'], tz=user_tz)
                rule = rrulestr(reminder_data['recurrence_rule'], dtstart=start_date)
                
                # Find the next occurrence *after* the one that just fired.
                # Using the stable `start_date` prevents timing drift.
                now_aware = datetime.now(user_tz)
                next_occurrence = rule.after(now_aware)

                if next_occurrence:
                    # Update the database with the new time for the next reminder.
                    next_timestamp = int(next_occurrence.timestamp())
                    await self.db_manager.update_reminder_time(reminder_id, next_timestamp)
                    
                    # Create a new asyncio task for the next occurrence.
                    next_reminder = reminder_data.copy()
                    next_reminder['reminder_time'] = next_timestamp
                    self._schedule_reminder_task(next_reminder)
                    self.logger.info(f"Rescheduled reminder {reminder_id} for {next_occurrence.isoformat()}.")
                else:
                    # If there are no more occurrences, delete the reminder.
                    self.logger.info(f"Recurring reminder {reminder_id} has no more occurrences. Deleting.")
                    await self.db_manager.delete_reminders([reminder_id])
            except Exception as e:
                self.logger.error(f"Failed to reschedule recurring reminder {reminder_id}: {e}", exc_info=True)
                # If rescheduling fails, delete the reminder to prevent error loops.
                await self.db_manager.delete_reminders([reminder_id]) # Delete if rescheduling fails
        else:
            # If it's not recurring, simply delete it from the database.
            await self.db_manager.delete_reminders([reminder_id])
            self.logger.info(f"Cleaned up non-recurring reminder {reminder_id} from database.")

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone, defaulting to UTC."""
        tz = await self.db_manager.get_user_timezone(user_id)
        return tz or "UTC"

    def _format_recurrence_rule(self, rule_str: str) -> str:
        """Formats an rrule string into a human-readable format."""
        if not rule_str:
            return ""

        try:
            from dateutil.rrule import rruleset
            rule = rrulestr(rule_str, ignoretz=True)

            # rrulestr can return an rrule or rruleset. We'll inspect the first rrule for display.
            if isinstance(rule, rruleset):
                # For a ruleset, we get the underlying rule. This is a simplification.
                rrule_list = getattr(rule, '_rrule', [])
                if not rrule_list:
                    return f"Repeats: {rule_str}" # Cannot parse further
                rule = rrule_list[0]

            # Use getattr to safely access internal attributes that Pylance warns about.
            freq_val = getattr(rule, '_freq', None)
            interval_val = getattr(rule, '_interval', 1)
            byweekday_val = getattr(rule, '_byweekday', None)

            if freq_val is None:
                 return f"Repeats: {rule_str}"

            freq_map = {YEARLY: "year", MONTHLY: "month", WEEKLY: "week", DAILY: "day", HOURLY: "hour", MINUTELY: "minute"}
            freq = freq_map.get(freq_val, "time")
            
            period = ""
            # Handle simple cases
            if interval_val == 1:
                period = f"every {freq}"
            else:
                period = f"every {interval_val} {freq}s"

            # Handle specific days of the week
            if byweekday_val:
                day_map = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday', 5: 'Saturday', 6: 'Sunday'}
                days = [day_map[d] for d in byweekday_val]
                if sorted(days) == ['Friday', 'Monday', 'Thursday', 'Tuesday', 'Wednesday']:
                    period = "every weekday"
                else:
                    period = f"every {', '.join(days)}"

            if freq == "day" and interval_val == 1: return "Repeats every day"
            
            return f"Repeats {period}"

        except Exception as e:
            self.logger.error(f"Failed to parse rrule string '{rule_str}': {e}")
            return f"Repeats: {rule_str}" # Fallback to raw rule

    async def _parse_reminder(self, query: str) -> tuple[str | None, str, str | None] | None:
        """
        Parses a query to separate the reminder message from the time string using a multi-stage approach.
        Returns a tuple of (message, time_string, recurrence_rule).
        """
        # --- Stage 1: Initial Sanitization ---
        # The NLP command registry has already matched one of the trigger words (e.g., "remind", "reminder").
        # This first pass removes that trigger phrase from the beginning of the query.
        trigger_patterns = [
            r'\bremind\b',
            r'\breminder\b',
            r'\bremember\b',
            r'set\s+a\s+reminder',
            r'set\s.*reminder'
        ]
        # Combine patterns into a single regex to find the first match at the start of the string.
        # This ensures we only strip the part that triggered the command.
        combined_pattern = r'^\s*(' + '|'.join(f'({p})' for p in trigger_patterns) + r')\s*'
        
        sanitized_query = re.sub(combined_pattern, '', query, count=1, flags=re.IGNORECASE).strip()
        
        # Further cleanup: if "me to", "me", or "to" are at the start, remove them as they are conversational padding.
        # We try to strip "me to" first, then fall back to stripping "me" or "to".
        if sanitized_query.lower().startswith('me to '):
            sanitized_query = sanitized_query[6:].lstrip()
        elif sanitized_query.lower().startswith('me '):
            sanitized_query = sanitized_query[3:].lstrip()
        elif sanitized_query.lower().startswith('to '):
            sanitized_query = sanitized_query[3:].lstrip()

        if not sanitized_query:
            return None

        # --- Stage 2: Detect and Extract Recurrence ---
        # Look for patterns like "every day", "every 2 weeks", "every monday" to create an RRULE string.
        recurrence_rule = None
        recurrence_match = re.search(
            r'\b(every\s+(?:(?P<interval>\d+)\s+)?(?P<freq>second|minute|hour|day|week|month|year)s?|every\s+(?P<weekday>weekday|(?P<day_name>sunday|monday|tuesday|wednesday|thursday|friday|saturday))|every\s+(?P<month_day>\d{1,2})(?:st|nd|rd|th)\s+of\s+the\s+month)\b',
            sanitized_query,
            re.IGNORECASE
        )
        if recurrence_match:
            groups = recurrence_match.groupdict()
            interval = int(groups.get('interval') or 1)
            
            if groups.get('weekday') == 'weekday':
                recurrence_rule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
            elif groups.get('day_name'):
                day_map = {'sunday': 'SU', 'monday': 'MO', 'tuesday': 'TU', 'wednesday': 'WE', 'thursday': 'TH', 'friday': 'FR', 'saturday': 'SA'}
                day = day_map[groups['day_name'].lower()]
                recurrence_rule = f"FREQ=WEEKLY;BYDAY={day};INTERVAL={interval}"
            elif groups.get('month_day'):
                day_of_month = int(groups['month_day'])
                recurrence_rule = f"FREQ=MONTHLY;BYMONTHDAY={day_of_month}"
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
                # Remove the recurrence part from the query to not confuse dateparser for the first occurrence.
                sanitized_query = sanitized_query.replace(recurrence_match.group(0), '', 1).strip()
                # If a recurrence rule is found, the remaining text is the message, and the time is 'now'.
                if sanitized_query:
                    self.logger.info(f"Parsed with recurrence. Message: '{sanitized_query}', Time: 'now', Recurrence: {recurrence_rule}")
                    return (sanitized_query, "now", recurrence_rule)

        # --- Stage 3: Intelligent Split with Keywords ---
        # Try to split the message and time string based on common keywords like "in", "at", "on".
        # We use rpartition to split on the *last* occurrence, which is more likely to be the time.
        time_keywords = [' on ', ' at ', ' in ', ' for ', ' next ', ' tomorrow', ' tonight']
        for keyword in time_keywords:
            if keyword in sanitized_query:
                message_part, sep, time_part = sanitized_query.rpartition(keyword)
                time_string = sep.strip() + ' ' + time_part.strip()
                
                # Validate that the extracted part is a parsable date to avoid false positives.
                # This is run in a thread to prevent blocking the event loop.
                if await asyncio.to_thread(dateparser.parse, time_string, settings={'PREFER_DATES_FROM': 'future'}):
                    self.logger.info(f"Parsed via keyword split. Message: '{message_part}', Time: '{time_string}', Recurrence: {recurrence_rule}")
                    return (message_part.strip(), time_string, recurrence_rule)

        # --- Stage 4: Time at the Front ---
        # Check if the beginning of the query is a time string (e.g., "in 5 minutes do the laundry").
        words = sanitized_query.split()
        for i in range(len(words), 0, -1):
            potential_time = ' '.join(words[:i])
            # Add a guard against short, non-numeric strings being parsed as time.
            if len(potential_time) <= 2 and not any(char.isdigit() for char in potential_time):
                continue
            if await asyncio.to_thread(dateparser.parse, potential_time, settings={'PREFER_DATES_FROM': 'future'}):
                message_part = ' '.join(words[i:])
                self.logger.info(f"Parsed with time at front. Message: '{message_part}', Time: '{potential_time}', Recurrence: {recurrence_rule}")
                return (message_part.strip() or None, potential_time, recurrence_rule)

        # --- Stage 5: Time at the Back ---
        # Check if the end of the query is a time string (e.g., "do the laundry in 5 minutes").
        for i in range(len(words)):
            potential_time = ' '.join(words[i:])
            # Add a guard against short, non-numeric strings being parsed as time.
            if len(potential_time) <= 2 and not any(char.isdigit() for char in potential_time):
                continue
            if await asyncio.to_thread(dateparser.parse, potential_time, settings={'PREFER_DATES_FROM': 'future'}):
                message_part = ' '.join(words[:i])
                self.logger.info(f"Parsed with time at back. Message: '{message_part}', Time: '{potential_time}', Recurrence: {recurrence_rule}")
                return (message_part.strip() or None, potential_time, recurrence_rule)

        # --- Stage 6: Fallback ---
        # If no time is found anywhere, assume the whole query is the message.
        # This will trigger the interactive flow later where the bot asks for the time.
        self.logger.warning(f"Could not find a time string in '{sanitized_query}'. Assuming it's all a message.")
        return (sanitized_query, "", recurrence_rule)

    async def _interactive_reminder_flow(self, ctx: 'commands.Context', initial_message: str = "", initial_time: str = "", initial_recurrence: Optional[str] = None) -> None:
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
                        r'\b(every\s+(?:(?P<interval>\d+)\s+)?(?P<freq>second|minute|hour|day|week|month|year)s?|every\s+(?P<weekday>weekday|(?P<day_name>sunday|monday|tuesday|wednesday|thursday|friday|saturday))|every\s+(?P<month_day>\d{1,2})(?:st|nd|rd|th)\s+of\s+the\s+month)\b',
                        time_str, re.IGNORECASE
                    )
                    if recurrence_match:
                        groups = recurrence_match.groupdict()
                        interval = int(groups.get('interval') or 1)
                        
                        if groups.get('weekday') == 'weekday':
                            recurrence_rule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
                        elif groups.get('day_name'):
                            day_map = {'sunday': 'SU', 'monday': 'MO', 'tuesday': 'TU', 'wednesday': 'WE', 'thursday': 'TH', 'friday': 'FR', 'saturday': 'SA'}
                            day = day_map[groups['day_name'].lower()]
                            recurrence_rule = f"FREQ=WEEKLY;BYDAY={day};INTERVAL={interval}"
                        elif groups.get('month_day'):
                            day_of_month = int(groups['month_day'])
                            recurrence_rule = f"FREQ=MONTHLY;BYMONTHDAY={day_of_month}"
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
                            self.logger.info(f"Detected recurrence rule in interactive flow: {recurrence_rule}")
                            # Strip the recurrence part to help dateparser
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
                new_reminder_id = await self.db_manager.add_reminder(
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

    async def remind(self, ctx: 'commands.Context', *, query: str) -> None:
        """The NLP handler for all reminder requests."""
        try:
            if not query.strip():
                await self._interactive_reminder_flow(ctx)
                return

            parsed = await self._parse_reminder(query)
            
            # If parsing fails to find a message or a time, start the interactive flow from scratch.
            if not parsed or not parsed[0] or not parsed[1]:
                self.logger.info(f"Failed to understand '{query}'. Starting interactive flow.")
                await ctx.send("I'm sorry, I couldn't understand the reminder. Let's set it up step-by-step.")
                await self._interactive_reminder_flow(ctx) # No context retained
                return

            reminder_message, time_str, recurrence_rule = parsed

            user_tz_str = await self._get_user_timezone(ctx.author.id)
            user_tz = pytz.timezone(user_tz_str)
            date_settings = {
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': user_tz_str,
                'RETURN_AS_TIMEZONE_AWARE': True
            }

            dt_object = None
            # If it's a recurring reminder starting 'now', calculate the first actual occurrence.
            if recurrence_rule and time_str == "now":
                now = datetime.now(user_tz)
                rule = rrulestr(recurrence_rule, dtstart=now)
                dt_object = rule.after(now)
            else:
                # Otherwise, parse the time string as usual.
                dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))
            
            # This check is a safeguard, but _parse_reminder should have validated the time string.
            if not dt_object:
                self.logger.error(f"Dateparser failed on a validated string: '{time_str}'. Starting interactive flow.")
                await ctx.send("I'm sorry, I got confused about the time. Let's set it up step-by-step.")
                await self._interactive_reminder_flow(ctx) # No context retained
                return

            timestamp = int(dt_object.timestamp())
            # Prevent setting reminders in the past.
            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past! Please try again.")
                # We retain context here because the user's intent was clear, just the time was wrong.
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_recurrence=recurrence_rule)
                return

            # --- Confirmation Step ---
            # Ask the user to confirm the parsed details before saving.
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
                    new_reminder_id = await self.db_manager.add_reminder(
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
                    # This case should ideally not be hit if parsing is correct.
                    await ctx.send("I seem to have lost the reminder message. Please try again.")
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
            reminders = await self.db_manager.get_user_reminders(ctx.author.id)

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
                line = (
                    f"**#{i}** - \"{reminder['message']}\"\n"
                    f"Due: <t:{reminder['reminder_time']}:F>"
                )
                if reminder.get('is_recurring') and reminder.get('recurrence_rule'):
                    rule_text = self._format_recurrence_rule(reminder['recurrence_rule'])
                    line += f"\n*{rule_text}*"
                description_lines.append(line)
            
            embed.description = "\n\n".join(description_lines)
            await ctx.send(embed=embed)
        except Exception as e:
            self.logger.error(f"Error checking reminders for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An error occurred while fetching your reminders.")

    async def delete_reminders_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for deleting reminders."""
        self.logger.info(f"Handling NLP request for deleting reminders from user {ctx.author.id}: '{query}'")
        
        # Find all numbers in the query string to allow for deleting multiple reminders at once.
        numbers_found = re.findall(r'\d+', query)
        
        if not numbers_found:
            await ctx.send("I see you want to delete a reminder, but you didn't specify which one. Please provide the reminder number (e.g., 'delete reminder 1').")
            return
            
        try:
            # 1. Get the user's current reminders to map the user-facing index to the db ID
            user_reminders = await self.db_manager.get_user_reminders(ctx.author.id)
            
            if not user_reminders:
                await ctx.send("You have no reminders to delete.")
                return

            # Create a mapping from user-facing index (#1, #2, etc.) to the actual database ID.
            index_to_id_map = {i + 1: r['id'] for i, r in enumerate(user_reminders)}

            ids_to_delete = set()
            invalid_numbers = []
            valid_numbers_deleted = []

            input_numbers = [int(num) for num in numbers_found]

            # --- Deletion Logic ---
            # Only allow deletion by the user-facing index.
            for num in input_numbers:
                db_id = index_to_id_map.get(num)
                if db_id:
                    ids_to_delete.add(db_id)
                    valid_numbers_deleted.append(f"#{num}")
                else:
                    invalid_numbers.append(str(num))

            if not ids_to_delete:
                await ctx.send(f"No valid reminder numbers provided. I couldn't find reminders for: {', '.join(invalid_numbers)}.")
                return

            # Cancel the asyncio tasks for all reminders being deleted.
            for db_id in ids_to_delete:
                if db_id in self.scheduled_tasks:
                    self.scheduled_tasks[db_id].cancel()
                    self.scheduled_tasks.pop(db_id, None)
                    self.logger.info(f"Cancelled and removed scheduled task for deleted reminder {db_id}.")

            # This is the crucial step: delete from the database so it doesn't recur on restart.
            await self.db_manager.delete_reminders(list(ids_to_delete))

            deleted_count = len(ids_to_delete)
            response_parts = [f"Successfully deleted {deleted_count} reminder(s): `{', '.join(sorted(valid_numbers_deleted))}`"]
            
            if invalid_numbers:
                response_parts.append(f"Could not find reminders for these numbers: `{', '.join(invalid_numbers)}`.")

            await ctx.send("\n".join(response_parts))
            self.logger.info(f"User {ctx.author.id} deleted {deleted_count} reminders. IDs: {list(ids_to_delete)}")

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
        display_tz_str = ""

        if tz_to_check in TIMEZONE_ABBREVIATIONS:
            final_tz_str = TIMEZONE_ABBREVIATIONS[tz_to_check]
            display_tz_str = final_tz_str # Use the full name for display
        
        if not final_tz_str:
            match = re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', tz_to_check)
            if match:
                sign = match.group(2)
                hour = int(match.group(3))
                # pytz uses Etc/GMT where the sign is inverted
                final_tz_str = f"Etc/GMT{-hour if sign == '+' else +hour}"
                display_tz_str = final_tz_str # Use the full name for display

        if not final_tz_str:
            final_tz_str = timezone_str
            display_tz_str = timezone_str

        try:
            tz = pytz.timezone(final_tz_str)
            
            # Use the resolved, full timezone name for storage.
            zone_to_store = tz.zone
            
            if not zone_to_store:
                self.logger.error(f"Could not resolve a storable timezone name from '{final_tz_str}'.")
                await ctx.send("I couldn't resolve that to a valid timezone name. Please try a different format.")
                return

            await self.db_manager.set_user_timezone(ctx.author.id, zone_to_store)
            
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
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
import config

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
            # Remove the task from the tracking dictionary to prevent memory leaks.
            # We check if the task in the dictionary is THIS task before removing it.
            # This prevents removing a newly scheduled task if this one was cancelled/replaced.
            if self.scheduled_tasks.get(reminder['id']) == task:
                self.scheduled_tasks.pop(reminder['id'], None)

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
            
            # Check user preference for reminder destination
            destination_pref = await self.db_manager.get_user_config(reminder['user_id'], 'reminder_destination')
            
            targetable = None
            
            if destination_pref == 'dm':
                targetable = user
            elif destination_pref and destination_pref.isdigit():
                # Specific channel preference
                try:
                    chan_id = int(destination_pref)
                    targetable = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
                except (discord.NotFound, discord.Forbidden):
                    self.logger.warning(f"Preferred channel {destination_pref} not found/accessible. Falling back to DM.")
                    targetable = user
            else:
                # Default to origin channel ('origin', 'channel', or None)
                try:
                    targetable = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])
                except (discord.NotFound, discord.Forbidden):
                    self.logger.warning(f"Original channel {reminder['channel_id']} not found/accessible. Falling back to DM.")
                    targetable = user

            if targetable and hasattr(targetable, 'send'):
                overdue_message = ""
                # If the reminder was overdue, add a note indicating how long ago it was due.
                if delay <= 0:
                    overdue_seconds = time.time() - reminder['reminder_time']
                    overdue_message = f" (This was due {self._format_overdue_time(overdue_seconds)})"

                # Cast to Messageable to satisfy static analysis
                targetable_dest = cast(discord.abc.Messageable, targetable)
                await targetable_dest.send(f"{user.mention}, you asked me to remind you: '{reminder['message']}'{overdue_message}")
                self.logger.info(f"Sent reminder {reminder['id']} to user {user.id} via {destination_pref or 'channel'}.")
            else:
                 self.logger.error(f"Could not find a valid destination for reminder {reminder['id']}.")

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
        # The task is now complete, cancelled, or has failed. The done callback will handle
        # cleanup of the database entry and the scheduled_tasks dictionary.

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
        """
        Fetches a user's timezone string and converts it to a pytz-compatible format 
        if it's a GMT/UTC offset. Defaults to UTC.
        """
        tz_str = await self.db_manager.get_user_timezone(user_id) or "UTC"

        # Check if it's a GMT/UTC offset that needs conversion for pytz
        match = re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', tz_str.lower())
        if match:
            sign = match.group(2)
            hour = int(match.group(3))
            # Invert the sign for the Etc/GMT format required by pytz
            return f"Etc/GMT{-hour if sign == '+' else +hour}"

        # For standard IANA names (e.g., 'US/Eastern') or 'UTC', return as is.
        return tz_str

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

    def _extract_recurrence_rule(self, text: str) -> tuple[Optional[str], str]:
        """
        Extracts a recurrence rule from the given text.
        Returns a tuple of (recurrence_rule_string, matched_text).
        """
        recurrence_rule = None
        matched_text = ""

        # Pattern A: Simple frequencies like "Daily", "Weekly", "Bi-weekly"
        simple_freq_match = re.search(r'\b(daily|weekly|bi-?weekly|monthly|yearly)\b', text, re.IGNORECASE)
        if simple_freq_match:
            matched_text = simple_freq_match.group(0)
            freq_map = {
                'daily': 'DAILY', 'weekly': 'WEEKLY', 'monthly': 'MONTHLY', 
                'yearly': 'YEARLY', 'bi-weekly': 'WEEKLY;INTERVAL=2'
            }
            clean_freq = simple_freq_match.group(1).lower().replace('-', '')
            recurrence_rule = f"FREQ={freq_map.get(clean_freq, 'DAILY')}"
            return recurrence_rule, matched_text

        # Pattern B: "Every Xth of the month"
        month_day_match = re.search(r'\bevery\s+(?P<month_day>\d{1,2})(?:st|nd|rd|th)\s+of\s+the\s+month\b', text, re.IGNORECASE)
        if month_day_match:
            matched_text = month_day_match.group(0)
            day_of_month = int(month_day_match.group('month_day'))
            recurrence_rule = f"FREQ=MONTHLY;BYMONTHDAY={day_of_month}"
            return recurrence_rule, matched_text

        # Pattern C: Complex phrases like "Every 2 days", "Every other Monday", "Every weekend"
        complex_freq_match = re.search(
            r'\bevery\s+(?:(?P<other>other)\s+)?(?:(?P<interval>\d+)\s+)?(?P<unit>second|minute|hour|day|week|month|year|weekend|weekday|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b',
            text, re.IGNORECASE
        )

        if complex_freq_match:
            matched_text = complex_freq_match.group(0)
            groups = complex_freq_match.groupdict()
            
            interval = 1
            if groups.get('interval'):
                interval = int(groups['interval'])
            if groups.get('other'):
                interval *= 2
            
            unit = groups['unit'].lower()
            
            day_map = {
                'sun': 'SU', 'sunday': 'SU', 'mon': 'MO', 'monday': 'MO',
                'tue': 'TU', 'tuesday': 'TU', 'wed': 'WE', 'wednesday': 'WE',
                'thu': 'TH', 'thursday': 'TH', 'fri': 'FR', 'friday': 'FR',
                'sat': 'SA', 'saturday': 'SA'
            }

            if unit in ['day', 'days']:
                recurrence_rule = f"FREQ=DAILY;INTERVAL={interval}"
            elif unit in ['week', 'weeks']:
                recurrence_rule = f"FREQ=WEEKLY;INTERVAL={interval}"
            elif unit in ['month', 'months']:
                recurrence_rule = f"FREQ=MONTHLY;INTERVAL={interval}"
            elif unit in ['year', 'years']:
                recurrence_rule = f"FREQ=YEARLY;INTERVAL={interval}"
            elif unit == 'weekday':
                recurrence_rule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
            elif unit == 'weekend':
                recurrence_rule = "FREQ=WEEKLY;BYDAY=SA,SU"
            elif unit in day_map:
                recurrence_rule = f"FREQ=WEEKLY;BYDAY={day_map[unit]};INTERVAL={interval}"
            
            return recurrence_rule, matched_text

        return None, ""

    async def _parse_reminder(self, query: str) -> tuple[str | None, str, str | None] | None:
        """
        Parses a query to separate the reminder message from the time string.
        Robustly handles recurrence and split-time entities (e.g. "On Dec 21 ... at 5am").
        """
        # ==========================================
        # Stage 1: Initial Sanitization & Triggers
        # ==========================================
        # We start by cleaning up the input. Users often speak to the bot conversationally,
        # saying things like "remind me to..." or "set a reminder for...".
        # We want to strip these trigger phrases so we can focus on the actual content.
        trigger_patterns = [
            r'\bremind\b', r'\breminder\b', r'\bremember\b',
            r'set\s+a\s+reminder', r'set\s.*reminder'
        ]
        combined_pattern = r'^\s*(' + '|'.join(f'({p})' for p in trigger_patterns) + r')\s*'
        sanitized_query = re.sub(combined_pattern, '', query, count=1, flags=re.IGNORECASE).strip()
        
        # Further cleanup of conversational fillers.
        # We look at the first few words to remove things like "me to", "us to", "him", etc.
        words = sanitized_query.split()
        if len(words) > 0:
            if words[0].lower() in ["me", "us", "him", "her", "them"]:
                words.pop(0)
            # Check again after popping, as we might have "me to" -> pop "me" -> now "to" is first.
            if words and words[0].lower() in ["to", "for", "that", "about"]:
                words.pop(0)
            sanitized_query = " ".join(words)

        if not sanitized_query:
            return None

        # ==========================================
        # Stage 2: Recurrence Extraction
        # ==========================================
        # We prioritize extracting recurrence rules (e.g., "every day") because they fundamentally
        # change how the reminder behaves. We use regex to find these patterns.
        recurrence_rule = None
        
        # Extract recurrence rule using the new helper method
        recurrence_rule, matched_recurrence_text = self._extract_recurrence_rule(sanitized_query)

        if recurrence_rule:
            self.logger.info(f"Detected recurrence: {recurrence_rule}")
            sanitized_query = sanitized_query.replace(matched_recurrence_text, '', 1)
            # Clean up any double spaces left behind by the removal
            sanitized_query = re.sub(r'\s+', ' ', sanitized_query).strip()

        # ==========================================
        # Stage 3 & 4: Split-Head/Tail Time Extraction
        # ==========================================
        # Natural language is messy. The time at the start ("Tomorrow go to the store"
        # or at the end ("Go to the store tomorrow"). Sometimes they split it ("On Friday go to the store at 5pm").
        # Instead attempt to "eat" valid time phrases from both ends of the sentence.
        
        words = sanitized_query.split()
        
        # --- Helper to consume words and check validity ---
        async def get_longest_valid_date_segment(candidate_words: list[str], direction: str) -> tuple[str, int]:
            """
            Tries to form a valid date string by incrementally adding words from the list.
            Returns the longest string that dateparser accepts as a valid date, 
            and the number of words consumed.
            """
            valid_segment = ""
            valid_count = 0
            
            # We try building phrases: "On", "On Dec", "On Dec 21"... 
            for i in range(1, len(candidate_words) + 1):
                phrase = " ".join(candidate_words[:i])
                
                # Optimization: Don't ask dateparser about obviously non-date single words
                # unless they are digits. This saves processing time.
                if i == 1 and (len(phrase) < 3 and not phrase[0].isdigit()):
                    if config.DEV_MODE:
                        self.logger.debug(f"[{direction}] Skipping short single word: '{phrase}'")
                    continue
                
                # Optimization: Stop if we hit a pure stop-word that rarely starts/ends a date
                # but is common in messages. This prevents "at 5pm to" where "to" is part of the message.
                word_to_check = candidate_words[i-1].lower()
                if i > 1 and word_to_check in ['to', 'that', 'my', 'the']:
                    # Special handling for "the": allow it if it looks like part of a date phrase
                    # e.g. "on the 25th", "on the next Friday"
                    should_stop = True
                    if word_to_check == 'the' and i < len(candidate_words):
                        next_word = candidate_words[i].lower()
                        # Check if next word is a digit (25th) or a relative keyword or day/month
                        if (next_word[0].isdigit() or 
                            next_word in ['next', 'last', 'following', 'first', 'second', 'third', 'fourth', 'fifth'] or
                            next_word in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
                                          'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun',
                                          'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
                                          'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']):
                            should_stop = False
                        
                    if should_stop:
                        if config.DEV_MODE:
                            self.logger.debug(f"[{direction}] Stopping at stop-word '{candidate_words[i-1]}' in phrase: '{phrase}'")
                        break

                # --- Pre-processing ---
                # We manually strip common prepositions from the start of the phrase.
                # This helps avoid false positives or confusion, and allows us to use
                # STRICT_PARSING: False safely.
                clean_phrase = phrase
                words_in_phrase = phrase.split()
                if words_in_phrase and words_in_phrase[0].lower() in ['on', 'at', 'in', 'for', 'by', 'from']:
                    # Remove the first word (the preposition)
                    clean_phrase = " ".join(words_in_phrase[1:])
                
                # If the cleaned phrase is empty or too short (and not a digit), skip it.
                # This prevents "on" -> "" -> Now, or "on a" -> "a" -> ?
                if not clean_phrase or (len(clean_phrase) < 3 and not clean_phrase[0].isdigit()):
                    continue

                # Prepare the phrase for checking.
                # If we are checking backwards, the phrase is reversed (e.g. "5pm at").
                # We must un-reverse it so dateparser sees natural order (e.g. "at 5pm").
                check_phrase = clean_phrase
                if direction == "BACK":
                    check_phrase = " ".join(clean_phrase.split()[::-1])

                # Check validity using dateparser with STRICT_PARSING: False.
                # We use False because True is too strict (fails on normal dates such as "December 21st").
                # We rely on our incremental build and stop-words to avoid over-consuming.
                is_valid = await asyncio.to_thread(
                    dateparser.parse, 
                    check_phrase, 
                    settings={'PREFER_DATES_FROM': 'future', 'STRICT_PARSING': False}
                )
                
                if config.DEV_MODE:
                    self.logger.debug(f"[{direction}] Checking phrase: '{phrase}' (check: '{check_phrase}') -> Valid: {bool(is_valid)}")

                if is_valid:
                    valid_segment = phrase
                    valid_count = i
            
            return valid_segment, valid_count

        # 1. Try consuming time info from the FRONT of the sentence
        if config.DEV_MODE:
            self.logger.debug(f"Starting Front Time Extraction with words: {words}")
        front_time_str, front_word_count = await get_longest_valid_date_segment(words, "FRONT")
        
        # 2. Try consuming time info from the BACK of the sentence
        # We only look at the back if we haven't already consumed the whole string from the front.
        back_time_str = ""
        back_word_count = 0
        
        remaining_words_at_back = len(words) - front_word_count
        if remaining_words_at_back > 0:
            if config.DEV_MODE:
                self.logger.debug(f"Starting Back Time Extraction. Remaining words: {remaining_words_at_back}")
            
            # Prepare words for back extraction (reverse them)
            # We take the words that were NOT consumed by the front extraction
            words_for_back = words[front_word_count:]
            reversed_words = words_for_back[::-1]
            
            # Use the helper to find the longest valid segment from the back
            back_segment, back_count = await get_longest_valid_date_segment(reversed_words, "BACK")
            
            if back_segment:
                # The segment returned is reversed (e.g. "am 5 at"). We need to un-reverse it.
                back_time_str = " ".join(back_segment.split()[::-1])
                back_word_count = back_count
                if config.DEV_MODE:
                    self.logger.debug(f"Found back time: '{back_time_str}' ({back_word_count} words)")
        
        # ==========================================
        # Stage 5: Synthesis and Validation
        # ==========================================
        # Now we decide what the final time string and message are based on what we found.
        
        final_time_string = ""
        message_words = words # Default to assuming everything is the message if no time found

        # Case A: Split Time ("On Monday" ... "at 5pm")
        # We found valid time parts at BOTH ends. We try to combine them.
        if front_time_str and back_time_str:
            self.logger.info(f"Found split time: '{front_time_str}' AND '{back_time_str}'")
            combined_candidate = f"{front_time_str} {back_time_str}"
            
            # Validate that the combined string makes sense
            if await asyncio.to_thread(dateparser.parse, combined_candidate, settings={'PREFER_DATES_FROM': 'future'}):
                final_time_string = combined_candidate
                # The message is whatever is left in the middle
                message_words = words[front_word_count : len(words) - back_word_count]
            else:
                # If they don't combine validly, we have to pick one. 
                # We default to the front one as a heuristic.
                final_time_string = front_time_str
                message_words = words[front_word_count:]

        # Case B: Front only ("Tomorrow go to store")
        elif front_time_str:
            self.logger.info(f"Found time at front: '{front_time_str}'")
            final_time_string = front_time_str
            message_words = words[front_word_count:]

        # Case C: Back only ("Go to store tomorrow")
        elif back_time_str:
            self.logger.info(f"Found time at back: '{back_time_str}'")
            final_time_string = back_time_str
            message_words = words[:len(words) - back_word_count]
            
        # ==========================================
        # Stage 6: Final Cleanup
        # ==========================================
        
        # If we found a recurrence rule but NO specific time (e.g. "Every day"),
        # we set the time to "now" so the recurrence starts immediately.
        # (Again this differs from the previous implementation but is better)
        if not final_time_string and recurrence_rule:
            final_time_string = "now"

        # If we still have no time string, we failed to parse a reminder.
        if not final_time_string:
            self.logger.warning(f"No time found in: '{query}'")
            return (sanitized_query, "", None)

        # Reassemble the message from the remaining words
        final_message = " ".join(message_words)
        
        # Clean junk words from message boundaries (e.g. "to", "that")
        junk_words = ['to', 'that', 'for', 'and', 'then', 'now', 'a', 'the', 'my']
        
        # Clean Start
        msg_words = final_message.split()
        while msg_words and msg_words[0].lower() in junk_words:
            msg_words.pop(0)
        # Clean End
        while msg_words and msg_words[-1].lower() in junk_words:
            msg_words.pop()
            
        final_message = " ".join(msg_words)

        return (final_message, final_time_string, recurrence_rule)
        # Pray that this works, because no god can ever fix this if it doesn't.

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
                    recurrence_rule, matched_text = self._extract_recurrence_rule(time_str)
                    
                    if recurrence_rule:
                        self.logger.info(f"Detected recurrence rule in interactive flow: {recurrence_rule}")
                        # Strip the recurrence part to help dateparser
                        time_str = time_str.replace(matched_text, '', 1).strip()

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
                
                # If we have a recurrence rule, ensure the first occurrence aligns with it.
                # e.g. "Every Friday at 7am" (parsed as Sunday 7am) -> Should be next Friday 7am.
                if dt_object and recurrence_rule:
                    try:
                        rule = rrulestr(recurrence_rule, dtstart=dt_object)
                        # Get the first occurrence that matches the rule, starting from dt_object.
                        # inc=True means if dt_object itself matches, use it.
                        next_occurrence = rule.after(dt_object, inc=True)
                        if next_occurrence:
                            dt_object = next_occurrence
                            self.logger.info(f"Aligned initial reminder time to recurrence rule: {dt_object}")
                    except Exception as e:
                        self.logger.warning(f"Failed to align time with recurrence rule: {e}")
            
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
            # North America
            "est": "America/New_York",    # Eastern Standard Time
            "edt": "America/New_York",    # Eastern Daylight Time
            "cst": "America/Chicago",     # Central Standard Time
            "cdt": "America/Chicago",     # Central Daylight Time
            "mst": "America/Denver",      # Mountain Standard Time
            "mdt": "America/Denver",      # Mountain Daylight Time
            "pst": "America/Los_Angeles", # Pacific Standard Time
            "pdt": "America/Los_Angeles", # Pacific Daylight Time
            "akst": "America/Anchorage",  # Alaska Standard Time
            "akdt": "America/Anchorage",  # Alaska Daylight Time
            "hst": "Pacific/Honolulu",    # Hawaii Standard Time

            # Europe
            "gmt": "Europe/London",       # Greenwich Mean Time
            "bst": "Europe/London",       # British Summer Time
            "wet": "WET",                 # Western European Time
            "west": "WET",                # Western European Summer Time
            "cet": "CET",                 # Central European Time
            "cest": "CET",                # Central European Summer Time
            "eet": "EET",                 # Eastern European Time
            "eest": "EET",                # Eastern European Summer Time
            "msk": "Europe/Moscow",       # Moscow Standard Time

            # Asia
            "ist": "Asia/Kolkata",        # Indian Standard Time
            "jst": "Asia/Tokyo",          # Japan Standard Time
            "kst": "Asia/Seoul",          # Korea Standard Time
            "sgt": "Asia/Singapore",      # Singapore Time

            # Australia
            "aest": "Australia/Sydney",   # Australian Eastern Standard Time
            "aedt": "Australia/Sydney",   # Australian Eastern Daylight Time
            "acst": "Australia/Darwin",   # Australian Central Standard Time
            "acdt": "Australia/Adelaide", # Australian Central Daylight Time
            "awst": "Australia/Perth",    # Australian Western Standard Time
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
                # pytz uses Etc/GMT where the sign is inverted for calculations
                final_tz_str = f"Etc/GMT{-hour if sign == '+' else +hour}"
                # But we want to store and display the user-friendly version
                display_tz_str = f"GMT{sign}{hour}"

        if not final_tz_str:
            final_tz_str = timezone_str
            display_tz_str = timezone_str

        try:
            # Use the calculation-friendly string for validation
            tz = pytz.timezone(final_tz_str)
            
            # Use the display-friendly string for storage
            zone_to_store = display_tz_str or tz.zone
            
            if not zone_to_store:
                self.logger.error(f"Could not resolve a storable timezone name from '{final_tz_str}'.")
                await ctx.send("I couldn't resolve that to a valid timezone name. Please try a different format.")
                return

            await self.db_manager.set_user_timezone(ctx.author.id, zone_to_store)
            
            now = datetime.now(tz)
            
            # Prepare the main confirmation message
            confirmation_message = (
                f"Your timezone has been set to `{zone_to_store}`.\n"
                f"The current time in your timezone is `{now.strftime('%Y-%m-%d %H:%M:%S')}`."
            )

            # If the user set a GMT/UTC offset, add an informational message about IANA timezones
            if re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', tz_to_check):
                iana_recommendation = (
                    "\n\n**Note:** You've set a fixed GMT/UTC offset. For automatic Daylight Saving Time adjustments, "
                    "we recommend using an IANA timezone name instead. Examples include:\n"
                    "- `America/New_York` (for US Eastern Time (P.S yes the underscore is neccessary))\n"
                    "- `Europe/London` (for UK time)\n"
                    "- `Asia/Tokyo` (for Japan Standard Time)"
                )
                confirmation_message += iana_recommendation

            await ctx.send(confirmation_message)
            self.logger.info(f"Timezone for user {ctx.author.id} set from '{timezone_str}' to '{zone_to_store}'.")

        except pytz.UnknownTimeZoneError:
            self.logger.warning(f"Failed to set timezone for user {ctx.author.id}: Unrecognized timezone '{timezone_str}'.")
            await ctx.send(f"`{timezone_str}` is not a recognized timezone. Please use a standard IANA name (e.g., `US/Eastern`, `Europe/London`), a common abbreviation (e.g., `EST`, `BST`), or a GMT/UTC offset (e.g., `GMT+5`).")
        except Exception as e:
            self.logger.error(f"Unexpected error in timezone NLP: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")

    async def edit_reminder_nlp(self, ctx: commands.Context, *, query: str):
        """
        Initiates an interactive conversation to edit an existing reminder.
        The user can choose to edit the reminder's message, time, or recurrence.
        """
        # Find the number in the query (e.g., "edit reminder 3").
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send("Please specify the number of the reminder you want to edit. Use `.sancho check reminders` to see the numbers.")
            return

        try:
            reminder_num_to_edit = int(match.group(0))
        except (ValueError, IndexError):
            await ctx.send("Invalid reminder number provided.")
            return

        user_reminders = await self.db_manager.get_user_reminders(ctx.author.id)

        if not (1 <= reminder_num_to_edit <= len(user_reminders)):
            await ctx.send(f"Invalid number. You only have {len(user_reminders)} reminders.")
            return

        # Map the user-facing number (1-based) to the actual reminder object
        reminder_to_edit = user_reminders[reminder_num_to_edit - 1]

        def check(m: discord.Message):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            await ctx.send(
                f"What would you like to edit for reminder **#{reminder_num_to_edit}** (\"{reminder_to_edit['message']}\")?\n"
                "1. Message\n"
                "2. Time\n"
                "3. Recurrence\n"
                "Please respond with the number of your choice, or say `exit` to cancel."
            )
            choice_msg = await self.bot.wait_for('message', check=check, timeout=30.0)
            choice = choice_msg.content.strip()

            if choice.lower() in ['exit', 'cancel']:
                await ctx.send("Edit cancelled.")
                return

            updates: dict[str, Any] = {}
            should_reschedule = False

            match choice:
                case '1':  # Edit Message
                    await ctx.send("What should the new message be?")
                    msg_response = await self.bot.wait_for('message', check=check, timeout=60.0)
                    new_message = msg_response.content.strip()
                    
                    if new_message.lower() in ['exit', 'cancel']:
                        await ctx.send("Edit cancelled.")
                        return
                    
                    updates['message'] = new_message

                case '2':  # Edit Time
                    await ctx.send("When should the new time be? (e.g., 'in 2 hours', 'tomorrow at 5pm')")
                    time_response = await self.bot.wait_for('message', check=check, timeout=60.0)
                    time_str = time_response.content.strip()
                    
                    if time_str.lower() in ['exit', 'cancel']:
                        await ctx.send("Edit cancelled.")
                        return

                    # Use existing logic to parse time
                    user_tz_str = await self._get_user_timezone(ctx.author.id)
                    date_settings = {
                        'PREFER_DATES_FROM': 'future',
                        'TIMEZONE': user_tz_str,
                        'RETURN_AS_TIMEZONE_AWARE': True
                    }
                    
                    dt_object = await asyncio.to_thread(dateparser.parse, time_str, settings=cast(Any, date_settings))
                    
                    if not dt_object or dt_object.timestamp() <= time.time():
                        await ctx.send("I couldn't understand that time or it's in the past. Edit cancelled.")
                        return

                    updates['reminder_time'] = int(dt_object.timestamp())
                    should_reschedule = True

                case '3':  # Edit Recurrence
                    await ctx.send("What should the recurrence be? (e.g., 'every day', 'weekly', or 'none' to remove)")
                    recurrence_response = await self.bot.wait_for('message', check=check, timeout=60.0)
                    recurrence_str = recurrence_response.content.strip()
                    
                    if recurrence_str.lower() in ['exit', 'cancel']:
                        await ctx.send("Edit cancelled.")
                        return
                    
                    if recurrence_str.lower() == 'none':
                        updates['is_recurring'] = False
                        updates['recurrence_rule'] = None
                    else:
                        # Use the helper method to extract the recurrence rule
                        recurrence_rule, _ = self._extract_recurrence_rule(recurrence_str)
                        
                        if recurrence_rule:
                            updates['is_recurring'] = True
                            updates['recurrence_rule'] = recurrence_rule
                            should_reschedule = True # Recurrence change might affect next run time logic if we were fancy, but definitely needs DB update
                        else:
                            await ctx.send("I couldn't understand that recurrence rule. Edit cancelled.")
                            return

                case _:
                    await ctx.send("Invalid choice. Edit cancelled.")
                    return

            if updates:
                rows_affected = await self.db_manager.update_reminder(reminder_to_edit['id'], ctx.author.id, updates)
                if rows_affected > 0:
                    await ctx.send(f"✅ Successfully updated reminder **#{reminder_num_to_edit}**.")
                    self.logger.info(f"User {ctx.author.id} updated reminder {reminder_to_edit['id']}.")
                    
                    if should_reschedule:
                        # Fetch the updated reminder data to ensure we have the full state
                        updated_reminder = await self.db_manager.get_reminder_by_id(reminder_to_edit['id'])
                        if updated_reminder:
                            self._schedule_reminder_task(updated_reminder)
                else:
                    await ctx.send("Something went wrong. I couldn't update that reminder.")
            else:
                await ctx.send("No changes were made.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Edit cancelled.")
        except Exception as e:
            self.logger.error(f"Error editing reminder for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while editing the reminder.")

    async def reminder_settings_nlp(self, ctx: commands.Context, *, query: str):
        """
        Opens an interactive settings menu for reminders.
        """
        # Fetch current settings
        dest_pref = await self.db_manager.get_user_config(ctx.author.id, 'reminder_destination') or 'origin'
        
        # Format display string
        display_dest = "Origin Channel"
        if dest_pref == 'dm':
            display_dest = "Direct Messages"
        elif dest_pref.isdigit():
            channel = self.bot.get_channel(int(dest_pref))
            # Check if channel has a name attribute (TextChannel, VoiceChannel, etc.)
            if channel and hasattr(channel, 'name'):
                display_dest = f"#{getattr(channel, 'name')}"
            else:
                display_dest = f"Unknown Channel (ID: {dest_pref})"
        elif dest_pref == 'channel': # Handle legacy value
            display_dest = "Origin Channel"
        
        embed = discord.Embed(title="Reminder Settings", color=discord.Color.blue())
        embed.description = (
            f"**1. Destination:** `{display_dest}`\n"
            "(Where reminders are sent)\n\n"
            "Reply with the number of the setting you want to change, or `exit`."
        )
        
        await ctx.send(embed=embed)
        
        def check(m: discord.Message):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            choice = msg.content.strip().lower()
            
            if choice in ['exit', 'cancel']:
                await ctx.send("Settings closed.")
                return
                
            if choice == '1':
                await ctx.send(
                    "Where should I send your reminders?\n"
                    "1. **DMs** (Direct Messages)\n"
                    "2. **Origin** (The channel where you set the reminder)\n"
                    "3. **Specific Channel** (Link a specific channel)\n"
                    "Reply with the number."
                )
                
                sub_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                sub_choice = sub_msg.content.strip()
                
                if sub_choice == '1':
                    await self.db_manager.set_user_config(ctx.author.id, 'reminder_destination', 'dm')
                    await ctx.send("✅ Destination set to **Direct Messages**.")
                elif sub_choice == '2':
                    await self.db_manager.set_user_config(ctx.author.id, 'reminder_destination', 'origin')
                    await ctx.send("✅ Destination set to **Origin Channel**.")
                elif sub_choice == '3':
                    await ctx.send("Please mention the channel you want to use (e.g. `#general`).")
                    chan_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                    
                    # Extract channel ID from mention or raw ID
                    chan_match = re.search(r'<#(\d+)>', chan_msg.content)
                    chan_id = None
                    if chan_match:
                        chan_id = int(chan_match.group(1))
                    elif chan_msg.content.strip().isdigit():
                        chan_id = int(chan_msg.content.strip())
                        
                    if chan_id:
                        # Verify bot can see the channel
                        channel = self.bot.get_channel(chan_id)
                        if channel:
                            await self.db_manager.set_user_config(ctx.author.id, 'reminder_destination', str(chan_id))
                            mention_str = getattr(channel, 'mention', f"#{getattr(channel, 'name', chan_id)}")
                            await ctx.send(f"✅ Destination set to {mention_str}.")
                        else:
                            await ctx.send("I can't find that channel or I don't have access to it.")
                    else:
                        await ctx.send("Invalid channel.")
                else:
                    await ctx.send("Invalid choice.")
            else:
                await ctx.send("Invalid choice.")
                
        except asyncio.TimeoutError:
            await ctx.send("Settings timed out.")

async def setup(bot: SanchoBot, **kwargs) -> None:
    """Standard setup, receiving the database path via kwargs from main.py."""
    await bot.add_cog(Reminders(bot))

"""cogs/reminders.py

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
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

import dateparser
import discord
import pytz
from dateutil.rrule import (DAILY, HOURLY, MINUTELY, MONTHLY, WEEKLY, YEARLY, rrulestr)
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.database import DatabaseManager
from utils.views import get_selection


class Reminders(BaseCog):
    """A cog for setting and checking natural language reminders."""

    def __init__(self, bot: CoreBot):
        """Initializes the Reminders cog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager

        # The single background task that handles all reminders.
        self.scheduler_task: Optional[asyncio.Task[None]] = None
        # Event to wake up the scheduler when a new reminder is added/edited/deleted.
        self.scheduler_event = asyncio.Event()

    async def cog_load(self) -> None:
        """Starts the reminder scheduler when the cog is loaded."""
        self.logger.info("Starting reminder system...")
        # Process missed reminders first (Catch-Up Phase).
        await self._process_missed_reminders()
        # Start the main scheduler loop.
        self.scheduler_task = self.bot.loop.create_task(self._scheduler_loop())

    async def cog_unload(self) -> None:
        """Stops the reminder scheduler when the cog is unloaded."""
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Reminder system stopped.")

    async def _process_missed_reminders(self) -> None:
        """Identifies and handles reminders that were missed while the bot was offline."""
        try:
            now = int(time.time())
            missed = await self.db_manager.get_due_reminders(now)

            if not missed:
                self.logger.info("No missed reminders found.")
                return

            self.logger.info(f"Processing {len(missed)} missed reminders...")

            # We use a semaphore to limit concurrent processing to avoid rate limits.
            sem = asyncio.Semaphore(5)

            async def process_one(reminder: Dict[str, Any]) -> None:
                async with sem:
                    await self._handle_missed_reminder(reminder, now)

            # Create tasks for all missed reminders.
            tasks = [process_one(r) for r in missed]
            await asyncio.gather(*tasks)

            self.logger.info("Finished processing missed reminders.")

        except Exception as e:
            self.logger.error(f"Error during missed reminder processing: {e}", exc_info=True)

    async def _handle_missed_reminder(self, reminder: Dict[str, Any], current_time: int) -> None:
        """Handles a single missed reminder, sending a summary and rescheduling if needed.

        Args:
            reminder (Dict[str, Any]): The reminder data.
            current_time (int): The current timestamp.
        """
        user_id = reminder['user_id']
        message = reminder['message']

        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)

            # Determine destination (same logic as before).
            destination_pref = await self.db_manager.get_user_config(user_id, 'reminder_destination')
            targetable = None

            if destination_pref == 'dm':
                targetable = user
            elif destination_pref and destination_pref.isdigit():
                try:
                    chan_id = int(destination_pref)
                    targetable = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
                except (discord.NotFound, discord.Forbidden):
                    targetable = user
            else:
                try:
                    targetable = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])
                except (discord.NotFound, discord.Forbidden):
                    targetable = user

            if not targetable:
                self.logger.warning(f"Could not find destination for missed reminder {reminder['id']}. Deleting.")
                await self.db_manager.delete_reminders([reminder['id']])
                return

            # Cast to Messageable to satisfy static analysis.
            targetable_dest = cast(discord.abc.Messageable, targetable)

            # Logic for Recurring vs One-off.
            if reminder.get('is_recurring') and reminder.get('recurrence_rule'):
                await self._handle_missed_recurring(reminder, targetable_dest, user, current_time)
            else:
                # One-off: Just say sorry and delete.
                msg = (f"{user.mention}, sorry I was offline! You had a reminder for: '{message}'\n"
                       f"It was due at <t:{reminder['reminder_time']}:F> (<t:{reminder['reminder_time']}:R>).")
                await targetable_dest.send(msg)
                await self.db_manager.delete_reminders([reminder['id']])

        except Exception as e:
            self.logger.error(f"Failed to handle missed reminder {reminder['id']}: {e}")
            # If we fail hard (e.g. user blocked bot), we might want to delete it to stop loops,
            # but for now let's just log it.

    async def _handle_missed_recurring(self, reminder: Dict[str, Any], targetable: discord.abc.Messageable, user: discord.User, current_time: int) -> None:
        """Calculates missed occurrences for a recurring reminder and reschedules it."""
        try:
            user_tz_str = await self._get_user_timezone(user.id)
            user_tz = pytz.timezone(user_tz_str)

            # Anchor to creation time for stability.
            start_date = datetime.fromtimestamp(reminder['created_at'], tz=user_tz)
            rule = rrulestr(reminder['recurrence_rule'], dtstart=start_date)

            # Find all occurrences between the LAST scheduled time and NOW.
            # We use the stored reminder_time as the start of our search window.
            last_scheduled_dt = datetime.fromtimestamp(reminder['reminder_time'], tz=user_tz)
            now_dt = datetime.fromtimestamp(current_time, tz=user_tz)

            # Get all missed occurrences.
            # inc=True to include the one that was exactly scheduled if it wasn't processed.
            missed_occurrences = rule.between(last_scheduled_dt, now_dt, inc=True)

            if not missed_occurrences:
                # This shouldn't happen if reminder_time <= current_time, but just in case.
                return

            # 1. Send Notification.
            count = len(missed_occurrences)
            timestamps = [f"<t:{int(dt.timestamp())}:F> (<t:{int(dt.timestamp())}:R>)" for dt in missed_occurrences]

            msg_header = f"{user.mention}, sorry I was offline! You missed **{count}** occurrences of your reminder: '{reminder['message']}'."

            if count > 5:
                # Truncate if too many.
                list_str = "\n".join(f"- {ts}" for ts in timestamps[-5:])
                msg_body = f"\nHere are the last 5 missed times:\n{list_str}"
            else:
                list_str = "\n".join(f"- {ts}" for ts in timestamps)
                msg_body = f"\nMissed times:\n{list_str}"

            await targetable.send(msg_header + msg_body)

            # 2. Reschedule to Next Future Time.
            next_occurrence = rule.after(now_dt)
            if next_occurrence:
                next_ts = int(next_occurrence.timestamp())
                await self.db_manager.update_reminder_time(reminder['id'], next_ts)
                self.logger.info(f"Rescheduled missed recurring reminder {reminder['id']} to {next_ts}.")
            else:
                await targetable.send("This reminder has no further occurrences scheduled.")
                await self.db_manager.delete_reminders([reminder['id']])

        except Exception as e:
            self.logger.error(f"Error handling missed recurring reminder {reminder['id']}: {e}")

    async def _scheduler_loop(self) -> None:
        """The main loop that waits for the next reminder and fires it."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                self.scheduler_event.clear()
                now = int(time.time())

                # 1. Process ALL currently due reminders.
                due_reminders = await self.db_manager.get_due_reminders(now)

                if due_reminders:
                    self.logger.info(f"Firing {len(due_reminders)} due reminders.")
                    # Fire all concurrently.
                    tasks = [self._fire_reminder(r) for r in due_reminders]
                    await asyncio.gather(*tasks)
                    # Loop immediately to check if more became due or if we need to sleep.
                    continue

                # 2. If nothing is due right now, find the next one.
                next_reminder = await self.db_manager.get_next_upcoming_reminder(now)

                if next_reminder:
                    delay = next_reminder['reminder_time'] - now
                    # Ensure delay is non-negative.
                    delay = max(0, delay)

                    self.logger.info(f"Next reminder {next_reminder['id']} due in {delay:.2f}s.")

                    try:
                        await asyncio.wait_for(self.scheduler_event.wait(), timeout=delay)
                        # Event triggered (new reminder added/changed).
                        self.logger.info("Scheduler woke up due to event.")
                    except asyncio.TimeoutError:
                        # Timeout reached, time to check DB again.
                        pass
                else:
                    self.logger.info("No upcoming reminders. Waiting for new ones...")
                    await self.scheduler_event.wait()
                    self.logger.info("Scheduler woke up due to event.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                await asyncio.sleep(5)  # Prevent tight loop on error.

    async def _fire_reminder(self, reminder: Dict[str, Any]) -> None:
        """Sends the reminder and handles recurrence/deletion."""
        try:
            # Double check it still exists (might have been deleted while waiting).
            current_state = await self.db_manager.get_reminder_by_id(reminder['id'])
            if not current_state:
                return

            user_id = reminder['user_id']
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)

            # Destination Logic (Refactored to be cleaner).
            destination_pref = await self.db_manager.get_user_config(user_id, 'reminder_destination')
            targetable = None

            # Try DM.
            if destination_pref == 'dm':
                targetable = user
            # Try Specific Channel.
            elif destination_pref and destination_pref.isdigit():
                try:
                    chan_id = int(destination_pref)
                    targetable = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
                except (discord.NotFound, discord.Forbidden):
                    targetable = user  # Fallback.
            # Try Origin Channel.
            else:
                try:
                    targetable = self.bot.get_channel(reminder['channel_id']) or await self.bot.fetch_channel(reminder['channel_id'])
                except (discord.NotFound, discord.Forbidden):
                    targetable = user  # Fallback.

            if targetable:
                # Reply Logic.
                reply_msg_id = reminder.get('reply_message_id')
                sent = False

                msg_content = f"{user.mention}, you asked me to remind you: '{reminder['message']}'"

                if reply_msg_id and hasattr(targetable, 'fetch_message'):
                    try:
                        # Cast to Any to bypass static analysis complaints about specific channel types.
                        targetable_with_fetch = cast(Any, targetable)
                        original_msg = await targetable_with_fetch.fetch_message(reply_msg_id)
                        await original_msg.reply(msg_content)
                        sent = True
                    except:  # noqa: E722 (REASON: We want to catch all exceptions here since discord may send us something weird as a response)
                        pass  # Fallback to normal send.

                if not sent:
                    if reply_msg_id:
                        msg_content += "\n(I couldn't find the message you wanted me to reply to!)"
                    # Cast to Messageable to satisfy static analysis.
                    targetable_dest = cast(discord.abc.Messageable, targetable)
                    await targetable_dest.send(msg_content)

            # Handle Recurrence or Deletion.
            if reminder.get('is_recurring') and reminder.get('recurrence_rule'):
                await self._reschedule_recurring(reminder)
            else:
                await self.db_manager.delete_reminders([reminder['id']])

        except (discord.NotFound, discord.Forbidden):
            # User blocked bot or left server -> Delete reminder.
            await self.db_manager.delete_reminders([reminder['id']])
        except Exception as e:
            self.logger.error(f"Error firing reminder {reminder['id']}: {e}", exc_info=True)

    async def _reschedule_recurring(self, reminder: Dict[str, Any]) -> None:
        """Calculates the next occurrence and updates the DB."""
        try:
            user_tz_str = await self._get_user_timezone(reminder['user_id'])
            user_tz = pytz.timezone(user_tz_str)

            start_date = datetime.fromtimestamp(reminder['created_at'], tz=user_tz)
            rule = rrulestr(reminder['recurrence_rule'], dtstart=start_date)

            now_aware = datetime.now(user_tz)
            next_occurrence = rule.after(now_aware)

            if next_occurrence:
                next_ts = int(next_occurrence.timestamp())
                await self.db_manager.update_reminder_time(reminder['id'], next_ts)
                self.logger.info(f"Rescheduled recurring reminder {reminder['id']} to {next_ts}.")
            else:
                await self.db_manager.delete_reminders([reminder['id']])
        except Exception as e:
            self.logger.error(f"Failed to reschedule {reminder['id']}: {e}")
            await self.db_manager.delete_reminders([reminder['id']])

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone string.

        Converts it to a pytz-compatible format if it's a GMT/UTC offset.
        Defaults to UTC.

        Args:
            user_id (int): The user's ID.

        Returns:
            str: The timezone string.
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
        """Formats an rrule string into a human-readable format.

        Args:
            rule_str (str): The recurrence rule string.

        Returns:
            str: A human-readable description of the recurrence.
        """
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
                    return f"Repeats: {rule_str}"  # Cannot parse further
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

            if freq == "day" and interval_val == 1:
                return "Repeats every day"

            return f"Repeats {period}"

        except Exception as e:
            self.logger.error(f"Failed to parse rrule string '{rule_str}': {e}")
            return f"Repeats: {rule_str}"  # Fallback to raw rule

    def _extract_recurrence_rule(self, text: str) -> Tuple[Optional[str], str]:
        """Extracts a recurrence rule from the given text.

        Args:
            text (str): The text to parse.

        Returns:
            Tuple[Optional[str], str]: A tuple containing the recurrence rule string
            (or None) and the matched text.
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
            r'\bevery\s+(?:(?P<other>other)\s+)?(?:(?P<interval>\d+)\s+)?(?P<unit>second|minute|hour|day|week|month|year|weekend|weekday|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b',  # noqa: E501
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

    async def _parse_reminder(self, query: str) -> Optional[Tuple[Optional[str], str, Optional[str]]]:
        """Parses a query to separate the reminder message from the time string.

        Robustly handles recurrence and split-time entities (e.g. "On Dec 21 ... at 5am").

        Args:
            query (str): The user's input string.

        Returns:
            Optional[Tuple[Optional[str], str, Optional[str]]]: A tuple containing
            (message, time_string, recurrence_rule), or None if parsing fails.
        """
        # ==========================================
        # Stage 1: Initial Sanitization & Triggers
        # ==========================================
        # Strip NLP trigger phrases so we can focus on the actual content.
        trigger_patterns = [
            r'\bremind\b', r'\breminder\b', r'\bremember\b',
            r'set\s+a\s+reminder', r'set\s.*reminder'
        ]
        combined_pattern = r'^\s*(' + '|'.join(f'({p})' for p in trigger_patterns) + r')\s*'
        sanitized_query = re.sub(combined_pattern, '', query, count=1, flags=re.IGNORECASE).strip()

        # Split the query into words for easier manipulation.
        words = sanitized_query.split()

        # Iteratively remove common filler words from the start of the query.
        # This handles "me to...", "for me...", "to...", etc.
        # Note: "that" is NOT included here because it can be the object of the reminder (e.g. "remind me of that").
        while words and words[0].lower() in ["me", "us", "him", "her", "them", "to", "for", "about", "of"]:
            words.pop(0)

        sanitized_query = " ".join(words)

        if not sanitized_query:
            return None

        # ==========================================
        # Stage 2: Recurrence Extraction
        # ==========================================
        # We prioritize extracting recurrence rules (e.g., "every day") because they fundamentally change how the reminder behaves.
        # Workaround for dateparser recurrence limitations.
        recurrence_rule = None

        # Extract recurrence rule using the helper method.
        recurrence_rule, matched_recurrence_text = self._extract_recurrence_rule(sanitized_query)

        if recurrence_rule:
            self.logger.info(f"Detected recurrence: {recurrence_rule}")
            sanitized_query = sanitized_query.replace(matched_recurrence_text, '', 1)
            # Clean up any double spaces left behind by the removal.
            sanitized_query = re.sub(r'\s+', ' ', sanitized_query).strip()

        # ==========================================
        # Stage 3 & 4: Split-Head/Tail Time Extraction
        # ==========================================
        # Natural language is messy. The time at the start ("Tomorrow go to the store"
        # or at the end ("Go to the store tomorrow"). Sometimes they split it ("On Friday go to the store at 5pm").
        # Instead attempt to "eat" valid time phrases from both ends of the sentence.

        words = sanitized_query.split()

        # --- Helper to consume words and check validity ---
        async def get_longest_valid_date_segment(candidate_words: List[str], direction: str) -> Tuple[str, int]:
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
                    # e.g. "on the 25th", "on the next Friday".
                    should_stop = True
                    if word_to_check == 'the' and i < len(candidate_words):
                        next_word = candidate_words[i].lower()
                        # Check if next word is a digit (25th) or a relative keyword or day/month.
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
                    # Remove the first word (the preposition).
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

        # 1. Try consuming time info from the FRONT of the sentence.
        if config.DEV_MODE:
            self.logger.debug(f"Starting Front Time Extraction with words: {words}")
        front_time_str, front_word_count = await get_longest_valid_date_segment(words, "FRONT")

        # 2. Try consuming time info from the BACK of the sentence.
        # We only look at the back if we haven't already consumed the whole string from the front.
        back_time_str = ""
        back_word_count = 0

        remaining_words_at_back = len(words) - front_word_count
        if remaining_words_at_back > 0:
            if config.DEV_MODE:
                self.logger.debug(f"Starting Back Time Extraction. Remaining words: {remaining_words_at_back}")

            # Prepare words for back extraction (reverse them).
            # We take the words that were NOT consumed by the front extraction.
            words_for_back = words[front_word_count:]
            reversed_words = words_for_back[::-1]

            # Use the helper to find the longest valid segment from the back.
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
        message_words = words  # Default to assuming everything is the message if no time found.

        # Case A: Split Time ("On Monday" ... "at 5pm").
        # We found valid time parts at BOTH ends. We try to combine them.
        if front_time_str and back_time_str:
            self.logger.info(f"Found split time: '{front_time_str}' AND '{back_time_str}'")
            combined_candidate = f"{front_time_str} {back_time_str}"

            # Validate that the combined string makes sense.
            if await asyncio.to_thread(dateparser.parse, combined_candidate, settings={'PREFER_DATES_FROM': 'future'}):
                final_time_string = combined_candidate
                # The message is whatever is left in the middle.
                message_words = words[front_word_count: len(words) - back_word_count]
            else:
                # If they don't combine validly, we abort to avoid malformed reminders.
                self.logger.warning(f"Split time found but failed to combine: '{combined_candidate}'. Aborting.")
                return None

        # Case B: Front only ("Tomorrow go to store").
        elif front_time_str:
            self.logger.info(f"Found time at front: '{front_time_str}'")
            final_time_string = front_time_str
            message_words = words[front_word_count:]

        # Case C: Back only ("Go to store tomorrow").
        elif back_time_str:
            self.logger.info(f"Found time at back: '{back_time_str}'")
            final_time_string = back_time_str
            message_words = words[:len(words) - back_word_count]

        # ==========================================
        # Stage 6: Final Cleanup
        # ==========================================

        # If we found a recurrence rule but NO specific time (e.g. "Every day"),
        # we set the time to "now" so the recurrence starts immediately.
        # Default to immediate execution if no time specified.
        if not final_time_string and recurrence_rule:
            final_time_string = "now"

        # If we still have no time string, we failed to parse a reminder.
        if not final_time_string:
            self.logger.warning(f"No time found in: '{query}'")
            return (sanitized_query, "", None)

        # Reassemble the message from the remaining words.
        final_message = " ".join(message_words)

        # Clean specific unambiguous connectors from the start of the message.
        # The list only contains "to" but can be expanded when needed.
        junk = ['to']

        msg_words = final_message.split()
        while msg_words and msg_words[0].lower() in junk:
            msg_words.pop(0)
        final_message = " ".join(msg_words)

        return (final_message, final_time_string, recurrence_rule)

    async def _confirm_and_save_reminder(
        self,
        ctx: commands.Context,
        reminder_message: str,
        time_str: str,
        dt_object: datetime,
        recurrence_rule: Optional[str],
        reply_message_id: Optional[int] = None
    ) -> None:
        """Handles the confirmation, saving, and scheduling of a reminder.

        Args:
            ctx (commands.Context): The command context.
            reminder_message (str): The reminder message.
            time_str (str): The original time string (for context in edits).
            dt_object (datetime): The parsed datetime object.
            recurrence_rule (Optional[str]): The recurrence rule, if any.
            reply_message_id (Optional[int]): The ID of the message to reply to, if any.
        """
        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        timestamp = int(dt_object.timestamp())
        confirmation_message = f"Okay, I will remind you on <t:{timestamp}:F> to '{reminder_message}'."
        if recurrence_rule:
            confirmation_message += "\nThis reminder will repeat."

        if reply_message_id:
            confirmation_message += "\nI'll also reply to the message you linked!"

        confirmation_message += "\nIs this correct? (`yes`, `edit`, `edit time`, `edit message`, `no`)"

        await ctx.send(confirmation_message)

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            content = msg.content.lower()

            if content in ['yes', 'y']:
                is_recurring = recurrence_rule is not None
                new_reminder_id = await self.db_manager.add_reminder(
                    ctx.author.id, ctx.channel.id, timestamp, reminder_message, int(time.time()),
                    is_recurring, recurrence_rule, reply_message_id
                )

                # Wake up the scheduler to pick up the new reminder
                self.scheduler_event.set()

                await ctx.send("✅ Reminder saved and scheduled!")
                self.logger.info(f"Reminder {new_reminder_id} set for user {ctx.author.id} at {timestamp} (Recurring: {is_recurring}).")

            elif content == 'edit':
                await ctx.send("Let's start over.")
                await self._interactive_reminder_flow(ctx, reply_message_id=reply_message_id)
            elif content == 'edit time':
                await ctx.send("Okay, let's pick a new time.")
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message, reply_message_id=reply_message_id)
            elif content == 'edit message':
                await ctx.send("Okay, let's change the message.")
                await self._interactive_reminder_flow(
                    ctx,
                    initial_time=time_str,
                    initial_recurrence=recurrence_rule,
                    reply_message_id=reply_message_id
                )
            else:
                await ctx.send("Reminder cancelled. You can start over if you wish.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Reminder creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error in confirmation flow for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while saving the reminder.")

    async def _interactive_reminder_flow(
        self,
        ctx: commands.Context,
        initial_message: str = "",
        initial_time: str = "",
        initial_recurrence: Optional[str] = None,
        reply_message_id: Optional[int] = None
    ) -> None:
        """Guides the user through creating a reminder interactively.

        Args:
            ctx (commands.Context): The command context.
            initial_message (str): The initial message, if any.
            initial_time (str): The initial time string, if any.
            initial_recurrence (Optional[str]): The initial recurrence rule, if any.
            reply_message_id (Optional[int]): The ID of the message to reply to, if any.
        """

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
                    time_str = ""  # Reset to re-ask
                    recurrence_rule = None  # Reset recurrence if time fails

            # 3. Confirmation and Saving
            await self._confirm_and_save_reminder(ctx, reminder_message, time_str, dt_object, recurrence_rule, reply_message_id)

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Reminder creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error in interactive reminder flow for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while creating the reminder.")

    async def remind(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all reminder requests.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        try:
            # Check if this is a reply to link context
            reply_message_id = None
            if ctx.message.reference and ctx.message.reference.message_id:
                reply_message_id = ctx.message.reference.message_id

            if not query.strip():
                await self._interactive_reminder_flow(ctx, reply_message_id=reply_message_id)
                return

            parsed = await self._parse_reminder(query)

            # If parsing fails to find a message or a time, start the interactive flow from scratch.
            if not parsed or not parsed[0] or not parsed[1]:
                self.logger.info(f"Failed to understand '{query}'. Starting interactive flow.")
                await ctx.send("I'm sorry, I couldn't understand the reminder. Let's set it up step-by-step.")
                await self._interactive_reminder_flow(ctx, reply_message_id=reply_message_id)  # No context retained
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
                await self._interactive_reminder_flow(ctx, reply_message_id=reply_message_id)  # No context retained
                return

            timestamp = int(dt_object.timestamp())
            # Prevent setting reminders in the past.
            if timestamp <= int(time.time()):
                await ctx.send("You can't set a reminder in the past! Please try again.")
                # We retain context here because the user's intent was clear, just the time was wrong.
                await self._interactive_reminder_flow(ctx, initial_message=reminder_message or "", initial_recurrence=recurrence_rule, reply_message_id=reply_message_id)
                return

            # --- Confirmation and Saving ---
            await self._confirm_and_save_reminder(ctx, reminder_message or "", time_str, dt_object, recurrence_rule, reply_message_id)

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Reminder creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error setting reminder for user {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("Sorry, an error occurred while setting your reminder.")

    async def check_reminders_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for checking reminders.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
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

    async def delete_reminders_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for deleting reminders.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
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

            # This is the crucial step: delete from the database so it doesn't recur on restart.
            await self.db_manager.delete_reminders(list(ids_to_delete))

            # Wake up the scheduler so it knows to stop waiting for a deleted reminder if it was next
            self.scheduler_event.set()

            deleted_count = len(ids_to_delete)
            response_parts = [f"Successfully deleted {deleted_count} reminder(s): `{', '.join(sorted(valid_numbers_deleted))}`"]

            if invalid_numbers:
                response_parts.append(f"Could not find reminders for these numbers: `{', '.join(invalid_numbers)}`.")

            await ctx.send("\n".join(response_parts))
            self.logger.info(f"User {ctx.author.id} deleted {deleted_count} reminders. IDs: {list(ids_to_delete)}")

        except Exception as e:
            self.logger.error(f"Unexpected error in reminderdelete NLP: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")

    async def set_timezone_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for setting a user's timezone.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
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
            "pst": "America/Los_Angeles",  # Pacific Standard Time
            "pdt": "America/Los_Angeles",  # Pacific Daylight Time
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
            "acdt": "Australia/Adelaide",  # Australian Central Daylight Time
            "awst": "Australia/Perth",    # Australian Western Standard Time
        }

        tz_to_check = timezone_str.lower()
        final_tz_str = None
        display_tz_str = ""

        if tz_to_check in TIMEZONE_ABBREVIATIONS:
            final_tz_str = TIMEZONE_ABBREVIATIONS[tz_to_check]
            display_tz_str = final_tz_str  # Use the full name for display

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
            await ctx.send(f"`{timezone_str}` is not a recognized timezone. Please use a standard IANA name (e.g., `US/Eastern`, `Europe/London`), a common abbreviation (e.g., `EST`, `BST`), or a GMT/UTC offset (e.g., `GMT+5`).")  # noqa: E501
        except Exception as e:
            self.logger.error(f"Unexpected error in timezone NLP: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred.")

    async def edit_reminder_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Initiates an interactive conversation to edit an existing reminder.

        The user can choose to edit the reminder's message, time, or recurrence.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        # Find the number in the query (e.g., "edit reminder 3").
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send("Please specify the number of the reminder you want to edit. Use `check reminders` to see the numbers.")
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

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            embed = discord.Embed(
                title=f"Edit Reminder #{reminder_num_to_edit}",
                description="What would you like to edit?",
                color=discord.Color.blue()
            )
            embed.add_field(name="1. Message", value=reminder_to_edit['message'], inline=False)
            embed.add_field(name="2. Time", value=f"<t:{int(reminder_to_edit['reminder_time'])}:F>", inline=False)
            embed.add_field(name="3. Recurrence", value=reminder_to_edit['recurrence_rule'] or 'None', inline=False)
            embed.set_footer(text="Click a button or reply with the number.")

            options = {
                "1️⃣ Message": "1",
                "2️⃣ Time": "2",
                "3️⃣ Recurrence": "3"
            }

            choice = await get_selection(ctx, embed, options, timeout=30.0)

            if not choice or choice.lower() in ['exit', 'cancel']:
                await ctx.send("Edit cancelled.")
                return

            updates: Dict[str, Any] = {}
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
                            should_reschedule = True  # Recurrence change might affect next run time logic if we were fancy, but definitely needs DB update
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
                        # Wake up the scheduler to pick up the changes
                        self.scheduler_event.set()
                else:
                    await ctx.send("Something went wrong. I couldn't update that reminder.")
            else:
                await ctx.send("No changes were made.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Edit cancelled.")
        except Exception as e:
            self.logger.error(f"Error editing reminder for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while editing the reminder.")

    async def reminder_settings_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Opens an interactive settings menu for reminders.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
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
        elif dest_pref == 'channel':  # Handle legacy value
            display_dest = "Origin Channel"

        embed = discord.Embed(title="Reminder Settings", color=discord.Color.blue())
        embed.description = (
            f"**1. Destination:** `{display_dest}`\n"
            "(Where reminders are sent)"
        )
        embed.set_footer(text="Click a button or reply with the number.")

        options = {
            "1️⃣ Destination": "1"
        }

        choice = await get_selection(ctx, embed, options, timeout=60.0)

        if not choice or choice.lower() in ['exit', 'cancel']:
            await ctx.send("Settings closed.")
            return

        if choice == '1':
            embed = discord.Embed(title="Select Destination", description="Where should I send your reminders?", color=discord.Color.blue())
            options = {
                "1️⃣ DMs": "1",
                "2️⃣ Origin": "2",
                "3️⃣ Specific Channel": "3"
            }
            sub_choice = await get_selection(ctx, embed, options, timeout=60.0)

            if not sub_choice:
                await ctx.send("Settings timed out.")
                return

            if sub_choice == '1':
                await self.db_manager.set_user_config(ctx.author.id, 'reminder_destination', 'dm')
                await ctx.send("✅ Destination set to **Direct Messages**.")
            elif sub_choice == '2':
                await self.db_manager.set_user_config(ctx.author.id, 'reminder_destination', 'origin')
                await ctx.send("✅ Destination set to **Origin Channel**.")
            elif sub_choice == '3':
                await ctx.send("Please mention the channel you want to use (e.g. `#general`).")

                def check(m: discord.Message) -> bool:
                    return m.author == ctx.author and m.channel == ctx.channel
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


async def setup(bot: CoreBot, **kwargs: Any) -> None:
    """Standard setup, receiving the database path via kwargs from main.py.

    Args:
        bot (CoreBot): The bot instance.
        **kwargs: Additional keyword arguments.
    """
    await bot.add_cog(Reminders(bot))

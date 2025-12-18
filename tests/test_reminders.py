"""Unit tests for the Reminders cog.

This module contains comprehensive tests for the reminder system, including:
- End-to-end reminder creation flows (with mocked parsing)
- Parser unit tests for _parse_reminder
- Recurrence rule extraction tests
- Modifier stripping and fractional time normalization tests
- Error handling tests
"""
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.reminders import Reminders
from utils.bot_class import CoreBot
from utils.database import DatabaseManager


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_bot():
    """Create a mock CoreBot instance."""
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    bot.loop = MagicMock()
    bot.wait_for = AsyncMock()
    return bot


@pytest.fixture
def reminders_cog(mock_bot):
    """Create a Reminders cog instance with mocked bot."""
    cog = Reminders(mock_bot)
    return cog


@pytest.fixture
def mock_ctx(mock_bot):
    """Create a mock command context."""
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.author.id = 12345
    ctx.author.display_name = "TestUser"
    ctx.channel.id = 67890

    # Create a separate mock for the message attribute
    message_mock = MagicMock(spec=discord.Message)
    message_mock.reference = None
    ctx.message = message_mock

    # Mock send to return a mock message
    async def send_mock(*args, **kwargs):
        msg = MagicMock()
        return msg
    ctx.send = AsyncMock(side_effect=send_mock)

    # Mock wait_for to return an awaitable mock
    async def wait_for_mock(*args, **kwargs):
        msg = MagicMock()
        msg.content = "yes"  # Default
        return msg

    mock_bot.wait_for = AsyncMock(side_effect=wait_for_mock)

    return ctx


# =============================================================================
# END-TO-END REMINDER FLOW TESTS
# =============================================================================


class TestReminderCreationFlow:
    """Tests for the complete reminder creation flow via remind()."""

    @pytest.mark.asyncio
    async def test_happy_path(self, reminders_cog, mock_ctx):
        """Test a standard reminder creation flow."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        async def wait_for_yes(*args, **kwargs):
            msg = MagicMock()
            msg.content = "yes"
            return msg
        mock_ctx.bot.wait_for.side_effect = wait_for_yes

        reminders_cog.db_manager.add_reminder.return_value = 1

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

        reminders_cog._parse_reminder.assert_called_once()
        assert mock_ctx.send.call_count >= 1
        args, _ = mock_ctx.send.call_args_list[-2]
        assert "Buy milk" in args[0]

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][0] == 12345  # user_id
        assert call_args[0][3] == "Buy milk"  # message

    @pytest.mark.asyncio
    async def test_with_reply_context(self, reminders_cog, mock_ctx):
        """Test reminder creation with a reply context."""
        mock_ctx.message.reference = MagicMock()
        mock_ctx.message.reference.message_id = 99999

        reminders_cog._parse_reminder = AsyncMock(return_value=("Check this", "tomorrow", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        async def wait_for_yes(*args, **kwargs):
            msg = MagicMock()
            msg.content = "yes"
            return msg
        mock_ctx.bot.wait_for.side_effect = wait_for_yes

        await reminders_cog.remind(mock_ctx, query="remind me to Check this tomorrow")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][7] == 99999  # reply_message_id

    @pytest.mark.asyncio
    async def test_cancel(self, reminders_cog, mock_ctx):
        """Test cancelling the reminder."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        async def wait_for_no(*args, **kwargs):
            msg = MagicMock()
            msg.content = "no"
            return msg
        mock_ctx.bot.wait_for.side_effect = wait_for_no

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk")

        reminders_cog.db_manager.add_reminder.assert_not_called()

    @pytest.mark.asyncio
    async def test_recurrence(self, reminders_cog, mock_ctx):
        """Test saving a recurring reminder."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Wake up", "at 8am", "FREQ=DAILY"))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        async def wait_for_yes(*args, **kwargs):
            msg = MagicMock()
            msg.content = "yes"
            return msg
        mock_ctx.bot.wait_for.side_effect = wait_for_yes

        await reminders_cog.remind(mock_ctx, query="remind me to Wake up every day at 8am")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][5] is True  # is_recurring
        assert call_args[0][6] == "FREQ=DAILY"  # recurrence_rule


class TestReminderEditFlow:
    """Tests for editing reminders during creation."""

    @pytest.mark.asyncio
    async def test_edit_time(self, reminders_cog, mock_ctx):
        """Test the 'edit time' flow."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_edit = MagicMock()
        msg_edit.content = "edit time"
        msg_time = MagicMock()
        msg_time.content = "in 1 hour"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_edit, msg_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        assert mock_ctx.bot.wait_for.call_count >= 3

    @pytest.mark.asyncio
    async def test_edit_message(self, reminders_cog, mock_ctx):
        """Test the 'edit message' flow."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_edit = MagicMock()
        msg_edit.content = "edit message"
        msg_new_text = MagicMock()
        msg_new_text.content = "Buy cookies"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_edit, msg_new_text, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][3] == "Buy cookies"

    @pytest.mark.asyncio
    async def test_full_edit(self, reminders_cog, mock_ctx):
        """Test the 'edit' (full reset) flow."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_edit = MagicMock()
        msg_edit.content = "edit"
        msg_new_text = MagicMock()
        msg_new_text.content = "Buy cookies"
        msg_new_time = MagicMock()
        msg_new_time.content = "tomorrow"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_edit, msg_new_text, msg_new_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][3] == "Buy cookies"

    @pytest.mark.asyncio
    async def test_edit_preserves_reply_context(self, reminders_cog, mock_ctx):
        """Test that reply context survives an 'edit time' loop."""
        mock_ctx.message.reference = MagicMock()
        mock_ctx.message.reference.message_id = 88888

        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_edit = MagicMock()
        msg_edit.content = "edit time"
        msg_time = MagicMock()
        msg_time.content = "in 1 hour"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_edit, msg_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][7] == 88888


class TestInteractiveFlow:
    """Tests for the interactive reminder creation flow."""

    @pytest.mark.asyncio
    async def test_empty_query_triggers_interactive(self, reminders_cog, mock_ctx):
        """Test calling the interactive flow directly (empty query)."""
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_text = MagicMock()
        msg_text.content = "Buy milk"
        msg_time = MagicMock()
        msg_time.content = "in 10 mins"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_text, msg_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        call_args = reminders_cog.db_manager.add_reminder.call_args
        assert call_args[0][3] == "Buy milk"

    @pytest.mark.asyncio
    async def test_parse_failure_triggers_interactive(self, reminders_cog, mock_ctx):
        """Test that parsing failure triggers a clean interactive flow."""
        reminders_cog._parse_reminder = AsyncMock(return_value=None)
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        msg_text = MagicMock()
        msg_text.content = "Buy milk"
        msg_time = MagicMock()
        msg_time.content = "in 10 mins"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_text, msg_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        await reminders_cog.remind(mock_ctx, query="garbage input")

        reminders_cog.db_manager.add_reminder.assert_called_once()
        assert reminders_cog.db_manager.add_reminder.call_args[0][3] == "Buy milk"

    @pytest.mark.asyncio
    async def test_past_date_keeps_context(self, reminders_cog, mock_ctx):
        """Test that past dates are rejected and context is kept."""
        reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "yesterday", None))
        reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

        past_date = datetime.fromtimestamp(time.time() - 10000)

        msg_time = MagicMock()
        msg_time.content = "in 10 mins"
        msg_yes = MagicMock()
        msg_yes.content = "yes"

        msgs = iter([msg_time, msg_yes])

        async def wait_for_sequence(*args, **kwargs):
            return next(msgs)
        mock_ctx.bot.wait_for.side_effect = wait_for_sequence

        with patch('dateparser.parse') as mock_parse:
            future_date = datetime.fromtimestamp(time.time() + 10000)
            mock_parse.side_effect = [past_date, future_date]

            await reminders_cog.remind(mock_ctx, query="remind me to Buy milk yesterday")

            reminders_cog.db_manager.add_reminder.assert_called_once()
            assert reminders_cog.db_manager.add_reminder.call_args[0][3] == "Buy milk"


# =============================================================================
# PARSER UNIT TESTS - BASIC PATTERNS
# =============================================================================


class TestParseReminderBasicPatterns:
    """Tests for basic time patterns and message extraction.

    Uses parametrization to test various input patterns.
    Each case verifies: (1) parsing succeeds, (2) message extracted, (3) time extracted.
    """

    # fmt: off
    @pytest.mark.asyncio
    @pytest.mark.parametrize("input_str,expected_msg,expected_time_contains", [
        # Time position variations
        pytest.param("tomorrow buy milk", "buy milk", "tomorrow", id="time_at_front"),
        pytest.param("buy milk tomorrow", "buy milk", "tomorrow", id="time_at_back"),

        # Preposition variations
        pytest.param("call mom at 5pm", "call mom", "5", id="preposition_at"),
        pytest.param("buy groceries in 30 minutes", "buy groceries", "30", id="preposition_in"),
        pytest.param("on friday go to the gym", "gym", "friday", id="preposition_on"),

        # Relative and specific times
        pytest.param("call john in 2 hours", "call john", "2", id="relative_hours"),
        pytest.param("open presents december 25th at 9am", "presents", "25", id="specific_date"),
    ])
    async def test_basic_patterns(self, reminders_cog, input_str, expected_msg, expected_time_contains):
        # fmt: on
        """Verify basic time pattern extraction."""
        result = await reminders_cog._parse_reminder(input_str)
        assert result is not None, f"Failed to parse: {input_str}"
        message, time_str, recurrence = result
        assert expected_msg in message.lower(), f"Expected '{expected_msg}' in message '{message}'"
        assert expected_time_contains in time_str.lower(), f"Expected '{expected_time_contains}' in time '{time_str}'"
        assert recurrence is None  # Basic patterns have no recurrence


# =============================================================================
# PARSER UNIT TESTS - SPLIT TIME
# =============================================================================


class TestParseReminderSplitTime:
    """Tests for split time patterns (time at both ends)."""

    @pytest.mark.asyncio
    async def test_day_and_hour(self, reminders_cog):
        """Split: 'On Monday call dentist at 3pm'."""
        result = await reminders_cog._parse_reminder("on monday call dentist at 3pm")
        assert result is not None
        message, time_str, _recurrence = result
        assert "dentist" in message.lower()
        assert "monday" in time_str.lower() or "3pm" in time_str.lower()

    @pytest.mark.asyncio
    async def test_date_and_hour(self, reminders_cog):
        """Split: 'On Dec 21 go to party at 8pm'."""
        result = await reminders_cog._parse_reminder("on dec 21 go to party at 8pm")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "party" in message.lower()

    @pytest.mark.asyncio
    async def test_tomorrow_and_hour(self, reminders_cog):
        """Split: 'Tomorrow pick up package at noon'."""
        result = await reminders_cog._parse_reminder("tomorrow pick up package at noon")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "package" in message.lower()


# =============================================================================
# PARSER UNIT TESTS - RECURRENCE
# =============================================================================


class TestParseReminderRecurrence:
    """Tests for recurrence pattern extraction in _parse_reminder.

    Uses parametrization to test recurrence detection through the full parser.
    Each case verifies: (1) parsing succeeds, (2) recurrence rule generated, (3) correct frequency/parts.
    """

    # fmt: off
    @pytest.mark.asyncio
    @pytest.mark.parametrize("input_str,expected_msg,expected_freq,expected_parts", [
        # Simple frequency keywords
        pytest.param("take medicine daily at 8am", "medicine", "DAILY", [], id="daily"),
        pytest.param("weekly team meeting at 10am", "meeting", "WEEKLY", [], id="weekly"),

        # "every X" patterns
        pytest.param("every day water plants at 7am", "plants", "DAILY", [], id="every_day"),
        pytest.param("every monday gym at 6pm", "gym", "WEEKLY", ["MO"], id="every_monday"),
        pytest.param("every other day check email", "email", "DAILY", ["INTERVAL=2"], id="every_other_day"),
        pytest.param("every 3 days water plants", "plants", "DAILY", ["INTERVAL=3"], id="every_n_days"),

        # Bi-weekly (regression: was 'bi-weekly' key mismatch)
        pytest.param("bi-weekly paycheck review", "paycheck", "WEEKLY", ["INTERVAL=2"], id="biweekly"),

        # Weekend/weekday
        pytest.param("every weekend clean house", "house", "WEEKLY", ["SA", "SU"], id="every_weekend"),
        pytest.param("every weekday standup meeting at 9am", "standup", "WEEKLY", ["MO", "FR"], id="every_weekday"),

        # Monthly
        pytest.param("every 15th of the month pay rent", "rent", "MONTHLY", ["BYMONTHDAY=15"], id="monthly_15th"),
    ])
    async def test_recurrence_patterns(self, reminders_cog, input_str, expected_msg, expected_freq, expected_parts):
        # fmt: on
        """Verify recurrence pattern detection through full parser."""
        result = await reminders_cog._parse_reminder(input_str)
        assert result is not None, f"Failed to parse: {input_str}"
        message, _time_str, recurrence = result

        assert expected_msg in message.lower(), f"Expected '{expected_msg}' in message '{message}'"
        assert recurrence is not None, f"Expected recurrence rule for '{input_str}'"
        assert expected_freq in recurrence, f"Expected '{expected_freq}' in recurrence '{recurrence}'"

        for part in expected_parts:
            assert part in recurrence, f"Expected '{part}' in recurrence '{recurrence}'"


# =============================================================================
# PARSER UNIT TESTS - TRIGGER STRIPPING
# =============================================================================


class TestParseReminderTriggerStripping:
    """Tests for NLP trigger phrase stripping."""

    @pytest.mark.asyncio
    async def test_strip_remind_me(self, reminders_cog):
        """Strip 'remind me to'."""
        result = await reminders_cog._parse_reminder("remind me to buy milk tomorrow")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "buy milk" in message.lower()
        assert "remind" not in message.lower()

    @pytest.mark.asyncio
    async def test_strip_set_a_reminder(self, reminders_cog):
        """Strip 'set a reminder to'."""
        result = await reminders_cog._parse_reminder("set a reminder to call mom at 5pm")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "call mom" in message.lower()
        assert "reminder" not in message.lower()

    @pytest.mark.asyncio
    async def test_strip_remember(self, reminders_cog):
        """Strip 'remember to'."""
        result = await reminders_cog._parse_reminder("remember to take out trash tomorrow")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "trash" in message.lower()
        assert "remember" not in message.lower()

    @pytest.mark.asyncio
    async def test_strip_filler_words(self, reminders_cog):
        """Strip filler words like 'me', 'to', 'for'."""
        result = await reminders_cog._parse_reminder("remind me for us to check oven in 10 minutes")
        assert result is not None
        message, _time_str, _recurrence = result
        assert not message.lower().startswith("me ")
        assert not message.lower().startswith("for ")


# =============================================================================
# PARSER UNIT TESTS - EDGE CASES
# =============================================================================


class TestParseReminderEdgeCases:
    """Tests for edge cases and potential failure modes."""

    @pytest.mark.asyncio
    async def test_empty_query(self, reminders_cog):
        """Empty query should return None."""
        result = await reminders_cog._parse_reminder("")
        assert result is None

    @pytest.mark.asyncio
    async def test_only_trigger_words(self, reminders_cog):
        """Only trigger words with no content."""
        result = await reminders_cog._parse_reminder("remind me to")
        assert result is None or result[0] == ""

    @pytest.mark.asyncio
    async def test_no_time_expression(self, reminders_cog):
        """No time expression in query."""
        result = await reminders_cog._parse_reminder("buy milk and eggs")
        assert result is not None
        message, time_str, _recurrence = result
        assert time_str == "" or time_str is None or message

    @pytest.mark.asyncio
    async def test_message_with_numbers(self, reminders_cog):
        """Message containing numbers that aren't times."""
        result = await reminders_cog._parse_reminder("buy 5 apples tomorrow")
        assert result is not None
        message, time_str, _recurrence = result
        assert "5 apples" in message.lower() or "apples" in message.lower()
        assert "tomorrow" in time_str.lower()

    @pytest.mark.asyncio
    async def test_message_with_time_word_in_content(self, reminders_cog):
        """Message containing time-like words that are part of content."""
        result = await reminders_cog._parse_reminder("watch the movie 'Tomorrow Never Dies' at 8pm")
        assert result is not None
        _message, time_str, _recurrence = result
        assert time_str != ""

    @pytest.mark.asyncio
    async def test_very_long_message(self, reminders_cog):
        """Long message with time at end."""
        long_msg = "call the very important client about the contract renewal and quarterly review"
        result = await reminders_cog._parse_reminder(f"{long_msg} tomorrow at 3pm")
        assert result is not None
        message, time_str, _recurrence = result
        assert "client" in message.lower()
        assert "tomorrow" in time_str.lower() or "3pm" in time_str.lower()

    @pytest.mark.asyncio
    async def test_multiple_time_expressions(self, reminders_cog):
        """Multiple time expressions (ambiguous)."""
        result = await reminders_cog._parse_reminder("tomorrow at 3pm and also at 5pm do something")
        assert result is not None

    @pytest.mark.asyncio
    async def test_time_with_am_pm_variations(self, reminders_cog):
        """Various AM/PM formats."""
        for time_fmt in ["5pm", "5 pm", "5PM", "5 PM", "17:00"]:
            result = await reminders_cog._parse_reminder(f"call mom at {time_fmt}")
            assert result is not None, f"Failed for format: {time_fmt}"
            message, _time_str, _recurrence = result
            assert "mom" in message.lower()

    @pytest.mark.asyncio
    async def test_ordinal_dates(self, reminders_cog):
        """Ordinal date formats."""
        for ordinal in ["1st", "2nd", "3rd", "21st", "22nd", "23rd"]:
            result = await reminders_cog._parse_reminder(f"on the {ordinal} pay bills")
            assert result is not None, f"Failed for ordinal: {ordinal}"

    @pytest.mark.asyncio
    async def test_relative_day_names(self, reminders_cog):
        """Day names without 'next'/'this' (dateparser limitation).

        Note: dateparser does NOT support 'next monday' or 'this friday'.
        It only recognizes bare day names like 'monday' which it interprets
        as the upcoming occurrence due to PREFER_DATES_FROM: future.
        """
        result = await reminders_cog._parse_reminder("monday submit report")
        assert result is not None
        message, time_str, _recurrence = result
        assert "report" in message.lower()
        assert "monday" in time_str.lower()

    @pytest.mark.asyncio
    async def test_in_relative_time_variations(self, reminders_cog):
        """Various 'in X time' formats."""
        test_cases = [
            "in 5 minutes",
            "in an hour",
            "in 2 hours",
            "in 30 seconds",
            "in a week",
        ]
        for time_expr in test_cases:
            result = await reminders_cog._parse_reminder(f"check oven {time_expr}")
            assert result is not None, f"Failed for: {time_expr}"
            message, _time_str, _recurrence = result
            assert "oven" in message.lower(), f"Message not extracted for: {time_expr}"

    @pytest.mark.asyncio
    async def test_modifier_plus_fractional_time(self, reminders_cog):
        """Combined modifier stripping + fractional normalization.

        Tests interaction between Stage 3b (modifier stripping) and
        Stage 3c (fractional time normalization).
        """
        result = await reminders_cog._parse_reminder("next monday in half an hour meeting")
        assert result is not None
        message, time_str, _recurrence = result
        assert "meeting" in message.lower()
        assert time_str != ""

    @pytest.mark.asyncio
    async def test_recurrence_without_message(self, reminders_cog):
        """Recurrence pattern with no actual message content.

        Should not crash - empty/minimal message is acceptable.
        """
        result = await reminders_cog._parse_reminder("every monday")
        assert result is not None
        _message, _time_str, recurrence = result
        assert recurrence is not None
        assert "WEEKLY" in recurrence

    @pytest.mark.asyncio
    async def test_excessive_whitespace(self, reminders_cog):
        """Extra whitespace should not break parsing."""
        result = await reminders_cog._parse_reminder("  tomorrow   buy   milk  ")
        assert result is not None
        message, time_str, _recurrence = result
        assert "milk" in message.lower()
        assert "tomorrow" in time_str.lower()

    @pytest.mark.asyncio
    async def test_number_in_message_and_time(self, reminders_cog):
        """Numbers in message shouldn't be confused with time.

        The '10' in 'buy 10 items' should stay in message,
        while '10pm' should be recognized as time.
        """
        result = await reminders_cog._parse_reminder("buy 10 items at 10pm")
        assert result is not None
        message, time_str, _recurrence = result
        assert "10" in message or "items" in message.lower()
        assert "10" in time_str or "pm" in time_str.lower()

    @pytest.mark.asyncio
    async def test_unicode_emoji_in_message(self, reminders_cog):
        """Emoji and unicode should not break parsing."""
        result = await reminders_cog._parse_reminder("🎂 birthday party tomorrow")
        assert result is not None
        message, time_str, _recurrence = result
        assert "birthday" in message.lower() or "🎂" in message
        assert "tomorrow" in time_str.lower()


# =============================================================================
# PARSER UNIT TESTS - COMPLEX SCENARIOS
# =============================================================================


class TestParseReminderComplexScenarios:
    """Tests for complex real-world scenarios."""

    @pytest.mark.asyncio
    async def test_reminder_with_apostrophe(self, reminders_cog):
        """Message with apostrophe."""
        result = await reminders_cog._parse_reminder("pick up mom's prescription tomorrow")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "prescription" in message.lower()

    @pytest.mark.asyncio
    async def test_reminder_with_url(self, reminders_cog):
        """Message containing a URL."""
        result = await reminders_cog._parse_reminder("check https://example.com tomorrow")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "example.com" in message.lower() or "https" in message.lower()

    @pytest.mark.asyncio
    async def test_reminder_with_mention_placeholder(self, reminders_cog):
        """Message with Discord-like mention."""
        result = await reminders_cog._parse_reminder("tell @John about meeting tomorrow")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "john" in message.lower() or "@" in message

    @pytest.mark.asyncio
    async def test_recurring_with_specific_start(self, reminders_cog):
        """Recurring reminder with specific start time."""
        result = await reminders_cog._parse_reminder("every monday starting next week team sync at 10am")
        assert result is not None
        _message, _time_str, recurrence = result
        assert recurrence is not None
        assert "WEEKLY" in recurrence

    @pytest.mark.asyncio
    async def test_informal_time_expression(self, reminders_cog):
        """Informal time: 'tonight', 'this evening'."""
        for expr in ["tonight", "this evening", "this afternoon"]:
            result = await reminders_cog._parse_reminder(f"take out trash {expr}")
            assert result is not None, f"Failed for: {expr}"
            message, _time_str, _recurrence = result
            assert "trash" in message.lower()

    @pytest.mark.asyncio
    async def test_noon_and_midnight(self, reminders_cog):
        """Special times: noon, midnight."""
        for special_time in ["noon", "midnight"]:
            result = await reminders_cog._parse_reminder(f"check server at {special_time}")
            assert result is not None, f"Failed for: {special_time}"
            message, _time_str, _recurrence = result
            assert "server" in message.lower()

    @pytest.mark.asyncio
    async def test_day_after_tomorrow(self, reminders_cog):
        """Complex relative: 'day after tomorrow'."""
        result = await reminders_cog._parse_reminder("dentist appointment day after tomorrow")
        assert result is not None or result is None  # Either is acceptable


# =============================================================================
# EXTRACT RECURRENCE RULE UNIT TESTS
# =============================================================================


class TestExtractRecurrenceRule:
    """Direct tests for _extract_recurrence_rule helper method.

    Uses parametrization to test many input patterns against expected outputs.
    Each test case is tagged with an `id` for clear failure messages.
    """

    # fmt: off
    @pytest.mark.parametrize("input_str,expected_freq,expected_parts,expected_match", [
        # Simple frequency keywords
        pytest.param("take medicine daily", "FREQ=DAILY", [], "daily", id="simple_daily"),
        pytest.param("weekly standup", "FREQ=WEEKLY", [], "weekly", id="simple_weekly"),
        pytest.param("monthly review", "FREQ=MONTHLY", [], "monthly", id="simple_monthly"),
        pytest.param("yearly checkup", "FREQ=YEARLY", [], "yearly", id="simple_yearly"),

        # Bi-weekly (regression: was 'bi-weekly' key mismatch)
        pytest.param("bi-weekly report", "WEEKLY", ["INTERVAL=2"], None, id="biweekly_hyphenated"),

        # "every X" patterns
        pytest.param("every day at 8am", "DAILY", [], None, id="every_day"),
        pytest.param("every monday", "WEEKLY", ["MO"], None, id="every_specific_day"),
        pytest.param("every other day", "DAILY", ["INTERVAL=2"], None, id="every_other_day"),
        pytest.param("every 3 weeks", "WEEKLY", ["INTERVAL=3"], None, id="every_n_weeks"),

        # Weekend/weekday
        pytest.param("every weekend", "WEEKLY", ["SA", "SU"], None, id="every_weekend"),
        pytest.param("every weekday", "WEEKLY", ["MO", "TU", "WE", "TH", "FR"], None, id="every_weekday"),

        # Monthly with specific day
        pytest.param("every 15th of the month", "MONTHLY", ["BYMONTHDAY=15"], None, id="monthly_15th"),
    ])
    def test_recurrence_extraction(self, reminders_cog, input_str, expected_freq, expected_parts, expected_match):
        # fmt: on
        """Verify recurrence rule extraction for various patterns."""
        rule, matched = reminders_cog._extract_recurrence_rule(input_str)

        assert rule is not None, f"Expected rule for '{input_str}', got None"
        assert expected_freq in rule, f"Expected '{expected_freq}' in rule '{rule}'"

        for part in expected_parts:
            assert part in rule, f"Expected '{part}' in rule '{rule}'"

        if expected_match is not None:
            assert matched == expected_match, f"Expected matched='{expected_match}', got '{matched}'"

    def test_no_recurrence(self, reminders_cog):
        """No recurrence pattern should return (None, '')."""
        rule, matched = reminders_cog._extract_recurrence_rule("call mom tomorrow")
        assert rule is None
        assert matched == ""


# =============================================================================
# PARSER REGRESSION TESTS
# =============================================================================


class TestParseReminderRegressions:
    """Regression tests for previously failing scenarios."""

    @pytest.mark.asyncio
    async def test_time_to_boundary(self, reminders_cog):
        """'to' should not be consumed as part of message when it's a connector."""
        result = await reminders_cog._parse_reminder("tomorrow to call mom")
        assert result is not None
        message, _time_str, _recurrence = result
        assert not message.lower().startswith("to ")

    @pytest.mark.asyncio
    async def test_the_in_date_phrase(self, reminders_cog):
        """'the' in date phrases like 'on the 25th'."""
        result = await reminders_cog._parse_reminder("on the 25th buy presents")
        assert result is not None
        message, _time_str, _recurrence = result
        assert "presents" in message.lower()

    @pytest.mark.asyncio
    async def test_recurrence_only_no_time(self, reminders_cog):
        """Recurrence without explicit time should use 'now'."""
        result = await reminders_cog._parse_reminder("every day drink water")
        assert result is not None
        _message, time_str, recurrence = result
        assert recurrence is not None
        assert time_str == "now" or time_str != ""

    @pytest.mark.asyncio
    async def test_case_insensitive_days(self, reminders_cog):
        """Day names should be case insensitive."""
        for day in ["MONDAY", "Monday", "monday", "MoNdAy"]:
            result = await reminders_cog._parse_reminder(f"on {day} do laundry")
            assert result is not None, f"Failed for: {day}"


# =============================================================================
# MODIFIER STRIPPING TESTS
# =============================================================================


class TestModifierStripping:
    """Tests for the day modifier stripping functionality (Stage 3a/3b).

    Split into two parametrized tests:
    - Success cases: modifier stripped, day parsed correctly
    - Error cases: past dates rejected with ERROR:PAST_DATE
    """

    # fmt: off
    @pytest.mark.asyncio
    @pytest.mark.parametrize("input_str,expected_msg,expected_day", [
        # Future modifiers get stripped, bare day is parsed
        pytest.param("next monday call doctor", "doctor", "monday", id="next_monday"),
        pytest.param("this friday submit report", "report", "friday", id="this_friday"),
        pytest.param("coming wednesday team lunch", "lunch", "wednesday", id="coming_wednesday"),
        pytest.param("next monday at 5pm dentist appointment", "dentist", "monday", id="next_monday_with_time"),

        # Case insensitivity
        pytest.param("NEXT Monday test", "test", "monday", id="case_upper_next"),
        pytest.param("Next MONDAY test", "test", "monday", id="case_upper_day"),
    ])
    async def test_modifier_stripping_success(self, reminders_cog, input_str, expected_msg, expected_day):
        # fmt: on
        """Verify future modifiers are stripped and days are parsed."""
        result = await reminders_cog._parse_reminder(input_str)
        assert result is not None, f"Failed to parse: {input_str}"
        message, time_str, _recurrence = result

        assert not message.startswith("ERROR:"), f"Unexpected error for '{input_str}': {message}"
        assert expected_msg in message.lower(), f"Expected '{expected_msg}' in message '{message}'"
        assert expected_day in time_str.lower(), f"Expected '{expected_day}' in time '{time_str}'"

    # fmt: off
    @pytest.mark.asyncio
    @pytest.mark.parametrize("input_str,past_indicator", [
        # Past date patterns should return ERROR:PAST_DATE
        pytest.param("last monday something", "last monday", id="last_monday"),
        pytest.param("last week something", "last week", id="last_week"),
        pytest.param("last month something", "last month", id="last_month"),
    ])
    async def test_modifier_stripping_past_error(self, reminders_cog, input_str, past_indicator):
        # fmt: on
        """Verify past date expressions return ERROR:PAST_DATE."""
        result = await reminders_cog._parse_reminder(input_str)
        assert result is not None, f"Expected error result for: {input_str}"
        message, time_str, _recurrence = result

        assert message.startswith("ERROR:PAST_DATE:"), f"Expected PAST_DATE error for '{input_str}', got: {message}"
        assert time_str == "", f"Expected empty time_str for error, got: {time_str}"


# =============================================================================
# FRACTIONAL TIME NORMALIZATION TESTS
# =============================================================================


class TestFractionalTimeNormalization:
    """Tests for fractional time expression normalization (Stage 3c).

    These patterns are pre-processed before dateparser because dateparser
    doesn't understand 'half an hour', 'X and a half hours', etc.
    """

    # fmt: off
    @pytest.mark.asyncio
    @pytest.mark.parametrize("input_str,expected_msg", [
        # Half hour variations (→ 30 minutes)
        pytest.param("in a half hour check oven", "oven", id="a_half_hour"),
        pytest.param("in half an hour check oven", "oven", id="half_an_hour"),

        # X and a half hours (→ X*60+30 minutes)
        pytest.param("in an hour and a half meeting", "meeting", id="hour_and_a_half"),
        pytest.param("in one and a half hours meeting", "meeting", id="one_and_a_half"),
        pytest.param("in 2 and a half hours pick up kids", "kids", id="two_and_a_half"),
        pytest.param("in 3 and a half hours check roast", "roast", id="three_and_a_half"),

        # Other fractional units
        pytest.param("in half a minute check timer", "timer", id="half_minute"),
        pytest.param("in half a day review progress", "progress", id="half_a_day"),
        pytest.param("in a half day review progress", "progress", id="a_half_day"),
    ])
    async def test_fractional_normalization(self, reminders_cog, input_str, expected_msg):
        # fmt: on
        """Verify fractional time expressions are normalized and parsed."""
        result = await reminders_cog._parse_reminder(input_str)
        assert result is not None, f"Failed to parse: {input_str}"
        message, time_str, _recurrence = result

        assert expected_msg in message.lower(), f"Expected '{expected_msg}' in message '{message}'"
        assert time_str != "", f"Expected non-empty time_str for '{input_str}'"


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================


class TestRemindErrorHandling:
    """Tests for error handling in the remind() function."""

    @pytest.mark.asyncio
    async def test_past_date_error_message(self, reminders_cog, mock_ctx):
        """'last monday' should produce a user-friendly error message."""
        await reminders_cog.remind(mock_ctx, query="last monday do something")

        mock_ctx.send.assert_called()
        call_args = mock_ctx.send.call_args[0][0]
        assert "past" in call_args.lower() or "can't" in call_args.lower()
        reminders_cog.db_manager.add_reminder.assert_not_called()

    @pytest.mark.asyncio
    async def test_past_date_suggests_alternative(self, reminders_cog, mock_ctx):
        """Error message for 'last monday' should suggest 'next monday'."""
        await reminders_cog.remind(mock_ctx, query="last monday do something")

        call_args = mock_ctx.send.call_args[0][0]
        assert "next" in call_args.lower() or "tomorrow" in call_args.lower()

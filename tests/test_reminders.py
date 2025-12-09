import os
import sys
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.reminders import Reminders
from utils.bot_class import CoreBot
from utils.database import DatabaseManager

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    bot.loop = MagicMock()
    bot.wait_for = AsyncMock()
    return bot


@pytest.fixture
def reminders_cog(mock_bot):
    cog = Reminders(mock_bot)
    return cog


@pytest.fixture
def mock_ctx(mock_bot):
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


@pytest.mark.asyncio
async def test_remind_happy_path(reminders_cog, mock_ctx):
    """Test a standard reminder creation flow."""
    # Mock parsing success.
    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))

    # Mock timezone.
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Mock user confirmation "yes".
    # We need to configure the side_effect of the ALREADY MOCKED wait_for.
    async def wait_for_yes(*args, **kwargs):
        msg = MagicMock()
        msg.content = "yes"
        return msg
    mock_ctx.bot.wait_for.side_effect = wait_for_yes

    # Mock DB add_reminder.
    reminders_cog.db_manager.add_reminder.return_value = 1

    await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

    # Verify parsing was called.
    reminders_cog._parse_reminder.assert_called_once()

    # Verify confirmation message sent.
    assert mock_ctx.send.call_count >= 1
    args, _ = mock_ctx.send.call_args_list[-2]  # The confirmation question.
    assert "Buy milk" in args[0]

    # Verify DB call.
    reminders_cog.db_manager.add_reminder.assert_called_once()
    call_args = reminders_cog.db_manager.add_reminder.call_args
    assert call_args[0][0] == 12345  # user_id.
    assert call_args[0][3] == "Buy milk"  # message.


@pytest.mark.asyncio
async def test_remind_with_reply(reminders_cog, mock_ctx):
    """Test reminder creation with a reply context."""
    # Setup reply context.
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

    # Verify DB call includes reply_message_id.
    reminders_cog.db_manager.add_reminder.assert_called_once()
    call_args = reminders_cog.db_manager.add_reminder.call_args
    assert call_args[0][7] == 99999  # reply_message_id.


@pytest.mark.asyncio
async def test_remind_edit_time(reminders_cog, mock_ctx):
    """Test the 'edit time' flow."""
    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Sequence of user inputs:
    # 1. "edit time" (at first confirmation).
    # 2. "in 1 hour" (new time input in interactive flow).
    # 3. "yes" (final confirmation).

    # Mock wait_for to return different values in sequence.
    msg_edit = MagicMock()
    msg_edit.content = "edit time"

    msg_time = MagicMock()
    msg_time.content = "in 1 hour"

    msg_yes = MagicMock()
    msg_yes.content = "yes"

    # Use an iterator for the side effect.
    msgs = iter([msg_edit, msg_time, msg_yes])

    async def wait_for_sequence(*args, **kwargs):
        return next(msgs)

    mock_ctx.bot.wait_for.side_effect = wait_for_sequence

    await reminders_cog.remind(mock_ctx, query="remind me to Buy milk in 10 minutes")

    # Verify DB call uses the NEW time (roughly).
    reminders_cog.db_manager.add_reminder.assert_called_once()
    # We can't easily check the exact timestamp, but we can check the flow completed.
    # The exact call count depends on implementation details, but it should be at least 3.
    assert mock_ctx.bot.wait_for.call_count >= 3


@pytest.mark.asyncio
async def test_remind_edit_message(reminders_cog, mock_ctx):
    """Test the 'edit message' flow."""
    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Sequence:
    # 1. "edit message" (at first confirmation).
    # 2. "Buy cookies" (new message input).
    # 3. "yes" (final confirmation).
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

    # Verify DB call uses the NEW message.
    reminders_cog.db_manager.add_reminder.assert_called_once()
    call_args = reminders_cog.db_manager.add_reminder.call_args
    assert call_args[0][3] == "Buy cookies"


@pytest.mark.asyncio
async def test_remind_full_edit(reminders_cog, mock_ctx):
    """Test the 'edit' (full reset) flow."""
    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Sequence:
    # 1. "edit" (at first confirmation).
    # 2. "Buy cookies" (new message input).
    # 3. "tomorrow" (new time input).
    # 4. "yes" (final confirmation).
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
async def test_edit_preserves_reply_context(reminders_cog, mock_ctx):
    """Test that reply context survives an 'edit time' loop."""
    mock_ctx.message.reference = MagicMock()
    mock_ctx.message.reference.message_id = 88888

    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "in 10 minutes", None))
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Sequence:
    # 1. "edit time" (at first confirmation).
    # 2. "in 1 hour" (new time input).
    # 3. "yes" (final confirmation).
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

    # Verify DB call STILL has the reply ID.
    reminders_cog.db_manager.add_reminder.assert_called_once()
    call_args = reminders_cog.db_manager.add_reminder.call_args
    assert call_args[0][7] == 88888


@pytest.mark.asyncio
async def test_interactive_flow_exclusive(reminders_cog, mock_ctx):
    """Test calling the interactive flow directly (empty query)."""
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Sequence:
    # 1. "Buy milk" (message input).
    # 2. "in 10 mins" (time input).
    # 3. "yes" (final confirmation).
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
async def test_remind_cancel(reminders_cog, mock_ctx):
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
async def test_remind_recurrence(reminders_cog, mock_ctx):
    """Test saving a recurring reminder."""
    # Mock parsing returning a recurrence rule.
    # We use "at 8am" instead of "every day at 8am" because _parse_reminder usually strips the recurrence part
    # and passing "every day" to dateparser might confuse it or cause it to fail, triggering the interactive loop.
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
    assert call_args[0][5] is True  # is_recurring.
    assert call_args[0][6] == "FREQ=DAILY"  # recurrence_rule.


@pytest.mark.asyncio
async def test_remind_parse_failure(reminders_cog, mock_ctx):
    """Test that parsing failure triggers a clean interactive flow."""
    # Mock parsing failure (returns None or empty tuple).
    reminders_cog._parse_reminder = AsyncMock(return_value=None)
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Since it fails, it should ask for message, then time, then confirm.
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

    # Verify it entered interactive flow and saved.
    reminders_cog.db_manager.add_reminder.assert_called_once()
    assert reminders_cog.db_manager.add_reminder.call_args[0][3] == "Buy milk"


@pytest.mark.asyncio
async def test_remind_past_date(reminders_cog, mock_ctx):
    """Test that past dates are rejected and context is kept."""
    reminders_cog._parse_reminder = AsyncMock(return_value=("Buy milk", "yesterday", None))
    reminders_cog._get_user_timezone = AsyncMock(return_value="UTC")

    # Mock dateparser to return a past date.
    past_date = datetime.fromtimestamp(time.time() - 10000)

    # We need to patch dateparser.parse specifically for the _parse_reminder result handling.
    # But since we are mocking _parse_reminder, the logic inside remind() calls dateparser.
    # We can rely on the fact that the code checks timestamp <= time.time().

    # Sequence:
    # 1. Code detects past date -> calls _interactive_reminder_flow with initial_message="Buy milk".
    # 2. Interactive flow asks for TIME (skipping message).
    # 3. User provides valid future time.
    # 4. Confirmation.

    msg_time = MagicMock()
    msg_time.content = "in 10 mins"

    msg_yes = MagicMock()
    msg_yes.content = "yes"

    msgs = iter([msg_time, msg_yes])

    async def wait_for_sequence(*args, **kwargs):
        return next(msgs)

    mock_ctx.bot.wait_for.side_effect = wait_for_sequence

    # We need to ensure the FIRST dateparser call (in remind) returns past,
    # and SECOND (in interactive) returns future.
    # However, remind() uses asyncio.to_thread(dateparser.parse).

    with patch('dateparser.parse') as mock_parse:
        # First call (in remind): Past date.
        # Second call (in interactive): Future date.
        future_date = datetime.fromtimestamp(time.time() + 10000)
        mock_parse.side_effect = [past_date, future_date]

        await reminders_cog.remind(mock_ctx, query="remind me to Buy milk yesterday")

        # Verify DB saved with "Buy milk" (context kept).
        reminders_cog.db_manager.add_reminder.assert_called_once()
        assert reminders_cog.db_manager.add_reminder.call_args[0][3] == "Buy milk"

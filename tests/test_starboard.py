import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.starboard import Starboard
from utils.bot_class import CoreBot
from utils.database import DatabaseManager

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    return bot


@pytest.fixture
def starboard_cog(mock_bot):
    # Patch aiohttp.ClientSession to avoid actual network calls
    with patch('aiohttp.ClientSession'):
        cog = Starboard(mock_bot)
        cog.http_session = AsyncMock()
        # Mock the get method context manager
        get_mock = AsyncMock()
        get_mock.__aenter__.return_value.status = 200
        get_mock.__aenter__.return_value.read.return_value = b'fake_image_data'
        cog.http_session.get.return_value = get_mock
        return cog


@pytest.mark.asyncio
async def test_fix_and_remake_starboard(starboard_cog, mock_bot):
    # Setup Guild
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1001
    guild.name = "Test Guild"

    # Setup Channels
    starboard_channel = MagicMock(spec=discord.TextChannel)
    starboard_channel.id = 2000
    starboard_channel.guild = guild
    starboard_channel.mention = "<#2000>"

    # Original Channels
    channels = []
    for i in range(1, 10):
        ch = MagicMock(spec=discord.TextChannel)
        ch.id = 3000 + i
        ch.guild = guild
        ch.mention = f"<#{ch.id}>"
        channels.append(ch)

    # Mock bot.get_channel to return correct channels
    def get_channel_side_effect(channel_id):
        if channel_id == starboard_channel.id:
            return starboard_channel
        for ch in channels:
            if ch.id == channel_id:
                return ch
        return None

    mock_bot.get_channel.side_effect = get_channel_side_effect

    # Mock bot.get_guild
    mock_bot.get_guild.return_value = guild

    # --- Setup DB Entries ---
    # 1. Perfect Entry
    entry1 = {
        'original_message_id': 4001,
        'starboard_message_id': 5001,
        'guild_id': guild.id,
        'original_channel_id': channels[0].id,
        'starboard_reply_id': 6001
    }

    # 2. Missing Guild ID (Recoverable from SB Msg)
    entry2 = {
        'original_message_id': 4002,
        'starboard_message_id': 5002,
        'guild_id': None,  # MISSING
        'original_channel_id': channels[1].id,
        'starboard_reply_id': None
    }

    # 3. Missing Original Channel ID (Recoverable from SB Msg)
    entry3 = {
        'original_message_id': 4003,
        'starboard_message_id': 5003,
        'guild_id': guild.id,
        'original_channel_id': None,  # MISSING
        'starboard_reply_id': None
    }

    # 4. Missing Starboard Reply ID (Recoverable from SB Msg Reference)
    entry4 = {
        'original_message_id': 4004,
        'starboard_message_id': 5004,
        'guild_id': guild.id,
        'original_channel_id': channels[3].id,
        'starboard_reply_id': None  # MISSING
    }

    # 5. Missing Starboard Message ID (Needs Repost)
    entry5 = {
        'original_message_id': 4005,
        'starboard_message_id': None,  # MISSING
        'guild_id': guild.id,
        'original_channel_id': channels[4].id,
        'starboard_reply_id': None
    }

    # 6. Missing Original Message ID (Recoverable from SB Msg)
    entry6 = {
        'original_message_id': None,  # MISSING
        'starboard_message_id': 5006,
        'guild_id': guild.id,
        'original_channel_id': channels[5].id,
        'starboard_reply_id': None
    }

    # 7. Lost Original Message (Needs Tombstone)
    # Has no SB msg, and Original is deleted.
    entry7 = {
        'original_message_id': 4007,
        'starboard_message_id': None,
        'guild_id': guild.id,
        'original_channel_id': channels[6].id,
        'starboard_reply_id': None
    }

    # 8. Valid Entry but Original Deleted (Needs Tombstone)
    entry8 = {
        'original_message_id': 4008,
        'starboard_message_id': 5008,
        'guild_id': guild.id,
        'original_channel_id': channels[7].id,
        'starboard_reply_id': None
    }

    all_entries = [entry1, entry2, entry3, entry4, entry5, entry6, entry7, entry8]

    # Mock DB responses
    mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
        "starboard_channel_id": str(starboard_channel.id),
        "starboard_emoji": "⭐",
        "starboard_threshold": "3"
    }.get(key)

    mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = all_entries

    # Mock update_starboard_entry to actually update our dicts
    async def update_entry_side_effect(entry):
        pass
    mock_bot.db_manager.update_starboard_entry.side_effect = update_entry_side_effect

    # Mock clear_starboard_for_guild
    async def clear_side_effect(g_id):
        pass
    mock_bot.db_manager.clear_starboard_for_guild.side_effect = clear_side_effect

    # Mock get_starboard_entry
    async def get_entry_side_effect(msg_id):
        for e in all_entries:
            if e['original_message_id'] == msg_id:
                return e
        return None
    mock_bot.db_manager.get_starboard_entry.side_effect = get_entry_side_effect

    # --- Mock Messages ---

    def create_mock_message(msg_id, channel, content="Test Content", author_id=123, embeds=None):
        msg = MagicMock(spec=discord.Message)
        msg.id = msg_id
        msg.channel = channel
        msg.guild = guild
        msg.content = content
        msg.author.id = author_id
        msg.author.display_name = "TestUser"
        msg.author.name = "testuser"
        msg.author.display_avatar.url = "http://avatar.url"
        msg.created_at = discord.utils.utcnow()
        msg.jump_url = f"https://discord.com/channels/{guild.id}/{channel.id}/{msg_id}"
        msg.embeds = embeds or []
        msg.attachments = []
        msg.reactions = []
        msg.reference = None
        msg.message_snapshots = []
        return msg

    # Original Messages
    orig_msg1 = create_mock_message(4001, channels[0])
    orig_msg2 = create_mock_message(4002, channels[1])
    orig_msg3 = create_mock_message(4003, channels[2])
    orig_msg4 = create_mock_message(4004, channels[3])
    orig_msg5 = create_mock_message(4005, channels[4])
    orig_msg6 = create_mock_message(4006, channels[5])
    # orig_msg7 is MISSING (Deleted)

    # Add reactions meeting threshold
    star_reaction = MagicMock()
    star_reaction.emoji = "⭐"
    star_reaction.count = 5

    for m in [orig_msg1, orig_msg2, orig_msg3, orig_msg4, orig_msg5, orig_msg6]:
        m.reactions = [star_reaction]

    # Starboard Messages
    def create_sb_embed(orig_msg):
        embed = MagicMock(spec=discord.Embed)
        field = MagicMock()
        field.name = 'Original Message'
        field.value = f"[Jump to Message]({orig_msg.jump_url})"
        embed.fields = [field]
        embed.description = orig_msg.content
        embed.image = None
        return embed

    sb_msg1 = create_mock_message(5001, starboard_channel)
    sb_msg1.embeds = [create_sb_embed(orig_msg1)]
    sb_msg1.reference = MagicMock()
    sb_msg1.reference.message_id = 6001

    sb_msg2 = create_mock_message(5002, starboard_channel)
    sb_msg2.embeds = [create_sb_embed(orig_msg2)]

    sb_msg3 = create_mock_message(5003, starboard_channel)
    sb_msg3.embeds = [create_sb_embed(orig_msg3)]

    sb_msg4 = create_mock_message(5004, starboard_channel)
    sb_msg4.embeds = [create_sb_embed(orig_msg4)]
    sb_msg4.reference = MagicMock()
    sb_msg4.reference.message_id = 6004  # Found reply ID

    sb_msg6 = create_mock_message(5006, starboard_channel)
    sb_msg6.embeds = [create_sb_embed(orig_msg6)]

    sb_msg8 = create_mock_message(5008, starboard_channel)
    # Fake original for embed creation only
    fake_orig_8 = MagicMock()
    fake_orig_8.jump_url = "http://jump.url/4008"
    fake_orig_8.content = "Deleted Content"
    sb_msg8.embeds = [create_sb_embed(fake_orig_8)]

    # Mock fetch_message
    async def fetch_message_side_effect(msg_id):
        if msg_id is None:
            raise TypeError("fetch_message ID cannot be None")

        msgs = {
            4001: orig_msg1, 5001: sb_msg1,
            4002: orig_msg2, 5002: sb_msg2,
            4003: orig_msg3, 5003: sb_msg3,
            4004: orig_msg4, 5004: sb_msg4,
            4005: orig_msg5,  # No SB msg
            4006: orig_msg6, 5006: sb_msg6,
            # 4007 is missing
            5008: sb_msg8,
            # 4008 is missing
        }
        if msg_id in msgs:
            return msgs[msg_id]
        raise discord.NotFound(MagicMock(), "Message not found")

    starboard_channel.fetch_message.side_effect = fetch_message_side_effect
    for ch in channels:
        ch.fetch_message.side_effect = fetch_message_side_effect

    # Mock guild.channels
    guild.channels = channels + [starboard_channel]

    # --- Test FIX Command ---
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author.id = 999
    ctx.send = AsyncMock()

    # Bypass command wrapper and call implementation directly
    starboard_cog._fast_mode = True

    # Mock post_to_starboard to simulate creating a new post
    async def post_to_starboard_side_effect(message, channel_id, emoji, count):
        # Simulate DB update that happens inside create_new_starboard_post
        # We just need to ensure it was called.
        pass
    starboard_cog.post_to_starboard = AsyncMock(side_effect=post_to_starboard_side_effect)

    # Mock _create_tombstone
    async def create_tombstone_side_effect(channel, orig_id):
        tomb = MagicMock(spec=discord.Message)
        tomb.id = 9999
        return tomb
    starboard_cog._create_tombstone = AsyncMock(side_effect=create_tombstone_side_effect)

    await starboard_cog._fix_impl(ctx)

    # Assertions for FIX

    # Entry 2 (Missing Guild ID) - Recovered from SB Msg Embed
    assert entry2['guild_id'] == guild.id, "Entry 2 Guild ID not recovered"

    # Entry 3 (Missing Original Channel ID) - Recovered from SB Msg Embed
    assert entry3['original_channel_id'] == channels[2].id, "Entry 3 Original Channel ID not recovered"

    # Entry 4 (Missing Reply ID) - Recovered from SB Msg Reference
    assert entry4['starboard_reply_id'] == 6004, "Entry 4 Reply ID not recovered"

    # Entry 6 (Missing Original Message ID) - Recovered from SB Msg Embed
    assert entry6['original_message_id'] == 4006, "Entry 6 Original Message ID not recovered"

    # Entry 5 (Missing SB Msg) - Should have triggered post_to_starboard
    # We check if post_to_starboard was called with orig_msg5
    starboard_cog.post_to_starboard.assert_any_call(orig_msg5, starboard_channel.id, "⭐", 5)

    # Entry 7 (Lost Original) - Should have triggered Tombstone
    starboard_cog._create_tombstone.assert_any_call(starboard_channel, 4007)
    assert entry7['starboard_message_id'] == 9999, "Entry 7 Tombstone ID not set"

    # Entry 8 (Valid Entry but Original Deleted) - Fix also catches this
    starboard_cog._create_tombstone.assert_any_call(starboard_channel, 4008)
    assert entry8['starboard_message_id'] == 9999, "Entry 8 Tombstone ID not set"

    # --- Test REMAKE Command ---
    # Reset mocks
    starboard_channel.reset_mock()
    starboard_channel.fetch_message.side_effect = fetch_message_side_effect
    starboard_cog.post_to_starboard.reset_mock()
    starboard_cog._create_tombstone.reset_mock()

    # Mock send to return a message with an ID so add_starboard_entry works
    async def send_side_effect(*args, **kwargs):
        msg = MagicMock(spec=discord.Message)
        msg.id = 8888
        return msg
    starboard_channel.send.side_effect = send_side_effect

    # Ensure all entries are now "valid" for remake (have all 4 fields)
    # Fix command should have populated them, but let's ensure they are set for the test logic
    entry5['starboard_message_id'] = 5005  # Simulate fix success (new post)

    # Entry 7 is now valid (has tombstone ID 9999)

    # Now run remake
    await starboard_cog._remake_impl(ctx)

    # Remake Logic:
    # 1. Wipes channel (deletes existing SB msgs)
    # 2. Clears DB
    # 3. Recreates posts

    # Check Deletions
    # Should try to delete 5001, 5002, 5003, 5004, 5005, 5006, 9999
    # Note: 5005 was "created" by fix, so it's in the list now.
    # 9999 was the tombstone from fix.

    # Check Recreations
    # Should recreate: 4001, 4002, 4003, 4004, 4005, 4006
    # 4007 is missing -> Should create Tombstone AGAIN (since we wiped channel)

    assert starboard_cog.post_to_starboard.call_count >= 6, f"Expected 6 recreations, got {starboard_cog.post_to_starboard.call_count}"

    # Check Tombstone for 4007
    starboard_cog._create_tombstone.assert_any_call(starboard_channel, 4007)

    # Check Tombstone for 4008 (Remake should catch the deleted original)
    starboard_cog._create_tombstone.assert_any_call(starboard_channel, 4008)


@pytest.mark.asyncio
async def test_starboard_channel_migration(starboard_cog, mock_bot):
    """Test that remake detects a channel change and performs non-destructive migration."""

    # Setup Guild
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1001
    guild.name = "Test Guild"

    # Setup OLD Starboard Channel (where messages currently exist)
    old_starboard_channel = MagicMock(spec=discord.TextChannel)
    old_starboard_channel.id = 2000
    old_starboard_channel.guild = guild
    old_starboard_channel.mention = "<#2000>"

    # Setup NEW Starboard Channel (configured channel)
    new_starboard_channel = MagicMock(spec=discord.TextChannel)
    new_starboard_channel.id = 2001
    new_starboard_channel.guild = guild
    new_starboard_channel.mention = "<#2001>"

    # Original Channel
    original_channel = MagicMock(spec=discord.TextChannel)
    original_channel.id = 3001
    original_channel.guild = guild
    original_channel.mention = "<#3001>"

    # Mock bot.get_channel
    def get_channel_side_effect(channel_id):
        if channel_id == old_starboard_channel.id:
            return old_starboard_channel
        if channel_id == new_starboard_channel.id:
            return new_starboard_channel
        if channel_id == original_channel.id:
            return original_channel
        return None

    mock_bot.get_channel.side_effect = get_channel_side_effect

    # Mock guild.text_channels for migration detection
    guild.text_channels = [old_starboard_channel, new_starboard_channel, original_channel]

    # --- Setup DB Entries (pointing to OLD channel's message IDs) ---
    entry1 = {
        'original_message_id': 4001,
        'starboard_message_id': 5001,  # This is in OLD channel
        'guild_id': guild.id,
        'original_channel_id': original_channel.id,
        'starboard_reply_id': None
    }

    entry2 = {
        'original_message_id': 4002,
        'starboard_message_id': 5002,  # This is in OLD channel
        'guild_id': guild.id,
        'original_channel_id': original_channel.id,
        'starboard_reply_id': 6002  # Has reply context
    }

    # Entry 3: Message no longer meets threshold (should be skipped/removed)
    entry3 = {
        'original_message_id': 4003,
        'starboard_message_id': 5003,
        'guild_id': guild.id,
        'original_channel_id': original_channel.id,
        'starboard_reply_id': None
    }

    # Entry 4: Original message deleted (should become tombstone)
    entry4 = {
        'original_message_id': 4004,
        'starboard_message_id': 5004,
        'guild_id': guild.id,
        'original_channel_id': original_channel.id,
        'starboard_reply_id': None
    }

    all_entries = [entry1, entry2, entry3, entry4]

    # Mock DB: Config points to NEW channel
    mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
        "starboard_channel_id": str(new_starboard_channel.id),  # NEW channel!
        "starboard_emoji": "⭐",
        "starboard_threshold": "3"
    }.get(key)

    mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = all_entries

    # Track DB updates
    updated_entries = []

    async def update_entry_side_effect(entry):
        updated_entries.append(dict(entry))
    mock_bot.db_manager.update_starboard_entry.side_effect = update_entry_side_effect

    # Track removed entries
    removed_entries = []

    async def remove_entry_side_effect(orig_id):
        removed_entries.append(orig_id)
    mock_bot.db_manager.remove_starboard_entry.side_effect = remove_entry_side_effect

    # --- Mock Messages ---
    def create_mock_message(msg_id, channel, content="Test Content"):
        msg = MagicMock(spec=discord.Message)
        msg.id = msg_id
        msg.channel = channel
        msg.guild = guild
        msg.content = content
        msg.author.id = 123
        msg.author.display_name = "TestUser"
        msg.author.name = "testuser"
        msg.author.display_avatar.url = "http://avatar.url"
        msg.created_at = discord.utils.utcnow()
        msg.jump_url = f"https://discord.com/channels/{guild.id}/{channel.id}/{msg_id}"
        msg.embeds = []
        msg.attachments = []
        msg.reactions = []
        msg.reference = None
        msg.message_snapshots = []
        return msg

    # Original Messages
    orig_msg1 = create_mock_message(4001, original_channel)
    orig_msg2 = create_mock_message(4002, original_channel)
    orig_msg3 = create_mock_message(4003, original_channel)  # Under threshold
    # orig_msg4 is DELETED

    # Add reactions
    star_reaction_high = MagicMock()
    star_reaction_high.emoji = "⭐"
    star_reaction_high.count = 5

    star_reaction_low = MagicMock()
    star_reaction_low.emoji = "⭐"
    star_reaction_low.count = 2  # Below threshold of 3

    orig_msg1.reactions = [star_reaction_high]
    orig_msg2.reactions = [star_reaction_high]
    orig_msg3.reactions = [star_reaction_low]  # Under threshold!

    # Old starboard messages (in OLD channel)
    old_sb_msg1 = create_mock_message(5001, old_starboard_channel)
    old_sb_msg2 = create_mock_message(5002, old_starboard_channel)
    old_sb_msg3 = create_mock_message(5003, old_starboard_channel)
    old_sb_msg4 = create_mock_message(5004, old_starboard_channel)

    # Mock fetch_message for OLD channel (where messages exist)
    async def old_channel_fetch(msg_id):
        msgs = {5001: old_sb_msg1, 5002: old_sb_msg2, 5003: old_sb_msg3, 5004: old_sb_msg4}
        if msg_id in msgs:
            return msgs[msg_id]
        raise discord.NotFound(MagicMock(), "Message not found")
    old_starboard_channel.fetch_message.side_effect = old_channel_fetch

    # Mock fetch_message for NEW channel (messages DON'T exist here yet)
    async def new_channel_fetch(msg_id):
        # Nothing exists in new channel yet - this triggers migration detection
        raise discord.NotFound(MagicMock(), "Message not found")
    new_starboard_channel.fetch_message.side_effect = new_channel_fetch

    # Mock fetch_message for original channel
    async def original_channel_fetch(msg_id):
        msgs = {4001: orig_msg1, 4002: orig_msg2, 4003: orig_msg3}
        if msg_id in msgs:
            return msgs[msg_id]
        raise discord.NotFound(MagicMock(), "Message not found")
    original_channel.fetch_message.side_effect = original_channel_fetch

    # Mock send for new channel (returns new message IDs)
    new_msg_counter = [7000]

    async def new_channel_send(*args, **kwargs):
        new_msg_counter[0] += 1
        msg = MagicMock(spec=discord.Message)
        msg.id = new_msg_counter[0]
        msg.reply = AsyncMock(side_effect=new_channel_send)
        return msg
    new_starboard_channel.send = AsyncMock(side_effect=new_channel_send)

    # --- Setup Context ---
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author.id = 999
    ctx.send = AsyncMock()

    # Mock _create_tombstone
    async def create_tombstone_side_effect(channel, orig_id):
        tomb = MagicMock(spec=discord.Message)
        tomb.id = 9999
        return tomb
    starboard_cog._create_tombstone = AsyncMock(side_effect=create_tombstone_side_effect)

    # Run remake (should detect migration)
    await starboard_cog._remake_impl(ctx)

    # --- Assertions ---

    # 1. Migration should have been detected (check for migration message)
    migration_detected = any(
        "migration detected" in str(call).lower()
        for call in ctx.send.call_args_list
    )
    assert migration_detected, "Migration should have been detected"

    # 2. Old channel messages should NOT have been deleted
    old_starboard_channel.fetch_message.assert_called()  # Only for detection
    # Check that delete() was never called on old messages
    for msg in [old_sb_msg1, old_sb_msg2, old_sb_msg3, old_sb_msg4]:
        msg.delete.assert_not_called()

    # 3. New messages should have been created in the new channel
    assert new_starboard_channel.send.call_count >= 2, \
        f"Expected at least 2 new posts, got {new_starboard_channel.send.call_count}"

    # 4. DB entries should have been UPDATED (not cleared and re-added)
    # Entry 1 and 2 should be updated with new starboard_message_id
    mock_bot.db_manager.clear_starboard_for_guild.assert_not_called()

    # Check that entries were updated
    assert len(updated_entries) >= 2, f"Expected at least 2 updates, got {len(updated_entries)}"

    # Find the updated entry1 and verify it has a new starboard_message_id
    updated_entry1 = next((e for e in updated_entries if e['original_message_id'] == 4001), None)
    assert updated_entry1 is not None, "Entry 1 should have been updated"
    assert updated_entry1['starboard_message_id'] != 5001, \
        f"Entry 1 starboard_message_id should have changed from 5001, got {updated_entry1['starboard_message_id']}"

    # 5. Entry 3 (under threshold) should have been removed from DB
    assert 4003 in removed_entries, "Entry 3 should have been removed (under threshold)"

    # 6. Entry 4 (deleted original) should have a tombstone
    starboard_cog._create_tombstone.assert_any_call(new_starboard_channel, 4004)
    updated_entry4 = next((e for e in updated_entries if e['original_message_id'] == 4004), None)
    assert updated_entry4 is not None, "Entry 4 should have been updated with tombstone"
    assert updated_entry4['starboard_message_id'] == 9999, "Entry 4 should have tombstone ID"

    # 7. Completion message should indicate migration success
    completion_msg = any(
        "migration complete" in str(call).lower()
        for call in ctx.send.call_args_list
    )
    assert completion_msg, "Migration completion message should have been sent"


@pytest.mark.asyncio
async def test_same_channel_remake_is_destructive(starboard_cog, mock_bot):
    """Test that remake on the SAME channel performs destructive remake (deletes old messages)."""

    # Setup Guild
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1001
    guild.name = "Test Guild"

    # Setup Starboard Channel (same channel for both config and existing messages)
    starboard_channel = MagicMock(spec=discord.TextChannel)
    starboard_channel.id = 2000
    starboard_channel.guild = guild
    starboard_channel.mention = "<#2000>"

    # Original Channel
    original_channel = MagicMock(spec=discord.TextChannel)
    original_channel.id = 3001
    original_channel.guild = guild

    # Mock bot.get_channel
    def get_channel_side_effect(channel_id):
        if channel_id == starboard_channel.id:
            return starboard_channel
        if channel_id == original_channel.id:
            return original_channel
        return None

    mock_bot.get_channel.side_effect = get_channel_side_effect

    # Mock guild.text_channels
    guild.text_channels = [starboard_channel, original_channel]

    # --- Setup DB Entry ---
    entry1 = {
        'original_message_id': 4001,
        'starboard_message_id': 5001,
        'guild_id': guild.id,
        'original_channel_id': original_channel.id,
        'starboard_reply_id': None
    }

    all_entries = [entry1]

    # Mock DB: Config points to SAME channel as existing messages
    mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
        "starboard_channel_id": str(starboard_channel.id),
        "starboard_emoji": "⭐",
        "starboard_threshold": "3"
    }.get(key)

    mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = all_entries

    # --- Mock Messages ---
    def create_mock_message(msg_id, channel):
        msg = MagicMock(spec=discord.Message)
        msg.id = msg_id
        msg.channel = channel
        msg.guild = guild
        msg.content = "Test"
        msg.author.id = 123
        msg.author.display_name = "TestUser"
        msg.author.name = "testuser"
        msg.author.display_avatar.url = "http://avatar.url"
        msg.created_at = discord.utils.utcnow()
        msg.jump_url = f"https://discord.com/channels/{guild.id}/{channel.id}/{msg_id}"
        msg.embeds = []
        msg.attachments = []
        msg.reactions = []
        msg.reference = None
        msg.message_snapshots = []
        msg.delete = AsyncMock()
        return msg

    orig_msg1 = create_mock_message(4001, original_channel)
    star_reaction = MagicMock()
    star_reaction.emoji = "⭐"
    star_reaction.count = 5
    orig_msg1.reactions = [star_reaction]

    sb_msg1 = create_mock_message(5001, starboard_channel)

    # Mock fetch_message - messages exist in SAME channel
    async def sb_channel_fetch(msg_id):
        if msg_id == 5001:
            return sb_msg1
        raise discord.NotFound(MagicMock(), "Message not found")
    starboard_channel.fetch_message.side_effect = sb_channel_fetch

    async def orig_channel_fetch(msg_id):
        if msg_id == 4001:
            return orig_msg1
        raise discord.NotFound(MagicMock(), "Message not found")
    original_channel.fetch_message.side_effect = orig_channel_fetch

    # Mock send for recreation
    async def channel_send(*args, **kwargs):
        msg = MagicMock(spec=discord.Message)
        msg.id = 8888
        return msg
    starboard_channel.send = AsyncMock(side_effect=channel_send)

    # --- Setup Context ---
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author.id = 999
    ctx.send = AsyncMock()

    starboard_cog._fast_mode = True

    # Mock post_to_starboard
    starboard_cog.post_to_starboard = AsyncMock()

    # Run remake
    await starboard_cog._remake_impl(ctx)

    # --- Assertions ---

    # 1. Should NOT detect migration
    migration_detected = any(
        "migration detected" in str(call).lower()
        for call in ctx.send.call_args_list
    )
    assert not migration_detected, "Should NOT have detected migration for same channel"

    # 2. Old message SHOULD be deleted (destructive remake)
    sb_msg1.delete.assert_called_once()

    # 3. DB should have been cleared
    mock_bot.db_manager.clear_starboard_for_guild.assert_called_once_with(guild.id)

    # 4. Post should have been recreated
    starboard_cog.post_to_starboard.assert_called()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

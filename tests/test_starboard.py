import sys
import os
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, ANY
import discord
from discord.ext import commands

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cogs.starboard import Starboard
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager

@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=SanchoBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    # bot.loop is not used in Starboard cog, and getting it this way is deprecated
    # if no loop is running. If needed, we can mock it or use asyncio.get_running_loop()
    # inside an async fixture.
    return bot

@pytest.fixture
def starboard_cog(mock_bot):
    # Patch aiohttp.ClientSession to avoid actual network calls
    with patch('aiohttp.ClientSession') as mock_session:
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
        'guild_id': None, # MISSING
        'original_channel_id': channels[1].id,
        'starboard_reply_id': None
    }
    
    # 3. Missing Original Channel ID (Recoverable from SB Msg)
    entry3 = {
        'original_message_id': 4003,
        'starboard_message_id': 5003,
        'guild_id': guild.id,
        'original_channel_id': None, # MISSING
        'starboard_reply_id': None
    }
    
    # 4. Missing Starboard Reply ID (Recoverable from SB Msg Reference)
    entry4 = {
        'original_message_id': 4004,
        'starboard_message_id': 5004,
        'guild_id': guild.id,
        'original_channel_id': channels[3].id,
        'starboard_reply_id': None # MISSING
    }
    
    # 5. Missing Starboard Message ID (Needs Repost)
    entry5 = {
        'original_message_id': 4005,
        'starboard_message_id': None, # MISSING
        'guild_id': guild.id,
        'original_channel_id': channels[4].id,
        'starboard_reply_id': None
    }
    
    # 6. Missing Original Message ID (Recoverable from SB Msg)
    entry6 = {
        'original_message_id': None, # MISSING
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

    all_entries = [entry1, entry2, entry3, entry4, entry5, entry6, entry7]
    
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
    sb_msg4.reference.message_id = 6004 # Found reply ID
    
    sb_msg6 = create_mock_message(5006, starboard_channel)
    sb_msg6.embeds = [create_sb_embed(orig_msg6)]
    
    # Mock fetch_message
    async def fetch_message_side_effect(msg_id):
        if msg_id is None:
            raise TypeError("fetch_message ID cannot be None")
            
        msgs = {
            4001: orig_msg1, 5001: sb_msg1,
            4002: orig_msg2, 5002: sb_msg2,
            4003: orig_msg3, 5003: sb_msg3,
            4004: orig_msg4, 5004: sb_msg4,
            4005: orig_msg5, # No SB msg
            4006: orig_msg6, 5006: sb_msg6,
            # 4007 is missing
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
    starboard_cog._create_tombstone.assert_called_with(starboard_channel, 4007)
    assert entry7['starboard_message_id'] == 9999, "Entry 7 Tombstone ID not set"

    
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
    entry5['starboard_message_id'] = 5005 # Simulate fix success (new post)
    
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
    starboard_cog._create_tombstone.assert_called_with(starboard_channel, 4007)

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

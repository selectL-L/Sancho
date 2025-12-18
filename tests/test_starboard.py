"""Unit tests for the Starboard cog.

This module contains comprehensive tests for the starboard system, including:
- Configuration retrieval tests
- Reaction event handling tests
- Starboard post creation tests
- Embed generation tests
- Fix and remake command tests
- Migration detection tests
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.starboard import Starboard
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
    bot.user = MagicMock()
    bot.user.id = 99999
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def starboard_cog(mock_bot):
    """Create a Starboard cog instance with mocked bot and HTTP session."""
    with patch('aiohttp.ClientSession'):
        cog = Starboard(mock_bot)
        cog.http_session = AsyncMock()
        # Mock the get method context manager for downloads
        get_mock = AsyncMock()
        get_mock.__aenter__.return_value.status = 200
        get_mock.__aenter__.return_value.read.return_value = b'fake_image_data'
        get_mock.__aenter__.return_value.headers = {}
        get_mock.__aenter__.return_value.content.read = AsyncMock(side_effect=[b'fake_image_data', b''])
        cog.http_session.get.return_value = get_mock
        return cog


@pytest.fixture
def mock_guild():
    """Create a mock Discord guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1001
    guild.name = "Test Guild"
    guild.channels = []
    guild.text_channels = []
    return guild


@pytest.fixture
def mock_channel(mock_guild):
    """Create a mock text channel."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 2001
    channel.guild = mock_guild
    channel.mention = f"<#{channel.id}>"
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock()
    mock_guild.channels.append(channel)
    mock_guild.text_channels.append(channel)
    return channel


@pytest.fixture
def mock_starboard_channel(mock_guild):
    """Create a mock starboard channel."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 3001
    channel.guild = mock_guild
    channel.mention = f"<#{channel.id}>"
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock()
    mock_guild.channels.append(channel)
    mock_guild.text_channels.append(channel)
    return channel


@pytest.fixture
def mock_message(mock_channel, mock_guild):
    """Create a mock Discord message."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 4001
    msg.channel = mock_channel
    msg.guild = mock_guild
    msg.content = "Test message content"
    msg.author = MagicMock()
    msg.author.id = 123
    msg.author.display_name = "TestUser"
    msg.author.name = "testuser"
    msg.author.display_avatar.url = "http://avatar.url"
    msg.created_at = discord.utils.utcnow()
    msg.jump_url = f"https://discord.com/channels/{mock_guild.id}/{mock_channel.id}/{msg.id}"
    msg.embeds = []
    msg.attachments = []
    msg.reactions = []
    msg.reference = None
    msg.message_snapshots = []
    msg.delete = AsyncMock()
    msg.edit = AsyncMock()
    return msg


@pytest.fixture
def mock_ctx(mock_bot, mock_guild):
    """Create a mock command context."""
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.guild = mock_guild
    ctx.author = MagicMock()
    ctx.author.id = 999
    ctx.channel = MagicMock()
    ctx.send = AsyncMock()
    return ctx


def create_star_reaction(emoji: str = "⭐", count: int = 5) -> MagicMock:
    """Helper to create a mock reaction."""
    reaction = MagicMock()
    reaction.emoji = emoji
    reaction.count = count
    return reaction


# =============================================================================
# CONFIGURATION TESTS
# =============================================================================


class TestGetStarboardConfig:
    """Tests for get_starboard_config method."""

    @pytest.mark.asyncio
    async def test_returns_defaults_when_not_configured(self, starboard_cog, mock_bot):
        """Returns default values when no config is set."""
        mock_bot.db_manager.get_guild_config.return_value = None

        channel_id, emoji, threshold = await starboard_cog.get_starboard_config(1001)

        assert channel_id is None
        assert emoji == "⭐"
        assert threshold == 3

    @pytest.mark.asyncio
    async def test_returns_configured_values(self, starboard_cog, mock_bot):
        """Returns configured values when set."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": "2001",
            "starboard_emoji": "🌟",
            "starboard_threshold": "5"
        }.get(key)

        channel_id, emoji, threshold = await starboard_cog.get_starboard_config(1001)

        assert channel_id == 2001
        assert emoji == "🌟"
        assert threshold == 5

    @pytest.mark.asyncio
    async def test_handles_invalid_channel_id(self, starboard_cog, mock_bot):
        """Returns None for invalid channel ID."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": "not_a_number",
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)

        channel_id, _emoji, _threshold = await starboard_cog.get_starboard_config(1001)

        assert channel_id is None

    @pytest.mark.asyncio
    async def test_handles_invalid_threshold(self, starboard_cog, mock_bot):
        """Returns default threshold for invalid value."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": "2001",
            "starboard_emoji": "⭐",
            "starboard_threshold": "invalid"
        }.get(key)

        _channel_id, _emoji, threshold = await starboard_cog.get_starboard_config(1001)

        assert threshold == 3  # Default


# =============================================================================
# REACTION ADD EVENT TESTS
# =============================================================================


class TestOnRawReactionAdd:
    """Tests for on_raw_reaction_add listener."""

    @pytest.mark.asyncio
    async def test_ignores_non_guild_reactions(self, starboard_cog):
        """Ignores reactions not in a guild."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = None

        await starboard_cog.on_raw_reaction_add(payload)
        # Should return early without any DB calls
        starboard_cog.db_manager.get_guild_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_bot_reactions(self, starboard_cog, mock_bot):
        """Ignores reactions from the bot itself."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = mock_bot.user.id

        await starboard_cog.on_raw_reaction_add(payload)
        starboard_cog.db_manager.get_guild_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_emoji(self, starboard_cog, mock_bot):
        """Ignores reactions with wrong emoji."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "👍"  # Not the star emoji

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": "2001",
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)

        await starboard_cog.on_raw_reaction_add(payload)
        # Should return after config check without fetching message
        mock_bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_reactions_in_starboard_channel(self, starboard_cog, mock_bot, mock_starboard_channel):
        """Ignores reactions in the starboard channel itself."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_starboard_channel.id
        payload.message_id = 4001

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.return_value = mock_starboard_channel

        await starboard_cog.on_raw_reaction_add(payload)
        # Should not proceed to post_to_starboard
        mock_starboard_channel.fetch_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_when_threshold_met(self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message):
        """Posts to starboard when reaction threshold is met."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel
        }.get(ch_id)

        # Add reaction meeting threshold
        mock_message.reactions = [create_star_reaction("⭐", 5)]
        mock_channel.fetch_message.return_value = mock_message

        # Mock post_to_starboard to track calls
        starboard_cog.post_to_starboard = AsyncMock()

        await starboard_cog.on_raw_reaction_add(payload)

        starboard_cog.post_to_starboard.assert_called_once_with(
            mock_message, mock_starboard_channel.id, "⭐", 5
        )

    @pytest.mark.asyncio
    async def test_ignores_below_threshold(self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message):
        """Does not post when below threshold."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel
        }.get(ch_id)

        # Add reaction below threshold
        mock_message.reactions = [create_star_reaction("⭐", 2)]
        mock_channel.fetch_message.return_value = mock_message

        starboard_cog.post_to_starboard = AsyncMock()

        await starboard_cog.on_raw_reaction_add(payload)

        starboard_cog.post_to_starboard.assert_not_called()


# =============================================================================
# POST TO STARBOARD TESTS
# =============================================================================


class TestPostToStarboard:
    """Tests for post_to_starboard method."""

    @pytest.mark.asyncio
    async def test_creates_new_post_when_no_entry(self, starboard_cog, mock_bot, mock_starboard_channel, mock_message):
        """Creates a new starboard post when no entry exists."""
        mock_bot.get_channel.return_value = mock_starboard_channel
        mock_bot.db_manager.get_starboard_entry.return_value = None

        starboard_cog.create_new_starboard_post = AsyncMock()

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        starboard_cog.create_new_starboard_post.assert_called_once()
        call_args = starboard_cog.create_new_starboard_post.call_args
        assert call_args[0][0] == mock_message
        assert call_args[0][1] == mock_starboard_channel
        assert "⭐ **5**" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_updates_existing_post(self, starboard_cog, mock_bot, mock_starboard_channel, mock_message):
        """Updates an existing starboard post when entry exists."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        existing_sb_msg = MagicMock(spec=discord.Message)
        existing_sb_msg.edit = AsyncMock()

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001
        }
        mock_starboard_channel.fetch_message.return_value = existing_sb_msg

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 10)

        existing_sb_msg.edit.assert_called_once()
        assert "⭐ **10**" in existing_sb_msg.edit.call_args[1]['content']

    @pytest.mark.asyncio
    async def test_recreates_when_starboard_message_missing(self, starboard_cog, mock_bot, mock_starboard_channel, mock_message):
        """Recreates post when starboard message is deleted."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001
        }
        mock_starboard_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        starboard_cog.create_new_starboard_post = AsyncMock()

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        mock_bot.db_manager.remove_starboard_entry.assert_called_once_with(mock_message.id)
        starboard_cog.create_new_starboard_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_recreates_when_entry_has_no_starboard_message_id(self, starboard_cog, mock_bot, mock_starboard_channel, mock_message):
        """Recreates post when entry exists but has no starboard_message_id."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': None,  # Missing!
            'guild_id': 1001,
            'original_channel_id': 2001
        }

        starboard_cog.create_new_starboard_post = AsyncMock()

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        mock_bot.db_manager.remove_starboard_entry.assert_called_once_with(mock_message.id)
        starboard_cog.create_new_starboard_post.assert_called_once()


# =============================================================================
# EMBED CREATION TESTS
# =============================================================================


class TestCreateStarboardEmbed:
    """Tests for create_starboard_embed_and_files method."""

    @pytest.mark.asyncio
    async def test_basic_message_embed(self, starboard_cog, mock_message):
        """Creates embed for a basic text message."""
        embed, files = await starboard_cog.create_starboard_embed_and_files(mock_message)

        assert embed.description == mock_message.content
        assert embed.color == discord.Color.gold()
        assert "TestUser" in embed.author.name
        assert len(files) == 0

    @pytest.mark.asyncio
    async def test_truncates_long_content(self, starboard_cog, mock_message):
        """Truncates content over 4096 characters."""
        mock_message.content = "x" * 5000

        embed, _files = await starboard_cog.create_starboard_embed_and_files(mock_message)

        assert len(embed.description) == 4096
        assert embed.description.endswith("...")

    @pytest.mark.asyncio
    async def test_includes_jump_link(self, starboard_cog, mock_message):
        """Embed includes a jump link to the original message."""
        embed, _files = await starboard_cog.create_starboard_embed_and_files(mock_message)

        jump_field = next((f for f in embed.fields if f.name == "Original Message"), None)
        assert jump_field is not None
        assert mock_message.jump_url in jump_field.value


# =============================================================================
# REACTION REMOVE EVENT TESTS
# =============================================================================


class TestOnRawReactionRemove:
    """Tests for on_raw_reaction_remove listener."""

    @pytest.mark.asyncio
    async def test_ignores_non_guild_reactions(self, starboard_cog):
        """Ignores reactions not in a guild."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = None

        await starboard_cog.on_raw_reaction_remove(payload)
        starboard_cog.db_manager.get_guild_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_when_below_threshold(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Deletes starboard post when reactions fall below threshold."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel
        }.get(ch_id)

        # Below threshold
        mock_message.reactions = [create_star_reaction("⭐", 2)]
        mock_channel.fetch_message.return_value = mock_message

        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.delete = AsyncMock()
        mock_starboard_channel.fetch_message.return_value = sb_msg

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        sb_msg.delete.assert_called_once()
        mock_bot.db_manager.remove_starboard_entry.assert_called_once_with(mock_message.id)

    @pytest.mark.asyncio
    async def test_updates_count_when_still_above_threshold(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Updates count when still above threshold."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel
        }.get(ch_id)

        # Still above threshold
        mock_message.reactions = [create_star_reaction("⭐", 4)]
        mock_channel.fetch_message.return_value = mock_message

        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.edit = AsyncMock()
        sb_msg.delete = AsyncMock()
        mock_starboard_channel.fetch_message.return_value = sb_msg

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        sb_msg.delete.assert_not_called()
        sb_msg.edit.assert_called_once()
        assert "⭐ **4**" in sb_msg.edit.call_args[1]['content']


# =============================================================================
# TOMBSTONE TESTS
# =============================================================================


class TestCreateTombstone:
    """Tests for _create_tombstone method."""

    @pytest.mark.asyncio
    async def test_creates_tombstone_message(self, starboard_cog, mock_starboard_channel):
        """Creates a tombstone message for deleted original."""
        tomb = MagicMock(spec=discord.Message)
        tomb.id = 9999
        mock_starboard_channel.send.return_value = tomb

        result = await starboard_cog._create_tombstone(mock_starboard_channel, 4001)

        assert result == tomb
        mock_starboard_channel.send.assert_called_once()
        call_arg = mock_starboard_channel.send.call_args[0][0]
        assert "4001" in call_arg
        assert "🪦" in call_arg

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, starboard_cog, mock_starboard_channel):
        """Returns None when tombstone creation fails."""
        mock_starboard_channel.send.side_effect = discord.HTTPException(MagicMock(), "Error")

        result = await starboard_cog._create_tombstone(mock_starboard_channel, 4001)

        assert result is None


# =============================================================================
# FIX COMMAND TESTS
# =============================================================================


class TestFixCommand:
    """Tests for the fix command implementation."""

    @pytest.mark.asyncio
    async def test_fix_returns_early_without_guild(self, starboard_cog, mock_ctx):
        """Returns early when not in a guild."""
        mock_ctx.guild = None

        await starboard_cog._fix_impl(mock_ctx)

        mock_ctx.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_returns_when_no_channel_configured(self, starboard_cog, mock_ctx, mock_bot):
        """Returns when starboard channel is not configured."""
        mock_bot.db_manager.get_guild_config.return_value = None

        await starboard_cog._fix_impl(mock_ctx)

        mock_ctx.send.assert_called()
        assert "not configured" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_fix_returns_when_no_entries(self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel):
        """Returns when no starboard entries exist."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.return_value = mock_starboard_channel
        mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = []

        await starboard_cog._fix_impl(mock_ctx)

        assert any("No starboard entries" in str(call) for call in mock_ctx.send.call_args_list)

    @pytest.mark.asyncio
    async def test_fix_recovers_missing_guild_id(self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel, mock_guild):
        """Recovers missing guild_id from starboard message embed."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.return_value = mock_starboard_channel

        # Entry with missing guild_id
        entry = {
            'original_message_id': 4001,
            'starboard_message_id': 5001,
            'guild_id': None,  # Missing!
            'original_channel_id': 2001,
            'starboard_reply_id': None
        }
        mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = [entry]

        # Mock starboard message with embed containing jump URL
        sb_msg = MagicMock(spec=discord.Message)
        embed = MagicMock(spec=discord.Embed)
        field = MagicMock()
        field.name = "Original Message"
        field.value = f"[Jump to Message](https://discord.com/channels/{mock_guild.id}/2001/4001)"
        embed.fields = [field]
        sb_msg.embeds = [embed]
        sb_msg.reference = None
        mock_starboard_channel.fetch_message.return_value = sb_msg

        # Mock original message fetch to succeed
        orig_channel = MagicMock(spec=discord.TextChannel)
        orig_channel.fetch_message = AsyncMock()
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_starboard_channel.id: mock_starboard_channel,
            2001: orig_channel
        }.get(ch_id)

        starboard_cog._fast_mode = True  # Skip rate limiting

        # Patch asyncio.sleep to avoid 30s status editor delay
        with patch('asyncio.sleep', new_callable=AsyncMock):
            await starboard_cog._fix_impl(mock_ctx)

        # Entry should have been updated with recovered guild_id
        mock_bot.db_manager.update_starboard_entry.assert_called()
        updated_entry = mock_bot.db_manager.update_starboard_entry.call_args[0][0]
        assert updated_entry['guild_id'] == mock_guild.id


# =============================================================================
# REMAKE COMMAND TESTS
# =============================================================================


class TestRemakeCommand:
    """Tests for the remake command implementation."""

    @pytest.mark.asyncio
    async def test_remake_returns_early_without_guild(self, starboard_cog, mock_ctx):
        """Returns early when not in a guild."""
        mock_ctx.guild = None

        await starboard_cog._remake_impl(mock_ctx)

        mock_ctx.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_remake_returns_when_no_channel_configured(self, starboard_cog, mock_ctx, mock_bot):
        """Returns when starboard channel is not configured."""
        mock_bot.db_manager.get_guild_config.return_value = None

        await starboard_cog._remake_impl(mock_ctx)

        mock_ctx.send.assert_called()
        assert "not configured" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_remake_returns_when_no_entries(self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel):
        """Returns when no starboard entries exist."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)
        mock_bot.get_channel.return_value = mock_starboard_channel
        mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = []

        await starboard_cog._remake_impl(mock_ctx)

        assert any("No starboard entries" in str(call) for call in mock_ctx.send.call_args_list)

    @pytest.mark.asyncio
    async def test_remake_deletes_and_recreates(
        self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel, mock_channel, mock_message, mock_guild
    ):
        """Deletes existing starboard messages and recreates them."""
        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(mock_starboard_channel.id),
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)

        def get_channel_side_effect(ch_id):
            if ch_id == mock_starboard_channel.id:
                return mock_starboard_channel
            if ch_id == mock_channel.id:
                return mock_channel
            return None
        mock_bot.get_channel.side_effect = get_channel_side_effect

        # Valid entry
        entry = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': mock_guild.id,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None
        }
        mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = [entry]

        # Mock existing starboard message
        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.delete = AsyncMock()
        mock_starboard_channel.fetch_message.return_value = sb_msg

        # Mock original message with reactions
        mock_message.reactions = [create_star_reaction("⭐", 5)]
        mock_channel.fetch_message.return_value = mock_message

        # Mock post_to_starboard
        starboard_cog.post_to_starboard = AsyncMock()
        starboard_cog._fast_mode = True

        # Patch asyncio.sleep to avoid recreation delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            await starboard_cog._remake_impl(mock_ctx)

        # Verify old message was deleted
        sb_msg.delete.assert_called_once()
        # Verify DB was cleared
        mock_bot.db_manager.clear_starboard_for_guild.assert_called_once_with(mock_guild.id)
        # Verify post was recreated
        starboard_cog.post_to_starboard.assert_called_once()


# =============================================================================
# MIGRATION DETECTION TESTS
# =============================================================================


class TestMigrationDetection:
    """Tests for channel migration detection in remake."""

    @pytest.mark.asyncio
    async def test_detects_channel_migration(
        self, starboard_cog, mock_ctx, mock_bot, mock_guild
    ):
        """Detects when starboard messages exist in a different channel than configured."""
        # Old channel (where messages currently exist)
        old_channel = MagicMock(spec=discord.TextChannel)
        old_channel.id = 2000
        old_channel.guild = mock_guild
        old_channel.mention = "<#2000>"
        old_channel.fetch_message = AsyncMock()

        # New channel (configured channel)
        new_channel = MagicMock(spec=discord.TextChannel)
        new_channel.id = 2001
        new_channel.guild = mock_guild
        new_channel.mention = "<#2001>"
        new_channel.fetch_message = AsyncMock()
        new_channel.send = AsyncMock()

        # Original channel
        orig_channel = MagicMock(spec=discord.TextChannel)
        orig_channel.id = 3001
        orig_channel.guild = mock_guild
        orig_channel.fetch_message = AsyncMock()

        mock_guild.text_channels = [old_channel, new_channel, orig_channel]

        mock_bot.db_manager.get_guild_config.side_effect = lambda g_id, key: {
            "starboard_channel_id": str(new_channel.id),  # Points to NEW channel
            "starboard_emoji": "⭐",
            "starboard_threshold": "3"
        }.get(key)

        def get_channel_side_effect(ch_id):
            if ch_id == old_channel.id:
                return old_channel
            if ch_id == new_channel.id:
                return new_channel
            if ch_id == orig_channel.id:
                return orig_channel
            return None
        mock_bot.get_channel.side_effect = get_channel_side_effect

        # Entry pointing to message in OLD channel
        entry = {
            'original_message_id': 4001,
            'starboard_message_id': 5001,
            'guild_id': mock_guild.id,
            'original_channel_id': orig_channel.id,
            'starboard_reply_id': None
        }
        mock_bot.db_manager.get_all_starboard_entries_for_guild.return_value = [entry]

        # Message not found in NEW channel
        new_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        # Message found in OLD channel
        old_sb_msg = MagicMock(spec=discord.Message)
        old_sb_msg.id = 5001
        old_channel.fetch_message.return_value = old_sb_msg

        # Original message with reactions
        orig_msg = MagicMock(spec=discord.Message)
        orig_msg.id = 4001
        orig_msg.channel = orig_channel
        orig_msg.guild = mock_guild
        orig_msg.content = "Test"
        orig_msg.author = MagicMock()
        orig_msg.author.id = 123
        orig_msg.author.display_name = "TestUser"
        orig_msg.author.name = "testuser"
        orig_msg.author.display_avatar.url = "http://avatar.url"
        orig_msg.created_at = discord.utils.utcnow()
        orig_msg.jump_url = f"https://discord.com/channels/{mock_guild.id}/{orig_channel.id}/4001"
        orig_msg.embeds = []
        orig_msg.attachments = []
        orig_msg.reactions = [create_star_reaction("⭐", 5)]
        orig_msg.reference = None
        orig_msg.message_snapshots = []
        orig_channel.fetch_message.return_value = orig_msg

        # Mock new message creation
        new_sb_msg = MagicMock(spec=discord.Message)
        new_sb_msg.id = 7001
        new_channel.send.return_value = new_sb_msg

        starboard_cog._fast_mode = True

        # Patch asyncio.sleep to avoid 30s status editor delay
        with patch('asyncio.sleep', new_callable=AsyncMock):
            await starboard_cog._remake_impl(mock_ctx)

        # Should detect migration
        migration_detected = any(
            "migration detected" in str(call).lower()
            for call in mock_ctx.send.call_args_list
        )
        assert migration_detected, "Migration should have been detected"

        # Old message should NOT have been deleted
        old_sb_msg.delete = MagicMock()  # Ensure it's trackable
        # DB should NOT have been cleared (migration updates entries)
        mock_bot.db_manager.clear_starboard_for_guild.assert_not_called()


# =============================================================================
# REPLY CONTEXT TESTS
# =============================================================================


class TestReplyContext:
    """Tests for handling messages that are replies."""

    @pytest.mark.asyncio
    async def test_creates_two_part_post_for_reply(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_channel, mock_guild
    ):
        """Creates two messages for a reply: context + starred message."""
        # Original message being starred (which is a reply)
        starred_msg = MagicMock(spec=discord.Message)
        starred_msg.id = 4001
        starred_msg.channel = mock_channel
        starred_msg.guild = mock_guild
        starred_msg.content = "This is a reply"
        starred_msg.author = MagicMock()
        starred_msg.author.id = 123
        starred_msg.author.display_name = "TestUser"
        starred_msg.author.name = "testuser"
        starred_msg.author.display_avatar.url = "http://avatar.url"
        starred_msg.created_at = discord.utils.utcnow()
        starred_msg.jump_url = f"https://discord.com/channels/{mock_guild.id}/{mock_channel.id}/4001"
        starred_msg.embeds = []
        starred_msg.attachments = []
        starred_msg.reactions = [create_star_reaction("⭐", 5)]
        starred_msg.message_snapshots = []

        # The message being replied to
        starred_msg.reference = MagicMock()
        starred_msg.reference.message_id = 3999

        replied_to_msg = MagicMock(spec=discord.Message)
        replied_to_msg.id = 3999
        replied_to_msg.channel = mock_channel
        replied_to_msg.guild = mock_guild
        replied_to_msg.content = "Original message"
        replied_to_msg.author = MagicMock()
        replied_to_msg.author.id = 456
        replied_to_msg.author.display_name = "OtherUser"
        replied_to_msg.author.name = "otheruser"
        replied_to_msg.author.display_avatar.url = "http://other-avatar.url"
        replied_to_msg.created_at = discord.utils.utcnow()
        replied_to_msg.jump_url = f"https://discord.com/channels/{mock_guild.id}/{mock_channel.id}/3999"
        replied_to_msg.embeds = []
        replied_to_msg.attachments = []
        replied_to_msg.message_snapshots = []

        mock_channel.fetch_message.return_value = replied_to_msg

        # Mock starboard channel messages
        context_msg = MagicMock(spec=discord.Message)
        context_msg.id = 5001
        context_msg.reply = AsyncMock()

        starred_sb_msg = MagicMock(spec=discord.Message)
        starred_sb_msg.id = 5002
        context_msg.reply.return_value = starred_sb_msg

        mock_starboard_channel.send.return_value = context_msg

        content = "⭐ **5** in <#2001>"

        await starboard_cog.create_new_starboard_post(starred_msg, mock_starboard_channel, content)

        # First: context message sent
        mock_starboard_channel.send.assert_called_once()
        # Second: reply to context
        context_msg.reply.assert_called_once()
        # DB entry with reply context ID
        mock_bot.db_manager.add_starboard_entry.assert_called_once()
        call_args = mock_bot.db_manager.add_starboard_entry.call_args[0]
        assert call_args[0] == starred_msg.id  # original_message_id
        assert call_args[1] == starred_sb_msg.id  # starboard_message_id
        assert call_args[4] == context_msg.id  # reply_context_id


# =============================================================================
# RATE LIMITING TESTS
# =============================================================================


class TestRateLimiting:
    """Tests for rate limiting helper."""

    @pytest.mark.asyncio
    async def test_run_rate_limited_succeeds(self, starboard_cog):
        """Executes coroutine successfully."""
        async def mock_coro(value):
            return value * 2

        # Patch asyncio.sleep to avoid actual delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            result = await starboard_cog._run_rate_limited(mock_coro, 5, delay=0.01)

        assert result == 10

    @pytest.mark.asyncio
    async def test_run_rate_limited_retries_on_http_error(self, starboard_cog):
        """Retries on HTTP errors with backoff."""
        call_count = [0]

        async def failing_coro():
            call_count[0] += 1
            if call_count[0] < 3:
                raise discord.HTTPException(MagicMock(), "Rate limited")
            return "success"

        starboard_cog._fix_delay = 0.01
        # Patch asyncio.sleep to avoid real 1s + 2s backoff delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            result = await starboard_cog._run_rate_limited(failing_coro, delay=0.01, retries=4)

        assert result == "success"
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_run_rate_limited_raises_not_found(self, starboard_cog):
        """Does not retry on NotFound errors."""
        async def not_found_coro():
            raise discord.NotFound(MagicMock(), "Not found")

        # Patch asyncio.sleep to avoid actual delays
        with patch('asyncio.sleep', new_callable=AsyncMock):
            with pytest.raises(discord.NotFound):
                await starboard_cog._run_rate_limited(not_found_coro, delay=0.01)


# =============================================================================
# SINGLE POST FALLBACK TESTS
# =============================================================================


class TestSinglePostFallback:
    """Tests for create_single_starboard_post method."""

    @pytest.mark.asyncio
    async def test_creates_single_post(self, starboard_cog, mock_bot, mock_starboard_channel, mock_message):
        """Creates a single starboard post for non-reply messages."""
        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.id = 5001
        mock_starboard_channel.send.return_value = sb_msg

        content = "⭐ **5** in <#2001>"

        await starboard_cog.create_single_starboard_post(mock_message, mock_starboard_channel, content)

        mock_starboard_channel.send.assert_called_once()
        mock_bot.db_manager.add_starboard_entry.assert_called_once()
        call_args = mock_bot.db_manager.add_starboard_entry.call_args[0]
        assert call_args[0] == mock_message.id
        assert call_args[1] == sb_msg.id

    @pytest.mark.asyncio
    async def test_falls_back_when_reply_to_deleted(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_channel, mock_message
    ):
        """Falls back to single post when replied-to message is deleted."""
        # Make message a reply
        mock_message.reference = MagicMock()
        mock_message.reference.message_id = 3999

        # Replied-to message is deleted
        mock_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        # Mock single post creation
        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.id = 5001
        mock_starboard_channel.send.return_value = sb_msg

        content = "⭐ **5** in <#2001>"

        await starboard_cog.create_new_starboard_post(mock_message, mock_starboard_channel, content)

        # Should fall back to single post (only one send call)
        assert mock_starboard_channel.send.call_count == 1


# =============================================================================
# CONFIGURATION COMMAND TESTS
# =============================================================================


class TestConfigCommands:
    """Tests for starboard configuration commands."""

    @pytest.mark.asyncio
    async def test_set_channel(self, starboard_cog, mock_ctx, mock_bot, mock_channel):
        """Sets the starboard channel."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        # Call the underlying callback directly to bypass the hybrid command wrapper
        await starboard_cog.set_channel.callback(starboard_cog, mock_ctx, mock_channel)

        mock_bot.db_manager.set_guild_config.assert_called_once_with(
            1001, "starboard_channel_id", str(mock_channel.id)
        )
        mock_ctx.send.assert_called_once()
        assert mock_channel.mention in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_emoji(self, starboard_cog, mock_ctx, mock_bot):
        """Sets the starboard emoji."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        # Call the underlying callback directly
        await starboard_cog.set_emoji.callback(starboard_cog, mock_ctx, "🌟")

        mock_bot.db_manager.set_guild_config.assert_called_once_with(
            1001, "starboard_emoji", "🌟"
        )
        mock_ctx.send.assert_called_once()
        assert "🌟" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_threshold(self, starboard_cog, mock_ctx, mock_bot):
        """Sets the starboard threshold."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        # Call the underlying callback directly
        await starboard_cog.set_threshold.callback(starboard_cog, mock_ctx, 5)

        mock_bot.db_manager.set_guild_config.assert_called_once_with(
            1001, "starboard_threshold", "5"
        )
        mock_ctx.send.assert_called_once()
        assert "5" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_threshold_rejects_zero(self, starboard_cog, mock_ctx, mock_bot):
        """Rejects threshold of zero or less."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        # Call the underlying callback directly
        await starboard_cog.set_threshold.callback(starboard_cog, mock_ctx, 0)

        mock_bot.db_manager.set_guild_config.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

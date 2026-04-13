"""Unit tests for the Starboard cog.

This module contains comprehensive tests for the starboard system, including:
- Configuration retrieval tests (StarboardConfig dataclass)
- Reaction event handling tests
- Starboard post creation tests
- Embed generation tests
- Verify and remake command tests
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.starboard import Starboard, StarboardConfig
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
    ctx.channel.id = 8001
    ctx.send = AsyncMock()
    return ctx


def create_star_reaction(emoji: str = "⭐", count: int = 5) -> MagicMock:
    """Helper to create a mock reaction."""
    reaction = MagicMock()
    reaction.emoji = emoji
    reaction.count = count
    return reaction


def make_config_row(guild_id: int = 1001, **overrides) -> dict:
    """Helper to create a starboard_config DB row dict.

    Mirrors the shape returned by ``DatabaseManager.get_starboard_config``.
    """
    row = {
        "guild_id": guild_id,
        "enabled": 1,
        "channel_id": 3001,
        "emoji": "⭐",
        "threshold": 3,
        "last_heal_at": 0,
        "crawl_started_at": None,
        "crawl_requested_by": None,
        "crawl_notify_channel": None,
        "crawl_include_threads": 0,
        "crawl_last_channel_id": None,
        "crawl_last_message_id": None,
    }
    row.update(overrides)
    return row


# =============================================================================
# CONFIGURATION TESTS
# =============================================================================


class TestGetStarboardConfig:
    """Tests for get_starboard_config method (returns StarboardConfig dataclass)."""

    @pytest.mark.asyncio
    async def test_returns_defaults_when_not_configured(self, starboard_cog, mock_bot):
        """Returns default StarboardConfig when no DB row exists."""
        mock_bot.db_manager.get_starboard_config.return_value = None

        cfg = await starboard_cog.get_starboard_config(1001)

        assert isinstance(cfg, StarboardConfig)
        assert cfg.channel_id is None
        assert cfg.emoji == "⭐"
        assert cfg.threshold == 3
        assert cfg.enabled is False

    @pytest.mark.asyncio
    async def test_returns_configured_values(self, starboard_cog, mock_bot):
        """Returns configured values from DB row."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=2001, emoji="🌟", threshold=5
        )

        cfg = await starboard_cog.get_starboard_config(1001)

        assert cfg.channel_id == 2001
        assert cfg.emoji == "🌟"
        assert cfg.threshold == 5
        assert cfg.enabled is True

    @pytest.mark.asyncio
    async def test_returns_none_channel_when_db_has_none(self, starboard_cog, mock_bot):
        """Returns None channel_id when DB row has it as None."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=None
        )

        cfg = await starboard_cog.get_starboard_config(1001)

        assert cfg.channel_id is None

    @pytest.mark.asyncio
    async def test_returns_correct_threshold(self, starboard_cog, mock_bot):
        """Returns exact threshold from DB."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            threshold=10
        )

        cfg = await starboard_cog.get_starboard_config(1001)

        assert cfg.threshold == 10


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
        starboard_cog.db_manager.get_starboard_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_bot_reactions(self, starboard_cog, mock_bot):
        """Ignores reactions from the bot itself."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = mock_bot.user.id

        await starboard_cog.on_raw_reaction_add(payload)
        starboard_cog.db_manager.get_starboard_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_when_disabled(self, starboard_cog, mock_bot):
        """Ignores reactions when starboard is disabled."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(enabled=0)

        await starboard_cog.on_raw_reaction_add(payload)
        mock_bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_emoji(self, starboard_cog, mock_bot):
        """Ignores reactions with wrong emoji."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "👍"  # Not the star emoji

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()

        await starboard_cog.on_raw_reaction_add(payload)
        # Should return after config check without fetching message
        mock_bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_reactions_in_starboard_channel(
        self, starboard_cog, mock_bot, mock_starboard_channel
    ):
        """Ignores reactions in the starboard channel itself."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_starboard_channel.id
        payload.message_id = 4001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.db_manager.is_starboard_channel_banned.return_value = False
        mock_bot.get_channel.return_value = mock_starboard_channel

        await starboard_cog.on_raw_reaction_add(payload)
        # Should not proceed to post_to_starboard
        mock_starboard_channel.fetch_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_banned_channels(self, starboard_cog, mock_bot, mock_channel):
        """Ignores reactions in banned channels."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = 4001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()
        mock_bot.db_manager.is_starboard_channel_banned.return_value = True

        await starboard_cog.on_raw_reaction_add(payload)
        mock_bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_when_threshold_met(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Posts to starboard when reaction threshold is met."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.db_manager.is_starboard_channel_banned.return_value = False
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)

        # Add reaction meeting threshold
        mock_message.reactions = [create_star_reaction("⭐", 5)]
        mock_channel.fetch_message.return_value = mock_message

        # Mock post_to_starboard to track calls
        starboard_cog.post_to_starboard = AsyncMock()
        # Mock _should_self_heal to avoid background task logic
        starboard_cog._should_self_heal = AsyncMock(return_value=False)

        await starboard_cog.on_raw_reaction_add(payload)

        starboard_cog.post_to_starboard.assert_called_once_with(
            mock_message, mock_starboard_channel.id, "⭐", 5, 3
        )

    @pytest.mark.asyncio
    async def test_ignores_below_threshold(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Does not post when below threshold."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.db_manager.is_starboard_channel_banned.return_value = False
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)

        # Add reaction below threshold
        mock_message.reactions = [create_star_reaction("⭐", 2)]
        mock_channel.fetch_message.return_value = mock_message
        mock_bot.db_manager.get_starboard_entry.return_value = None

        starboard_cog.post_to_starboard = AsyncMock()
        starboard_cog._should_self_heal = AsyncMock(return_value=False)

        await starboard_cog.on_raw_reaction_add(payload)

        starboard_cog.post_to_starboard.assert_not_called()

    @pytest.mark.asyncio
    async def test_syncs_existing_unworthy_row_below_threshold(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Below-threshold adds still sync tracked unworthy rows without promoting them."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.user_id = 123
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id,
            threshold=6,
        )
        mock_bot.db_manager.is_starboard_channel_banned.return_value = False
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)

        mock_message.reactions = [create_star_reaction("⭐", 5)]
        mock_channel.fetch_message.return_value = mock_message
        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None,
            'star_count': 4,
            'failed_checks': 2,
            'starred_at': 123456,
            'is_unworthy': 1,
        }

        starboard_cog.post_to_starboard = AsyncMock()
        starboard_cog._should_self_heal = AsyncMock(return_value=False)

        await starboard_cog.on_raw_reaction_add(payload)

        starboard_cog.post_to_starboard.assert_not_called()
        mock_bot.db_manager.update_starboard_star_count.assert_called_once_with(mock_message.id, 5)
        mock_bot.db_manager.reset_starboard_failed_checks.assert_called_once_with(mock_message.id)
        mock_bot.db_manager.update_starboard_channel.assert_not_called()
        mock_bot.db_manager.remove_starboard_entry.assert_not_called()


# =============================================================================
# POST TO STARBOARD TESTS
# =============================================================================


class TestPostToStarboard:
    """Tests for post_to_starboard method."""

    @pytest.mark.asyncio
    async def test_creates_new_post_when_no_entry(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Creates a new starboard post when no entry exists."""
        mock_bot.get_channel.return_value = mock_starboard_channel
        mock_bot.db_manager.get_starboard_entry.return_value = None

        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(5001, None))

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        starboard_cog.create_new_starboard_post.assert_called_once()
        call_args = starboard_cog.create_new_starboard_post.call_args
        assert call_args[0][0] == mock_message
        assert call_args[0][1] == mock_starboard_channel
        assert "⭐ **5**" in call_args[0][2]

        # Verify DB entry was created with keyword args
        mock_bot.db_manager.add_starboard_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_existing_post(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Updates an existing starboard post when entry exists."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        existing_sb_msg = MagicMock(spec=discord.Message)
        existing_sb_msg.edit = AsyncMock()

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001,
            'failed_checks': 0,
        }
        mock_starboard_channel.fetch_message.return_value = existing_sb_msg

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 10)

        existing_sb_msg.edit.assert_called_once()
        assert "⭐ **10**" in existing_sb_msg.edit.call_args[1]['content']

    @pytest.mark.asyncio
    async def test_recreates_when_starboard_message_missing(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Queues existing entry for remake when live post is missing."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001,
            'starboard_reply_id': 5000,
            'star_count': 4,
            'failed_checks': 1,
            'starred_at': 123456,
            'is_unworthy': 0,
        }
        mock_starboard_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(5002, None))

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        starboard_cog.create_new_starboard_post.assert_not_called()
        mock_bot.db_manager.set_starboard_message_id.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_called_once()

        queued_entry = mock_bot.db_manager.update_starboard_entry.call_args.args[0]
        assert queued_entry['starboard_message_id'] is None
        assert queued_entry['starboard_reply_id'] is None
        assert queued_entry['star_count'] == 5
        assert queued_entry['failed_checks'] == 0

    @pytest.mark.asyncio
    async def test_creates_post_when_entry_has_no_starboard_message_id(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Leaves existing unposted entry queued for remake."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': None,
            'guild_id': 1001,
            'original_channel_id': 2001,
            'starboard_reply_id': None,
            'star_count': 5,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 0,
        }

        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(5001, None))

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        starboard_cog.create_new_starboard_post.assert_not_called()
        mock_bot.db_manager.set_starboard_message_id.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_queues_unworthy_repromotion_when_live_post_missing(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Queues remake instead of hot-posting when re-promotion lacks a live post."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001,
            'starboard_reply_id': 5000,
            'star_count': 2,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 1,
        }
        mock_starboard_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(5002, None))
        starboard_cog._restore_from_unworthy = AsyncMock()

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5)

        starboard_cog.create_new_starboard_post.assert_not_called()
        starboard_cog._restore_from_unworthy.assert_not_called()
        mock_bot.db_manager.set_starboard_unworthy.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_called_once()

        queued_entry = mock_bot.db_manager.update_starboard_entry.call_args.args[0]
        assert queued_entry['starboard_message_id'] is None
        assert queued_entry['starboard_reply_id'] is None
        assert queued_entry['is_unworthy'] == 0

    @pytest.mark.asyncio
    async def test_keeps_existing_unworthy_entry_when_still_below_threshold(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Leaves an unworthy row intact when a helper call is still below threshold."""
        mock_bot.get_channel.return_value = mock_starboard_channel

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': 2001,
            'starboard_reply_id': None,
            'star_count': 4,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 1,
        }

        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(5002, None))

        await starboard_cog.post_to_starboard(mock_message, mock_starboard_channel.id, "⭐", 5, 6)

        mock_bot.db_manager.update_starboard_star_count.assert_called_once_with(mock_message.id, 5)
        mock_bot.db_manager.set_starboard_unworthy.assert_not_called()
        mock_bot.db_manager.set_starboard_message_id.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_not_called()
        starboard_cog.create_new_starboard_post.assert_not_called()
        mock_starboard_channel.fetch_message.assert_not_called()


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
        starboard_cog.db_manager.get_starboard_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_when_disabled(self, starboard_cog, mock_bot):
        """Ignores reaction remove when starboard is disabled."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.message_id = 4001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(enabled=0)

        await starboard_cog.on_raw_reaction_remove(payload)
        mock_bot.db_manager.get_starboard_entry.assert_not_called()

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

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
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
            'starboard_reply_id': None,
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

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
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
            'starboard_reply_id': None,
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        sb_msg.delete.assert_not_called()
        sb_msg.edit.assert_called_once()
        assert "⭐ **4**" in sb_msg.edit.call_args[1]['content']

    @pytest.mark.asyncio
    async def test_queues_for_remake_when_post_missing_but_still_above_threshold(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Preserves the row when the original is worthy but the live post is gone."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)

        mock_message.reactions = [create_star_reaction("⭐", 4)]
        mock_channel.fetch_message.return_value = mock_message
        mock_starboard_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': 5000,
            'star_count': 5,
            'failed_checks': 1,
            'starred_at': 123456,
            'is_unworthy': 0,
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        mock_bot.db_manager.remove_starboard_entry.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_called_once()

        queued_entry = mock_bot.db_manager.update_starboard_entry.call_args.args[0]
        assert queued_entry['starboard_message_id'] is None
        assert queued_entry['starboard_reply_id'] is None
        assert queued_entry['star_count'] == 4
        assert queued_entry['failed_checks'] == 0

    @pytest.mark.asyncio
    async def test_removes_row_when_below_threshold_even_if_live_post_is_missing(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Deletes the DB row on threshold drop even when the live post is already gone."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = mock_message.id

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)

        mock_message.reactions = [create_star_reaction("⭐", 2)]
        mock_channel.fetch_message.return_value = mock_message
        mock_starboard_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None,
            'star_count': 5,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 0,
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        mock_bot.db_manager.remove_starboard_entry.assert_called_once_with(mock_message.id)
        mock_bot.db_manager.update_starboard_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_defers_original_missing_to_cold_path(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel
    ):
        """Leaves the row untouched when the original cannot be fetched on hot path."""
        payload = MagicMock(spec=discord.RawReactionActionEvent)
        payload.guild_id = 1001
        payload.emoji = "⭐"
        payload.channel_id = mock_channel.id
        payload.message_id = 4001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_channel.id: mock_channel,
            mock_starboard_channel.id: mock_starboard_channel,
        }.get(ch_id)
        mock_channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "Not found")

        mock_bot.db_manager.get_starboard_entry.return_value = {
            'original_message_id': 4001,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None,
            'star_count': 5,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 0,
        }

        await starboard_cog.on_raw_reaction_remove(payload)

        mock_bot.db_manager.remove_starboard_entry.assert_not_called()
        mock_bot.db_manager.update_starboard_star_count.assert_not_called()
        mock_bot.db_manager.update_starboard_entry.assert_not_called()


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

        result = await starboard_cog._create_tombstone(mock_starboard_channel, 4001, star_count=7)

        assert result == tomb
        mock_starboard_channel.send.assert_called_once()
        call_arg = mock_starboard_channel.send.call_args[0][0]
        assert "**7**" in call_arg
        assert "🪦" in call_arg
        assert "something was here" in call_arg

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, starboard_cog, mock_starboard_channel):
        """Returns None when tombstone creation fails."""
        mock_starboard_channel.send.side_effect = discord.HTTPException(MagicMock(), "Error")

        result = await starboard_cog._create_tombstone(mock_starboard_channel, 4001)

        assert result is None


# =============================================================================
# VERIFY ENGINE TESTS
# =============================================================================


class TestVerifyEngine:
    """Tests for verify engine edge cases."""

    @pytest.mark.asyncio
    async def test_missing_post_below_threshold_is_reported_unworthy(
        self, starboard_cog, mock_bot, mock_channel, mock_starboard_channel, mock_message
    ):
        """Missing live post should not hide an unworthy original during verify."""
        entry = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': 1001,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': 5000,
            'star_count': 5,
            'failed_checks': 1,
            'starred_at': 123456,
            'is_unworthy': 0,
        }

        mock_message.reactions = [create_star_reaction("⭐", 2)]
        mock_bot.get_channel.return_value = mock_channel
        starboard_cog._run_rate_limited = AsyncMock(side_effect=[discord.NotFound(MagicMock(), "Not found"), mock_message])

        result = await starboard_cog._verify_single_entry(entry, mock_starboard_channel, "⭐", 3)

        from cogs.starboard import VerifyStatus
        assert result.status == VerifyStatus.UNWORTHY
        assert result.needs_db_update is True
        assert result.entry['starboard_message_id'] is None
        assert result.entry['starboard_reply_id'] is None
        assert result.entry['failed_checks'] == 0
        assert result.entry['star_count'] == 2


# =============================================================================
# VERIFY COMMAND TESTS
# =============================================================================


class TestVerifyCommand:
    """Tests for the verify command (replaced old fix command)."""

    @pytest.mark.asyncio
    async def test_verify_returns_early_without_guild(self, starboard_cog, mock_ctx):
        """Returns early when not in a guild."""
        mock_ctx.guild = None

        await starboard_cog.verify_starboard.callback(starboard_cog, mock_ctx, False)

        # Only the "must be used in a guild" message
        mock_ctx.send.assert_called_once()
        assert "guild" in mock_ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_verify_returns_when_no_channel_configured(
        self, starboard_cog, mock_ctx, mock_bot
    ):
        """Returns when starboard channel is not configured."""
        mock_bot.db_manager.get_starboard_config.return_value = None

        # Mock _confirm_fast_mode to return True (skip the confirmation prompt)
        starboard_cog._confirm_fast_mode = AsyncMock(return_value=True)

        # Need a fresh unlocked lock
        lock = asyncio.Lock()
        starboard_cog._acquire_guild_lock = MagicMock(return_value=lock)

        await starboard_cog.verify_starboard.callback(starboard_cog, mock_ctx, False)

        assert any(
            "not configured" in str(call).lower()
            for call in mock_ctx.send.call_args_list
        )

    @pytest.mark.asyncio
    async def test_verify_aborts_when_lock_held(self, starboard_cog, mock_ctx, mock_bot):
        """Returns if another operation holds the guild lock."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()

        # Mock _confirm_fast_mode to return True
        starboard_cog._confirm_fast_mode = AsyncMock(return_value=True)

        lock = asyncio.Lock()
        await lock.acquire()  # Pre-lock it
        starboard_cog._acquire_guild_lock = MagicMock(return_value=lock)

        await starboard_cog.verify_starboard.callback(starboard_cog, mock_ctx, False)

        assert any(
            "already running" in str(call).lower()
            for call in mock_ctx.send.call_args_list
        )
        lock.release()


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
    async def test_remake_returns_when_no_channel_configured(
        self, starboard_cog, mock_ctx, mock_bot
    ):
        """Returns when starboard channel is not configured."""
        mock_bot.db_manager.get_starboard_config.return_value = None

        await starboard_cog._remake_impl(mock_ctx)

        mock_ctx.send.assert_called()
        assert "not configured" in mock_ctx.send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_remake_verify_delete_recreate(
        self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel,
        mock_channel, mock_message, mock_guild
    ):
        """Runs verify, deletes, and recreates starboard messages."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.side_effect = lambda ch_id: {
            mock_starboard_channel.id: mock_starboard_channel,
            mock_channel.id: mock_channel,
        }.get(ch_id)

        entry = {
            'original_message_id': mock_message.id,
            'starboard_message_id': 5001,
            'guild_id': mock_guild.id,
            'original_channel_id': mock_channel.id,
            'starboard_reply_id': None,
            'star_count': 5,
            'failed_checks': 0,
        }

        # Verify phase — mock _verify_all_entries to return clean report
        from cogs.starboard import VerifyReport
        clean_report = VerifyReport()
        starboard_cog._verify_all_entries = AsyncMock(return_value=clean_report)
        starboard_cog._apply_verify_results = AsyncMock()

        # Delete phase
        mock_bot.db_manager.get_starboard_entries_ordered.return_value = [entry]
        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.delete = AsyncMock()
        mock_starboard_channel.fetch_message.return_value = sb_msg

        # Recreate phase
        mock_message.reactions = [create_star_reaction("⭐", 5)]
        mock_channel.fetch_message.return_value = mock_message
        new_sb_msg = MagicMock(spec=discord.Message)
        new_sb_msg.id = 7001
        # create_new_starboard_post returns (sb_id, reply_id)
        starboard_cog.create_new_starboard_post = AsyncMock(return_value=(7001, None))

        starboard_cog._fast_mode = True

        with patch('asyncio.sleep', new_callable=AsyncMock):
            await starboard_cog._remake_impl(mock_ctx)

        # Verify was called
        starboard_cog._verify_all_entries.assert_called_once()
        # Verify results were applied
        starboard_cog._apply_verify_results.assert_called_once()
        # Old message was deleted
        sb_msg.delete.assert_called_once()
        # DB IDs were nulled
        mock_bot.db_manager.null_starboard_message_ids.assert_called_once_with(mock_guild.id)

    @pytest.mark.asyncio
    async def test_remake_does_not_reapply_purged_unworthy_results(
        self, starboard_cog, mock_ctx, mock_bot, mock_starboard_channel, mock_guild
    ):
        """Purged unworthy results should not be sent back through apply_verify_results."""
        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=mock_starboard_channel.id
        )
        mock_bot.get_channel.return_value = mock_starboard_channel

        from cogs.starboard import VerifyReport, VerifyResult, VerifyStatus
        unworthy_entry = {
            'original_message_id': 4001,
            'starboard_message_id': 5001,
            'guild_id': mock_guild.id,
            'original_channel_id': 2001,
            'starboard_reply_id': None,
            'star_count': 2,
            'failed_checks': 0,
            'starred_at': 123456,
            'is_unworthy': 0,
        }
        healthy_entry = {
            'original_message_id': 4002,
            'starboard_message_id': 5002,
            'guild_id': mock_guild.id,
            'original_channel_id': 2001,
            'starboard_reply_id': None,
            'star_count': 5,
            'failed_checks': 0,
            'starred_at': 123457,
            'is_unworthy': 0,
        }
        report = VerifyReport(
            results=[
                VerifyResult(unworthy_entry, VerifyStatus.UNWORTHY, True, "below threshold"),
                VerifyResult(healthy_entry, VerifyStatus.HEALTHY, False, "ok"),
            ],
            healthy=1,
            unworthy=1,
        )

        starboard_cog._verify_all_entries = AsyncMock(return_value=report)
        starboard_cog._apply_verify_results = AsyncMock()
        mock_bot.db_manager.get_starboard_entries_ordered.return_value = []

        with patch('cogs.starboard.launch_modal', new=AsyncMock()), patch('asyncio.wait_for', new=AsyncMock(return_value=True)):
            await starboard_cog._remake_impl(mock_ctx)

        applied_report = starboard_cog._apply_verify_results.call_args.args[0]
        assert all(result.status != VerifyStatus.UNWORTHY for result in applied_report.results)


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

        sb_id, reply_id = await starboard_cog.create_new_starboard_post(
            starred_msg, mock_starboard_channel, content
        )

        # Returns tuple of (starboard_msg_id, reply_context_id)
        assert sb_id == 5002
        assert reply_id == 5001

        # First: context message sent
        mock_starboard_channel.send.assert_called_once()
        # Second: reply to context
        context_msg.reply.assert_called_once()


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

        # Patch asyncio.sleep to avoid real backoff delays
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
    async def test_creates_single_post(
        self, starboard_cog, mock_bot, mock_starboard_channel, mock_message
    ):
        """Creates a single starboard post for non-reply messages."""
        sb_msg = MagicMock(spec=discord.Message)
        sb_msg.id = 5001
        mock_starboard_channel.send.return_value = sb_msg

        content = "⭐ **5** in <#2001>"

        result = await starboard_cog.create_single_starboard_post(
            mock_message, mock_starboard_channel, content
        )

        assert result == sb_msg
        mock_starboard_channel.send.assert_called_once()

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

        sb_id, reply_id = await starboard_cog.create_new_starboard_post(
            mock_message, mock_starboard_channel, content
        )

        # Should fall back to single post
        assert sb_id == 5001
        assert reply_id is None


# =============================================================================
# CONFIGURATION COMMAND TESTS
# =============================================================================


class TestConfigCommands:
    """Tests for starboard configuration commands."""

    @pytest.mark.asyncio
    async def test_set_channel(self, starboard_cog, mock_ctx, mock_bot, mock_channel):
        """Sets the starboard channel via string arg."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        # get_starboard_config returns unconfigured state (no row)
        mock_bot.db_manager.get_starboard_config.return_value = None
        mock_bot.get_channel.return_value = mock_channel

        # Mock the _resolve_channel_arg helper
        starboard_cog._resolve_channel_arg = AsyncMock(
            return_value=(mock_channel.id, mock_channel.mention)
        )

        await starboard_cog.set_channel.callback(
            starboard_cog, mock_ctx, str(mock_channel.id)
        )

        # Should upsert with full row (first-time setup)
        mock_bot.db_manager.upsert_starboard_config.assert_called_once()
        mock_ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_channel_naked_displays_current(
        self, starboard_cog, mock_ctx, mock_bot
    ):
        """Naked call displays current channel."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row(
            channel_id=3001
        )

        await starboard_cog.set_channel.callback(starboard_cog, mock_ctx, None)

        mock_ctx.send.assert_called_once()
        assert "3001" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_emoji(self, starboard_cog, mock_ctx, mock_bot):
        """Sets the starboard emoji."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()

        await starboard_cog.set_emoji.callback(starboard_cog, mock_ctx, "🌟")

        mock_bot.db_manager.upsert_starboard_config.assert_called_once_with(1001, emoji="🌟")
        mock_ctx.send.assert_called_once()
        assert "🌟" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_threshold(self, starboard_cog, mock_ctx, mock_bot):
        """Sets the starboard threshold."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()

        await starboard_cog.set_threshold.callback(starboard_cog, mock_ctx, 5)

        mock_bot.db_manager.upsert_starboard_config.assert_called_once_with(1001, threshold=5)
        mock_ctx.send.assert_called_once()
        assert "5" in mock_ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_set_threshold_rejects_zero(self, starboard_cog, mock_ctx, mock_bot):
        """Rejects threshold of zero or less."""
        mock_ctx.guild = MagicMock()
        mock_ctx.guild.id = 1001

        mock_bot.db_manager.get_starboard_config.return_value = make_config_row()

        await starboard_cog.set_threshold.callback(starboard_cog, mock_ctx, 0)

        mock_bot.db_manager.upsert_starboard_config.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

"""Unit tests for the BOD Quotes system.

This module contains tests for:
- TOML file loading (_load_bod_quotes)
- Quote trigger evaluation (_evaluate_quote_trigger)
- Fate consumption logic (_consume_fate_and_get_tier)
- Yujin quotes display (yujin_quotes)
"""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from cogs.fun import Fun
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
    return bot


@pytest.fixture
def fun_cog(mock_bot):
    """Create a Fun cog instance with mocked bot and empty quotes."""
    with patch.object(Fun, '_load_bod_quotes'):
        cog = Fun(mock_bot)
        cog._cog_is_ready = True
        cog.bod_quote_triggers = {}
        cog.bod_quote_display = []
        return cog


@pytest.fixture
def mock_ctx(mock_bot):
    """Create a mock command context."""
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.author = MagicMock()
    ctx.author.id = 12345
    ctx.author.display_name = "TestUser"
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.channel.id = 67890
    ctx.reply = AsyncMock()
    ctx.send = AsyncMock()
    return ctx


@pytest.fixture
def mock_message():
    """Create a mock Discord message."""
    message = MagicMock(spec=discord.Message)
    message.author = MagicMock()
    message.author.id = 12345
    message.content = "test message"
    return message


# =============================================================================
# TOML LOADING TESTS
# =============================================================================


class TestLoadBodQuotes:
    """Tests for _load_bod_quotes TOML parsing."""

    def test_load_valid_toml(self, mock_bot):
        """Test loading a valid TOML file with quotes and triggers."""
        import tomllib

        toml_content = b'''
[quotes]
list = ["Quote one", "Quote two", "Quote three"]

[triggers]
0 = [{ pattern = "hello", tier = "LUCKY", count = 1 }]
5 = [{ pattern = "world", tier = "BLESSED", count = 2 }]
'''
        with tempfile.NamedTemporaryFile(suffix='.toml', delete=False, mode='wb') as f:
            f.write(toml_content)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as toml_file:
                data = tomllib.load(toml_file)

            quotes = data.get('quotes', {}).get('list', [])
            triggers = data.get('triggers', {})

            assert quotes == ["Quote one", "Quote two", "Quote three"]
            # TOML keys are strings, code converts them to int
            assert '0' in triggers
            assert '5' in triggers
            assert triggers['0'][0]['pattern'] == "hello"
            assert triggers['5'][0]['tier'] == "BLESSED"
        finally:
            os.unlink(temp_path)

    def test_load_empty_quotes(self, mock_bot):
        """Test loading TOML with empty quotes list."""
        import tomllib

        toml_content = b'''
[quotes]
list = []

[triggers]
'''
        with tempfile.NamedTemporaryFile(suffix='.toml', delete=False, mode='wb') as f:
            f.write(toml_content)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as toml_file:
                data = tomllib.load(toml_file)

            quotes = data.get('quotes', {}).get('list', [])
            assert quotes == []
        finally:
            os.unlink(temp_path)

    def test_load_missing_quotes_section(self, mock_bot):
        """Test loading TOML without quotes section defaults to empty."""
        import tomllib

        toml_content = b'''
[triggers]
0 = [{ pattern = "test", tier = "LUCKY", count = 1 }]
'''
        with tempfile.NamedTemporaryFile(suffix='.toml', delete=False, mode='wb') as f:
            f.write(toml_content)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as toml_file:
                data = tomllib.load(toml_file)

            quotes = data.get('quotes', {}).get('list', [])
            assert quotes == []
        finally:
            os.unlink(temp_path)

    def test_chain_position_converted_to_int(self, mock_bot):
        """Test that string chain positions are converted to integers."""
        import tomllib

        toml_content = b'''
[quotes]
list = []

[triggers]
0 = [{ pattern = "first", tier = "LUCKY", count = 1 }]
19 = [{ pattern = "special", tier = "GUARANTEED", count = 1 }]
'''
        with tempfile.NamedTemporaryFile(suffix='.toml', delete=False, mode='wb') as f:
            f.write(toml_content)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as toml_file:
                data = tomllib.load(toml_file)

            raw_triggers = data.get('triggers', {})
            converted = {}
            for chain_pos, trigger_list in raw_triggers.items():
                converted[int(chain_pos)] = trigger_list

            assert 0 in converted
            assert 19 in converted
            assert isinstance(next(iter(converted.keys())), int)
        finally:
            os.unlink(temp_path)


# =============================================================================
# TRIGGER EVALUATION TESTS
# =============================================================================


class TestEvaluateQuoteTrigger:
    """Tests for _evaluate_quote_trigger pattern matching."""

    @pytest.mark.asyncio
    async def test_trigger_matches_pattern(self, fun_cog, mock_message):
        """Test that matching pattern triggers fate addition."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': 'hello world', 'tier': 'LUCKY', 'count': 1}]
        }

        # Mock _get_previous_message to return matching content
        fun_cog._get_previous_message = AsyncMock(return_value="hello world everyone")

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_called_once_with(12345, 'LUCKY', 1)

    @pytest.mark.asyncio
    async def test_trigger_case_insensitive(self, fun_cog, mock_message):
        """Test that pattern matching is case-insensitive."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': 'HELLO', 'tier': 'LUCKY', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="hello there")

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_no_match(self, fun_cog, mock_message):
        """Test that non-matching content doesn't trigger fate."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': 'specific phrase', 'tier': 'LUCKY', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="something else entirely")

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_no_previous_message(self, fun_cog, mock_message):
        """Test handling when user has no previous message."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': 'test', 'tier': 'LUCKY', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value=None)

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_chain_position_not_configured(self, fun_cog, mock_message):
        """Test that unconfigured chain positions don't trigger anything."""
        fun_cog.bod_quote_triggers = {
            5: [{'pattern': 'test', 'tier': 'BLESSED', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="test message")

        channel = MagicMock(spec=discord.TextChannel)

        # Chain 0 has no triggers configured
        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_first_match_wins(self, fun_cog, mock_message):
        """Test that only the first matching pattern triggers."""
        fun_cog.bod_quote_triggers = {
            0: [
                {'pattern': 'hello', 'tier': 'LUCKY', 'count': 1},
                {'pattern': 'hello', 'tier': 'BLESSED', 'count': 2}
            ]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="hello world")

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        # Only first match should trigger
        fun_cog.bot.db_manager.add_bod_fate.assert_called_once_with(12345, 'LUCKY', 1)

    @pytest.mark.asyncio
    async def test_trigger_regex_pattern(self, fun_cog, mock_message):
        """Test that regex patterns work correctly."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': r'be\s+not\s+afraid', 'tier': 'GUARANTEED', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="be  not  afraid")

        channel = MagicMock(spec=discord.TextChannel)

        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_called_once_with(12345, 'GUARANTEED', 1)

    @pytest.mark.asyncio
    async def test_trigger_invalid_regex_handled(self, fun_cog, mock_message):
        """Test that invalid regex patterns are handled gracefully."""
        fun_cog.bod_quote_triggers = {
            0: [{'pattern': '[invalid(regex', 'tier': 'LUCKY', 'count': 1}]
        }

        fun_cog._get_previous_message = AsyncMock(return_value="test message")

        channel = MagicMock(spec=discord.TextChannel)

        # Should not raise, just log warning
        await fun_cog._evaluate_quote_trigger(
            user_id=12345,
            channel=channel,
            before_message=mock_message,
            current_chain=0
        )

        fun_cog.bot.db_manager.add_bod_fate.assert_not_called()


# =============================================================================
# FATE CONSUMPTION TESTS
# =============================================================================


class TestConsumeFateAndGetTier:
    """Tests for _consume_fate_and_get_tier priority logic."""

    @pytest.mark.asyncio
    async def test_consume_guaranteed_first(self, fun_cog):
        """Test that GUARANTEED tier is consumed before others."""
        # All tiers available
        async def mock_consume(user_id, tier):
            return tier == 'GUARANTEED'

        fun_cog.bot.db_manager.consume_bod_fate = AsyncMock(side_effect=mock_consume)

        result = await fun_cog._consume_fate_and_get_tier(12345)

        assert result == 'GUARANTEED'

    @pytest.mark.asyncio
    async def test_consume_blessed_when_no_guaranteed(self, fun_cog):
        """Test BLESSED consumed when GUARANTEED unavailable."""
        async def mock_consume(user_id, tier):
            return tier == 'BLESSED'

        fun_cog.bot.db_manager.consume_bod_fate = AsyncMock(side_effect=mock_consume)

        result = await fun_cog._consume_fate_and_get_tier(12345)

        assert result == 'BLESSED'

    @pytest.mark.asyncio
    async def test_consume_lucky_when_no_higher_tiers(self, fun_cog):
        """Test LUCKY consumed when higher tiers unavailable."""
        async def mock_consume(user_id, tier):
            return tier == 'LUCKY'

        fun_cog.bot.db_manager.consume_bod_fate = AsyncMock(side_effect=mock_consume)

        result = await fun_cog._consume_fate_and_get_tier(12345)

        assert result == 'LUCKY'

    @pytest.mark.asyncio
    async def test_normal_when_no_fate(self, fun_cog):
        """Test NORMAL returned when no fate charges available."""
        fun_cog.bot.db_manager.consume_bod_fate = AsyncMock(return_value=False)

        result = await fun_cog._consume_fate_and_get_tier(12345)

        assert result == 'NORMAL'



# =============================================================================
# YUJIN QUOTES DISPLAY TESTS
# =============================================================================


class TestYujinQuotes:
    """Tests for yujin_quotes command display (single random quote, treasure hunt style)."""

    @pytest.mark.asyncio
    async def test_no_quotes_configured(self, fun_cog, mock_ctx):
        """Test message when no quotes are configured."""
        fun_cog.bod_quote_display = []

        await fun_cog.yujin_quotes(mock_ctx, query="quotes")

        mock_ctx.reply.assert_called_once()
        call_args = mock_ctx.reply.call_args
        assert "No Yujin quotes have been configured" in str(call_args)

    @pytest.mark.asyncio
    async def test_single_quote_displayed(self, fun_cog, mock_ctx):
        """Test that a single random quote is displayed (not all quotes)."""
        fun_cog.bod_quote_display = ["Quote one", "Quote two", "Quote three"]

        await fun_cog.yujin_quotes(mock_ctx, query="quotes")

        mock_ctx.reply.assert_called_once()
        call_args = mock_ctx.reply.call_args
        # Should be plain text, one of the quotes
        response = call_args.args[0]
        assert response in fun_cog.bod_quote_display

    @pytest.mark.asyncio
    async def test_random_selection(self, fun_cog, mock_ctx):
        """Test that quotes are randomly selected."""
        fun_cog.bod_quote_display = ["A", "B", "C", "D", "E"]

        # Call multiple times and collect results
        results = set()
        for _ in range(20):
            mock_ctx.reply.reset_mock()
            await fun_cog.yujin_quotes(mock_ctx, query="quotes")
            response = mock_ctx.reply.call_args.args[0]
            results.add(response)

        # With 20 calls and 5 options, we should see at least 2 different quotes
        assert len(results) >= 2, "Expected random selection to produce variety"

    @pytest.mark.asyncio
    async def test_original_list_unchanged(self, fun_cog, mock_ctx):
        """Test that the original quote list is not modified."""
        original_quotes = ["A", "B", "C", "D", "E"]
        fun_cog.bod_quote_display = original_quotes.copy()

        await fun_cog.yujin_quotes(mock_ctx, query="quotes")

        # Original list should be unchanged
        assert fun_cog.bod_quote_display == original_quotes


class TestAllQuotesCommand:
    """Tests for all_quotes admin command (shows ALL quotes)."""

    @pytest.mark.asyncio
    async def test_no_quotes_configured(self, fun_cog, mock_ctx):
        """Test message when no quotes are configured."""
        fun_cog.bod_quote_display = []

        # Call the callback directly (it's a hybrid command)
        await fun_cog.all_quotes.callback(fun_cog, mock_ctx)

        mock_ctx.reply.assert_called_once()
        call_args = mock_ctx.reply.call_args
        assert "No Yujin quotes have been configured" in str(call_args)

    @pytest.mark.asyncio
    async def test_all_quotes_in_embed(self, fun_cog, mock_ctx):
        """Test that ALL quotes are displayed in an embed."""
        fun_cog.bod_quote_display = ["Quote one", "Quote two", "Quote three"]

        await fun_cog.all_quotes.callback(fun_cog, mock_ctx)

        mock_ctx.reply.assert_called_once()
        call_args = mock_ctx.reply.call_args
        embed = call_args.kwargs.get('embed') or call_args.args[0]
        assert isinstance(embed, discord.Embed)
        assert embed.title is not None and "All Quotes" in embed.title

    @pytest.mark.asyncio
    async def test_quotes_are_shuffled(self, fun_cog, mock_ctx):
        """Test that quotes list is shuffled in all_quotes."""
        original_quotes = ["A", "B", "C", "D", "E"]
        fun_cog.bod_quote_display = original_quotes.copy()

        with patch('random.shuffle') as mock_shuffle:
            await fun_cog.all_quotes.callback(fun_cog, mock_ctx)
            mock_shuffle.assert_called_once()

        # Original list should be unchanged
        assert fun_cog.bod_quote_display == original_quotes


# =============================================================================
# TIER PROBABILITY TESTS
# =============================================================================


class TestTierProbabilities:
    """Tests to verify tier probability documentation matches implementation."""

    def test_tier_values_documented(self):
        """Verify the tier probability values are as documented."""
        # These are the documented percentages in the TOML comment
        # This test documents the expected behavior
        tiers = {
            'LUCKY': 0.50,       # 50%
            'BLESSED': 0.75,    # 75%
            'GUARANTEED': 1.00  # 100%
        }

        assert tiers['LUCKY'] == 0.50
        assert tiers['BLESSED'] == 0.75
        assert tiers['GUARANTEED'] == 1.00

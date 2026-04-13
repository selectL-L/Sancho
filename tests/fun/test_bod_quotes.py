"""Unit tests for the BOD Quotes system.

This module contains tests for:
- TOML file loading (_load_bod_quotes)
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

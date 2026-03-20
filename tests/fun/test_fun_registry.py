"""Unit tests for the Fun command registry and dispatcher.

This module contains tests for:
- FunCommand dataclass validation
- _FUN_COMMAND_LOOKUP dictionary
- FUN_NLP_ENTRIES auto-export
- __getattr__ dynamic method resolution
- _dispatch_fun_command dispatcher
- _resolve_fun_source helper
"""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

import config
from cogs.fun import (
    Fun,
    FunCommand,
    FUN_COMMANDS,
    FUN_NLP_ENTRIES,
    _FUN_COMMAND_LOOKUP,
)
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
    bot.get_cog = MagicMock(return_value=None)
    return bot


@pytest.fixture
def fun_cog(mock_bot):
    """Create a Fun cog instance with mocked bot."""
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


# =============================================================================
# FUNCOMMAND DATACLASS TESTS
# =============================================================================


class TestFunCommandDataclass:
    """Tests for the FunCommand dataclass."""

    def test_basic_creation(self):
        """Test creating a FunCommand with minimal args."""
        cmd = FunCommand(
            name='test',
            patterns=(r'\btest\b',),
            error_msg="Test error"
        )
        assert cmd.name == 'test'
        assert cmd.patterns == (r'\btest\b',)
        assert cmd.error_msg == "Test error"

    def test_default_values(self):
        """Test that defaults are set correctly."""
        cmd = FunCommand('test', (r'\btest\b',), error_msg="Error")
        assert cmd.is_image is False
        assert cmd.content is None
        assert cmd.file is None
        assert cmd.attr is None
        assert cmd.random is True
        assert cmd.require_query is False
        assert cmd.query_error == "You need to provide something!"  # Has default

    def test_with_content(self):
        """Test FunCommand with static content."""
        cmd = FunCommand(
            'static', (r'\bstatic\b',),
            content="Static response",
            error_msg="Error"
        )
        assert cmd.content == "Static response"

    def test_with_file(self):
        """Test FunCommand with file source."""
        cmd = FunCommand(
            'file_cmd', (r'\bfile\b',),
            file='test.txt',
            error_msg="Error"
        )
        assert cmd.file == 'test.txt'

    def test_with_require_query(self):
        """Test FunCommand that requires a query."""
        cmd = FunCommand(
            'query_cmd', (r'\bquery\b',),
            file='test.txt',
            require_query=True,
            query_error="Need a query!",
            error_msg="Error"
        )
        assert cmd.require_query is True
        assert cmd.query_error == "Need a query!"


# =============================================================================
# REGISTRY STRUCTURE TESTS
# =============================================================================


class TestRegistryStructure:
    """Tests for the FUN_COMMANDS registry and related data structures."""

    def test_fun_commands_is_list(self):
        """Test that FUN_COMMANDS is a list."""
        assert isinstance(FUN_COMMANDS, list)

    def test_all_entries_are_funcommands(self):
        """Test that all registry entries are FunCommand instances."""
        for cmd in FUN_COMMANDS:
            assert isinstance(cmd, FunCommand), f"{cmd} is not a FunCommand"

    def test_all_entries_have_required_fields(self):
        """Test that all entries have name, patterns, and error_msg."""
        for cmd in FUN_COMMANDS:
            assert cmd.name, f"Command missing name: {cmd}"
            assert cmd.patterns, f"Command {cmd.name} missing patterns"
            assert cmd.error_msg, f"Command {cmd.name} missing error_msg"

    def test_all_names_unique(self):
        """Test that all command names are unique."""
        names = [cmd.name for cmd in FUN_COMMANDS]
        assert len(names) == len(set(names)), "Duplicate command names found"

    def test_lookup_dict_matches_registry(self):
        """Test that _FUN_COMMAND_LOOKUP contains all registry entries."""
        assert len(_FUN_COMMAND_LOOKUP) == len(FUN_COMMANDS)
        for cmd in FUN_COMMANDS:
            assert cmd.name in _FUN_COMMAND_LOOKUP
            assert _FUN_COMMAND_LOOKUP[cmd.name] is cmd


class TestNlpEntriesExport:
    """Tests for FUN_NLP_ENTRIES auto-export."""

    def test_entries_is_list(self):
        """Test that FUN_NLP_ENTRIES is a list."""
        assert isinstance(FUN_NLP_ENTRIES, list)

    def test_entries_count_matches_registry(self):
        """Test that NLP entries count matches registry."""
        assert len(FUN_NLP_ENTRIES) == len(FUN_COMMANDS)

    def test_entry_format(self):
        """Test that each entry has correct format (patterns, cog_name, method_name)."""
        for entry in FUN_NLP_ENTRIES:
            assert isinstance(entry, tuple), f"Entry is not a tuple: {entry}"
            assert len(entry) == 3, f"Entry doesn't have 3 elements: {entry}"
            patterns, cog_name, method_name = entry
            assert isinstance(patterns, tuple), f"Patterns is not a tuple: {patterns}"
            assert cog_name == 'Fun', f"Cog name is not 'Fun': {cog_name}"
            assert isinstance(method_name, str), f"Method name is not a string: {method_name}"

    def test_patterns_are_regex_strings(self):
        """Test that all patterns are valid regex strings."""
        import re
        for entry in FUN_NLP_ENTRIES:
            patterns, _, _ = entry
            for pattern in patterns:
                # Should not raise
                re.compile(pattern)


# =============================================================================
# __getattr__ DYNAMIC METHOD RESOLUTION TESTS
# =============================================================================


class TestGetattr:
    """Tests for __getattr__ dynamic method resolution."""

    def test_getattr_returns_handler_for_registered_command(self, fun_cog):
        """Test that __getattr__ returns a handler for registered commands."""
        # 'sanitize' is in the registry
        handler = getattr(fun_cog, 'sanitize', None)
        assert handler is not None
        assert callable(handler)

    def test_getattr_raises_for_unknown_attribute(self, fun_cog):
        """Test that __getattr__ raises AttributeError for unknown names."""
        with pytest.raises(AttributeError):
            _ = fun_cog.nonexistent_method

    def test_explicit_methods_take_precedence(self, fun_cog):
        """Test that explicit methods (bod, yujin_quotes) aren't handled by __getattr__."""
        # These are explicit methods, not registry-driven
        bod_method = fun_cog.bod
        assert bod_method is not None
        # Should be the actual method, not a __getattr__ wrapper
        assert hasattr(bod_method, '__self__')  # Bound method check

    def test_handler_is_coroutine_function(self, fun_cog):
        """Test that returned handler is an async function."""
        import asyncio
        # getattr is intentional - testing __getattr__ dynamic resolution
        handler = getattr(fun_cog, 'issues')  # noqa: B009
        assert asyncio.iscoroutinefunction(handler)


# =============================================================================
# _resolve_fun_source TESTS
# =============================================================================


class TestResolveFunSource:
    """Tests for _resolve_fun_source helper method."""

    @pytest.mark.asyncio
    async def test_resolve_content(self, fun_cog):
        """Test resolving static content."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            content="Static content",
            error_msg="Error"
        )
        result = await fun_cog._resolve_fun_source(cmd)
        assert result == ["Static content"]

    @pytest.mark.asyncio
    async def test_resolve_file(self, fun_cog):
        """Test resolving content from a file."""
        # Create a temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Line 1\nLine 2\nLine 3\n")
            temp_path = f.name

        try:
            cmd = FunCommand(
                'test', (r'\btest\b',),
                file=os.path.basename(temp_path),
                error_msg="Error"
            )
            # Patch ASSETS_PATH to point to temp dir
            with patch.object(config, 'ASSETS_PATH', os.path.dirname(temp_path)):
                result = await fun_cog._resolve_fun_source(cmd)
            assert result == ["Line 1", "Line 2", "Line 3"]
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_resolve_attr(self, fun_cog):
        """Test resolving content from runtime attribute."""
        fun_cog.test_attr = ["Item A", "Item B"]
        cmd = FunCommand(
            'test', (r'\btest\b',),
            attr='test_attr',
            error_msg="Error"
        )
        result = await fun_cog._resolve_fun_source(cmd)
        assert result == ["Item A", "Item B"]

    @pytest.mark.asyncio
    async def test_resolve_attr_missing(self, fun_cog):
        """Test resolving missing attribute returns empty list."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            attr='nonexistent_attr',
            error_msg="Error"
        )
        result = await fun_cog._resolve_fun_source(cmd)
        assert result == []

    @pytest.mark.asyncio
    async def test_resolve_empty_returns_empty(self, fun_cog):
        """Test that command with no source returns empty list."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            error_msg="Error"
        )
        result = await fun_cog._resolve_fun_source(cmd)
        assert result == []


# =============================================================================
# _dispatch_fun_command TESTS
# =============================================================================


class TestDispatchFunCommand:
    """Tests for _dispatch_fun_command dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_static_content(self, fun_cog, mock_ctx):
        """Test dispatching a command with static content."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            content="Hello!",
            error_msg="Error"
        )
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once_with("Hello!")

    @pytest.mark.asyncio
    async def test_dispatch_random_from_list(self, fun_cog, mock_ctx):
        """Test dispatching picks random item when random=True."""
        fun_cog.test_list = ["A", "B", "C"]
        cmd = FunCommand(
            'test', (r'\btest\b',),
            attr='test_list',
            random=True,
            error_msg="Error"
        )
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once()
        # Response should be one of the items
        response = mock_ctx.reply.call_args[0][0]
        assert response in ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_dispatch_first_from_list(self, fun_cog, mock_ctx):
        """Test dispatching picks first item when random=False."""
        fun_cog.test_list = ["First", "Second", "Third"]
        cmd = FunCommand(
            'test', (r'\btest\b',),
            attr='test_list',
            random=False,
            error_msg="Error"
        )
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once_with("First")

    @pytest.mark.asyncio
    async def test_dispatch_empty_source_sends_error(self, fun_cog, mock_ctx):
        """Test dispatching with empty source sends error message."""
        fun_cog.empty_list = []
        cmd = FunCommand(
            'test', (r'\btest\b',),
            attr='empty_list',
            error_msg="Nothing available!"
        )
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once_with("Nothing available!")

    @pytest.mark.asyncio
    async def test_dispatch_require_query_no_query(self, fun_cog, mock_ctx):
        """Test that require_query=True with no query sends query_error."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            content="Response",
            require_query=True,
            query_error="Need a question!",
            error_msg="Error"
        )
        # Query contains only the command trigger
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once_with("Need a question!")

    @pytest.mark.asyncio
    async def test_dispatch_require_query_with_query(self, fun_cog, mock_ctx):
        """Test that require_query=True with query proceeds normally."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            content="Response",
            require_query=True,
            query_error="Need a question!",
            error_msg="Error"
        )
        # Query contains command trigger plus additional text
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test will I succeed?")
        mock_ctx.reply.assert_called_once_with("Response")

    @pytest.mark.asyncio
    async def test_dispatch_file_not_found(self, fun_cog, mock_ctx):
        """Test that missing file sends error message."""
        cmd = FunCommand(
            'test', (r'\btest\b',),
            file='nonexistent.txt',
            error_msg="File not found!"
        )
        await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")
        mock_ctx.reply.assert_called_once_with("File not found!")


class TestDispatchFunCommandImage:
    """Tests for _dispatch_fun_command with image commands."""

    @pytest.mark.asyncio
    async def test_dispatch_image_from_content(self, fun_cog, mock_ctx):
        """Test dispatching an image command with content (filename) source."""
        # Create temp image file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            f.write(b'fake image data')
            temp_path = f.name

        try:
            # Use content= for direct filename, not file= (which is for text files)
            cmd = FunCommand(
                'test_img', (r'\btest\b',),
                content=os.path.basename(temp_path),
                is_image=True,
                error_msg="Error"
            )
            with patch.object(config, 'ASSETS_PATH', os.path.dirname(temp_path)):
                await fun_cog._dispatch_fun_command(cmd, mock_ctx, "test")

            mock_ctx.reply.assert_called_once()
            call_kwargs = mock_ctx.reply.call_args.kwargs
            assert 'file' in call_kwargs
            assert isinstance(call_kwargs['file'], discord.File)
            # Close file handle before cleanup
            call_kwargs['file'].close()
        finally:
            os.unlink(temp_path)


# =============================================================================
# END-TO-END GETATTR -> DISPATCH TESTS
# =============================================================================


class TestGetAttrDispatchIntegration:
    """Integration tests for __getattr__ -> _dispatch_fun_command flow."""

    @pytest.mark.asyncio
    async def test_issues_command_via_getattr(self, fun_cog, mock_ctx):
        """Test that 'issues' command works via __getattr__."""
        # getattr is intentional - testing __getattr__ dynamic resolution
        handler = getattr(fun_cog, 'issues')  # noqa: B009
        await handler(mock_ctx, query="issues")

        mock_ctx.reply.assert_called_once()
        response = mock_ctx.reply.call_args[0][0]
        assert "issues" in response.lower() or "github" in response.lower()

    @pytest.mark.asyncio
    async def test_sanitize_command_via_getattr(self, fun_cog, mock_ctx):
        """Test that 'sanitize' command works via __getattr__."""
        # Create a temp file to serve as the sanitize image
        with tempfile.NamedTemporaryFile(suffix='.webp', delete=False) as f:
            f.write(b'fake image')
            temp_dir = os.path.dirname(f.name)
            # The sanitize command expects 'sanitize.webp'
            temp_file = os.path.join(temp_dir, 'sanitize.webp')

        try:
            # Copy to expected name
            import shutil
            shutil.copy(f.name, temp_file)
            os.unlink(f.name)

            # getattr is intentional - testing __getattr__ dynamic resolution
            handler = getattr(fun_cog, 'sanitize')  # noqa: B009
            with patch.object(config, 'ASSETS_PATH', temp_dir):
                await handler(mock_ctx, query="sanitize")

            mock_ctx.reply.assert_called_once()
            call_kwargs = mock_ctx.reply.call_args.kwargs
            assert 'file' in call_kwargs
            # Close file handle before cleanup
            call_kwargs['file'].close()
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)


# =============================================================================
# DYNAMIC NLP REGISTRATION TESTS
# =============================================================================


class TestDynamicNlpRegistration:
    """Tests for dynamic NLP registration in CoreBot."""

    def test_corebot_has_dynamic_nlp_groups(self):
        """Test that CoreBot has _dynamic_nlp_groups list."""
        with patch('utils.bot_class.discover_cogs'):
            bot = CoreBot()
            assert hasattr(bot, '_dynamic_nlp_groups')
            assert isinstance(bot._dynamic_nlp_groups, list)
            assert len(bot._dynamic_nlp_groups) == 0

    def test_register_nlp_group_adds_entries(self):
        """Test that register_nlp_group adds entries to the list."""
        with patch('utils.bot_class.discover_cogs'):
            bot = CoreBot()
            test_entries = [
                ((r'\btest\b',), 'TestCog', 'test_method'),
            ]
            bot.register_nlp_group(test_entries)
            assert len(bot._dynamic_nlp_groups) == 1
            assert bot._dynamic_nlp_groups[0] is test_entries

    def test_multiple_registrations(self):
        """Test registering multiple groups."""
        with patch('utils.bot_class.discover_cogs'):
            bot = CoreBot()
            group1 = [((r'\bone\b',), 'Cog1', 'method1')]
            group2 = [((r'\btwo\b',), 'Cog2', 'method2')]

            bot.register_nlp_group(group1)
            bot.register_nlp_group(group2)

            assert len(bot._dynamic_nlp_groups) == 2

"""Pytest configuration for the test suite.

This file is automatically loaded by pytest before any tests run.
It ensures the project root is in sys.path so that imports work correctly
in CI environments (e.g., GitHub Actions).

Shared fixtures:
    mock_bot: A minimal mock CoreBot with db_manager and loop.
    mock_ctx: A minimal mock commands.Context tied to mock_bot.

Test files can override these fixtures locally if they need extra attributes
(e.g., bot.wait_for, bot.user, ctx.guild).
"""
import os
import sys

# Add project root to sys.path BEFORE any test modules are imported
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from utils.bot_class import CoreBot
from utils.database import DatabaseManager


@pytest.fixture
def mock_bot():
    """Create a minimal mock CoreBot instance.

    Provides: spec=CoreBot, db_manager, loop.
    Override locally to add bot.wait_for, bot.user, etc.
    """
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def mock_ctx(mock_bot):
    """Create a minimal mock commands.Context.

    Provides: spec=commands.Context, bot, author (id, display_name, mention),
    channel.id, send (returns mock message).
    Override locally to add ctx.guild, ctx.message, etc.
    """
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.author.id = 12345
    ctx.author.display_name = "TestUser"
    ctx.author.mention = "<@12345>"
    ctx.channel.id = 67890

    async def send_mock(*args, **kwargs):
        return MagicMock()
    ctx.send = AsyncMock(side_effect=send_mock)

    return ctx

"""Startup-focused tests for the Music cog."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.music import Music


@pytest.fixture
def mock_bot():
    """Create a mock bot instance."""
    bot = MagicMock()
    bot.db_manager = MagicMock()
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def music_cog(mock_bot):
    """Create a Music cog with minimal startup dependencies."""
    cog = Music(mock_bot)
    return cog


class TestMusicStartup:
    """Tests for non-blocking POT startup behavior."""

    @pytest.mark.asyncio
    async def test_cog_ready_schedules_pot_startup_in_background(self, music_cog):
        """cog_ready should not wait for POT startup readiness checks."""
        music_cog.bot.loop = asyncio.get_running_loop()
        pot_start_entered = asyncio.Event()
        release_pot_start = asyncio.Event()

        async def delayed_start() -> bool:
            pot_start_entered.set()
            await release_pot_start.wait()
            return True

        async def run_to_thread(func, *args):
            return func(*args)

        with patch('cogs.music.YTDLP_AVAILABLE', True):
            with patch.object(music_cog, '_start_pot_server', new=AsyncMock(side_effect=delayed_start)) as start_mock:
                with patch.object(music_cog.cache_manager, 'initialize', new=AsyncMock()):
                    with patch.object(music_cog, '_presence_loop', new=AsyncMock()):
                        with patch.object(music_cog, '_start_cache_background_tasks'):
                            with patch.object(music_cog, '_pot_health_watchdog', new=AsyncMock()):
                                with patch('cogs.music.subscribe_playlist_change'):
                                    with patch('cogs.music.start_music', return_value=(None, None)):
                                        with patch('cogs.music.ambience.initialize'):
                                            with patch('cogs.music.asyncio.to_thread', new=AsyncMock(side_effect=run_to_thread)):
                                                with patch('utils.musicutils.music_auth.detect_youtube_auth') as detect_mock:
                                                    await asyncio.wait_for(music_cog.cog_ready(), timeout=0.2)

                                                    await asyncio.wait_for(pot_start_entered.wait(), timeout=0.2)
                                                    assert music_cog._pot_start_task is not None
                                                    assert not music_cog._pot_start_task.done()
                                                    assert start_mock.await_count == 1

                                                    pot_task = music_cog._pot_start_task
                                                    release_pot_start.set()
                                                    await asyncio.wait_for(pot_task, timeout=0.2)
                                                    await asyncio.sleep(0)

                                                    detect_mock.assert_called_once_with('startup')
                                                    assert music_cog._pot_start_task is None

    @pytest.mark.asyncio
    async def test_stop_pot_server_cancels_pending_background_startup(self, music_cog):
        """Stopping POT should cancel any in-flight background startup task."""
        startup_cancelled = asyncio.Event()

        async def pending_startup() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                startup_cancelled.set()
                raise

        music_cog._pot_start_task = asyncio.create_task(pending_startup())
        await asyncio.sleep(0)

        await music_cog._stop_pot_server()

        assert startup_cancelled.is_set()
        assert music_cog._pot_start_task is None

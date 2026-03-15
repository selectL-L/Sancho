"""tests/test_lifecycle.py

Test harness for the lifecycle shutdown redesign.

Tests the full shutdown flow with mocked Discord calls on Windows by
patching platform checks and Linux-only dependencies. Covers:
    - ShutdownContext construction from all six reason paths
    - Template matching (reasons → visual outcomes)
    - D-Bus deposit handlers (PrepareForShutdown signal simulation)
    - Goodbye message dispatch (channel messages + owner DMs)
    - Concurrent cog teardown with priority ordering and timing
    - Duplicate shutdown guard
    - Full shutdown_handler orchestration end-to-end
"""

import asyncio
import signal
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from utils.lifecycle import (
    ShutdownReason,
    ShutdownContext,
    PendingSystemShutdown,
    GOODBYE_TEMPLATES,
    _OWNER_NOTIFY_REASONS,
    _find_template,
    build_shutdown_context,
    log_phase,
    _on_prepare_for_shutdown,
    _on_prepare_for_shutdown_with_metadata,
    _send_goodbye_message,
    _send_owner_notification,
    _teardown_cogs_ordered,
    shutdown_handler,
    teardown_shutdown_detection,
)
import utils.lifecycle as lifecycle_module


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset all module-level state between tests.

    Without this, _is_shutting_down=True from one test would cause the next
    shutdown_handler call to early-return.
    """
    lifecycle_module._is_shutting_down = False
    lifecycle_module._has_initialized = False
    lifecycle_module._current_phase = None
    lifecycle_module._dbus_connection = None
    lifecycle_module._inhibitor_fd = None
    lifecycle_module._pending_system_shutdown = None
    yield
    # Clean up after test too
    lifecycle_module._is_shutting_down = False
    lifecycle_module._has_initialized = False
    lifecycle_module._current_phase = None
    lifecycle_module._dbus_connection = None
    lifecycle_module._inhibitor_fd = None
    lifecycle_module._pending_system_shutdown = None


@pytest.fixture
def mock_bot():
    """A mock CoreBot with the interface lifecycle.py expects."""
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.name = "TestBot"
    bot.user.id = 111111
    bot.user.__str__ = lambda self: "TestBot#0001"

    # Guilds
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Test Guild"
    guild.id = 222222
    bot.guilds = [guild]

    # System channel — must pass isinstance(channel, discord.TextChannel)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock())
    bot.get_channel = MagicMock(return_value=channel)

    # Command tree
    bot.tree = MagicMock()
    bot.tree.sync = AsyncMock()
    bot.tree.copy_global_to = MagicMock()

    # NLP registration
    bot.register_nlp_command = MagicMock()

    # Presence
    bot.current_visibility = discord.Status.online
    bot.change_presence = AsyncMock()

    # Cog ready
    bot.ready_all_cogs = AsyncMock()

    # Resource tracker
    bot.resource_tracker = MagicMock()
    bot.resource_tracker.start = AsyncMock()
    bot.resource_tracker.stop = AsyncMock()

    # Extensions and cogs for teardown
    bot.extensions = {}
    bot.cogs = {}
    bot.unload_extension = AsyncMock()
    bot.close = AsyncMock()

    # Owner DM support
    mock_owner = MagicMock()
    mock_owner.send = AsyncMock()
    bot.fetch_user = AsyncMock(return_value=mock_owner)

    return bot


def _make_cog_mock(name: str, priority: int = 0) -> MagicMock:
    """Creates a mock cog with an optional SHUTDOWN_PRIORITY."""
    cog = MagicMock()
    cog.__class__.__name__ = name
    if priority > 0:
        cog.SHUTDOWN_PRIORITY = priority
    else:
        # Simulate no attribute — getattr(..., 'SHUTDOWN_PRIORITY', 0) returns 0
        del cog.SHUTDOWN_PRIORITY
    return cog


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Types & Template Matching
# ═══════════════════════════════════════════════════════════════════════════════

class TestShutdownReason:
    """Tests for the ShutdownReason enum and is_returning property."""

    @pytest.mark.parametrize("reason,expected", [
        pytest.param(ShutdownReason.RESTART, True, id="restart"),
        pytest.param(ShutdownReason.UPGRADE_RESTART, True, id="upgrade_restart"),
        pytest.param(ShutdownReason.SYSTEM_REBOOT, True, id="system_reboot"),
        pytest.param(ShutdownReason.SYSTEM_UPGRADE, True, id="system_upgrade"),
        pytest.param(ShutdownReason.MANUAL_STOP, False, id="manual_stop"),
        pytest.param(ShutdownReason.SYSTEM_POWEROFF, False, id="system_poweroff"),
    ])
    def test_is_returning(self, reason: ShutdownReason, expected: bool) -> None:
        assert reason.is_returning == expected

    def test_all_reasons_have_six_members(self) -> None:
        assert len(ShutdownReason) == 6


class TestTemplateMatching:
    """Tests that every reason maps to the correct goodbye template."""

    def test_all_reasons_covered_by_templates(self) -> None:
        """Every ShutdownReason must appear in exactly one template."""
        covered_reasons: set[ShutdownReason] = set()
        for template in GOODBYE_TEMPLATES:
            covered_reasons.update(template.reasons)
        assert covered_reasons == set(ShutdownReason)

    def test_no_reason_in_multiple_templates(self) -> None:
        """No reason should appear in more than one template."""
        seen: set[ShutdownReason] = set()
        for template in GOODBYE_TEMPLATES:
            overlap = seen & template.reasons
            assert not overlap, f"Reasons {overlap} appear in multiple templates"
            seen.update(template.reasons)

    @pytest.mark.parametrize("reason,expected_gif", [
        pytest.param(ShutdownReason.RESTART, "reboot.gif", id="restart→reboot"),
        pytest.param(ShutdownReason.UPGRADE_RESTART, "reboot.gif", id="upgrade_restart→reboot"),
        pytest.param(ShutdownReason.SYSTEM_REBOOT, "reboot.gif", id="system_reboot→reboot"),
        pytest.param(ShutdownReason.SYSTEM_UPGRADE, "upgrade.gif", id="system_upgrade→upgrade"),
        pytest.param(ShutdownReason.MANUAL_STOP, "shutdown.gif", id="manual_stop→shutdown"),
        pytest.param(ShutdownReason.SYSTEM_POWEROFF, "shutdown.gif", id="system_poweroff→shutdown"),
    ])
    def test_reason_to_gif_mapping(self, reason: ShutdownReason, expected_gif: str) -> None:
        template = _find_template(reason)
        assert template.gif == expected_gif

    @patch('utils.lifecycle.config')
    def test_format_title_substitutes_bot_name(self, mock_config: MagicMock) -> None:
        mock_config.BOT_NAME = "Marine"
        template = _find_template(ShutdownReason.RESTART)
        title = template.format_title()
        assert "Marine" in title
        assert "{name}" not in title

    def test_owner_notify_reasons(self) -> None:
        """Only UPGRADE_RESTART and SYSTEM_UPGRADE trigger owner DMs."""
        assert _OWNER_NOTIFY_REASONS == {
            ShutdownReason.UPGRADE_RESTART,
            ShutdownReason.SYSTEM_UPGRADE,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Test: D-Bus Deposit Handlers
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBusHandlers:
    """Tests the D-Bus signal handlers that deposit PendingSystemShutdown."""

    @patch('utils.lifecycle._read_reboot_required_packages', return_value=[])
    @patch('utils.lifecycle._release_inhibitor')
    def test_prepare_for_shutdown_deposits_info(self, mock_release: MagicMock, mock_pkgs: MagicMock) -> None:
        """PrepareForShutdown (pre-255) deposits with shutdown_type=None."""
        _on_prepare_for_shutdown(active=True)

        pending = lifecycle_module._pending_system_shutdown
        assert pending is not None
        assert pending.shutdown_type is None
        assert pending.packages == []
        assert pending.timestamp > 0
        mock_release.assert_called_once()

    @patch('utils.lifecycle._read_reboot_required_packages', return_value=[])
    @patch('utils.lifecycle._release_inhibitor')
    def test_prepare_for_shutdown_ignores_inactive(self, mock_release: MagicMock, mock_pkgs: MagicMock) -> None:
        """active=False means shutdown was cancelled — ignore it."""
        _on_prepare_for_shutdown(active=False)
        assert lifecycle_module._pending_system_shutdown is None
        mock_release.assert_not_called()

    @patch('utils.lifecycle._read_reboot_required_packages', return_value=['linux-image-6.1'])
    @patch('utils.lifecycle._release_inhibitor')
    def test_prepare_with_metadata_reboot(self, mock_release: MagicMock, mock_pkgs: MagicMock) -> None:
        """PrepareForShutdownWithMetadata with type='reboot' and packages."""
        _on_prepare_for_shutdown_with_metadata(active=True, metadata={'type': 'reboot'})

        pending = lifecycle_module._pending_system_shutdown
        assert pending is not None
        assert pending.shutdown_type == 'reboot'
        assert pending.packages == ['linux-image-6.1']
        mock_release.assert_called_once()

    @patch('utils.lifecycle._read_reboot_required_packages', return_value=[])
    @patch('utils.lifecycle._release_inhibitor')
    def test_prepare_with_metadata_poweroff(self, mock_release: MagicMock, mock_pkgs: MagicMock) -> None:
        """PrepareForShutdownWithMetadata with type='poweroff'."""
        _on_prepare_for_shutdown_with_metadata(active=True, metadata={'type': 'poweroff'})

        pending = lifecycle_module._pending_system_shutdown
        assert pending is not None
        assert pending.shutdown_type == 'poweroff'
        mock_release.assert_called_once()

    @patch('utils.lifecycle._read_reboot_required_packages', return_value=[])
    @patch('utils.lifecycle._release_inhibitor')
    def test_prepare_with_metadata_dbus_variant(self, mock_release: MagicMock, mock_pkgs: MagicMock) -> None:
        """D-Bus Variant objects have a .value attribute — handler should unwrap."""
        variant = MagicMock()
        variant.value = 'reboot'
        _on_prepare_for_shutdown_with_metadata(active=True, metadata={'type': variant})

        pending = lifecycle_module._pending_system_shutdown
        assert pending is not None
        assert pending.shutdown_type == 'reboot'


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Context Resolution (build_shutdown_context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildShutdownContext:
    """Tests build_shutdown_context — the decision engine."""

    def test_explicit_reason_passthrough(self) -> None:
        """When reason is provided, it's used directly."""
        ctx = build_shutdown_context(signal.SIGTERM, reason=ShutdownReason.MANUAL_STOP)
        assert ctx.reason == ShutdownReason.MANUAL_STOP
        assert ctx.trigger == signal.SIGTERM

    def test_explicit_reason_with_pending_packages(self) -> None:
        """Even with explicit reason, packages from D-Bus are included."""
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='reboot', packages=['libssl3'], timestamp=time.monotonic()
        )
        ctx = build_shutdown_context(signal.SIGTERM, reason=ShutdownReason.RESTART)
        assert ctx.reason == ShutdownReason.RESTART
        assert ctx.packages == ['libssl3']

    def test_sigterm_no_dbus_resolves_manual_stop(self) -> None:
        """SIGTERM without D-Bus context → MANUAL_STOP."""
        ctx = build_shutdown_context(signal.SIGTERM, reason=None)
        assert ctx.reason == ShutdownReason.MANUAL_STOP
        assert ctx.packages == []

    def test_sigterm_with_dbus_poweroff(self) -> None:
        """SIGTERM with D-Bus poweroff → SYSTEM_POWEROFF."""
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='poweroff', packages=[], timestamp=time.monotonic()
        )
        ctx = build_shutdown_context(signal.SIGTERM, reason=None)
        assert ctx.reason == ShutdownReason.SYSTEM_POWEROFF

    def test_sigterm_with_dbus_reboot_no_packages(self) -> None:
        """SIGTERM with D-Bus reboot, no packages → SYSTEM_REBOOT."""
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='reboot', packages=[], timestamp=time.monotonic()
        )
        ctx = build_shutdown_context(signal.SIGTERM, reason=None)
        assert ctx.reason == ShutdownReason.SYSTEM_REBOOT

    def test_sigterm_with_dbus_reboot_with_packages(self) -> None:
        """SIGTERM with D-Bus reboot + packages → SYSTEM_UPGRADE."""
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='reboot', packages=['linux-image-6.1', 'util-linux'], timestamp=time.monotonic()
        )
        ctx = build_shutdown_context(signal.SIGTERM, reason=None)
        assert ctx.reason == ShutdownReason.SYSTEM_UPGRADE
        assert ctx.packages == ['linux-image-6.1', 'util-linux']

    @patch('utils.lifecycle.sys')
    @patch('utils.lifecycle._is_apt_upgrade_active', return_value=False)
    def test_sigusr1_plain_restart(self, mock_apt: MagicMock, mock_sys: MagicMock) -> None:
        """SIGUSR1 on Linux without apt-daily-upgrade → RESTART."""
        mock_sys.platform = 'linux'
        SIGUSR1_VALUE: int = getattr(signal, 'SIGUSR1', MagicMock(value=10)).value  # type: ignore[union-attr]
        fake_sig = MagicMock()
        fake_sig.value = SIGUSR1_VALUE
        fake_sig.name = 'SIGUSR1'
        # Patch signal.SIGUSR1 on Windows where it doesn't exist
        fake_sigusr1 = MagicMock()
        fake_sigusr1.value = SIGUSR1_VALUE
        with patch.object(signal, 'SIGUSR1', fake_sigusr1, create=True):
            ctx = build_shutdown_context(fake_sig, reason=None)
        assert ctx.reason == ShutdownReason.RESTART

    @patch('utils.lifecycle.sys')
    @patch('utils.lifecycle._is_apt_upgrade_active', return_value=True)
    def test_sigusr1_upgrade_restart(self, mock_apt: MagicMock, mock_sys: MagicMock) -> None:
        """SIGUSR1 on Linux with apt-daily-upgrade active → UPGRADE_RESTART."""
        mock_sys.platform = 'linux'
        SIGUSR1_VALUE: int = getattr(signal, 'SIGUSR1', MagicMock(value=10)).value  # type: ignore[union-attr]
        fake_sig = MagicMock()
        fake_sig.value = SIGUSR1_VALUE
        fake_sig.name = 'SIGUSR1'
        fake_sigusr1 = MagicMock()
        fake_sigusr1.value = SIGUSR1_VALUE
        with patch.object(signal, 'SIGUSR1', fake_sigusr1, create=True):
            ctx = build_shutdown_context(fake_sig, reason=None)
        assert ctx.reason == ShutdownReason.UPGRADE_RESTART

    def test_context_has_timestamp(self) -> None:
        """ShutdownContext captures a monotonic timestamp."""
        before = time.monotonic()
        ctx = build_shutdown_context(signal.SIGTERM, reason=ShutdownReason.MANUAL_STOP)
        after = time.monotonic()
        assert before <= ctx.timestamp <= after

    def test_context_cog_timings_starts_empty(self) -> None:
        """cog_timings dict starts empty, to be filled during teardown."""
        ctx = build_shutdown_context(signal.SIGTERM, reason=ShutdownReason.MANUAL_STOP)
        assert ctx.cog_timings == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Shutdown Messaging
# ═══════════════════════════════════════════════════════════════════════════════

class TestShutdownMessaging:
    """Tests goodbye messages and owner notification dispatch."""

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_goodbye_message_sent_to_channel(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Goodbye message is sent to the system channel."""
        mock_config.SYSTEM_CHANNEL_ID = 999999
        mock_config.BOT_NAME = "Marine"
        mock_config.ASSETS_PATH = "/fake"

        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _send_goodbye_message(mock_bot, context)

        channel.send.assert_called_once()
        # Verify the build was called with the right template (shutdown.gif for MANUAL_STOP)
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs['gif_filename'] == 'shutdown.gif'

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    async def test_goodbye_skipped_without_channel_id(self, mock_config: MagicMock, mock_bot: MagicMock) -> None:
        """No message sent when SYSTEM_CHANNEL_ID is not configured."""
        mock_config.SYSTEM_CHANNEL_ID = None

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _send_goodbye_message(mock_bot, context)
        mock_bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_owner_upgrade_notification(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """SYSTEM_UPGRADE sends package list DM to owners."""
        mock_config.OWNER_IDS = {12345, 67890}
        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.SYSTEM_UPGRADE,
            packages=['linux-image-6.1', 'util-linux'],
            timestamp=time.monotonic(),
        )
        await _send_owner_notification(mock_bot, context)

        # fetch_user called for each owner
        assert mock_bot.fetch_user.call_count == 2
        # Each owner gets a DM
        mock_owner = mock_bot.fetch_user.return_value
        assert mock_owner.send.call_count == 2

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_owner_reexec_notification(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """UPGRADE_RESTART sends daemon-reexec DM to owners."""
        mock_config.OWNER_IDS = {12345}
        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.UPGRADE_RESTART,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _send_owner_notification(mock_bot, context)

        mock_bot.fetch_user.assert_called_once_with(12345)
        mock_owner = mock_bot.fetch_user.return_value
        mock_owner.send.assert_called_once()

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_non_notify_reason_skips_owner_dm(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """RESTART (plain) does NOT trigger owner DMs."""
        mock_config.OWNER_IDS = {12345}
        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.RESTART,
            packages=[],
            timestamp=time.monotonic(),
        )
        # _send_owner_notification is only called when reason is in _OWNER_NOTIFY_REASONS,
        # so calling directly should route to nothing
        await _send_owner_notification(mock_bot, context)
        mock_bot.fetch_user.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Cog Teardown
# ═══════════════════════════════════════════════════════════════════════════════

class TestCogTeardown:
    """Tests priority ordering, timing, and error handling in cog teardown."""

    @pytest.mark.asyncio
    async def test_teardown_order_respects_priority(self, mock_bot: MagicMock) -> None:
        """Cogs with higher SHUTDOWN_PRIORITY are unloaded first."""
        # Set up three cogs: Music (priority 10), Starboard (priority 5), Fun (default 0)
        mock_bot.extensions = {
            'cogs.fun': MagicMock(),
            'cogs.starboard': MagicMock(),
            'cogs.music': MagicMock(),
        }
        mock_bot.cogs = {
            'Fun': _make_cog_mock('Fun', priority=0),
            'Starboard': _make_cog_mock('Starboard', priority=5),
            'Music': _make_cog_mock('Music', priority=10),
        }

        unload_order: list[str] = []

        async def track_unload(ext: str) -> None:
            unload_order.append(ext)

        mock_bot.unload_extension = AsyncMock(side_effect=track_unload)

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _teardown_cogs_ordered(mock_bot, context)

        # Music (10) first, Starboard (5) second, Fun (0) last
        assert unload_order == ['cogs.music', 'cogs.starboard', 'cogs.fun']

    @pytest.mark.asyncio
    async def test_teardown_records_timings(self, mock_bot: MagicMock) -> None:
        """Each cog's unload time is recorded in context.cog_timings."""
        mock_bot.extensions = {'cogs.fun': MagicMock()}
        mock_bot.cogs = {'Fun': _make_cog_mock('Fun')}

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _teardown_cogs_ordered(mock_bot, context)

        assert 'cogs.fun' in context.cog_timings
        assert context.cog_timings['cogs.fun'] >= 0

    @pytest.mark.asyncio
    async def test_teardown_handles_exception(self, mock_bot: MagicMock) -> None:
        """A cog that throws during unload doesn't stop the others."""
        mock_bot.extensions = {
            'cogs.broken': MagicMock(),
            'cogs.healthy': MagicMock(),
        }
        mock_bot.cogs = {
            'Broken': _make_cog_mock('Broken'),
            'Healthy': _make_cog_mock('Healthy'),
        }

        call_count = 0

        async def unload_with_error(ext: str) -> None:
            nonlocal call_count
            call_count += 1
            if ext == 'cogs.broken':
                raise RuntimeError("simulated cog unload failure")

        mock_bot.unload_extension = AsyncMock(side_effect=unload_with_error)

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )
        await _teardown_cogs_ordered(mock_bot, context)

        # Both cogs attempted
        assert call_count == 2
        assert 'cogs.broken' in context.cog_timings
        assert 'cogs.healthy' in context.cog_timings

    @pytest.mark.asyncio
    async def test_teardown_timeout_per_cog(self, mock_bot: MagicMock) -> None:
        """A hung cog is cancelled after 10s without blocking others."""
        mock_bot.extensions = {'cogs.hung': MagicMock(), 'cogs.fast': MagicMock()}
        mock_bot.cogs = {
            'Hung': _make_cog_mock('Hung', priority=10),
            'Fast': _make_cog_mock('Fast'),
        }

        async def hang_forever(ext: str) -> None:
            if ext == 'cogs.hung':
                await asyncio.sleep(999)  # Will be cancelled by timeout

        mock_bot.unload_extension = AsyncMock(side_effect=hang_forever)

        context = ShutdownContext(
            trigger=signal.SIGTERM,
            reason=ShutdownReason.MANUAL_STOP,
            packages=[],
            timestamp=time.monotonic(),
        )

        # Patch the timeout to 0.1s so the test completes fast
        with patch('utils.lifecycle.asyncio.wait_for', wraps=asyncio.wait_for):
            # Replace the actual call with a short timeout version
            original_wait_for = asyncio.wait_for

            async def short_timeout_wait_for(coro, *, timeout):  # type: ignore[no-untyped-def]
                return await original_wait_for(coro, timeout=0.1)

            with patch('utils.lifecycle.asyncio.wait_for', side_effect=short_timeout_wait_for):
                await _teardown_cogs_ordered(mock_bot, context)

        # Both recorded — hung one via timeout, fast one normally
        assert 'cogs.hung' in context.cog_timings
        assert 'cogs.fast' in context.cog_timings


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Full Shutdown Flow (End-to-End)
# ═══════════════════════════════════════════════════════════════════════════════

class TestShutdownHandlerE2E:
    """End-to-end tests for shutdown_handler — the full orchestration."""

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_manual_stop_full_flow(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Full shutdown flow for a manual stop: teardown + goodbye, no owner DM."""
        mock_config.SYSTEM_CHANNEL_ID = 999999
        mock_config.BOT_NAME = "Marine"
        mock_config.ASSETS_PATH = "/fake"
        mock_config.RESOURCE_TRACK_INTERVAL = 15

        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)

        mock_bot.extensions = {'cogs.fun': MagicMock()}
        mock_bot.cogs = {'Fun': _make_cog_mock('Fun')}

        await shutdown_handler(
            signal.SIGTERM, mock_bot,
            reason=ShutdownReason.MANUAL_STOP,
        )

        # Cogs unloaded
        mock_bot.unload_extension.assert_called_once_with('cogs.fun')
        # Goodbye sent
        channel.send.assert_called_once()
        # Resource tracker stopped
        mock_bot.resource_tracker.stop.assert_called_once()
        # Bot closed
        mock_bot.close.assert_called_once()
        # No owner DM (MANUAL_STOP is not in _OWNER_NOTIFY_REASONS)
        mock_bot.fetch_user.assert_not_called()

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_system_upgrade_sends_owner_dm(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """SYSTEM_UPGRADE triggers owner DM with package list."""
        mock_config.SYSTEM_CHANNEL_ID = 999999
        mock_config.BOT_NAME = "Marine"
        mock_config.ASSETS_PATH = "/fake"
        mock_config.OWNER_IDS = {12345}
        mock_config.RESOURCE_TRACK_INTERVAL = 15

        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)

        # Deposit D-Bus info
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='reboot',
            packages=['linux-image-6.1'],
            timestamp=time.monotonic(),
        )

        await shutdown_handler(signal.SIGTERM, mock_bot, reason=None)

        # Owner DM sent
        mock_bot.fetch_user.assert_called_once_with(12345)
        mock_owner = mock_bot.fetch_user.return_value
        mock_owner.send.assert_called_once()

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_duplicate_shutdown_ignored(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Second shutdown call is ignored (duplicate signal guard)."""
        mock_config.SYSTEM_CHANNEL_ID = None
        mock_config.BOT_NAME = "Marine"
        mock_config.RESOURCE_TRACK_INTERVAL = 15
        mock_build.return_value = (MagicMock(), [])

        await shutdown_handler(signal.SIGTERM, mock_bot, reason=ShutdownReason.MANUAL_STOP)
        mock_bot.close.assert_called_once()

        # Second call — should be ignored
        mock_bot.close.reset_mock()
        await shutdown_handler(signal.SIGTERM, mock_bot, reason=ShutdownReason.MANUAL_STOP)
        mock_bot.close.assert_not_called()

    @pytest.mark.asyncio
    @patch('utils.lifecycle.config')
    @patch('utils.lifecycle._build_lifecycle_message')
    async def test_restart_flow_uses_reboot_gif(
        self, mock_build: MagicMock, mock_config: MagicMock, mock_bot: MagicMock
    ) -> None:
        """RESTART reason uses reboot.gif template."""
        mock_config.SYSTEM_CHANNEL_ID = 999999
        mock_config.BOT_NAME = "Marine"
        mock_config.ASSETS_PATH = "/fake"
        mock_config.RESOURCE_TRACK_INTERVAL = 15

        mock_view = MagicMock()
        mock_build.return_value = (mock_view, [])

        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)

        await shutdown_handler(
            signal.SIGTERM, mock_bot,
            reason=ShutdownReason.RESTART,
        )

        # Check that the build was called with reboot.gif
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs['gif_filename'] == 'reboot.gif'


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Teardown Detection Cleanup
# ═══════════════════════════════════════════════════════════════════════════════

class TestTeardownDetection:
    """Tests cleanup of D-Bus state."""

    @pytest.mark.asyncio
    async def test_teardown_clears_pending(self) -> None:
        """teardown_shutdown_detection clears _pending_system_shutdown."""
        lifecycle_module._pending_system_shutdown = PendingSystemShutdown(
            shutdown_type='reboot', packages=[], timestamp=time.monotonic()
        )
        await teardown_shutdown_detection()
        assert lifecycle_module._pending_system_shutdown is None

    @pytest.mark.asyncio
    async def test_teardown_disconnects_dbus(self) -> None:
        """teardown_shutdown_detection disconnects D-Bus if connected."""
        mock_conn = MagicMock()
        lifecycle_module._dbus_connection = mock_conn

        await teardown_shutdown_detection()

        mock_conn.disconnect.assert_called_once()
        assert lifecycle_module._dbus_connection is None

    @pytest.mark.asyncio
    async def test_teardown_safe_when_nothing_set(self) -> None:
        """teardown_shutdown_detection is safe to call when nothing was set up."""
        await teardown_shutdown_detection()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Log Phase
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogPhase:
    """Tests the log fold marker system."""

    def test_phase_opens_region(self) -> None:
        """log_phase opens a new region."""
        log_phase("TEST")
        assert lifecycle_module._current_phase == "TEST"

    def test_phase_closes_previous(self) -> None:
        """log_phase closes the previous region before opening new one."""
        log_phase("FIRST")
        log_phase("SECOND")
        assert lifecycle_module._current_phase == "SECOND"

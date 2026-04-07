"""utils/lifecycle.py

Handles the bot's startup and shutdown sequences with structured phases.

Startup Phases:
    INIT: Logging, config validation, database setup
    LOAD: Cog module loading
    CONNECT: Discord gateway connection, app command sync, startup message
    READY: "Bot is ready!", cog background tasks, resource tracker start

Shutdown Phases:
    SHUTDOWN: Signal received, context snapshot, concurrent teardown + messaging
    GOODBYE: Session summary, log finalization, connection close

Shutdown Taxonomy:
    Six reasons exist for logging granularity, mapped to three visual outcomes:

    Returning (reboot.gif):  RESTART, UPGRADE_RESTART, SYSTEM_REBOOT
    Upgrading (upgrade.gif): SYSTEM_UPGRADE
    Going away (shutdown.gif): MANUAL_STOP, SYSTEM_POWEROFF

Shutdown Detection:
    Uses two layered mechanisms on Linux/systemd for deterministic detection:
    1. RestartKillSignal=SIGUSR1 in the systemd unit file — distinguishes restart vs stop
    2. logind PrepareForShutdown D-Bus signal — distinguishes system reboot/poweroff vs manual stop
    3. /var/run/reboot-required.pkgs — distinguishes upgrade reboots from manual reboots
    4. apt-daily-upgrade.service state — distinguishes daemon-reexec from manual restart
    See Impls/SHUTDOWN_DETECTION_OVERHAUL.md and Impls/UPGRADE_AWARE_SHUTDOWN.md for design rationale.
"""

import asyncio
import enum
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import discord
from discord import ui

import config

if TYPE_CHECKING:
    from .bot_class import CoreBot


# ═══════════════════════════════════════════════════════════════════════════════
# Types & Constants
# ═══════════════════════════════════════════════════════════════════════════════

class ShutdownReason(enum.Enum):
    """Why the bot is shutting down. Determined from signal type, D-Bus flags, and system state.

    Six distinct reasons for logging granularity, mapped to three goodbye types:
        Returning (reboot.gif):  RESTART, UPGRADE_RESTART, SYSTEM_REBOOT
        Upgrading (upgrade.gif): SYSTEM_UPGRADE
        Going away (shutdown.gif): MANUAL_STOP, SYSTEM_POWEROFF
    """
    MANUAL_STOP = "manual_stop"            # systemctl stop / exit command / Ctrl+C
    RESTART = "restart"                    # systemctl restart (SIGUSR1), no upgrade active
    UPGRADE_RESTART = "upgrade_restart"    # daemon-reexec caused by apt-daily-upgrade
    SYSTEM_REBOOT = "system_reboot"        # System reboot detected via PrepareForShutdown
    SYSTEM_POWEROFF = "system_poweroff"    # System poweroff detected via PrepareForShutdown
    SYSTEM_UPGRADE = "system_upgrade"      # Reboot triggered by unattended-upgrades

    @property
    def is_returning(self) -> bool:
        """Whether the bot is expected to come back shortly."""
        return self in (
            ShutdownReason.RESTART,
            ShutdownReason.UPGRADE_RESTART,
            ShutdownReason.SYSTEM_REBOOT,
            ShutdownReason.SYSTEM_UPGRADE,
        )


@dataclass
class PendingSystemShutdown:
    """Information deposited by D-Bus before SIGTERM arrives.

    This is NOT the shutdown context. It's a partial snapshot that gets
    folded into ShutdownContext when the signal handler runs.
    """
    shutdown_type: Optional[str]    # 'reboot', 'poweroff', 'halt', or None
    packages: list[str]             # from /var/run/reboot-required.pkgs, may be empty
    timestamp: float                # time.monotonic()


@dataclass
class ShutdownContext:
    """Complete snapshot of why the bot is shutting down.

    Built once from signal type + pending D-Bus info + system state.
    Read-only after construction — no flags to check, no state to mutate.
    """
    trigger: signal.Signals          # what signal hit us
    reason: ShutdownReason           # resolved reason enum
    packages: list[str]              # upgrade packages, if any
    timestamp: float                 # time.monotonic() when shutdown started
    cog_timings: dict[str, float] = field(default_factory=dict)  # filled during teardown


@dataclass
class GoodbyeTemplate:
    """A goodbye message template that accepts specific shutdown reasons.

    Templates declare which reasons they accept. The shutdown flow finds the
    matching template and builds the message. Reasons don't own messages —
    messages own reasons.
    """
    reasons: set[ShutdownReason]
    title: str                    # format string, receives {name}
    gif: str                      # filename in assets folder
    log_msg: str                  # what to write to the log

    def format_title(self) -> str:
        """Returns the title with BOT_NAME substituted."""
        return self.title.format(name=config.BOT_NAME)


GOODBYE_TEMPLATES: list[GoodbyeTemplate] = [
    GoodbyeTemplate(
        reasons={ShutdownReason.RESTART, ShutdownReason.UPGRADE_RESTART, ShutdownReason.SYSTEM_REBOOT},
        title="{name} needs a breather, {name} will hopefully be back on air shortly!",
        gif="reboot.gif",
        log_msg="Shutdown: returning (reboot/restart)",
    ),
    GoodbyeTemplate(
        reasons={ShutdownReason.SYSTEM_UPGRADE},
        title="{name} needs to install a new game, {name} will return soon!",
        gif="upgrade.gif",
        log_msg="Shutdown: returning (system upgrade reboot)",
    ),
    GoodbyeTemplate(
        reasons={ShutdownReason.MANUAL_STOP, ShutdownReason.SYSTEM_POWEROFF},
        title="{name} is going off air. Goodnight and see you soon!",
        gif="shutdown.gif",
        log_msg="Shutdown: going away",
    ),
]

# Reasons that trigger an additional owner DM with diagnostic info
_OWNER_NOTIFY_REASONS: set[ShutdownReason] = {
    ShutdownReason.UPGRADE_RESTART,
    ShutdownReason.SYSTEM_UPGRADE,
}


def _find_template(reason: ShutdownReason) -> GoodbyeTemplate:
    """Finds the goodbye template for a shutdown reason.

    Args:
        reason: The shutdown reason to match.

    Returns:
        The matching GoodbyeTemplate.
    """
    for template in GOODBYE_TEMPLATES:
        if reason in template.reasons:
            return template
    # Defensive fallback — should never happen if templates are exhaustive
    return GOODBYE_TEMPLATES[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Module State
# ═══════════════════════════════════════════════════════════════════════════════

# Guard against duplicate shutdown calls (signal handlers use create_task,
# so rapid signals can schedule multiple shutdown tasks)
_is_shutting_down: bool = False

# Guard against duplicate on_ready calls (Discord.py fires on_ready after every
# reconnect, not just initial connection). We only want cog_ready() once.
_has_initialized: bool = False

# Track current phase for fold markers
_current_phase: Optional[str] = None

# D-Bus connection reference (for cleanup only)
_dbus_connection: Optional[object] = None

# File descriptor for the logind delay inhibitor lock
_inhibitor_fd: Optional[int] = None

# The ONE piece of cross-boundary state: D-Bus deposits, signal handler reads
_pending_system_shutdown: Optional[PendingSystemShutdown] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Shared Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _silent_marker_path() -> str:
    """Returns the path to the silent restart marker file.

    Returns:
        Absolute path under APP_PATH.
    """
    return os.path.join(config.APP_PATH, "silent_restart.marker")


def _write_silent_marker() -> None:
    """Writes the silent restart marker so the next boot suppresses its startup message.

    Called during shutdown when the reason is RESTART (plain SIGUSR1).
    """
    path = _silent_marker_path()
    try:
        with open(path, 'w') as f:
            f.write('')
        logging.info(f"Silent restart marker written to {path}")
    except OSError as e:
        logging.error(f"Failed to write silent restart marker: {e}", exc_info=True)


def _consume_silent_marker() -> bool:
    """Checks for and deletes the silent restart marker file.

    Returns:
        True if the marker existed and was successfully consumed.
        False if the marker was absent or could not be deleted.
    """
    path = _silent_marker_path()
    try:
        os.remove(path)
        logging.info("Silent restart marker consumed")
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logging.error(f"Failed to consume silent restart marker: {e}", exc_info=True)
        return False


def log_phase(phase: str) -> None:
    """Logs a phase separator with fold markers for Notepad++ collapsing.

    Emits #region/#endregion markers that can be folded in Notepad++
    with a custom User Defined Language.

    Args:
        phase: The name of the phase to log.
    """
    global _current_phase

    # Close previous region if one was open
    if _current_phase:
        logging.info(f"#endregion {_current_phase}")

    # Open new region with visual separator
    separator = f"─── {phase} " + "─" * (40 - len(phase))
    logging.info(f"#region {separator}")
    _current_phase = phase


def _build_lifecycle_message(
    title: str,
    gif_filename: Optional[str] = None,
    description: Optional[str] = None,
) -> tuple[ui.LayoutView, list[discord.File]]:
    """Builds a Components V2 lifecycle message with optional gif and description.

    Used by startup, shutdown, and notification messages for consistent
    presentation. Produces a Container with a title header, optional gif via
    MediaGallery, and optional description text.

    Args:
        title: The heading text displayed in the container.
        gif_filename: Filename of a gif in the assets folder (e.g. "startup.gif").
            If the file doesn't exist or exceeds the upload limit, the gif is
            omitted gracefully.
        description: Optional body text displayed below the title.

    Returns:
        Tuple of (LayoutView, list of discord.File attachments).
    """
    max_file_size = 10 * 1024 * 1024  # 10 MB (non-boosted server upload limit)

    view = ui.LayoutView(timeout=None)
    files: list[discord.File] = []

    container = ui.Container()
    container.add_item(ui.TextDisplay(f"## {title}"))

    if gif_filename:
        gif_path = os.path.join(config.ASSETS_PATH, gif_filename)
        if os.path.exists(gif_path):
            file_size = os.path.getsize(gif_path)
            if file_size <= max_file_size:
                files.append(discord.File(gif_path, filename=gif_filename))
                container.add_item(ui.MediaGallery(
                    discord.MediaGalleryItem(media=f"attachment://{gif_filename}")
                ))
            else:
                logging.warning(
                    f"Lifecycle gif {gif_filename} is {file_size / 1024 / 1024:.1f}MB, "
                    f"exceeds {max_file_size / 1024 / 1024:.0f}MB upload limit — skipping attachment"
                )

    if description:
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(description))

    view.add_item(container)
    return view, files


# ═══════════════════════════════════════════════════════════════════════════════
# D-Bus Shutdown Detection
# ═══════════════════════════════════════════════════════════════════════════════

def _read_reboot_required_packages() -> list[str]:
    """Reads /var/run/reboot-required.pkgs and returns the package names.

    Returns:
        List of package name strings, or empty list if file doesn't exist or is empty.
    """
    pkg_file = '/var/run/reboot-required.pkgs'
    try:
        with open(pkg_file) as f:
            packages = [line.strip() for line in f if line.strip()]
        return packages
    except FileNotFoundError:
        return []
    except OSError as e:
        logging.warning(f"Failed to read {pkg_file}: {e}")
        return []


def _release_inhibitor() -> None:
    """Releases the logind delay inhibitor lock by closing the file descriptor."""
    global _inhibitor_fd

    if _inhibitor_fd is not None:
        try:
            os.close(_inhibitor_fd)
            logging.debug(f"Released logind inhibitor lock (fd={_inhibitor_fd})")
        except OSError as e:
            logging.warning(f"Failed to release inhibitor lock: {e}")
        _inhibitor_fd = None


def _on_prepare_for_shutdown(active: bool) -> None:
    """Handler for logind's PrepareForShutdown signal (pre-systemd 255).

    Deposits information and releases the inhibitor lock. No messaging,
    no async tasks — just capture and get out of the way.

    Args:
        active: True when shutdown is starting, False when it's cancelled.
    """
    global _pending_system_shutdown

    if not active:
        return

    _pending_system_shutdown = PendingSystemShutdown(
        shutdown_type=None,  # can't distinguish reboot vs poweroff
        packages=_read_reboot_required_packages(),
        timestamp=time.monotonic(),
    )

    logging.info("PrepareForShutdown received (no metadata)")
    _release_inhibitor()


def _on_prepare_for_shutdown_with_metadata(active: bool, metadata: dict) -> None:  # type: ignore[type-arg]
    """Handler for logind's PrepareForShutdownWithMetadata signal (systemd 255+).

    Deposits information including shutdown type and releases the inhibitor lock.
    No messaging, no async tasks — just capture and get out of the way.

    Args:
        active: True when shutdown is starting, False when it's cancelled.
        metadata: Dict with 'type' key ('reboot', 'poweroff', 'halt').
    """
    global _pending_system_shutdown

    if not active:
        return

    shutdown_type = metadata.get('type')
    if shutdown_type is not None and hasattr(shutdown_type, 'value'):
        shutdown_type = shutdown_type.value

    _pending_system_shutdown = PendingSystemShutdown(
        shutdown_type=str(shutdown_type) if shutdown_type else None,
        packages=_read_reboot_required_packages(),
        timestamp=time.monotonic(),
    )

    logging.info(f"PrepareForShutdownWithMetadata received — type={_pending_system_shutdown.shutdown_type}")
    _release_inhibitor()


async def setup_shutdown_detection(bot: "CoreBot") -> None:
    """Connects to the system D-Bus and subscribes to logind's PrepareForShutdown signal.

    On Linux with D-Bus available, this:
    1. Takes a "delay" inhibitor lock from logind — guarantees we hear the
       PrepareForShutdown broadcast before systemd starts sending SIGTERM.
    2. Subscribes to PrepareForShutdown (or PrepareForShutdownWithMetadata on
       systemd 255+) to deposit system state into _pending_system_shutdown.

    On Windows or systems without D-Bus, this no-ops gracefully.

    Args:
        bot: The CoreBot instance (unused in redesign, kept for interface stability).
    """
    global _dbus_connection, _inhibitor_fd

    if sys.platform != 'linux':
        logging.debug("Shutdown detection skipped (not Linux)")
        return

    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
    except ImportError:
        logging.debug("Shutdown detection skipped (dbus-fast not installed)")
        return

    try:
        bus = await asyncio.wait_for(
            MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect(),
            timeout=5.0
        )
        _dbus_connection = bus

        introspection = await asyncio.wait_for(
            bus.introspect('org.freedesktop.login1', '/org/freedesktop/login1'),
            timeout=5.0
        )
        proxy = bus.get_proxy_object('org.freedesktop.login1', '/org/freedesktop/login1', introspection)
        manager = proxy.get_interface('org.freedesktop.login1.Manager')

        # Take a delay inhibitor lock — logind will wait for us to release it
        # before telling systemd to begin stopping services.
        try:
            fd = await asyncio.wait_for(
                manager.call_inhibit(
                    'shutdown',             # what
                    config.BOT_NAME,        # who
                    'Detecting shutdown type',  # why
                    'delay'                 # mode
                ),
                timeout=5.0
            )
            _inhibitor_fd = fd
            logging.info(f"Acquired logind delay inhibitor lock (fd={fd})")
        except Exception as e:
            logging.warning(f"Failed to acquire logind inhibitor lock: {e!r} — shutdown type detection disabled")
            logging.info("Shutdown detection inactive (no inhibitor lock)")
            return

        # Subscribe to PrepareForShutdownWithMetadata first (systemd 255+),
        # falling back to PrepareForShutdown for older versions.
        try:
            manager.on_prepare_for_shutdown_with_metadata(_on_prepare_for_shutdown_with_metadata)
            logging.debug("Subscribed to PrepareForShutdownWithMetadata (systemd 255+)")
        except AttributeError:
            manager.on_prepare_for_shutdown(_on_prepare_for_shutdown)
            logging.debug("Subscribed to PrepareForShutdown (pre-255 fallback)")

        logging.info("Shutdown detection active (D-Bus)")

    except asyncio.TimeoutError:
        logging.warning("Shutdown detection timed out during D-Bus setup — continuing without it")
        _dbus_connection = None
    except Exception as e:
        logging.debug(f"Shutdown detection unavailable: {e}")
        _dbus_connection = None


async def teardown_shutdown_detection() -> None:
    """Disconnects from D-Bus and releases the inhibitor lock if still held.

    Called during shutdown cleanup and before module purge on soft restart.
    Safe to call multiple times or when detection was never set up.
    """
    global _dbus_connection, _pending_system_shutdown

    _release_inhibitor()

    if _dbus_connection is not None:
        try:
            _dbus_connection.disconnect()  # type: ignore[union-attr]
            logging.debug("D-Bus connection closed")
        except Exception as e:
            logging.debug(f"D-Bus disconnect error (non-fatal): {e}")
        _dbus_connection = None

    _pending_system_shutdown = None


# ═══════════════════════════════════════════════════════════════════════════════
# Context Resolution
# ═══════════════════════════════════════════════════════════════════════════════

def _is_apt_upgrade_active() -> bool:
    """Checks if apt-daily-upgrade.service is currently running.

    Called during SIGUSR1 handling to distinguish plain restarts from
    daemon-reexec triggered by package upgrades. Synchronous and fast
    (milliseconds).

    Returns:
        True if apt-daily-upgrade.service is active.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'apt-daily-upgrade.service'],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() == 'active'
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def build_shutdown_context(
    sig: signal.Signals,
    reason: Optional[ShutdownReason] = None,
) -> ShutdownContext:
    """Builds the shutdown context from signal + system state.

    Called once at the top of shutdown_handler(). After this returns,
    the reason is fully resolved and nothing else needs to check flags.

    When reason is explicitly provided (e.g. from control commands), it's
    used directly. When None, the reason is resolved from the signal type,
    D-Bus pending state, and system service state.

    Args:
        sig: The signal that triggered shutdown.
        reason: Pre-resolved reason from the caller, or None to auto-resolve.

    Returns:
        The fully resolved ShutdownContext.
    """
    timestamp = time.monotonic()
    packages: list[str] = []

    if reason is not None:
        # Caller already knows the reason (control commands, explicit SIGUSR1)
        # Still pull packages from pending D-Bus info if available
        if _pending_system_shutdown is not None:
            packages = _pending_system_shutdown.packages
        return ShutdownContext(
            trigger=sig,
            reason=reason,
            packages=packages,
            timestamp=timestamp,
        )

    # Auto-resolve from signal + system state
    if sys.platform == 'linux' and sig.value == signal.SIGUSR1.value:
        # Service-level restart. Check if caused by daemon-reexec during upgrade.
        if _is_apt_upgrade_active():
            reason = ShutdownReason.UPGRADE_RESTART
            logging.info("Shutdown context: SIGUSR1 with apt-daily-upgrade active → UPGRADE_RESTART")
        else:
            reason = ShutdownReason.RESTART
            logging.info("Shutdown context: SIGUSR1 without apt-daily-upgrade → RESTART (silent)")

    elif _pending_system_shutdown is not None:
        # SIGTERM with D-Bus context — system is going down.
        packages = _pending_system_shutdown.packages

        if _pending_system_shutdown.shutdown_type == 'poweroff':
            reason = ShutdownReason.SYSTEM_POWEROFF
        elif packages:
            reason = ShutdownReason.SYSTEM_UPGRADE
        else:
            reason = ShutdownReason.SYSTEM_REBOOT
        logging.info(
            f"Shutdown context: SIGTERM with D-Bus pending "
            f"(type={_pending_system_shutdown.shutdown_type}, pkgs={len(packages)}) → {reason.value}"
        )

    else:
        # SIGTERM without D-Bus — manual stop.
        reason = ShutdownReason.MANUAL_STOP
        logging.info("Shutdown context: SIGTERM without PrepareForShutdown → MANUAL_STOP")

    return ShutdownContext(
        trigger=sig,
        reason=reason,
        packages=packages,
        timestamp=timestamp,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Shutdown Messaging
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_goodbye_message(bot: "CoreBot", context: ShutdownContext) -> None:
    """Sends the themed goodbye message to the system channel.

    Finds the matching template for the shutdown reason, builds the
    Components V2 message, and sends it with the appropriate gif.

    Args:
        bot: The CoreBot instance.
        context: The shutdown context with the resolved reason.
    """
    if not config.SYSTEM_CHANNEL_ID:
        logging.info("No system channel configured — goodbye message will not be sent")
        return

    channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        logging.warning(f"System channel {config.SYSTEM_CHANNEL_ID} not found or not a text channel")
        return

    template = _find_template(context.reason)
    logging.info(template.log_msg)

    try:
        view, files = _build_lifecycle_message(
            title=template.format_title(),
            gif_filename=template.gif,
        )
        if files:
            await channel.send(view=view, files=files)
        else:
            await channel.send(view=view)
        logging.info(f"Goodbye message sent to channel {config.SYSTEM_CHANNEL_ID}")
    except discord.HTTPException as e:
        logging.error(f"Failed to send goodbye message: {e}")


async def _send_owner_notification(bot: "CoreBot", context: ShutdownContext) -> None:
    """Sends diagnostic DMs to owner(s) for upgrade-related shutdowns.

    UPGRADE_RESTART: Notifies that apt-daily-upgrade caused a daemon-reexec.
    SYSTEM_UPGRADE: Sends the package list that triggered the reboot.

    Args:
        bot: The CoreBot instance.
        context: The shutdown context with the resolved reason.
    """
    if context.reason == ShutdownReason.SYSTEM_UPGRADE:
        await _send_owner_upgrade_notification(bot, context)
    elif context.reason == ShutdownReason.UPGRADE_RESTART:
        await _send_owner_reexec_notification(bot)


async def _send_owner_upgrade_notification(bot: "CoreBot", context: ShutdownContext) -> None:
    """DMs the owner(s) with the package list that triggered the reboot.

    Args:
        bot: The CoreBot instance.
        context: The shutdown context containing the package list.
    """
    if not context.packages:
        return

    pkg_list = '\n'.join(f'`{pkg}`' for pkg in context.packages)
    view, _ = _build_lifecycle_message(
        title="System upgrade detected",
        description=f"The following packages triggered a reboot:\n{pkg_list}",
    )

    for owner_id in config.OWNER_IDS:
        try:
            user = await bot.fetch_user(owner_id)
            await user.send(view=view)
            logging.info(f"Sent upgrade notification DM to owner {owner_id}")
        except discord.HTTPException as e:
            logging.warning(f"Failed to DM owner {owner_id} upgrade notification: {e}")


async def _send_owner_reexec_notification(bot: "CoreBot") -> None:
    """DMs the owner(s) that a daemon-reexec was triggered by upgrades.

    Args:
        bot: The CoreBot instance.
    """
    view, _ = _build_lifecycle_message(
        title="System maintenance detected",
        description=(
            "Unattended upgrades triggered a systemd daemon re-execution.\n"
            "This caused a service-level restart (not a full reboot).\n"
            "`apt-daily-upgrade.service` was active when the restart occurred."
        ),
    )

    for owner_id in config.OWNER_IDS:
        try:
            user = await bot.fetch_user(owner_id)
            await user.send(view=view)
            logging.info(f"Sent reexec notification DM to owner {owner_id}")
        except discord.HTTPException as e:
            logging.warning(f"Failed to DM owner {owner_id} reexec notification: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Cog Teardown
# ═══════════════════════════════════════════════════════════════════════════════

async def _teardown_cogs_ordered(bot: "CoreBot", context: ShutdownContext) -> None:
    """Unloads all cogs in priority order with per-cog timing and timeouts.

    Cogs declaring a SHUTDOWN_PRIORITY class attribute are unloaded first
    (higher value = earlier teardown). Cogs without a priority default to 0.

    Each cog gets a 10-second timeout to prevent a single hung cog from
    consuming the entire TimeoutStopSec budget. Timings are recorded in
    the shutdown context for diagnostic logging.

    Args:
        bot: The CoreBot instance.
        context: The shutdown context (cog_timings dict is populated in-place).
    """
    cog_names = list(bot.extensions.keys())

    # Sort by explicit priority (higher = earlier teardown)
    teardown_order = sorted(
        cog_names,
        key=lambda name: getattr(bot.cogs.get(name.split('.')[-1].title()), 'SHUTDOWN_PRIORITY', 0),
        reverse=True,
    )

    for ext in teardown_order:
        start = time.monotonic()
        try:
            await asyncio.wait_for(bot.unload_extension(ext), timeout=10.0)
        except asyncio.TimeoutError:
            logging.error(f"Cog {ext} teardown timed out after 10s")
        except Exception as e:
            logging.error(f"Error unloading {ext}: {e}")
        elapsed = time.monotonic() - start
        context.cog_timings[ext] = elapsed

        if elapsed > 5.0:
            logging.warning(f"Cog {ext} took {elapsed:.1f}s to unload")


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle Handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def startup_handler(bot: "CoreBot") -> None:
    """Handles the CONNECT and READY phases of bot startup.

    Called from on_ready event. Logs connection info, syncs commands,
    sends startup message, then starts background tasks.

    Discord.py fires on_ready after every reconnect (not just initial startup).
    The _has_initialized guard ensures the full startup sequence only runs once;
    reconnects only restore presence and log the event.

    Args:
        bot: The CoreBot instance.
    """
    global _has_initialized
    if _has_initialized:
        logging.info("Reconnect detected (on_ready fired again). Skipping startup sequence.")
        # Restore presence — discord.py may have reset it during reconnect
        try:
            await bot.change_presence(status=bot.current_visibility)
        except Exception as e:
            logging.warning(f"Failed to restore presence after reconnect: {e}")
        return

    # ─── CONNECT ───
    log_phase("CONNECT")

    if bot.user:
        logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    else:
        logging.error("Bot user information not available on ready.")

    guild_count = len(bot.guilds)
    logging.info(f"Connected to {guild_count} guild{'s' if guild_count != 1 else ''}:")
    for guild in bot.guilds:
        logging.info(f"  - {guild.name} (ID: {guild.id})")

    # Register the /nlp command before syncing
    try:
        bot.register_nlp_command()
    except Exception as e:
        logging.error(f"Failed to register /nlp command: {e}")

    # Sync app commands (with timeout to prevent hanging on rate limits)
    # Note: global sync can take up to an hour to propagate to all guilds.
    try:
        if config.DEV_MODE and config.DEV_GUILD:
            guild = discord.Object(id=config.DEV_GUILD)
            bot.tree.copy_global_to(guild=guild)
            await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=30.0)
            logging.info(f"App commands synced to dev guild {config.DEV_GUILD}")
        else:
            await asyncio.wait_for(bot.tree.sync(), timeout=30.0)
            logging.info("App commands synced globally")
    except asyncio.TimeoutError:
        logging.warning("App command sync timed out after 30s (possible rate limit)")
    except Exception as e:
        logging.error(f"Failed to sync app commands: {e}")

    # Check for silent restart marker — if present, suppress the startup message
    _silent_boot = _consume_silent_marker()
    if _silent_boot:
        logging.info("Silent restart detected — startup message suppressed")

    # Send startup message (skipped on silent restart)
    if _silent_boot:
        pass  # Already logged above
    elif config.SYSTEM_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
            if isinstance(channel, discord.TextChannel):
                view, files = _build_lifecycle_message(
                    title=f"Ahoy!, {config.BOT_NAME} is now on air!",
                    gif_filename="startup.gif",
                )
                if files:
                    await channel.send(view=view, files=files)
                else:
                    await channel.send(view=view)
                logging.info(f"Startup message sent to channel {config.SYSTEM_CHANNEL_ID}")
            else:
                logging.warning(f"System channel {config.SYSTEM_CHANNEL_ID} is not a valid text channel.")
        except discord.HTTPException as e:
            logging.error(f"Failed to send startup message: {e}")
    else:
        logging.info("SYSTEM_CHANNEL_ID not configured — startup/goodbye messages will be skipped")

    # ─── READY ───
    log_phase("READY")

    logging.info(f"{config.BOT_NAME} is ready!")

    # Apply initial visibility from config
    await bot.change_presence(status=bot.current_visibility)
    if bot.current_visibility != discord.Status.online:
        logging.info(f"Initial visibility set to: {bot.current_visibility.name}")

    _has_initialized = True

    # Call cog_ready() on all cogs to start their background tasks
    await bot.ready_all_cogs()

    # Start resource tracker
    if hasattr(bot, 'resource_tracker') and bot.resource_tracker:
        await bot.resource_tracker.start()
        logging.info(f"[ResourceTracker] Started ({config.RESOURCE_TRACK_INTERVAL} min interval)")

    # Safety-net: verify the silent restart marker was consumed.
    # If something crashed between marker write and primary consume, this catches it.
    async def _marker_safety_check() -> None:
        await asyncio.sleep(300)  # 5 minutes
        marker_path = _silent_marker_path()
        if not os.path.exists(marker_path):
            logging.debug("Marker safety check: clean (no stale marker)")
            return
        # Marker still on disk — attempt to consume it
        result = _consume_silent_marker()
        if result:
            logging.warning("Stale silent restart marker consumed by safety check — primary consume may have failed")
        else:
            # _consume_silent_marker logged its own error with exc_info,
            # but the marker file still exists — escalate
            logging.error(
                f"Silent restart marker exists at {marker_path} but could not be consumed "
                "— check file permissions"
            )

    asyncio.create_task(_marker_safety_check())  # noqa: RUF006


async def shutdown_handler(
    sig: signal.Signals,
    bot: "CoreBot",
    reason: Optional[ShutdownReason] = None,
    log_path: Optional[str] = None
) -> None:
    """Handles the graceful shutdown of the bot with structured phases.

    Snapshot first, teardown second. The instant we know we're shutting down,
    we capture WHY in a ShutdownContext — before touching cogs, before sending
    messages. Then cog teardown, goodbye messaging, and owner notifications
    run concurrently while the bot is still connected.

    Args:
        sig: The signal that triggered the shutdown.
        bot: The CoreBot instance.
        reason: Pre-resolved shutdown reason from the caller (e.g. control commands).
            When None, the reason is auto-resolved from signal type and D-Bus flags.
        log_path: Path to the current log file for finalization.
    """
    global _is_shutting_down
    if _is_shutting_down:
        logging.warning(f"Ignoring duplicate {sig.name} signal, shutdown already in progress")
        return
    _is_shutting_down = True

    # ─── SHUTDOWN ───
    log_phase("SHUTDOWN")
    logging.info(f"Received exit signal {sig.name}")

    # Snapshot: build the complete shutdown context immediately
    context = build_shutdown_context(sig, reason)
    logging.info(f"Shutdown reason: {context.reason.value}")

    # Silent restart: suppress goodbye message and write marker for next boot
    silent = context.reason == ShutdownReason.RESTART
    if silent:
        _write_silent_marker()

    # Concurrent shutdown work: cog teardown + messaging run in parallel.
    # The bot is still connected during all of this — no risk of the Discord
    # connection dying before messages are sent.
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_teardown_cogs_ordered(bot, context))
        if not silent:
            tg.create_task(_send_goodbye_message(bot, context))
        if context.reason in _OWNER_NOTIFY_REASONS:
            tg.create_task(_send_owner_notification(bot, context))

    logging.info("Teardown tasks complete (cogs unloaded%s)" % (", goodbye suppressed (silent restart)" if silent else ", goodbye sent"))

    # Stop resource tracker (after cogs, uses their timing data)
    if hasattr(bot, 'resource_tracker') and bot.resource_tracker:
        await bot.resource_tracker.stop()
        logging.info("[ResourceTracker] Stopped, history logged")

    # Clean up D-Bus connection
    await teardown_shutdown_detection()

    # ─── GOODBYE ───
    log_phase("GOODBYE")

    # Session summary: log cog teardown timings for diagnostic visibility
    if context.cog_timings:
        timings_summary = ", ".join(
            f"{name.split('.')[-1]}={elapsed:.1f}s"
            for name, elapsed in sorted(context.cog_timings.items(), key=lambda x: x[1], reverse=True)
        )
        logging.info(f"Cog teardown timings: {timings_summary}")

    # Close the final GOODBYE region
    global _current_phase
    if _current_phase:
        logging.info(f"#endregion {_current_phase}")
        _current_phase = None

    await bot.close()
    # Note: Code after bot.close() won't execute - control returns to main.py


def purge_modules() -> None:
    """Removes all bot-related modules from sys.modules to force a reload.

    This targets 'utils', 'cogs', and 'config' modules.
    """
    to_purge = [
        module_name for module_name in sys.modules.keys()
        if module_name.startswith(('utils.', 'cogs.')) or module_name == 'config'
    ]

    logging.info(f"Purging {len(to_purge)} modules for restart...")
    for module_name in to_purge:
        del sys.modules[module_name]
    logging.info("Modules purged.")

"""utils/lifecycle.py

Handles the bot's startup and shutdown sequences with structured phases.

Startup Phases:
    INIT: Logging, config validation, database setup
    LOAD: Cog module loading
    CONNECT: Discord gateway connection, app command sync, startup message
    READY: "Bot is ready!", cog background tasks, resource tracker start

Shutdown Phases:
    SHUTDOWN: Signal received, cog cleanup, resource tracker stop
    GOODBYE: Shutdown message, log finalization, connection close

Shutdown Detection:
    Uses two layered mechanisms on Linux/systemd for deterministic detection:
    1. RestartKillSignal=SIGUSR1 in the systemd unit file — distinguishes restart vs stop
    2. logind PrepareForShutdown D-Bus signal — distinguishes system reboot/poweroff vs manual stop
    See Impls/SHUTDOWN_DETECTION_OVERHAUL.md for full design rationale.
"""

import asyncio
import enum
import logging
import os
import signal
import sys
from typing import Optional, TYPE_CHECKING

import discord

import config

if TYPE_CHECKING:
    from .bot_class import CoreBot


# ─── Shutdown Reason ─────────────────────────────────────────────────────────

class ShutdownReason(enum.Enum):
    """Why the bot is shutting down. Determined from signal type and D-Bus flags.

    The enum preserves granularity for future use (e.g. different messages for
    restart vs reboot), even though the current goodbye-message logic groups
    them into two buckets: "returning" (reboot.gif) and "going away" (shutdown.gif).
    """
    MANUAL_STOP = "manual_stop"          # systemctl stop / exit command / Ctrl+C
    RESTART = "restart"                  # systemctl restart (SIGUSR1) or soft restart (TCP/console)
    SYSTEM_REBOOT = "system_reboot"      # System reboot detected via PrepareForShutdown
    SYSTEM_POWEROFF = "system_poweroff"  # System poweroff detected via PrepareForShutdown

    @property
    def is_returning(self) -> bool:
        """Whether the bot is expected to come back shortly."""
        return self in (ShutdownReason.RESTART, ShutdownReason.SYSTEM_REBOOT)


# ─── Module-Level State ──────────────────────────────────────────────────────

# Track current phase for fold markers
_current_phase: Optional[str] = None

# Guard against duplicate shutdown calls (signal handlers use create_task,
# so rapid SIGTERM signals can schedule multiple shutdown tasks)
_is_shutting_down: bool = False

# Guard against duplicate on_ready calls (Discord.py fires on_ready after every
# reconnect, not just initial connection). We only want cog_ready() once.
_has_initialized: bool = False

# Set True by the PrepareForShutdown D-Bus handler BEFORE SIGTERM arrives.
# Checked by resolve_shutdown_reason() when a SIGTERM is received.
_system_shutdown_flag: bool = False

# "reboot", "poweroff", or "halt" — from PrepareForShutdownWithMetadata (systemd 255+).
# None if metadata wasn't available or D-Bus detection is inactive.
_shutdown_type: Optional[str] = None

# File descriptor for the logind delay inhibitor lock. Held from startup until
# PrepareForShutdown fires (or teardown). Keeps logind from proceeding until
# we've recorded the shutdown type.
_inhibitor_fd: Optional[int] = None

# Reference to the D-Bus connection for cleanup on shutdown/restart.
_dbus_connection: Optional[object] = None


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


# ─── D-Bus Shutdown Detection ────────────────────────────────────────────────

async def setup_shutdown_detection() -> None:
    """Connects to the system D-Bus and subscribes to logind's PrepareForShutdown signal.

    On Linux with D-Bus available, this:
    1. Takes a "delay" inhibitor lock from logind — guarantees we hear the
       PrepareForShutdown broadcast before systemd starts sending SIGTERM.
    2. Subscribes to PrepareForShutdown (or PrepareForShutdownWithMetadata on
       systemd 255+) to set the _system_shutdown_flag before our signal handler runs.

    On Windows or systems without D-Bus, this no-ops gracefully.
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

        # Get the logind Manager interface
        introspection = await asyncio.wait_for(
            bus.introspect('org.freedesktop.login1', '/org/freedesktop/login1'),
            timeout=5.0
        )
        proxy = bus.get_proxy_object('org.freedesktop.login1', '/org/freedesktop/login1', introspection)
        manager = proxy.get_interface('org.freedesktop.login1.Manager')

        # Take a delay inhibitor lock — logind will wait for us to release it
        # before telling systemd to begin stopping services.
        # Without the lock, there's a race: logind could broadcast PrepareForShutdown
        # and systemd could send SIGTERM before our D-Bus handler runs.
        # So signal subscription is contingent on having the lock.
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
            # Without the lock we can't guarantee the PrepareForShutdown handler
            # runs before SIGTERM arrives, so subscribing would be unreliable.
            logging.info("Shutdown detection inactive (no inhibitor lock)")
            return

        # Subscribe to PrepareForShutdownWithMetadata first (systemd 255+),
        # falling back to PrepareForShutdown for older versions.
        try:
            manager.on_prepare_for_shutdown_with_metadata(_on_prepare_for_shutdown_with_metadata)
            logging.debug("Subscribed to PrepareForShutdownWithMetadata (systemd 255+)")
        except AttributeError:
            # Signal not available — older systemd, fall back
            manager.on_prepare_for_shutdown(_on_prepare_for_shutdown)
            logging.debug("Subscribed to PrepareForShutdown (pre-255 fallback)")

        logging.info("Shutdown detection active (D-Bus)")

    except asyncio.TimeoutError:
        logging.warning("Shutdown detection timed out during D-Bus setup — continuing without it")
        _dbus_connection = None
    except Exception as e:
        # D-Bus unavailable (container, no systemd, etc.) — degrade gracefully
        logging.debug(f"Shutdown detection unavailable: {e}")
        _dbus_connection = None


def _on_prepare_for_shutdown(active: bool) -> None:
    """Handler for logind's PrepareForShutdown signal (pre-systemd 255).

    Fires before systemd begins stopping services. Sets the flag and releases
    the inhibitor lock so logind can proceed.

    Args:
        active: True when shutdown is starting, False when it's cancelled.
    """
    global _system_shutdown_flag

    if active:
        _system_shutdown_flag = True
        logging.info("PrepareForShutdown received — system is shutting down")
        _release_inhibitor()


def _on_prepare_for_shutdown_with_metadata(active: bool, metadata: dict) -> None:
    """Handler for logind's PrepareForShutdownWithMetadata signal (systemd 255+).

    Same as above, but also records the shutdown type (reboot vs poweroff).

    Args:
        active: True when shutdown is starting, False when it's cancelled.
        metadata: Dict with 'type' key ('reboot', 'poweroff', 'halt').
    """
    global _system_shutdown_flag, _shutdown_type

    if active:
        _system_shutdown_flag = True
        # Extract the type variant value if present
        shutdown_type = metadata.get('type')
        if shutdown_type is not None and hasattr(shutdown_type, 'value'):
            shutdown_type = shutdown_type.value
        _shutdown_type = str(shutdown_type) if shutdown_type is not None else None
        logging.info(f"PrepareForShutdownWithMetadata received — type={_shutdown_type}")
        _release_inhibitor()


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


async def teardown_shutdown_detection() -> None:
    """Disconnects from D-Bus and releases the inhibitor lock if still held.

    Called before module purge on soft restart, and during normal shutdown cleanup.
    Safe to call multiple times or when detection was never set up.
    """
    global _dbus_connection, _system_shutdown_flag, _shutdown_type

    _release_inhibitor()

    if _dbus_connection is not None:
        try:
            _dbus_connection.disconnect()  # type: ignore[union-attr]
            logging.debug("D-Bus connection closed")
        except Exception as e:
            logging.debug(f"D-Bus disconnect error (non-fatal): {e}")
        _dbus_connection = None

    # Reset flags for clean state on soft restart
    _system_shutdown_flag = False
    _shutdown_type = None


def resolve_shutdown_reason(sig: signal.Signals) -> ShutdownReason:
    """Determines the shutdown reason from the received signal and D-Bus flags.

    Logic:
        - SIGUSR1 → RESTART (systemd RestartKillSignal)
        - SIGTERM + _system_shutdown_flag → SYSTEM_REBOOT or SYSTEM_POWEROFF
        - SIGTERM without flag → MANUAL_STOP
        - SIGINT → MANUAL_STOP (dev Ctrl+C)

    Args:
        sig: The signal that triggered shutdown.

    Returns:
        The resolved ShutdownReason.
    """
    if sys.platform == 'linux' and sig.value == signal.SIGUSR1.value:
        return ShutdownReason.RESTART

    if _system_shutdown_flag:
        if _shutdown_type == 'poweroff':
            return ShutdownReason.SYSTEM_POWEROFF
        # 'reboot', 'halt', or unknown type — treat as reboot
        return ShutdownReason.SYSTEM_REBOOT

    return ShutdownReason.MANUAL_STOP


async def startup_handler(bot: "CoreBot") -> None:
    """Handles the CONNECT and READY phases of bot startup.

    Called from on_ready event. Logs connection info, syncs commands,
    sends startup message, then starts background tasks.

    Args:
        bot: The CoreBot instance.
    """
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

    # Send startup message
    if config.SYSTEM_CHANNEL_ID:
        try:
            channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(title=f"Good morning, {config.BOT_NAME} is awake!")

                startup_gif_path = os.path.join(config.ASSETS_PATH, "startup.gif")
                if os.path.exists(startup_gif_path):
                    file = discord.File(startup_gif_path, filename="startup.gif")
                    embed.set_image(url="attachment://startup.gif")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Startup message sent to channel {config.SYSTEM_CHANNEL_ID}")
            else:
                logging.warning(f"System channel {config.SYSTEM_CHANNEL_ID} is not a valid text channel.")
        except discord.HTTPException as e:
            logging.error(f"Failed to send startup message: {e}")

    # ─── READY ───
    log_phase("READY")

    logging.info(f"{config.BOT_NAME} is ready!")

    # Apply initial visibility from config
    # This ensures the bot starts in the configured state (e.g., invisible for dev)
    await bot.change_presence(status=bot.current_visibility)
    if bot.current_visibility != discord.Status.online:
        logging.info(f"Initial visibility set to: {bot.current_visibility.name}")

    # Guard: Discord.py fires on_ready after every reconnect, not just initial startup.
    # We only want to run cog_ready() once to avoid duplicate servers, tasks, etc.
    global _has_initialized
    if _has_initialized:
        logging.info("Reconnect detected (on_ready fired again). Skipping cog_ready() calls.")
        return
    _has_initialized = True

    # Call cog_ready() on all cogs to start their background tasks
    await bot.ready_all_cogs()

    # Start resource tracker
    if hasattr(bot, 'resource_tracker') and bot.resource_tracker:
        await bot.resource_tracker.start()
        logging.info(f"[ResourceTracker] Started ({config.RESOURCE_TRACK_INTERVAL} min interval)")


async def shutdown_handler(
    sig: signal.Signals,
    bot: "CoreBot",
    reason: Optional[ShutdownReason] = None,
    log_path: Optional[str] = None
) -> None:
    """Handles the graceful shutdown of the bot with structured phases.

    Args:
        sig: The signal that triggered the shutdown.
        bot: The CoreBot instance.
        reason: The shutdown reason, if known by the caller (e.g. control commands).
            When None, the reason is resolved from the signal type and D-Bus flags.
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

    # Resolve shutdown reason from signal + D-Bus flags if not explicitly provided
    if reason is None:
        reason = resolve_shutdown_reason(sig)
    logging.info(f"Shutdown reason: {reason.value}")

    # Unload all cogs gracefully (this calls cog_unload on each)
    cog_names = list(bot.extensions.keys())
    for ext in cog_names:
        try:
            await bot.unload_extension(ext)
        except Exception as e:
            logging.error(f"Error unloading {ext}: {e}")

    # Stop resource tracker
    if hasattr(bot, 'resource_tracker') and bot.resource_tracker:
        await bot.resource_tracker.stop()
        logging.info("[ResourceTracker] Stopped, history logged")

    # Clean up D-Bus connection
    await teardown_shutdown_detection()

    # ─── GOODBYE ───
    log_phase("GOODBYE")

    # Build goodbye message based on shutdown reason.
    # Two GIF buckets: "returning" (reboot.gif) vs "going away" (shutdown.gif).
    # Each reason is logged individually for traceability / future differentiation.
    if reason == ShutdownReason.RESTART:
        logging.info("Shutdown initiated by service restart. Service should be back shortly...")
    elif reason == ShutdownReason.SYSTEM_REBOOT:
        logging.info("Shutdown initiated by system reboot. Service should be back shortly...")
    elif reason == ShutdownReason.SYSTEM_POWEROFF:
        logging.info("Shutdown initiated by system poweroff.")
    else:
        logging.info("Shutdown initiated by manual stop or exit.")

    if reason.is_returning:
        embed = discord.Embed(
            title=f"{config.BOT_NAME} is taking a small nap, {config.BOT_NAME} will be back shortly!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "reboot.gif")
        attachment_name = "reboot.gif"
    else:
        embed = discord.Embed(
            title=f"{config.BOT_NAME} is heading to bed. Goodnight!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "shutdown.gif")
        attachment_name = "shutdown.gif"

    # Send the shutdown message to the configured channel.
    if config.SYSTEM_CHANNEL_ID:
        channel = bot.get_channel(config.SYSTEM_CHANNEL_ID)
        if channel and isinstance(channel, discord.TextChannel):
            try:
                if os.path.exists(gif_path):
                    file = discord.File(gif_path, filename=attachment_name)
                    embed.set_image(url=f"attachment://{attachment_name}")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                logging.info(f"Shutdown message sent to channel {config.SYSTEM_CHANNEL_ID}")
            except discord.HTTPException as e:
                logging.error(f"Failed to send shutdown message to channel {config.SYSTEM_CHANNEL_ID}: {e}")
        else:
            logging.warning(f"System channel {config.SYSTEM_CHANNEL_ID} was configured but not found or not a text channel.")

    logging.info("Closing Discord connection...")

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

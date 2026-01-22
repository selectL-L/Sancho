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
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import Optional, TYPE_CHECKING

import discord

import config

if TYPE_CHECKING:
    from .bot_class import CoreBot

# Track current phase for fold markers
_current_phase: Optional[str] = None

# Guard against duplicate shutdown calls (signal handlers use create_task,
# so rapid SIGTERM signals can schedule multiple shutdown tasks)
_is_shutting_down: bool = False


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


def is_system_rebooting() -> bool:
    """Checks if the system is in the process of rebooting or shutting down.

    Platform behavior:
        - Linux (systemd): Queries `systemctl list-jobs` to detect reboot/shutdown targets.
        - Windows: Always returns False (no equivalent detection; shows generic shutdown message).

    Returns:
        bool: True if a reboot/shutdown is detected, False otherwise.
    """
    if not sys.platform.startswith('linux'):
        return False

    try:
        result = subprocess.run(
            ['systemctl', 'list-jobs'],
            capture_output=True, text=True, check=False
        )
        output = result.stdout
        # If a reboot or shutdown job is running, we consider it a system reboot.
        if 'reboot.target' in output or 'shutdown.target' in output:
            logging.info("System reboot or shutdown detected via systemctl.")
            return True
    except FileNotFoundError:
        # This will be triggered if systemctl is not found on a Linux system.
        logging.warning("Running on Linux, but 'systemctl' command not found. Assuming not a systemd reboot.")
        return False
    return False


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

    # Call cog_ready() on all cogs to start their background tasks
    await bot.ready_all_cogs()

    # Start resource tracker
    if hasattr(bot, 'resource_tracker') and bot.resource_tracker:
        await bot.resource_tracker.start()
        logging.info(f"[ResourceTracker] Started ({config.RESOURCE_TRACK_INTERVAL} min interval)")


async def shutdown_handler(
    sig: signal.Signals,
    bot: "CoreBot",
    is_restart: bool = False,
    log_path: Optional[str] = None
) -> None:
    """Handles the graceful shutdown of the bot with structured phases.

    Args:
        sig: The signal that triggered the shutdown.
        bot: The CoreBot instance.
        is_restart: Whether this is a soft restart.
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

    # ─── GOODBYE ───
    log_phase("GOODBYE")

    # Determine the shutdown reason and prepare the message.
    rebooting = is_system_rebooting() or is_restart
    if rebooting:
        logging.info("Shutdown initiated by a system reboot or soft restart. Service should be back shortly...")
        embed = discord.Embed(
            title=f"{config.BOT_NAME} is taking a small nap, {config.BOT_NAME} will be back shortly!",
        )
        gif_path = os.path.join(config.ASSETS_PATH, "reboot.gif")
        attachment_name = "reboot.gif"
    else:
        logging.info("Shutdown initiated by a manual stop or exit.")
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
            logging.warning(f"System channel {config.SYSTEM_CHANNEL_ID} was configuered but not found or not a text channel.")

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

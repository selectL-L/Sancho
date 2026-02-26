"""cogs/starboard.py

Starboard system — highlights popular messages in a dedicated channel.

When a message receives enough star reactions, it gets posted to the starboard
channel. The system is self-healing, chronologically ordered, and supports
back-crawling missed messages.

Architecture:
    - Hot-path healing: Inline fixes on every reaction event (channel drift,
      star count sync, failed_checks reset).
    - Cold-path self-healing: Background audit every 12 hours, triggered by
      any starboard activity.
    - Verify engine: Reusable audit logic shared by self-heal, /starboard verify,
      and /starboard remake (as a prerequisite).
    - Remake engine: Destructive recreation — verify first, delete Discord
      messages, recreate in chronological order from DB.
    - Crawl engine: Bounded startup catch-up + deep historical crawl (whole
      server or single channel) that populates the DB without posting.
      Retries transient errors with backoff and checkpoints progress for
      resume across restarts.

File layout follows the reading-order convention documented in
Impls/STARBOARD_OVERHAUL.md — helpers are always defined before callers.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Imports
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import enum
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.database import DatabaseManager
from utils.views import FastConfirmModal, launch_modal

# ═══════════════════════════════════════════════════════════════════════════════
# Type Aliases
# ═══════════════════════════════════════════════════════════════════════════════

# Channels that can contain starrable messages (text, voice text chat, threads)
MessageableGuildChannel = (discord.TextChannel, discord.VoiceChannel, discord.Thread)

# ═══════════════════════════════════════════════════════════════════════════════
# Module-Level Utilities
# ═══════════════════════════════════════════════════════════════════════════════


def snowflake_to_unix(snowflake_id: int) -> int:
    """Convert a Discord snowflake ID to a Unix timestamp in seconds.

    Discord epoch is 2015-01-01T00:00:00Z (1420070400 seconds since Unix epoch).
    Bits 22-63 of a snowflake encode milliseconds since the Discord epoch.

    Args:
        snowflake_id: A Discord snowflake ID.

    Returns:
        Unix timestamp in whole seconds.
    """
    return ((snowflake_id >> 22) + 1420070400000) // 1000


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclasses & Enums
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class StarboardConfig:
    """Per-guild starboard configuration.

    Returned by the database layer. A default instance (with the guild_id set)
    represents an unconfigured guild — ``enabled`` is False and ``channel_id``
    is None.

    Attributes:
        guild_id: The Discord guild ID.
        enabled: Whether the starboard is active.
        channel_id: The starboard channel ID, or None if not set.
        emoji: The reaction emoji that triggers starboard.
        threshold: Minimum reaction count to qualify.
        last_heal_at: Unix timestamp of last self-heal run.
        crawl_started_at: Unix timestamp when a deep crawl was started, or None.
        crawl_requested_by: User ID who requested the crawl, or None.
        crawl_notify_channel: Channel ID to notify on crawl completion, or None.
        crawl_include_threads: Whether the crawl should include threads.
        crawl_last_channel_id: Resume point — last fully crawled channel, or None.
        crawl_last_message_id: Resume point — last processed message, or None.
    """
    guild_id: int
    enabled: bool = False
    channel_id: Optional[int] = None
    emoji: str = "\u2b50"  # ⭐
    threshold: int = 3
    last_heal_at: int = 0
    crawl_started_at: Optional[int] = None
    crawl_requested_by: Optional[int] = None
    crawl_notify_channel: Optional[int] = None
    crawl_include_threads: bool = False
    crawl_last_channel_id: Optional[int] = None
    crawl_last_message_id: Optional[int] = None

    def to_db_dict(self) -> Dict[str, Any]:
        """Export all fields (except guild_id) as a DB-friendly dict.

        Bools are converted to ints for SQLite storage.

        Returns:
            Dict suitable for passing to ``upsert_starboard_config(**d)``.
        """
        return {
            "enabled": int(self.enabled),
            "channel_id": self.channel_id,
            "emoji": self.emoji,
            "threshold": self.threshold,
            "last_heal_at": self.last_heal_at,
            "crawl_started_at": self.crawl_started_at,
            "crawl_requested_by": self.crawl_requested_by,
            "crawl_notify_channel": self.crawl_notify_channel,
            "crawl_include_threads": int(self.crawl_include_threads),
            "crawl_last_channel_id": self.crawl_last_channel_id,
            "crawl_last_message_id": self.crawl_last_message_id,
        }


@dataclass
class StarboardEmbedStyle:
    """Configuration for starboard embed appearance.

    Attributes:
        embed_color: The colour of the embed sidebar.
        jump_field_name: The label for the "Jump to Message" field.
        jump_link_text: The clickable link text within the field.
    """
    embed_color: discord.Color = field(default_factory=lambda: discord.Color.gold())
    jump_field_name: str = "Original Message"
    jump_link_text: str = "Jump to Message"


class VerifyStatus(enum.Enum):
    """Outcome of verifying a single starboard entry."""
    HEALTHY = "healthy"             # Both messages exist, data consistent
    FLAGGED = "flagged"             # Original not found, first failure
    TOMBSTONED = "tombstoned"       # Original not found, repeat failure — edited in-place or marked
    MISSING_POST = "missing_post"   # Starboard message missing, entry nulled for remake
    UNRESOLVABLE = "unresolvable"   # Cannot be automatically fixed (permissions, API errors)
    BANNED = "banned"               # Entry is in a banned channel — deleted from DB


@dataclass
class VerifyResult:
    """Result of verifying one starboard entry.

    Attributes:
        entry: The (possibly updated) entry dict.
        status: The verification outcome.
        needs_db_update: Whether the DB row needs writing.
        message: Human-readable explanation of what happened.
    """
    entry: Dict[str, Any]
    status: VerifyStatus
    needs_db_update: bool
    message: str


@dataclass
class VerifyReport:
    """Aggregate results from verifying all entries in a guild.

    Attributes:
        results: Per-entry verification results.
        healthy: Count of HEALTHY entries.
        flagged: Count of FLAGGED entries.
        tombstoned: Count of TOMBSTONED entries.
        missing_post: Count of MISSING_POST entries.
        unresolvable: Count of UNRESOLVABLE entries.
        banned: Count of BANNED entries (deleted from DB).
    """
    results: List[VerifyResult] = field(default_factory=list)
    healthy: int = 0
    flagged: int = 0
    tombstoned: int = 0
    missing_post: int = 0
    unresolvable: int = 0
    banned: int = 0

    @property
    def has_unresolvable(self) -> bool:
        """Whether any entry is unresolvable."""
        return self.unresolvable > 0

    @property
    def has_banned(self) -> bool:
        """Whether any entries were deleted for being in banned channels."""
        return self.banned > 0

    @property
    def total(self) -> int:
        """Total entries checked."""
        return len(self.results)


# ═══════════════════════════════════════════════════════════════════════════════
# Class: Starboard(BaseCog)
# ═══════════════════════════════════════════════════════════════════════════════


class Starboard(BaseCog):
    """Starboard feature — highlights popular messages in a dedicated channel."""

    # Prefix applied to the starboard channel name when enabled
    CHANNEL_PREFIX: str = "\U0001f31f-"  # 🌟-

    # ── Embed Styles ──────────────────────────────────────────────────────────
    # Preset styles applied to the two parts of a starboard post.
    STARRED_STYLE = StarboardEmbedStyle(jump_field_name="Jump to starred message")
    CONTEXT_STYLE = StarboardEmbedStyle(jump_field_name="Jump to reply")

    # Example: every field overridden — shows what's available for customisation.
    # CUSTOM_STYLE = StarboardEmbedStyle(
    #     embed_color=discord.Color.blue(),   # Sidebar colour (default: gold)
    #     jump_field_name="Source",            # Field label     (default: "Original Message")
    #     jump_link_text="View Original",      # Link text       (default: "Jump to Message")
    # )

    # ── Section 1: Init & Lifecycle ───────────────────────────────────────────

    def __init__(self, bot: CoreBot):
        """Initializes the Starboard cog.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager

        # HTTP session for downloading attachments
        self.http_session: Optional[aiohttp.ClientSession] = None

        # Per-message locks to prevent double-posting from simultaneous reactions
        self._locks: Dict[int, asyncio.Lock] = {}

        # Per-guild locks to prevent concurrent verify/remake/crawl
        self._guild_op_locks: Dict[int, asyncio.Lock] = {}

        # Rate-limiting controls for slow operations.
        # Normal mode targets ~33-50% of Discord's rate limit capacity.
        # Fast mode targets ~95% — still respects Discord boundaries.
        self._rate_semaphore = asyncio.Semaphore(1)
        self._rate_delay = 0.6       # seconds between external calls (normal)
        self._rate_delay_fast = 0.05  # seconds between external calls (fast, ~95% of Discord limits)
        self._rate_retries = 4

        # Fast-mode override — when True, uses aggressive (but safe) rate limits
        self._fast_mode = False

        # Track pending self-heal tasks so we don't spawn duplicates
        self._heal_tasks: Dict[int, asyncio.Task] = {}  # type: ignore[type-arg]

        # Track pending crawl tasks
        self._crawl_tasks: Dict[int, asyncio.Task] = {}  # type: ignore[type-arg]

    async def cog_ready(self) -> None:
        """Called after the bot is fully connected and ready.

        Starts the HTTP session, runs bounded catch-up crawl for each guild
        with a configured starboard, and resumes any interrupted deep crawls.
        """
        self.http_session = aiohttp.ClientSession()
        self.logger.info("Starboard cog ready.")

        # Background tasks spawned during startup; prevent GC
        self._startup_tasks: List[asyncio.Task] = []  # type: ignore[type-arg]

        # Iterate guilds the bot is in and run startup tasks
        for guild in self.bot.guilds:
            cfg = await self.get_starboard_config(guild.id)

            # Resume deep crawl if one was interrupted — independent of enabled flag.
            # A crawl in progress should always be resumed, even if starboard was
            # disabled after the crawl started.
            if cfg.crawl_started_at and cfg.channel_id:
                task = asyncio.create_task(self._resume_crawl_if_pending(guild.id))
                self._startup_tasks.append(task)

            if not cfg.enabled or not cfg.channel_id:
                continue

            # Bounded catch-up crawl
            task = asyncio.create_task(self._bounded_catchup_crawl(guild.id))
            self._startup_tasks.append(task)

    async def cog_unload(self) -> None:
        """Clean up resources when the cog is unloaded."""
        # Cancel any running heal/crawl tasks
        for task in self._heal_tasks.values():
            task.cancel()
        for task in self._crawl_tasks.values():
            task.cancel()

        if self.http_session:
            await self.http_session.close()
        self.logger.info("Starboard cog unloaded.")

    # ── Section 2: Configuration ──────────────────────────────────────────────

    async def get_starboard_config(self, guild_id: int) -> StarboardConfig:
        """Fetches starboard configuration for a guild as a dataclass.

        If no row exists, returns a default StarboardConfig whose defaults
        are defined on the dataclass itself (the single source of truth).

        Args:
            guild_id: The ID of the guild.

        Returns:
            A StarboardConfig instance.
        """
        row = await self.db_manager.get_starboard_config(guild_id)
        if row is None:
            return StarboardConfig(guild_id=guild_id)

        return StarboardConfig(
            guild_id=row["guild_id"],
            enabled=bool(row["enabled"]),
            channel_id=row["channel_id"],
            emoji=row["emoji"],
            threshold=row["threshold"],
            last_heal_at=row["last_heal_at"] or 0,
            crawl_started_at=row["crawl_started_at"],
            crawl_requested_by=row["crawl_requested_by"],
            crawl_notify_channel=row["crawl_notify_channel"],
            crawl_include_threads=bool(row["crawl_include_threads"]),
            crawl_last_channel_id=row["crawl_last_channel_id"],
            crawl_last_message_id=row["crawl_last_message_id"],
        )

    # ── Channel Rename Helpers ────────────────────────────────────────────────

    async def _add_channel_prefix(self, channel: discord.TextChannel) -> None:
        """Add the 🌟- prefix to a starboard channel name.

        Args:
            channel: The channel to rename.
        """
        if not channel.name.startswith(self.CHANNEL_PREFIX):
            try:
                await channel.edit(name=f"{self.CHANNEL_PREFIX}{channel.name}")
            except discord.Forbidden:
                self.logger.warning(f"No permission to rename channel {channel.id}.")
            except discord.HTTPException as e:
                self.logger.error(f"Failed to rename channel {channel.id}: {e}")

    async def _remove_channel_prefix(self, channel: discord.TextChannel) -> None:
        """Remove the 🌟- prefix from a starboard channel name.

        Args:
            channel: The channel to rename.
        """
        if channel.name.startswith(self.CHANNEL_PREFIX):
            try:
                await channel.edit(name=channel.name[len(self.CHANNEL_PREFIX):])
            except discord.Forbidden:
                self.logger.warning(f"No permission to rename channel {channel.id}.")
            except discord.HTTPException as e:
                self.logger.error(f"Failed to rename channel {channel.id}: {e}")

    async def _resolve_channel_arg(
        self, ctx: commands.Context, value: str
    ) -> Tuple[int, str]:
        """Resolve a user-provided channel argument to (channel_id, display_label).

        Tries the TextChannel converter first (handles mentions, names, IDs that
        the bot can see), then falls back to parsing as a raw integer ID.

        Args:
            ctx: The invocation context.
            value: The raw argument string.

        Returns:
            A tuple of (channel_id, label) where label is a mention if resolved
            or ``<#id>`` if only a raw ID.

        Raises:
            commands.BadArgument: If the value is neither a resolvable channel
                nor a valid integer ID.
        """
        try:
            channel = await commands.TextChannelConverter().convert(ctx, value)
            return channel.id, channel.mention
        except commands.BadArgument:
            pass

        # Strip <# > wrapper if someone pastes a mention of a channel the bot can't see
        cleaned = value.strip()
        if cleaned.startswith("<#") and cleaned.endswith(">"):
            cleaned = cleaned[2:-1]

        try:
            channel_id = int(cleaned)
            return channel_id, f"<#{channel_id}>"
        except ValueError:
            raise commands.BadArgument(
                f"Could not resolve `{value}` to a channel or ID."
            ) from None

    @commands.hybrid_group(
        name="starboard",
        hidden=True,
        usage="<subcommand>",
        help="Manages starboard settings."
    )
    async def starboard_group(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Manages starboard settings."""
        if ctx.invoked_subcommand is None:
            help_cog: Any = self.bot.get_cog('Help')
            if help_cog and hasattr(help_cog, 'send_command_help'):
                await help_cog.send_command_help(ctx, ctx.command)
            else:
                await ctx.send_help(ctx.command)

    @starboard_group.command(
        name="set",
        help="Sets or displays the starboard channel."
    )
    @commands.check_any(
        commands.has_guild_permissions(manage_channels=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def set_channel(self, ctx: commands.Context, channel: Optional[str] = None) -> None:  # type: ignore[type-arg]
        """Sets the starboard channel, or displays the current one if called naked.

        When changing channels, the previous channel is automatically added to
        the banned list to prevent re-tracking already-posted messages.

        Accepts a channel mention/name or a raw ID.

        Args:
            channel: The channel to use (mention, name, or ID), or None to display current.
        """
        if not ctx.guild:
            return

        config = await self.get_starboard_config(ctx.guild.id)

        if channel is None:
            # Naked call — display current
            if config.channel_id:
                await ctx.send(f"Starboard channel: <#{config.channel_id}> (`{config.channel_id}`)")
            else:
                await ctx.send("Starboard channel is not set.")
            return

        channel_id, label = await self._resolve_channel_arg(ctx, channel)
        old_channel_id = config.channel_id

        # Auto-ban the old channel if switching
        if old_channel_id and old_channel_id != channel_id:
            await self.db_manager.add_starboard_banned_channel(ctx.guild.id, old_channel_id)
            self.logger.info(f"Auto-banned old starboard channel {old_channel_id} for guild {ctx.guild.id}.")

        # Build a full row from dataclass defaults + the new channel_id.
        # This is the only code path that creates a starboard_config row.
        cfg = StarboardConfig(guild_id=ctx.guild.id, channel_id=channel_id)
        if config.channel_id:
            # Row already exists — preserve existing settings, just update channel
            await self.db_manager.upsert_starboard_config(ctx.guild.id, channel_id=channel_id)
        else:
            # First-time setup — write the full row with dataclass defaults
            await self.db_manager.upsert_starboard_config(ctx.guild.id, **cfg.to_db_dict())

        msg = f"Starboard channel set to {label}."
        if old_channel_id and old_channel_id != channel_id:
            msg += f" Previous channel <#{old_channel_id}> has been added to the ban list."

        # Warn if the bot can't actually see the channel
        resolved = self.bot.get_channel(channel_id)
        if not isinstance(resolved, discord.TextChannel):
            msg += "\n⚠️ I can't see this channel. Make sure it exists and I have access to it."

        await ctx.send(msg)

    @starboard_group.command(
        name="emoji",
        help="Sets or displays the starboard emoji."
    )
    @commands.check_any(
        commands.has_guild_permissions(manage_channels=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def set_emoji(self, ctx: commands.Context, emoji: Optional[str] = None) -> None:  # type: ignore[type-arg]
        """Sets the starboard emoji, or displays the current one if called naked.

        Args:
            emoji: The emoji to use, or None to display current.
        """
        if not ctx.guild:
            return

        if emoji is None:
            config = await self.get_starboard_config(ctx.guild.id)
            await ctx.send(f"Starboard emoji: {config.emoji}")
            return

        await self.db_manager.upsert_starboard_config(ctx.guild.id, emoji=emoji)
        await ctx.send(f"Starboard emoji set to {emoji}")

    @starboard_group.command(
        name="threshold",
        help="Sets or displays the reaction threshold."
    )
    @commands.check_any(
        commands.has_guild_permissions(manage_channels=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def set_threshold(self, ctx: commands.Context, threshold: Optional[int] = None) -> None:  # type: ignore[type-arg]
        """Sets the reaction threshold, or displays the current one if called naked.

        Args:
            threshold: The minimum reactions required, or None to display current.
        """
        if not ctx.guild:
            return

        if threshold is None:
            config = await self.get_starboard_config(ctx.guild.id)
            await ctx.send(f"Starboard threshold: **{config.threshold}**")
            return

        if threshold < 1:
            await ctx.send("Threshold must be at least 1.")
            return

        await self.db_manager.upsert_starboard_config(ctx.guild.id, threshold=threshold)
        await ctx.send(f"Starboard threshold set to **{threshold}**")

    @starboard_group.command(
        name="toggle",
        help="Toggles the starboard on/off, or displays current state."
    )
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="on/off, true/false, yes/no. Omit to show current state."
    )
    async def toggle_starboard(self, ctx: commands.Context, enabled: Optional[str] = None) -> None:  # type: ignore[type-arg]
        """Toggles the starboard on or off, or displays the current state.

        Naked call shows the current enabled/disabled status.
        Enabling requires a channel to be set first and adds the 🌟- prefix.
        Disabling removes the prefix but keeps settings and entries.

        Accepts: on/off, true/false, enable/disable.

        Args:
            enabled: String toggle value, or None to display current state.
        """
        if not ctx.guild:
            return

        config = await self.get_starboard_config(ctx.guild.id)

        # Parse the toggle value
        _TRUTHY = {'on', 'true', 'enable', 'enabled'}
        _FALSY = {'off', 'false', 'disable', 'disabled'}
        toggle: Optional[bool] = None

        if enabled is not None:
            val = enabled.lower().strip()
            if val in _TRUTHY:
                toggle = True
            elif val in _FALSY:
                toggle = False
            else:
                await ctx.send(f"Unknown value `{enabled}`. Use on/off, true/false, or enable/disable.")
                return

        if toggle is None:
            # Naked call — display current state
            state = "enabled" if config.enabled else "disabled"
            msg = f"Starboard is **{state}**."
            if config.channel_id:
                msg += f" Channel: <#{config.channel_id}>"
            await ctx.send(msg)
            return

        if toggle:
            # Enable
            if config.channel_id is None:
                await ctx.send("Cannot enable starboard — no channel is set. Use `/starboard channel` first.")
                return

            if config.enabled:
                await ctx.send("Starboard is already enabled.")
                return

            channel = self.bot.get_channel(config.channel_id)
            if not isinstance(channel, discord.TextChannel):
                await ctx.send(
                    f"Cannot enable starboard — I can't see <#{config.channel_id}>. "
                    "Make sure the channel exists and I have access to it."
                )
                return

            await self.db_manager.set_starboard_enabled(ctx.guild.id, True)
            await self._add_channel_prefix(channel)
            await ctx.send(f"Starboard enabled in {channel.mention}.")
        else:
            # Disable
            if not config.enabled:
                await ctx.send("Starboard is already disabled.")
                return

            await self.db_manager.set_starboard_enabled(ctx.guild.id, False)

            if config.channel_id:
                channel = self.bot.get_channel(config.channel_id)
                if isinstance(channel, discord.TextChannel):
                    await self._remove_channel_prefix(channel)

            await ctx.send("Starboard disabled. Settings and entries are preserved.")

    @starboard_group.command(
        name="unset",
        help="Unsets the starboard channel and disables the starboard."
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def unset_starboard(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Unsets the starboard channel, disabling it.

        The old channel is auto-banned. Emoji, threshold, and entries are kept.
        """
        if not ctx.guild:
            return

        config = await self.get_starboard_config(ctx.guild.id)
        if config.channel_id is None:
            await ctx.send("Starboard channel is not set.")
            return

        old_channel_id = config.channel_id

        # Auto-ban + remove prefix
        await self.db_manager.add_starboard_banned_channel(ctx.guild.id, old_channel_id)
        channel = self.bot.get_channel(old_channel_id)
        if isinstance(channel, discord.TextChannel):
            await self._remove_channel_prefix(channel)

        await self.db_manager.upsert_starboard_config(ctx.guild.id, channel_id=None, enabled=0)
        await ctx.send(
            f"Starboard unset. <#{old_channel_id}> has been added to the ban list. "
            "Emoji, threshold, and entries are preserved."
        )

    @starboard_group.command(
        name="ban",
        help="Bans a channel from the starboard, or lists banned channels."
    )
    @commands.check_any(
        commands.has_guild_permissions(manage_channels=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def ban_channel(self, ctx: commands.Context, channel: Optional[str] = None) -> None:  # type: ignore[type-arg]
        """Bans a channel from starboard tracking, or lists banned channels.

        Accepts a channel mention/name or a raw ID.

        Args:
            channel: The channel to ban (mention, name, or ID), or None to list banned channels.
        """
        if not ctx.guild:
            return

        if channel is None:
            # List banned channels
            banned = await self.db_manager.get_starboard_banned_channels(ctx.guild.id)
            if not banned:
                await ctx.send("No channels are banned from the starboard.")
                return
            lines = [f"• <#{cid}> (`{cid}`)" for cid in banned]
            await ctx.send("**Banned starboard channels:**\n" + "\n".join(lines))
            return

        channel_id, label = await self._resolve_channel_arg(ctx, channel)
        await self.db_manager.add_starboard_banned_channel(ctx.guild.id, channel_id)
        await ctx.send(f"{label} has been banned from the starboard.")

    @starboard_group.command(
        name="unban",
        help="Unbans a channel from the starboard."
    )
    @commands.check_any(
        commands.has_guild_permissions(manage_channels=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def unban_channel(self, ctx: commands.Context, channel: str) -> None:  # type: ignore[type-arg]
        """Removes a channel from the starboard ban list.

        Accepts a channel mention/name or a raw ID (for deleted channels).

        Args:
            channel: The channel to unban (mention, name, or ID).
        """
        if not ctx.guild:
            return

        channel_id, label = await self._resolve_channel_arg(ctx, channel)

        banned = await self.db_manager.get_starboard_banned_channels(ctx.guild.id)
        if channel_id not in banned:
            await ctx.send(f"{label} is not in the ban list.")
            return

        await self.db_manager.remove_starboard_banned_channel(ctx.guild.id, channel_id)
        await ctx.send(f"{label} has been unbanned from the starboard.")

    # ── Section 3: Shared Infrastructure ──────────────────────────────────────

    def _acquire_guild_lock(self, guild_id: int) -> asyncio.Lock:
        """Get or create a per-guild operation lock.

        This lock prevents concurrent verify/remake/crawl operations for the
        same guild. Normal reaction handling is NOT gated by this lock.

        Args:
            guild_id: The guild ID.

        Returns:
            The asyncio.Lock for this guild.
        """
        if guild_id not in self._guild_op_locks:
            self._guild_op_locks[guild_id] = asyncio.Lock()
        return self._guild_op_locks[guild_id]

    async def _run_rate_limited(self, coro_func: Any, *args: Any, delay: Optional[float] = None, retries: Optional[int] = None) -> Any:
        """Run a coroutine-callable under the rate semaphore with backoff.

        In fast mode, uses a much shorter delay (~95% of Discord's rate limit
        capacity) instead of the normal conservative delay. The semaphore and
        backoff logic remain active in both modes.

        Args:
            coro_func: A callable that returns an awaitable when called with *args.
            *args: Arguments to pass to coro_func.
            delay: Delay in seconds after success. Defaults to self._rate_delay
                   (or self._rate_delay_fast in fast mode).
            retries: Number of retries. Defaults to self._rate_retries.

        Returns:
            The result of the coroutine.

        Raises:
            discord.NotFound: Re-raised immediately (no retry on 404).
        """
        if delay is None:
            delay = self._rate_delay_fast if self._fast_mode else self._rate_delay
        if retries is None:
            retries = self._rate_retries

        async with self._rate_semaphore:
            backoff = 2.0
            last_exc: Optional[Exception] = None
            for attempt in range(retries):
                try:
                    result = await coro_func(*args)
                    await asyncio.sleep(delay)
                    return result
                except discord.NotFound:
                    raise
                except (discord.HTTPException, aiohttp.ClientError) as e:
                    last_exc = e
                    wait = backoff
                    self.logger.debug(f"_run_rate_limited attempt {attempt + 1}/{retries} failed: {e}; backing off {wait}s")
                    backoff = min(backoff * 2, 30)
                    await asyncio.sleep(wait)
                except Exception:
                    raise
            if last_exc:
                raise last_exc
            return None  # Should not reach here

    async def _status_editor(
        self,
        status_message: discord.Message,
        progress: Dict[str, Any],
        stop_event: asyncio.Event,
        interval: float = 15.0,
        operation: str = "operation"
    ) -> None:
        """Periodically edit a status message to show progress.

        Args:
            status_message: The Discord message to edit.
            progress: Mutable dict with 'done', 'total', 'elapsed' keys.
            stop_event: Set this to stop the editor.
            interval: Seconds between edits.
            operation: Label for the operation (e.g. "verify", "remake").
        """
        try:
            while not stop_event.is_set():
                await asyncio.sleep(interval)
                progress['elapsed'] = progress.get('elapsed', 0) + int(interval)
                done = progress.get('done', 0)
                total = progress.get('total', '?')
                elapsed = progress.get('elapsed', 0)
                try:
                    await status_message.edit(
                        content=f"Starboard {operation} running... processed {done}/{total}. Elapsed: {elapsed}s."
                    )
                except Exception:
                    pass  # Ignore edit errors; keep looping
        except asyncio.CancelledError:
            return

    async def _notify_user(self, user_id: int, channel_id: int, message: str) -> None:
        """Attempt to DM a user, falling back to a channel ping.

        Args:
            user_id: The Discord user ID to notify.
            channel_id: Fallback channel ID for pinging.
            message: The notification text.
        """
        user = self.bot.get_user(user_id)
        if user:
            try:
                await user.send(message)
                return
            except discord.Forbidden:
                pass
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"<@{user_id}> (I couldn't DM you — open your DMs!) {message}")

    async def _confirm_fast_mode(self, ctx: commands.Context, fast: bool) -> bool:  # type: ignore[type-arg]
        """Handles the confirmation logic for fast mode.

        Fast mode is an owner-only override that operates at ~95% of Discord's
        rate limit capacity instead of the normal conservative ~33-50%. The
        semaphore and backoff logic remain active — it's faster, not unsafe.

        Args:
            ctx: The command context.
            fast: Whether fast mode was requested.

        Returns:
            True if the operation should proceed, False to abort.
        """
        if not ctx.guild:
            return False

        msg = getattr(ctx, 'message', None)
        msg_content = msg.content.lower() if msg and getattr(msg, 'content', None) else ''
        fast_requested = bool(fast) or ('--fast' in msg_content)

        if not fast_requested:
            self._fast_mode = False
            return True

        # Fast mode is owner-only
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("Fast mode is restricted to the bot owner.")
            return False

        future: asyncio.Future = asyncio.get_event_loop().create_future()  # type: ignore[var-annotated]
        modal = FastConfirmModal(future)
        await launch_modal(ctx, modal)

        try:
            confirmed = await asyncio.wait_for(future, timeout=45.0)
        except asyncio.TimeoutError:
            await ctx.send('Fast mode cancelled (timed out).')
            return False

        if not confirmed:
            return False

        self._fast_mode = True
        self.logger.warning(
            f"FAST MODE ENABLED by {ctx.author} ({ctx.author.id}) in guild {ctx.guild.id}"
        )
        return True

    # ── Section 4: Starboard Post Creation ────────────────────────────────────

    async def create_starboard_embed_and_files(
        self,
        message: discord.Message,
        style: Optional[StarboardEmbedStyle] = None
    ) -> Tuple[discord.Embed, List[discord.File]]:
        """Creates an embed and file list for a starboard message.

        Handles regular content, attachments, forwarded snapshots, and embeds.

        Args:
            message: The source message.
            style: Embed appearance config. Uses default gold style if None.

        Returns:
            Tuple of (embed, list of discord.File objects).
        """
        if style is None:
            style = StarboardEmbedStyle()

        description_parts: List[str] = []
        files: List[discord.File] = []

        MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB per file
        MAX_TOTAL_SIZE = 25 * 1024 * 1024   # 25 MB total
        current_total_size = 0

        async def download_content(url: str, filename: str, spoiler: bool = False) -> None:
            nonlocal current_total_size
            if current_total_size >= MAX_TOTAL_SIZE:
                return
            if self.http_session is None:
                self.logger.error("HTTP session is not initialized.")
                return
            try:
                async with self.http_session.get(url) as resp:
                    if resp.status == 200:
                        content_length = resp.headers.get('Content-Length')
                        if content_length and int(content_length) > MAX_FILE_SIZE:
                            self.logger.warning(f"Skipping attachment {filename}: exceeds 10MB limit.")
                            return

                        data = io.BytesIO()
                        file_size = 0
                        while True:
                            chunk = await resp.content.read(4096)
                            if not chunk:
                                break
                            file_size += len(chunk)
                            if file_size > MAX_FILE_SIZE:
                                self.logger.warning(f"Skipping attachment {filename}: exceeds 10MB limit during download.")
                                return
                            if current_total_size + file_size > MAX_TOTAL_SIZE:
                                self.logger.warning(f"Skipping attachment {filename}: exceeds total 25MB limit.")
                                return
                            data.write(chunk)

                        data.seek(0)
                        current_total_size += file_size
                        files.append(discord.File(data, filename=filename, spoiler=spoiler))
            except Exception as e:
                self.logger.error(f"Failed to download attachment {filename}: {e}")

        # Message content
        if message.content:
            description_parts.append(message.content)

        # Attachments
        for attachment in message.attachments:
            await download_content(attachment.url, attachment.filename, attachment.is_spoiler())

        # Forwarded snapshots
        if hasattr(message, 'message_snapshots') and message.message_snapshots:
            for snapshot in message.message_snapshots:
                if snapshot.content:
                    description_parts.append(snapshot.content)
                for attachment in snapshot.attachments:
                    await download_content(attachment.url, attachment.filename, attachment.is_spoiler())
        # Embeds (only if no snapshots)
        elif message.embeds:
            embed = message.embeds[0]
            if embed.description:
                description_parts.append(embed.description)
            if embed.image and embed.image.url:
                filename = embed.image.url.split('/')[-1].split('?')[0] or "embedded_image.png"
                await download_content(embed.image.url, filename)

        description = "\n\n".join(description_parts)
        if len(description) > 4096:
            description = description[:4093] + "..."

        new_embed = discord.Embed(
            description=description,
            color=style.embed_color,
            timestamp=message.created_at
        )
        new_embed.set_author(
            name=f"{message.author.display_name} ({message.author.name})",
            icon_url=message.author.display_avatar.url
        )
        new_embed.set_footer(text=f"ID: {message.id}")
        new_embed.add_field(
            name=style.jump_field_name,
            value=f"[{style.jump_link_text}]({message.jump_url})",
            inline=False
        )

        return new_embed, files

    async def _create_tombstone(self, starboard_channel: discord.TextChannel, original_message_id: int) -> Optional[discord.Message]:
        """Creates a tombstone message for a lost original message.

        Args:
            starboard_channel: The starboard channel to post in.
            original_message_id: The ID of the lost original message.

        Returns:
            The tombstone message, or None on failure.
        """
        try:
            return await starboard_channel.send(f"🪦 Original Message {original_message_id} Lost")
        except Exception as e:
            self.logger.error(f"Failed to create tombstone for {original_message_id}: {e}")
            return None

    async def _edit_to_tombstone(self, starboard_message: discord.Message, original_message_id: int) -> bool:
        """Edits an existing starboard message into a tombstone.

        Used by verify/self-heal when the original message is confirmed dead
        but the starboard message still exists (preserves chronological position).

        Args:
            starboard_message: The existing starboard channel message.
            original_message_id: The ID of the lost original message.

        Returns:
            True if the edit succeeded.
        """
        try:
            await starboard_message.edit(
                content=f"🪦 Original Message {original_message_id} Lost",
                embed=None
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to edit starboard message {starboard_message.id} into tombstone: {e}")
            return False

    async def create_single_starboard_post(
        self,
        message: discord.Message,
        starboard_channel: discord.TextChannel,
        content: str
    ) -> Optional[discord.Message]:
        """Creates a single starboard post (no reply context).

        Args:
            message: The original message.
            starboard_channel: The starboard channel.
            content: The header content string (e.g. "⭐ 5 in #general").

        Returns:
            The sent starboard message, or None on failure.
        """
        embed, files = await self.create_starboard_embed_and_files(message)
        try:
            starboard_message = await starboard_channel.send(content=content, embed=embed, files=files)
            self.logger.info(f"Created starboard post {starboard_message.id} for original {message.id}.")
            return starboard_message
        except discord.HTTPException as e:
            self.logger.error(f"Failed to create single starboard post: {e}")
            return None
        finally:
            for f in files:
                f.close()

    async def create_new_starboard_post(
        self,
        message: discord.Message,
        starboard_channel: discord.TextChannel,
        content: str
    ) -> Tuple[Optional[int], Optional[int]]:
        """Creates a new starboard post, handling the reply two-message system.

        If the original message is a reply, posts the replied-to message first
        as context, then replies to that with the starred message.

        Args:
            message: The original message.
            starboard_channel: The starboard channel.
            content: The header content string.

        Returns:
            Tuple of (starboard_message_id, reply_context_id) or (None, None) on failure.
        """
        # Handle reply two-message system
        if message.reference and message.reference.message_id and isinstance(message.channel, MessageableGuildChannel):
            reply_files: List[discord.File] = []
            main_files: List[discord.File] = []
            reply_context_message: Optional[discord.Message] = None
            try:
                replied_to = await message.channel.fetch_message(message.reference.message_id)

                # 1. Post the reply context
                reply_embed, reply_files = await self.create_starboard_embed_and_files(
                    replied_to, style=self.CONTEXT_STYLE
                )
                reply_context_message = await starboard_channel.send(embed=reply_embed, files=reply_files)

                # 2. Post the starred message as a reply to the context
                main_embed, main_files = await self.create_starboard_embed_and_files(
                    message, style=self.STARRED_STYLE
                )
                starboard_message = await reply_context_message.reply(content=content, embed=main_embed, files=main_files)

                return starboard_message.id, reply_context_message.id

            except discord.NotFound:
                # Replied-to message is gone — fall through to single post
                self.logger.debug(f"Replied-to message not found for {message.id}, falling back to single post.")
            except discord.HTTPException as e:
                self.logger.error(f"Failed to create two-part starboard post: {e}")
                if reply_context_message:
                    try:
                        await reply_context_message.delete()
                    except discord.HTTPException:
                        pass
                return None, None
            finally:
                for f in reply_files:
                    f.close()
                for f in main_files:
                    f.close()

        # Single post (non-reply or fallback)
        sb_msg = await self.create_single_starboard_post(message, starboard_channel, content)
        if sb_msg:
            return sb_msg.id, None
        return None, None

    async def post_to_starboard(
        self,
        message: discord.Message,
        starboard_channel_id: int,
        starboard_emoji: str,
        star_count: int
    ) -> None:
        """Posts or updates a message on the starboard. Upsert semantics.

        If an entry exists, updates the star count on the existing post.
        If the starboard message is missing (404), removes the stale ID and
        recreates. If no entry exists, creates a new post and DB entry.

        Args:
            message: The original message.
            starboard_channel_id: The ID of the starboard channel.
            starboard_emoji: The emoji string.
            star_count: The current number of reactions.
        """
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            self.logger.error(f"Starboard channel {starboard_channel_id} not found or not a text channel.")
            return

        content = f"{starboard_emoji} **{star_count}** in <#{message.channel.id}>"
        existing_entry = await self.db_manager.get_starboard_entry(message.id)

        if existing_entry:
            # --- Hot-path healing: sync star_count ---
            await self.db_manager.update_starboard_star_count(message.id, star_count)

            # --- Hot-path healing: sync channel ID if it drifted ---
            if existing_entry.get('original_channel_id') != message.channel.id:
                await self.db_manager.update_starboard_channel(message.id, message.channel.id)
                self.logger.info(f"Hot-path healed channel for {message.id}: {existing_entry['original_channel_id']} -> {message.channel.id}")

            # --- Hot-path healing: reset failed_checks (message is alive) ---
            if existing_entry.get('failed_checks', 0) > 0:
                await self.db_manager.reset_starboard_failed_checks(message.id)

            sb_msg_id = existing_entry.get('starboard_message_id')
            if not sb_msg_id:
                # Entry exists but has no starboard message (crawled, not yet posted).
                # Create the post.
                sb_id, reply_id = await self.create_new_starboard_post(message, starboard_channel, content)
                if sb_id:
                    await self.db_manager.set_starboard_message_id(message.id, sb_id, reply_id)
                return

            try:
                starboard_message = await starboard_channel.fetch_message(sb_msg_id)
                await starboard_message.edit(content=content)
            except discord.NotFound:
                # Starboard message was deleted — recreate
                self.logger.warning(f"Starboard message for {message.id} not found. Recreating.")
                sb_id, reply_id = await self.create_new_starboard_post(message, starboard_channel, content)
                if sb_id:
                    await self.db_manager.set_starboard_message_id(message.id, sb_id, reply_id)
        else:
            # New entry — create post and DB row
            sb_id, reply_id = await self.create_new_starboard_post(message, starboard_channel, content)
            if sb_id and message.guild:
                await self.db_manager.add_starboard_entry(
                    original_message_id=message.id,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_created_at=snowflake_to_unix(message.id),
                    star_count=star_count,
                    starboard_message_id=sb_id,
                    starboard_reply_id=reply_id
                )

    # ── Section 5: Verification Engine ────────────────────────────────────────

    async def _verify_single_entry(
        self,
        entry: Dict[str, Any],
        starboard_channel: discord.TextChannel,
        starboard_emoji: str
    ) -> VerifyResult:
        """Verify one starboard DB entry against live Discord state.

        Checks that both the original and starboard messages exist.
        Updates star_count, flags missing originals, tombstones repeat failures.

        Args:
            entry: The starboard entry dict from the DB.
            starboard_channel: The starboard channel object.
            starboard_emoji: The emoji string for this guild.

        Returns:
            A VerifyResult describing what happened.
        """
        original_id = entry.get('original_message_id')
        sb_msg_id = entry.get('starboard_message_id')
        channel_id = entry.get('original_channel_id')
        failed_checks = entry.get('failed_checks', 0)

        # -- Check starboard message --
        sb_msg: Optional[discord.Message] = None
        sb_exists = False
        if sb_msg_id:
            try:
                sb_msg = await self._run_rate_limited(starboard_channel.fetch_message, sb_msg_id)
                sb_exists = True
            except discord.NotFound:
                sb_exists = False
            except (discord.Forbidden, discord.HTTPException) as e:
                return VerifyResult(
                    entry=entry, status=VerifyStatus.UNRESOLVABLE,
                    needs_db_update=False,
                    message=f"Cannot access starboard message {sb_msg_id}: {e}"
                )

        # -- Check original message --
        original_exists = False
        original_msg: Optional[discord.Message] = None
        if channel_id and original_id:
            ch = self.bot.get_channel(channel_id)
            if isinstance(ch, MessageableGuildChannel):
                try:
                    original_msg = await self._run_rate_limited(ch.fetch_message, original_id)
                    original_exists = True
                except discord.NotFound:
                    original_exists = False
                except (discord.Forbidden, discord.HTTPException) as e:
                    return VerifyResult(
                        entry=entry, status=VerifyStatus.UNRESOLVABLE,
                        needs_db_update=False,
                        message=f"Cannot access original channel {channel_id} for message {original_id}: {e}"
                    )
            else:
                # Channel gone or not messageable — treat original as missing
                original_exists = False

        # -- Both exist: HEALTHY --
        if original_exists and sb_exists and original_msg:
            star_reaction = discord.utils.get(original_msg.reactions, emoji=starboard_emoji)
            live_count = star_reaction.count if star_reaction else 0
            needs_update = False

            if entry.get('star_count') != live_count:
                entry['star_count'] = live_count
                needs_update = True
            if failed_checks > 0:
                entry['failed_checks'] = 0
                needs_update = True

            return VerifyResult(
                entry=entry, status=VerifyStatus.HEALTHY,
                needs_db_update=needs_update,
                message=f"OK (stars={live_count})"
            )

        # -- Starboard message missing, original alive: MISSING_POST --
        if original_exists and not sb_exists:
            entry['starboard_message_id'] = None
            entry['starboard_reply_id'] = None
            entry['failed_checks'] = 0
            return VerifyResult(
                entry=entry, status=VerifyStatus.MISSING_POST,
                needs_db_update=True,
                message=f"Starboard message {sb_msg_id} missing; nulled for remake."
            )

        # -- Original missing --
        if not original_exists:
            if failed_checks == 0:
                # First failure — flag it
                entry['failed_checks'] = 1
                return VerifyResult(
                    entry=entry, status=VerifyStatus.FLAGGED,
                    needs_db_update=True,
                    message=f"Original message {original_id} not found (first failure, flagged)."
                )
            else:
                # Repeat failure — tombstone
                if sb_exists and sb_msg:
                    # Edit the existing starboard message into a tombstone
                    await self._edit_to_tombstone(sb_msg, original_id or 0)
                else:
                    # Both gone — mark in DB for remake to handle
                    entry['starboard_message_id'] = None
                    entry['starboard_reply_id'] = None

                entry['failed_checks'] = failed_checks + 1
                return VerifyResult(
                    entry=entry, status=VerifyStatus.TOMBSTONED,
                    needs_db_update=True,
                    message=f"Original message {original_id} confirmed dead (strike {failed_checks + 1}). "
                            + ("Edited to tombstone." if sb_exists else "Marked for remake.")
                )

        # Fallback — should not normally reach here
        return VerifyResult(
            entry=entry, status=VerifyStatus.UNRESOLVABLE,
            needs_db_update=False,
            message=f"Unexpected state for entry {original_id}."
        )

    async def _verify_all_entries(
        self,
        guild_id: int,
        starboard_channel: discord.TextChannel,
        starboard_emoji: str,
        progress: Optional[Dict[str, Any]] = None
    ) -> VerifyReport:
        """Run verification on all starboard entries for a guild.

        Args:
            guild_id: The guild ID.
            starboard_channel: The starboard channel object.
            starboard_emoji: The emoji string.
            progress: Optional mutable dict for status updates ('done', 'total').

        Returns:
            A VerifyReport with per-entry results and aggregate counts.
        """
        entries = await self.db_manager.get_starboard_entries_ordered(guild_id)
        report = VerifyReport()

        # Pre-fetch banned channels for this guild
        banned_channels = set(await self.db_manager.get_starboard_banned_channels(guild_id))

        if progress is not None:
            progress['total'] = len(entries)
            progress['done'] = 0

        for entry in entries:
            original_channel_id = entry.get('original_channel_id')

            # Check if the entry's channel is banned
            if original_channel_id and original_channel_id in banned_channels:
                original_id = entry.get('original_message_id', 0)
                await self.db_manager.remove_starboard_entry(int(original_id))
                result = VerifyResult(
                    entry=entry, status=VerifyStatus.BANNED,
                    needs_db_update=False,  # Already deleted
                    message=f"Entry {original_id} in banned channel <#{original_channel_id}> — deleted."
                )
                report.results.append(result)
                report.banned += 1
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                continue

            result = await self._verify_single_entry(entry, starboard_channel, starboard_emoji)
            report.results.append(result)

            # Update aggregate counts
            if result.status == VerifyStatus.HEALTHY:
                report.healthy += 1
            elif result.status == VerifyStatus.FLAGGED:
                report.flagged += 1
            elif result.status == VerifyStatus.TOMBSTONED:
                report.tombstoned += 1
            elif result.status == VerifyStatus.MISSING_POST:
                report.missing_post += 1
            elif result.status == VerifyStatus.UNRESOLVABLE:
                report.unresolvable += 1
            elif result.status == VerifyStatus.BANNED:
                report.banned += 1

            if progress is not None:
                progress['done'] = progress.get('done', 0) + 1

        return report

    async def _apply_verify_results(self, report: VerifyReport) -> None:
        """Write verification results back to the database.

        Args:
            report: The verify report to apply.
        """
        for result in report.results:
            if result.needs_db_update:
                await self.db_manager.update_starboard_entry(result.entry)

    async def _should_self_heal(self, guild_id: int) -> bool:
        """Check if 12+ hours have passed since the last self-heal.

        Args:
            guild_id: The guild ID.

        Returns:
            True if self-heal should run.
        """
        cfg = await self.get_starboard_config(guild_id)
        if cfg.last_heal_at == 0:
            return True
        return (int(time.time()) - cfg.last_heal_at) >= 43200  # 12 hours

    async def _trigger_self_heal(self, guild_id: int) -> None:
        """Spawn a background self-heal task if one isn't already running.

        The task acquires the guild operation lock, runs full verification,
        applies results, and updates starboard_last_heal_at.

        Args:
            guild_id: The guild ID.
        """
        if guild_id in self._heal_tasks and not self._heal_tasks[guild_id].done():
            return  # Already running

        async def _heal() -> None:
            lock = self._acquire_guild_lock(guild_id)
            if lock.locked():
                self.logger.debug(f"Self-heal skipped for guild {guild_id}: another operation is running.")
                return

            async with lock:
                self.logger.info(f"Self-heal started for guild {guild_id}.")
                cfg = await self.get_starboard_config(guild_id)
                if not cfg.channel_id:
                    return
                starboard_channel = self.bot.get_channel(cfg.channel_id)
                if not isinstance(starboard_channel, discord.TextChannel):
                    return

                report = await self._verify_all_entries(guild_id, starboard_channel, cfg.emoji)
                await self._apply_verify_results(report)

                # Update last heal timestamp
                await self.db_manager.upsert_starboard_config(guild_id, last_heal_at=int(time.time()))

                self.logger.info(
                    f"Self-heal complete for guild {guild_id}: "
                    f"healthy={report.healthy}, flagged={report.flagged}, "
                    f"tombstoned={report.tombstoned}, missing_post={report.missing_post}, "
                    f"unresolvable={report.unresolvable}"
                )

        task = asyncio.create_task(_heal())
        self._heal_tasks[guild_id] = task

    @starboard_group.command(
        name="verify",
        help="Verifies all starboard entries."
    )
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        fast="If True, skips rate limits (Dangerous!)."
    )
    async def verify_starboard(self, ctx: commands.Context, fast: bool = False) -> None:  # type: ignore[type-arg]
        """Runs a full verification audit on all starboard entries.

        Checks every entry against live Discord state, flags missing originals,
        tombstones repeat failures, and recovers missing starboard messages.

        Args:
            fast: Whether to enable fast mode.
        """
        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        if not await self._confirm_fast_mode(ctx, fast):
            return

        lock = self._acquire_guild_lock(ctx.guild.id)
        if lock.locked():
            await ctx.send("Another starboard operation is already running for this guild.")
            return

        async with lock:
            cfg = await self.get_starboard_config(ctx.guild.id)
            if not cfg.channel_id:
                await ctx.send("Starboard channel is not configured.")
                self._fast_mode = False
                return
            starboard_channel = self.bot.get_channel(cfg.channel_id)
            if not isinstance(starboard_channel, discord.TextChannel):
                await ctx.send("Starboard channel not found.")
                self._fast_mode = False
                return

            # Start status reporting
            stop_event = asyncio.Event()
            progress: Dict[str, Any] = {'done': 0, 'total': 0, 'elapsed': 0}
            status_msg = await ctx.send("Starboard verify starting...")
            status_task = asyncio.create_task(
                self._status_editor(status_msg, progress, stop_event, interval=15.0, operation="verify")
            )

            try:
                report = await self._verify_all_entries(ctx.guild.id, starboard_channel, cfg.emoji, progress)
                await self._apply_verify_results(report)

                # Update last heal timestamp
                await self.db_manager.upsert_starboard_config(ctx.guild.id, last_heal_at=int(time.time()))
            finally:
                stop_event.set()
                try:
                    await status_task
                except Exception:
                    pass
                self._fast_mode = False

            await ctx.send(
                f"✅ Starboard verify complete.\n"
                f"Healthy: {report.healthy}, Flagged: {report.flagged}, "
                f"Tombstoned: {report.tombstoned}, Missing post: {report.missing_post}, "
                f"Banned: {report.banned}, Unresolvable: {report.unresolvable}"
            )

    # ── Section 6: Remake Engine ──────────────────────────────────────────────

    async def _delete_starboard_messages(
        self,
        entries: List[Dict[str, Any]],
        starboard_channel: discord.TextChannel
    ) -> int:
        """Delete all starboard Discord messages and null their DB IDs.

        Iterates entries, deletes the starboard message and reply context from
        Discord, then nulls starboard_message_id for the entire guild in one
        DB call.

        Args:
            entries: The list of entry dicts.
            starboard_channel: The starboard channel.

        Returns:
            Count of Discord messages successfully deleted.
        """
        deleted = 0
        guild_id = entries[0]['guild_id'] if entries else None

        for entry in entries:
            sb_msg_id = entry.get('starboard_message_id')
            reply_id = entry.get('starboard_reply_id')

            if sb_msg_id:
                try:
                    msg = await starboard_channel.fetch_message(sb_msg_id)
                    await msg.delete()
                    deleted += 1
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    self.logger.error(f"Failed to delete starboard message {sb_msg_id}: {e}")

            if reply_id:
                try:
                    reply_msg = await starboard_channel.fetch_message(reply_id)
                    await reply_msg.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    self.logger.error(f"Failed to delete reply context {reply_id}: {e}")

        # Null all message IDs in one DB call
        if guild_id is not None:
            await self.db_manager.null_starboard_message_ids(guild_id)

        return deleted

    async def _recreate_in_order(
        self,
        guild_id: int,
        starboard_channel: discord.TextChannel,
        starboard_emoji: str,
        starboard_threshold: int,
        progress: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, int, int]:
        """Recreate all starboard posts in chronological order from DB.

        Fetches entries ordered by message_created_at, posts each one,
        and writes back the new starboard message IDs.

        Args:
            guild_id: The guild ID.
            starboard_channel: The starboard channel.
            starboard_emoji: The emoji string.
            starboard_threshold: The reaction threshold.
            progress: Optional mutable dict for status tracking.

        Returns:
            Tuple of (recreated_count, tombstone_count, failed_count).
        """
        entries = await self.db_manager.get_starboard_entries_ordered(guild_id)
        recreated = 0
        tombstoned = 0
        failed = 0

        if progress is not None:
            progress['total'] = len(entries)
            progress['done'] = 0

        for entry in entries:
            original_id = entry.get('original_message_id')
            channel_id = entry.get('original_channel_id')
            failed_checks = entry.get('failed_checks', 0)

            # Tombstoned entries — post tombstone in chronological position
            if failed_checks >= 2:
                tomb = await self._create_tombstone(starboard_channel, original_id or 0)
                if tomb:
                    await self.db_manager.set_starboard_message_id(original_id, tomb.id)  # type: ignore[arg-type]
                    tombstoned += 1
                else:
                    failed += 1
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                await asyncio.sleep(0.5)
                continue

            # Try to fetch the original message
            original_channel = self.bot.get_channel(channel_id) if channel_id else None
            if not isinstance(original_channel, MessageableGuildChannel):
                # Channel gone — tombstone
                tomb = await self._create_tombstone(starboard_channel, original_id or 0)
                if tomb:
                    await self.db_manager.set_starboard_message_id(original_id, tomb.id)  # type: ignore[arg-type]
                    await self.db_manager.increment_starboard_failed_checks(original_id)  # type: ignore[arg-type]
                    tombstoned += 1
                else:
                    failed += 1
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                await asyncio.sleep(0.5)
                continue

            try:
                message = await original_channel.fetch_message(original_id)  # type: ignore[arg-type]
            except discord.NotFound:
                # Original deleted — tombstone
                tomb = await self._create_tombstone(starboard_channel, original_id or 0)
                if tomb:
                    await self.db_manager.set_starboard_message_id(original_id, tomb.id)  # type: ignore[arg-type]
                    await self.db_manager.increment_starboard_failed_checks(original_id)  # type: ignore[arg-type]
                    tombstoned += 1
                else:
                    failed += 1
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                await asyncio.sleep(0.5)
                continue
            except Exception as e:
                self.logger.error(f"Failed to fetch original message {original_id}: {e}")
                failed += 1
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                continue

            # Check reaction count (skip if below threshold unless fast mode)
            star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)
            current_count = star_reaction.count if star_reaction else 0

            if not self._fast_mode and current_count < starboard_threshold:
                self.logger.info(f"Message {original_id} below threshold ({current_count} < {starboard_threshold}), skipping.")
                if progress is not None:
                    progress['done'] = progress.get('done', 0) + 1
                continue

            content = f"{starboard_emoji} **{current_count}** in <#{message.channel.id}>"
            sb_id, reply_id = await self.create_new_starboard_post(message, starboard_channel, content)
            if sb_id:
                await self.db_manager.set_starboard_message_id(original_id, sb_id, reply_id)  # type: ignore[arg-type]
                # Also sync star count
                await self.db_manager.update_starboard_star_count(original_id, current_count)  # type: ignore[arg-type]
                recreated += 1
            else:
                failed += 1

            if progress is not None:
                progress['done'] = progress.get('done', 0) + 1
            await asyncio.sleep(0.5)

        return recreated, tombstoned, failed

    async def _remake_impl(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Implementation of the remake command.

        Flow: verify → abort on unresolvable → apply verify → delete Discord
        messages → null DB IDs → recreate in chronological order.

        Args:
            ctx: The command context.
        """
        if not ctx.guild:
            return
        guild = ctx.guild

        cfg = await self.get_starboard_config(guild.id)
        if not cfg.channel_id:
            await ctx.send("Starboard channel is not configured.")
            return
        starboard_channel = self.bot.get_channel(cfg.channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            await ctx.send("Starboard channel not found.")
            return

        # ── Phase 1: Verify ──
        await ctx.send("**Phase 1/3:** Running verification before remake...")
        verify_progress: Dict[str, Any] = {'done': 0, 'total': 0, 'elapsed': 0}
        report = await self._verify_all_entries(guild.id, starboard_channel, cfg.emoji, verify_progress)

        # Abort on unresolvable
        if report.has_unresolvable:
            unresolvable_details = [
                r.message for r in report.results if r.status == VerifyStatus.UNRESOLVABLE
            ]
            detail_text = "\n".join(f"• {d}" for d in unresolvable_details[:10])
            abort_msg = (
                f"❌ **Remake aborted.** {report.unresolvable} unresolvable entries found.\n"
                f"These must be manually investigated before remake can proceed:\n{detail_text}"
            )
            await self._notify_user(ctx.author.id, ctx.channel.id, abort_msg)
            return

        # Abort on banned — entries were deleted, let the user inspect before continuing
        if report.has_banned:
            banned_details = [
                r.message for r in report.results if r.status == VerifyStatus.BANNED
            ]
            detail_text = "\n".join(f"• {d}" for d in banned_details[:10])
            if len(banned_details) > 10:
                detail_text += f"\n… and {len(banned_details) - 10} more."
            abort_msg = (
                f"⚠️ **Remake paused.** {report.banned} entries were in banned channels and have been deleted.\n"
                f"Review the deletions, then run `/starboard remake` again to continue:\n{detail_text}"
            )
            await self._notify_user(ctx.author.id, ctx.channel.id, abort_msg)
            return

        # Apply verify results (DB updates, in-place tombstone edits)
        await self._apply_verify_results(report)
        await ctx.send(
            f"Verify done: {report.healthy} healthy, {report.flagged} flagged, "
            f"{report.tombstoned} tombstoned, {report.missing_post} missing post, "
            f"{report.banned} banned."
        )

        # ── Phase 2: Delete Discord messages ──
        await ctx.send("**Phase 2/3:** Deleting existing starboard messages...")
        entries = await self.db_manager.get_starboard_entries_ordered(guild.id)
        deleted = await self._delete_starboard_messages(entries, starboard_channel)
        await ctx.send(f"Deleted {deleted} starboard messages. DB entries preserved.")

        # ── Phase 3: Recreate in order ──
        await ctx.send("**Phase 3/3:** Recreating starboard in chronological order...")
        stop_event = asyncio.Event()
        recreate_progress: Dict[str, Any] = {'done': 0, 'total': 0, 'elapsed': 0}
        status_msg = await ctx.send("Recreating... 0/? processed.")
        status_task = asyncio.create_task(
            self._status_editor(status_msg, recreate_progress, stop_event, interval=15.0, operation="remake")
        )

        try:
            recreated, tombstoned, failed_count = await self._recreate_in_order(
                guild.id, starboard_channel, cfg.emoji, cfg.threshold, recreate_progress
            )
        finally:
            stop_event.set()
            try:
                await status_task
            except Exception:
                pass

        await ctx.send(
            f"✅ Starboard remake complete.\n"
            f"Recreated: {recreated}, Tombstones: {tombstoned}, Failed: {failed_count}."
        )

    @starboard_group.command(
        name="remake",
        help="Recreates all starboard posts in chronological order."
    )
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        fast="If True, skips rate limits and thresholds (Dangerous!)."
    )
    async def remake_starboard(self, ctx: commands.Context, fast: bool = False) -> None:  # type: ignore[type-arg]
        """Recreates starboard posts from DB state in chronological order.

        Runs a full verify first. If any entries are unresolvable, aborts and
        notifies the caller. Otherwise deletes existing Discord messages, preserves
        DB rows, and recreates everything in message_created_at order.

        Args:
            fast: Whether to enable fast mode.
        """
        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        if not await self._confirm_fast_mode(ctx, fast):
            return

        lock = self._acquire_guild_lock(ctx.guild.id)
        if lock.locked():
            await ctx.send("Another starboard operation is already running for this guild.")
            return

        async with lock:
            try:
                await self._remake_impl(ctx)
            finally:
                self._fast_mode = False

    # ── Section 7: Crawl Engine ───────────────────────────────────────────────

    async def _scan_channel_for_stars(
        self,
        channel: discord.abc.Messageable,
        starboard_emoji: str,
        threshold: int,
        after: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Scan a channel's history for messages with qualifying star reactions.

        Args:
            channel: The channel to scan.
            starboard_emoji: The emoji to look for.
            threshold: Minimum reaction count.
            after: Only consider messages after this snowflake ID (optional).
            limit: Max messages to scan (optional, None = unbounded).

        Returns:
            List of entry dicts ready for bulk_insert_starboard_entries.
        """
        found: List[Dict[str, Any]] = []
        count = 0
        after_dt = discord.utils.snowflake_time(after) if after else None

        # discord.py's history() already handles pagination internally
        async for message in channel.history(limit=limit, after=after_dt, oldest_first=False):  # type: ignore[arg-type]
            count += 1
            star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)
            if star_reaction and star_reaction.count >= threshold and message.guild:
                found.append({
                    'original_message_id': message.id,
                    'guild_id': message.guild.id,
                    'original_channel_id': message.channel.id,
                    'message_created_at': snowflake_to_unix(message.id),
                    'star_count': star_reaction.count,
                })

        return found

    async def _bounded_catchup_crawl(self, guild_id: int) -> None:
        """Bounded startup crawl — catches stars missed while bot was offline.

        Scans each text channel up to a soft limit of 500 messages. If the bot
        was offline for longer than 500 messages cover, keeps going until reaching
        the last heal timestamp. Posts any newly found entries to the starboard
        channel in chronological order.

        Args:
            guild_id: The guild ID.
        """
        try:
            cfg = await self.get_starboard_config(guild_id)
            if not cfg.enabled or not cfg.channel_id:
                return

            last_heal = cfg.last_heal_at if cfg.last_heal_at else None

            # Only run if bot was offline for >5 minutes (or no heal recorded)
            if last_heal and (int(time.time()) - last_heal) < 300:
                return

            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            starboard_channel = self.bot.get_channel(cfg.channel_id)
            if not isinstance(starboard_channel, discord.TextChannel):
                return

            self.logger.info(f"Starting bounded catch-up crawl for guild {guild_id}.")
            all_found: List[Dict[str, Any]] = []
            SOFT_LIMIT = 500

            for ch in guild.text_channels:
                if ch.id == cfg.channel_id:
                    continue  # Don't scan the starboard channel itself

                try:
                    count = 0
                    exceeded_soft_limit = False
                    async for message in ch.history(limit=None, oldest_first=False):
                        count += 1

                        # Soft limit logging
                        if count == SOFT_LIMIT and not exceeded_soft_limit:
                            # Check if we need to go further
                            if last_heal and snowflake_to_unix(message.id) > last_heal:
                                exceeded_soft_limit = True
                                self.logger.warning(
                                    f"Catch-up crawl for #{ch.name} exceeded {SOFT_LIMIT} messages "
                                    f"(now at {count}), bot was offline since {last_heal}"
                                )
                            else:
                                break  # Message is old enough, we're done

                        if count > SOFT_LIMIT and not exceeded_soft_limit:
                            break

                        # Hard stop: message is older than when we were last online
                        if last_heal and snowflake_to_unix(message.id) < last_heal:
                            break

                        star_reaction = discord.utils.get(message.reactions, emoji=cfg.emoji)
                        if star_reaction and star_reaction.count >= cfg.threshold and message.guild:
                            # Check it's not already tracked
                            existing = await self.db_manager.get_starboard_entry(message.id)
                            if not existing:
                                all_found.append({
                                    'original_message_id': message.id,
                                    'guild_id': message.guild.id,
                                    'original_channel_id': message.channel.id,
                                    'message_created_at': snowflake_to_unix(message.id),
                                    'star_count': star_reaction.count,
                                })

                except (discord.Forbidden, discord.HTTPException) as e:
                    self.logger.debug(f"Cannot scan #{ch.name} during catch-up: {e}")
                    continue

            if not all_found:
                self.logger.info(f"Bounded catch-up crawl for guild {guild_id}: no new entries found.")
                # Update heal timestamp even if nothing found
                await self.db_manager.upsert_starboard_config(guild_id, last_heal_at=int(time.time()))
                return

            # Insert into DB
            inserted = await self.db_manager.bulk_insert_starboard_entries(all_found)
            self.logger.info(f"Bounded catch-up crawl for guild {guild_id}: found {len(all_found)}, inserted {inserted} new entries.")

            # Post unposted entries in chronological order
            unposted = await self.db_manager.get_unposted_starboard_entries(guild_id)
            posted_count = 0
            for entry in unposted:
                original_channel = self.bot.get_channel(entry['original_channel_id'])
                if not isinstance(original_channel, MessageableGuildChannel):
                    continue
                try:
                    message = await original_channel.fetch_message(entry['original_message_id'])
                    star_reaction = discord.utils.get(message.reactions, emoji=cfg.emoji)
                    current_count = star_reaction.count if star_reaction else 0
                    content = f"{cfg.emoji} **{current_count}** in <#{message.channel.id}>"
                    sb_id, reply_id = await self.create_new_starboard_post(message, starboard_channel, content)
                    if sb_id:
                        await self.db_manager.set_starboard_message_id(entry['original_message_id'], sb_id, reply_id)
                        posted_count += 1
                    await asyncio.sleep(1.0)  # Rate limit between posts
                except Exception as e:
                    self.logger.error(f"Failed to post catch-up entry {entry['original_message_id']}: {e}")

            self.logger.info(f"Bounded catch-up crawl complete for guild {guild_id}: posted {posted_count} new entries.")
            await self.db_manager.upsert_starboard_config(guild_id, last_heal_at=int(time.time()))

        except Exception as e:
            self.logger.error(f"Bounded catch-up crawl failed for guild {guild_id}: {e}")

    async def _deep_crawl_task(self, guild_id: int, target_channel_id: Optional[int] = None) -> None:
        """Background task for deep historical crawl.

        Scans the message history of all text channels in a guild (and
        optionally threads), or a single target channel, for qualifying star
        reactions. Populates the DB but does NOT post to the starboard channel.

        Progress is checkpointed every ``CHECKPOINT_INTERVAL`` messages scanned
        and persisted to ``starboard_config`` for resume on restart. Each
        channel is retried up to ``MAX_CHANNEL_RETRIES`` times with exponential
        backoff before being skipped.

        Args:
            guild_id: The guild ID.
            target_channel_id: If provided, only crawl this specific channel
                instead of all text channels in the guild.
        """
        CHECKPOINT_INTERVAL = 500     # Save progress every N messages scanned
        MAX_CHANNEL_RETRIES = 3       # Retries per channel on transient errors
        BASE_RETRY_DELAY = 10.0       # Base delay in seconds for retry backoff
        PAGE_PACE_DELAY = 0.6         # Seconds to sleep per API page (~100 msgs) to stay under rate limits
        LOG_SUMMARY_INTERVAL = 30     # Emit a progress summary every N checkpoints

        # Crawl-wide counters (declared here so finally can always clean up)
        total_found = 0
        total_scanned = 0
        checkpoints_since_log = 0
        rate_limit_count = 0

        # Temporary filter to count 429s from discord.http during this crawl
        discord_http_logger = logging.getLogger('discord.http')

        class _RateLimitCounter(logging.Filter):
            """Counts rate-limit warnings without suppressing them."""

            def filter(self, record: logging.LogRecord) -> bool:
                if 'rate limited' in record.getMessage().lower():
                    nonlocal rate_limit_count
                    rate_limit_count += 1
                return True  # Always pass through

        rl_filter = _RateLimitCounter()
        discord_http_logger.addFilter(rl_filter)

        try:
            cfg = await self.get_starboard_config(guild_id)
            if not cfg.channel_id:
                return

            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            # Determine where to resume from
            resume_channel_id = cfg.crawl_last_channel_id
            resume_message_id = cfg.crawl_last_message_id

            # Build channel list
            if target_channel_id:
                # Single-channel mode
                target_ch = self.bot.get_channel(target_channel_id)
                if not target_ch or not isinstance(target_ch, MessageableGuildChannel):
                    self.logger.error(f"Deep crawl target channel {target_channel_id} not found or not messageable.")
                    return
                channels: List[discord.abc.Messageable] = [target_ch]
            else:
                channels = list(guild.text_channels)
                if cfg.crawl_include_threads:
                    try:
                        channels.extend(list(guild.threads))
                    except Exception as e:
                        self.logger.warning(f"Failed to enumerate threads for crawl: {e}")

                # If resuming, skip channels we've already completed
                if resume_channel_id:
                    skip = True
                    filtered: List[discord.abc.Messageable] = []
                    for ch in channels:
                        if ch.id == resume_channel_id:  # type: ignore[union-attr]
                            skip = False
                        if not skip:
                            filtered.append(ch)
                    channels = filtered or channels  # Fallback to full list if resume channel not found

            self.logger.info(f"Deep crawl started for guild {guild_id}: {len(channels)} channel(s) to scan."
                             + (f" (target: {target_channel_id})" if target_channel_id else ""))

            # Skip the starboard channel itself
            starboard_ch_id = cfg.channel_id

            for ch in channels:
                ch_id = ch.id  # type: ignore[union-attr]
                if ch_id == starboard_ch_id:
                    continue

                ch_name = getattr(ch, 'name', str(ch_id))
                self.logger.info(f"Crawl [{guild_id}]: scanning channel #{ch_name} ({ch_id})")

                # Update crawl state — channel pointer
                await self.db_manager.upsert_starboard_config(guild_id, crawl_last_channel_id=ch_id)

                after_snowflake = None
                if resume_channel_id and ch_id == resume_channel_id and resume_message_id:
                    after_snowflake = resume_message_id
                    resume_message_id = None  # Only use once

                # Retry loop for transient errors on this channel
                batch: List[Dict[str, Any]] = []
                last_good_message_id: Optional[int] = after_snowflake
                for attempt in range(1, MAX_CHANNEL_RETRIES + 1):
                    try:
                        after_dt = discord.utils.snowflake_time(after_snowflake) if after_snowflake else None
                        batch = []
                        scanned_since_checkpoint = 0
                        last_good_message_id = after_snowflake

                        async for message in ch.history(limit=None, after=after_dt, oldest_first=True):  # type: ignore[arg-type]
                            star_reaction = discord.utils.get(message.reactions, emoji=cfg.emoji)
                            if star_reaction and star_reaction.count >= cfg.threshold and message.guild:
                                batch.append({
                                    'original_message_id': message.id,
                                    'guild_id': message.guild.id,
                                    'original_channel_id': message.channel.id,
                                    'message_created_at': snowflake_to_unix(message.id),
                                    'star_count': star_reaction.count,
                                })

                            scanned_since_checkpoint += 1
                            total_scanned += 1

                            # Pace: sleep at page boundaries to stay under rate limits.
                            # discord.py fetches 100 messages per API call, so every
                            # 100 messages scanned means a new HTTP request is imminent.
                            if scanned_since_checkpoint % 100 == 0:
                                await asyncio.sleep(PAGE_PACE_DELAY)

                            if scanned_since_checkpoint >= CHECKPOINT_INTERVAL:
                                # Flush any accumulated batch first
                                if batch:
                                    inserted = await self.db_manager.bulk_insert_starboard_entries(batch)
                                    total_found += inserted
                                    batch.clear()
                                # Save the last good message ID (the one BEFORE this checkpoint window)
                                # so that on crash-resume we don't skip anything
                                await self.db_manager.upsert_starboard_config(
                                    guild_id, crawl_last_message_id=last_good_message_id
                                )
                                last_good_message_id = message.id
                                scanned_since_checkpoint = 0
                                checkpoints_since_log += 1

                                # Emit a batched progress summary periodically
                                if checkpoints_since_log >= LOG_SUMMARY_INTERVAL:
                                    self.logger.info(
                                        f"Crawl progress [{guild_id}]: {total_scanned:,} msgs scanned, "
                                        f"{total_found} stars found, {rate_limit_count} rate-limits, "
                                        f"channel {ch_id}"
                                    )
                                    checkpoints_since_log = 0

                        # Flush remaining batch
                        if batch:
                            inserted = await self.db_manager.bulk_insert_starboard_entries(batch)
                            total_found += inserted

                        # Channel done — save final position
                        if last_good_message_id:
                            await self.db_manager.upsert_starboard_config(
                                guild_id, crawl_last_message_id=last_good_message_id
                            )

                        break  # Channel completed successfully, exit retry loop

                    except discord.Forbidden as e:
                        self.logger.warning(f"Cannot scan channel {ch_id} during deep crawl (no permission): {e}")
                        break  # Permission errors won't be fixed by retrying
                    except asyncio.CancelledError:
                        # Flush progress before exiting on cancellation
                        if batch:
                            inserted = await self.db_manager.bulk_insert_starboard_entries(batch)
                            total_found += inserted
                        if last_good_message_id:
                            await self.db_manager.upsert_starboard_config(
                                guild_id, crawl_last_message_id=last_good_message_id
                            )
                        self.logger.info(f"Deep crawl cancelled for guild {guild_id}. Progress saved.")
                        return
                    except Exception as e:
                        # Transient error — retry with backoff
                        # Flush whatever batch we have so far
                        if batch:
                            try:
                                inserted = await self.db_manager.bulk_insert_starboard_entries(batch)
                                total_found += inserted
                            except Exception:
                                pass  # DB write failed too; will re-scan these on resume
                            batch.clear()
                        # Save checkpoint so resume can pick up here
                        if last_good_message_id:
                            try:
                                await self.db_manager.upsert_starboard_config(
                                    guild_id, crawl_last_message_id=last_good_message_id
                                )
                                # Resume from this position on next attempt
                                after_snowflake = last_good_message_id
                            except Exception:
                                pass

                        if attempt < MAX_CHANNEL_RETRIES:
                            delay = BASE_RETRY_DELAY * (2 ** (attempt - 1))
                            self.logger.warning(
                                f"Transient error scanning channel {ch_id} during deep crawl "
                                f"(attempt {attempt}/{MAX_CHANNEL_RETRIES}): {type(e).__name__}: {e}. "
                                f"Retrying in {delay:.0f}s...",
                                exc_info=True
                            )
                            await asyncio.sleep(delay)
                        else:
                            self.logger.error(
                                f"Channel {ch_id} failed after {MAX_CHANNEL_RETRIES} attempts during "
                                f"deep crawl for guild {guild_id}. Skipping channel.",
                                exc_info=True
                            )

            # Crawl complete — save notify info before clearing state
            self.logger.info(
                f"Deep crawl complete for guild {guild_id}: "
                f"{total_scanned:,} msgs scanned, {total_found} stars found, "
                f"{rate_limit_count} rate-limits encountered."
            )

            requester_id = cfg.crawl_requested_by
            notify_ch_id = cfg.crawl_notify_channel

            # Clear crawl state
            await self.db_manager.upsert_starboard_config(
                guild_id,
                crawl_started_at=None,
                crawl_requested_by=None,
                crawl_notify_channel=None,
                crawl_include_threads=False,
                crawl_last_channel_id=None,
                crawl_last_message_id=None,
            )

            # Notify the requester
            if requester_id:
                await self._notify_user(
                    requester_id, notify_ch_id or 0,
                    f"✅ Starboard crawl complete for guild {guild_id}. "
                    f"Found {total_found} new entries. Run `/starboard remake` to post them."
                )

        except asyncio.CancelledError:
            self.logger.info(f"Deep crawl task cancelled for guild {guild_id}.")
        except Exception as e:
            self.logger.error(
                f"Deep crawl failed for guild {guild_id}: {type(e).__name__}: {e}",
                exc_info=True
            )
            # Flush crawl state so resume can pick up from last checkpoint
            try:
                cfg = await self.get_starboard_config(guild_id)
                requester_id = cfg.crawl_requested_by
                notify_ch_id = cfg.crawl_notify_channel
                # Do NOT clear crawl_started_at — leave it set so resume picks this up
                # crawl_last_channel_id and crawl_last_message_id are already persisted
                if requester_id:
                    await self._notify_user(
                        requester_id, notify_ch_id or 0,
                        f"⚠️ Starboard crawl for guild {guild_id} hit an error and stopped. "
                        f"It will resume automatically on next restart. Error: {type(e).__name__}"
                    )
            except Exception:
                self.logger.error("Failed to notify user about crawl failure.", exc_info=True)
        finally:
            discord_http_logger.removeFilter(rl_filter)

    async def _resume_crawl_if_pending(self, guild_id: int) -> None:
        """Check if a deep crawl was interrupted and resume it.

        Called from cog_ready(). If crawl state exists in starboard_config,
        the crawl resumes from where it left off.

        Args:
            guild_id: The guild ID.
        """
        cfg = await self.get_starboard_config(guild_id)
        if not cfg.crawl_started_at:
            return

        # Let the bot fully settle after startup before resuming.
        # During READY, many cogs fire API calls (command sync, playlist
        # refresh, etc.) which eat into our rate-limit budget.
        RESUME_DELAY = 30
        self.logger.info(
            f"Resuming interrupted deep crawl for guild {guild_id} in {RESUME_DELAY}s."
        )

        # Notify the requester that the crawl is being resumed
        try:
            if cfg.crawl_requested_by:
                await self._notify_user(
                    cfg.crawl_requested_by, cfg.crawl_notify_channel or 0,
                    f"🔄 Starboard crawl for guild {guild_id} is resuming after a restart "
                    f"(starting in {RESUME_DELAY}s)."
                )
        except Exception:
            self.logger.debug("Failed to notify user about crawl resume.", exc_info=True)

        await asyncio.sleep(RESUME_DELAY)

        task = asyncio.create_task(self._deep_crawl_task(guild_id))
        self._crawl_tasks[guild_id] = task

    @starboard_group.command(
        name="crawl",
        help="Deep-crawls server history for missed starboard entries. Owner only."
    )
    @commands.is_owner()
    @app_commands.describe(
        include_threads="Also scan threads (significantly slower). Default: False.",
        channel="Crawl only this specific channel (mention, name, or ID). Omit to crawl the entire server."
    )
    async def crawl_starboard(self, ctx: commands.Context, channel: Optional[str] = None, include_threads: bool = False) -> None:  # type: ignore[type-arg]
        """Starts a deep historical crawl of the server or a specific channel.

        When no channel is specified, scans all text channels (and optionally
        threads) for qualifying star reactions and adds them to the database.
        When a channel is provided, only that channel is crawled.

        Does NOT post to the starboard channel — run ``/starboard remake``
        after the crawl completes.

        The crawl runs in the background and can take hours or days. Progress
        is checkpointed and resumable across bot restarts. Transient errors
        are retried automatically.

        Args:
            channel: Channel to crawl (mention, name, or ID). Omit to crawl
                the entire server.
            include_threads: Whether to also scan threads (server-wide crawl only).
        """
        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        lock = self._acquire_guild_lock(ctx.guild.id)
        if lock.locked():
            await ctx.send("Another starboard operation is already running for this guild.")
            return

        # Check if a crawl is already running
        if ctx.guild.id in self._crawl_tasks and not self._crawl_tasks[ctx.guild.id].done():
            await ctx.send("A crawl is already running for this guild.")
            return

        # Resolve optional channel argument
        target_channel_id: Optional[int] = None
        target_label = "entire server"
        if channel is not None:
            target_channel_id, target_label = await self._resolve_channel_arg(ctx, channel)

        # Persist crawl state
        now = int(time.time())
        await self.db_manager.upsert_starboard_config(
            ctx.guild.id,
            crawl_requested_by=ctx.author.id,
            crawl_started_at=now,
            crawl_include_threads=include_threads,
            crawl_notify_channel=ctx.channel.id,
            crawl_last_channel_id=target_channel_id,
            crawl_last_message_id=None,
        )

        if target_channel_id:
            await ctx.send(
                f"🔍 **Deep crawl started for {target_label}.** This may take a while.\n"
                "Progress is saved — the crawl resumes automatically if the bot restarts.\n"
                "You'll be notified via DM when it's done. Then run `/starboard remake` to post the results."
            )
        else:
            await ctx.send(
                "🔍 **Deep crawl started.** This may take hours or days depending on server size.\n"
                "Progress is saved — the crawl resumes automatically if the bot restarts.\n"
                "You'll be notified via DM when it's done. Then run `/starboard remake` to post the results."
            )

        task = asyncio.create_task(self._deep_crawl_task(ctx.guild.id, target_channel_id=target_channel_id))
        self._crawl_tasks[ctx.guild.id] = task

    # ── Section 8: Reaction Event Handlers ────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Handles raw reaction add events to check for starboard triggers.

        This is the hot path — inline healing happens here (channel drift,
        star count sync, failed_checks reset). Also triggers cold-path
        self-healing if 12+ hours have passed.

        Args:
            payload: The reaction event payload.
        """
        if not payload.guild_id or not self.bot.user or payload.user_id == self.bot.user.id:
            return

        cfg = await self.get_starboard_config(payload.guild_id)
        if not cfg.enabled or not cfg.channel_id or str(payload.emoji) != cfg.emoji:
            return

        # Skip banned channels
        if await self.db_manager.is_starboard_channel_banned(payload.guild_id, payload.channel_id):
            return

        # Trigger cold-path self-heal if due
        if await self._should_self_heal(payload.guild_id):
            await self._trigger_self_heal(payload.guild_id)

        # Per-message lock to prevent double-posting
        if payload.message_id not in self._locks:
            self._locks[payload.message_id] = asyncio.Lock()
        lock = self._locks[payload.message_id]

        try:
            async with lock:
                channel = self.bot.get_channel(payload.channel_id)
                if not isinstance(channel, MessageableGuildChannel) or channel.id == cfg.channel_id:
                    return

                try:
                    message = await channel.fetch_message(payload.message_id)
                except discord.NotFound:
                    self.logger.debug(f"Starboard: Message {payload.message_id} not found (deleted).")
                    return

                star_reaction = discord.utils.get(message.reactions, emoji=cfg.emoji)
                if not star_reaction:
                    return

                if star_reaction.count >= cfg.threshold:
                    await self.post_to_starboard(message, cfg.channel_id, cfg.emoji, star_reaction.count)
        finally:
            if not lock.locked():
                self._locks.pop(payload.message_id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Handles raw reaction remove events.

        Updates the star count on the starboard message. If reactions drop
        below threshold, deletes the starboard message and its reply context,
        and removes the DB entry.

        Args:
            payload: The reaction event payload.
        """
        if not payload.guild_id:
            return

        cfg = await self.get_starboard_config(payload.guild_id)
        if not cfg.enabled or not cfg.channel_id or str(payload.emoji) != cfg.emoji:
            return

        existing_entry = await self.db_manager.get_starboard_entry(payload.message_id)
        if not existing_entry:
            return

        starboard_channel = self.bot.get_channel(cfg.channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, MessageableGuildChannel):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
            star_reaction = discord.utils.get(message.reactions, emoji=cfg.emoji)
            star_count = star_reaction.count if star_reaction else 0

            # Sync star count to DB
            await self.db_manager.update_starboard_star_count(message.id, star_count)

            sb_msg_id = existing_entry.get('starboard_message_id')
            if not sb_msg_id:
                return  # No starboard message to update/delete

            starboard_message = await starboard_channel.fetch_message(sb_msg_id)

            if star_count < cfg.threshold:
                # Delete starboard message + reply context
                await starboard_message.delete()
                reply_id = existing_entry.get('starboard_reply_id')
                if reply_id:
                    try:
                        reply_msg = await starboard_channel.fetch_message(reply_id)
                        await reply_msg.delete()
                    except discord.NotFound:
                        pass

                await self.db_manager.remove_starboard_entry(message.id)
                self.logger.info(f"Removed starboard entry for {message.id} (below threshold).")
            else:
                content = f"{cfg.emoji} **{star_count}** in <#{message.channel.id}>"
                await starboard_message.edit(content=content)

        except discord.NotFound:
            # Original or starboard message deleted
            await self.db_manager.remove_starboard_entry(payload.message_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Module Setup
# ═══════════════════════════════════════════════════════════════════════════════


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot: The bot instance.
    """
    await bot.add_cog(Starboard(bot))

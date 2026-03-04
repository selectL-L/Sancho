"""cogs/admin.py

This cog contains owner/admin-only commands for administrative tasks, such as
viewing bot status and managing configurations.
"""

import asyncio
import os
import platform
import shutil
import sys
import tempfile
import time
import typing
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.extensions import discover_cogs
from utils.musicutils import (
    MUTAGEN_AVAILABLE,
    YTDLP_AVAILABLE,
    MusicCacheManager,
    get_youtube_auth_status,
)
from utils.musicutils.music_auth import _detect_youtube_auth
from utils.views import get_selection, show_dashboard, show_status, StatusData, StatusHealth

if TYPE_CHECKING:
    from cogs.limbus import Limbus


class AdminCog(BaseCog):
    """Administrative and owner-only commands."""

    def __init__(self, bot: CoreBot):
        """Initializes the AdminCog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager = bot.db_manager

    @commands.hybrid_command(
        name="global_limit",
        hidden=True,
        description="Set the global skill limit for all users.",
        help="Set the global skill limit for all users."
    )
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        limit="The new global skill limit (1-100)."
    )
    async def global_limit(self, ctx: commands.Context, limit: int) -> None:
        """Set the global skill limit for all users.

        Args:
            ctx (commands.Context): The command context.
            limit (int): The new global skill limit (1-100).
        """
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        await self.db_manager.set_skill_limit(limit)
        await ctx.send(f"✅ The global skill limit has been updated to **{limit}** per user.")
        self.logger.warning(f"Admin {ctx.author} set global skill limit to {limit}.")

    @commands.hybrid_command(
        name="user_limit",
        hidden=True,
        description="Set the skill limit for a specific user.",
        help="Set the skill limit for a specific user."
    )
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        user="The user to set the limit for. (this can be a mention or an ID)",
        limit="The new skill limit for the user (1-100)."
    )
    async def user_limit(self, ctx: commands.Context, user: discord.Member, limit: int) -> None:
        """Set the skill limit for a specific user.

        Args:
            ctx (commands.Context): The command context.
            user (discord.Member): The user to set the limit for.
            limit (int): The new skill limit for the user (1-100).
        """
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        await self.db_manager.set_user_skill_limit(user.id, limit)
        await ctx.send(f"✅ {user.mention}'s skill limit has been updated to **{limit}**.")
        self.logger.warning(f"Admin {ctx.author} set skill limit for {user} ({user.id}) to {limit}.")

    @commands.hybrid_command(
        name="report",
        hidden=True,
        description="Opens the Admin Dashboard. Use this to find IDs for the edit_entry command.",
        help="Opens the Admin Dashboard. Use this to find IDs for the edit_entry command."
    )
    @commands.is_owner()
    async def report(self, ctx: commands.Context) -> None:
        """Opens the Admin Dashboard.

        Allows viewing skills and reminders in a paginated interface,
        exporting data to a file, and finding IDs for the `edit_entry` command.
        """
        await ctx.send("`Loading dashboard...`")

        try:
            all_skills = await self.db_manager.get_all_skills()
            all_reminders = await self.db_manager.get_all_reminders()

            # --- Helper Functions ---
            def get_user_display(user_id: int) -> str:
                """Get display name for a user, falling back to ID if not cached."""
                user = self.bot.get_user(user_id)
                if user:
                    return f"{user.name} ({user_id})"
                return f"User {user_id}"

            def wrap_text(text: str, max_width: int = 60) -> str:
                """Wrap text to specified width, preserving word boundaries."""
                if len(text) <= max_width:
                    return text
                words = text.split()
                lines = []
                current_line = []
                current_length = 0
                for word in words:
                    if current_length + len(word) + 1 <= max_width:
                        current_line.append(word)
                        current_length += len(word) + 1
                    else:
                        if current_line:
                            lines.append(' '.join(current_line))
                        current_line = [word]
                        current_length = len(word)
                if current_line:
                    lines.append(' '.join(current_line))
                return '\n'.join(lines)

            # 1. Generate Skill Pages (grouped by user)
            skill_pages = []
            if all_skills:
                # Group skills by user
                skills_by_user: dict[int, list] = {}
                for skill in all_skills:
                    uid = skill['user_id']
                    if uid not in skills_by_user:
                        skills_by_user[uid] = []
                    skills_by_user[uid].append(skill)

                # Build pages with clear user sections
                current_embed = discord.Embed(
                    title="Database: Skills", color=discord.Color.blue())
                field_count = 0
                page_num = 1

                for user_id, user_skills in skills_by_user.items():
                    user_display = get_user_display(user_id)

                    # Add user header
                    if field_count >= 6:  # Start new page if near limit
                        current_embed.set_footer(
                            text=f"Page {page_num} | Total Skills: {len(all_skills)}")
                        skill_pages.append(current_embed)
                        current_embed = discord.Embed(
                            title="Database: Skills", color=discord.Color.blue())
                        field_count = 0
                        page_num += 1

                    # User header separator
                    current_embed.add_field(
                        name=f"━━━ {user_display} ━━━",
                        value=f"*{len(user_skills)} skill(s)*",
                        inline=False
                    )
                    field_count += 1

                    for skill in user_skills:
                        if field_count >= 8:  # Max fields before new page
                            current_embed.set_footer(
                                text=f"Page {page_num} | Total Skills: {len(all_skills)}")
                            skill_pages.append(current_embed)
                            current_embed = discord.Embed(
                                title="Database: Skills", color=discord.Color.blue())
                            field_count = 0
                            page_num += 1
                            # Re-add user header on continued page
                            current_embed.add_field(
                                name=f"━━━ {user_display} (cont.) ━━━",
                                value="",
                                inline=False
                            )
                            field_count += 1

                        desc = skill.get('description') or 'No description'
                        desc_wrapped = wrap_text(desc, 50)
                        current_embed.add_field(
                            name=f"`ID:{skill['id']}` {skill['name']}",
                            value=f"**Roll:** `{skill['dice_roll']}`\n**Type:** {skill['skill_type']}\n{desc_wrapped}",
                            inline=False
                        )
                        field_count += 1

                # Add final page
                if field_count > 0:
                    current_embed.set_footer(
                        text=f"Page {page_num} | Total Skills: {len(all_skills)}")
                    skill_pages.append(current_embed)

                # Update page numbers in footers
                for i, embed in enumerate(skill_pages):
                    embed.set_footer(
                        text=f"Page {i+1}/{len(skill_pages)} | Total Skills: {len(all_skills)}")

            # 2. Generate Reminder Pages (grouped by user with better formatting)
            reminder_pages = []
            if all_reminders:
                # Group reminders by user
                reminders_by_user: dict[int, list] = {}
                for rem in all_reminders:
                    uid = rem['user_id']
                    if uid not in reminders_by_user:
                        reminders_by_user[uid] = []
                    reminders_by_user[uid].append(rem)

                # Build pages with clear user sections
                current_embed = discord.Embed(
                    title="Database: Reminders", color=discord.Color.orange())
                field_count = 0
                page_num = 1

                for user_id, user_reminders in reminders_by_user.items():
                    user_display = get_user_display(user_id)

                    # Add user header
                    # Start new page if near limit (fewer per page for reminders)
                    if field_count >= 5:
                        current_embed.set_footer(
                            text=f"Page {page_num} | Total Reminders: {len(all_reminders)}")
                        reminder_pages.append(current_embed)
                        current_embed = discord.Embed(
                            title="Database: Reminders", color=discord.Color.orange())
                        field_count = 0
                        page_num += 1

                    # User header separator
                    current_embed.add_field(
                        name=f"━━━ {user_display} ━━━",
                        value=f"*{len(user_reminders)} reminder(s)*",
                        inline=False
                    )
                    field_count += 1

                    for rem in user_reminders:
                        if field_count >= 6:  # Fewer fields per page for readability
                            current_embed.set_footer(
                                text=f"Page {page_num} | Total Reminders: {len(all_reminders)}")
                            reminder_pages.append(current_embed)
                            current_embed = discord.Embed(
                                title="Database: Reminders", color=discord.Color.orange())
                            field_count = 0
                            page_num += 1
                            # Re-add user header on continued page
                            current_embed.add_field(
                                name=f"━━━ {user_display} (cont.) ━━━",
                                value="",
                                inline=False
                            )
                            field_count += 1

                        # Format message with word wrap
                        message_wrapped = wrap_text(rem['message'], 70)

                        # Build timing info
                        recurring_str = ""
                        if rem.get('is_recurring') and rem.get('recurrence_rule'):
                            recurring_str = f"\n🔁 **Recurring:** `{rem['recurrence_rule']}`"

                        current_embed.add_field(
                            name=f"`ID:{rem['id']}` Due: <t:{rem['reminder_time']}:f> (<t:{rem['reminder_time']}:R>)",
                            value=f"**Message:**\n{message_wrapped}{recurring_str}",
                            inline=False
                        )
                        field_count += 1

                # Add final page
                if field_count > 0:
                    current_embed.set_footer(
                        text=f"Page {page_num} | Total Reminders: {len(all_reminders)}")
                    reminder_pages.append(current_embed)

                # Update page numbers in footers
                for i, embed in enumerate(reminder_pages):
                    embed.set_footer(
                        text=f"Page {i+1}/{len(reminder_pages)} | Total Reminders: {len(all_reminders)}")

            # 3. Define Export Callback (human-readable report)
            async def export_callback(interaction_ctx):
                assert config.BOT_NAME is not None
                report_lines = [f"--- {config.BOT_NAME.upper()} DATABASE REPORT ---",
                                f"Generated: {discord.utils.utcnow()}", ""]

                report_lines.append(f"\n--- SKILLS ({len(all_skills)}) ---")
                for s in all_skills:
                    report_lines.append(
                        f"ID: {s['id']} | User: {s['user_id']} | Name: {s['name']} | Roll: {s['dice_roll']} | Type: {s['skill_type']}")

                report_lines.append(
                    f"\n--- REMINDERS ({len(all_reminders)}) ---")
                for r in all_reminders:
                    report_lines.append(
                        f"ID: {r['id']} | User: {r['user_id']} | Time: {r['reminder_time']} | Msg: {r['message']}")

                with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", suffix="_db_report.txt") as f:
                    f.write("\n".join(report_lines))
                    temp_path = f.name

                try:
                    await interaction_ctx.send("Database report attached:", file=discord.File(temp_path, filename="db_report.txt"))
                finally:
                    os.remove(temp_path)

            # 4. Create Dashboard Embed
            dashboard_embed = discord.Embed(
                title="Admin Dashboard",
                description="Select a category to view database entries.\n\n"
                            "**💾 Export to File** - Human-readable text report",
                color=discord.Color.dark_grey()
            )
            dashboard_embed.add_field(
                name="Stats", value=f"Skills: **{len(all_skills)}**\nReminders: **{len(all_reminders)}**")

            # 5. Launch Dashboard
            await show_dashboard(
                ctx=ctx,
                skill_pages=skill_pages,
                reminder_pages=reminder_pages,
                report_file_callback=export_callback,
                dashboard_embed=dashboard_embed
            )

        except Exception as e:
            self.logger.error(f"Error generating dashboard: {e}", exc_info=True)
            await ctx.send(f"An error occurred: {e}")

    @commands.hybrid_command(
        name="edit_entry",
        hidden=True,
        description="Edit a database entry by ID. Use the report command to find IDs.",
        help="Edit a database entry by ID. Use the report command to find IDs."
    )
    @commands.is_owner()
    @app_commands.describe(
        entry_type="The type of entry ('skill' or 'reminder').",
        entry_id="The numeric ID of the entry."
    )
    async def edit_entry(self, ctx: commands.Context, entry_type: str, entry_id: int) -> None:
        """Edit a database entry by ID.

        Use the `report` command to find the IDs of skills and reminders.

        Args:
            ctx (commands.Context): The command context.
            entry_type (str): 'skill' or 'reminder'.
            entry_id (int): The ID of the entry.
        """
        entry_type = entry_type.lower()
        if entry_type not in ['skill', 'reminder']:
            await ctx.send("Invalid type. Please use `skill` or `reminder`.")
            return

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            if entry_type == 'skill':
                # --- EDIT SKILL ---
                skill = await self.db_manager.get_skill_by_id(entry_id)
                if not skill:
                    await ctx.send(f"No skill found with ID {entry_id}.")
                    return

                embed = discord.Embed(
                    title=f"Edit Skill #{entry_id}", color=discord.Color.blue())
                embed.add_field(
                    name="1. Name", value=skill['name'], inline=False)
                embed.add_field(
                    name="2. Roll", value=skill['dice_roll'], inline=False)
                embed.add_field(
                    name="3. Description", value=skill['description'] or "None", inline=False)
                embed.add_field(name="4. DELETE",
                                value="⚠️ Delete this skill", inline=False)
                embed.set_footer(text="Select an option to edit.")

                options = {"1️⃣ Name": "1", "2️⃣ Roll": "2",
                           "3️⃣ Desc": "3", "🗑️ Delete": "4"}
                choice = await get_selection(ctx, embed, options)

                if not choice:
                    await ctx.send("Edit cancelled.")
                    return

                updates = {}
                if choice == '1':
                    await ctx.send(f"Current Name: `{skill['name']}`. Enter new name:")
                    msg = await self.bot.wait_for('message', check=check, timeout=30)
                    updates['name'] = msg.content.strip()
                elif choice == '2':
                    await ctx.send(f"Current Roll: `{skill['dice_roll']}`. Enter new roll:")
                    msg = await self.bot.wait_for('message', check=check, timeout=30)
                    updates['dice_roll'] = msg.content.strip()
                elif choice == '3':
                    await ctx.send(f"Current Desc: `{skill['description']}`. Enter new description (or 'none'):")
                    msg = await self.bot.wait_for('message', check=check, timeout=60)
                    content = msg.content.strip()
                    updates['description'] = None if content.lower(
                    ) == 'none' else content
                elif choice == '4':
                    await ctx.send("Are you sure you want to DELETE this skill? (yes/no)")
                    msg = await self.bot.wait_for('message', check=check, timeout=30)
                    if msg.content.lower() in ['yes', 'y']:
                        await self.db_manager.delete_skill(skill['user_id'], entry_id)
                        await ctx.send("✅ Skill deleted.")
                        self.logger.warning(f"Admin {ctx.author} deleted skill #{entry_id} (user={skill['user_id']}, name={skill['name']}).")
                        return
                    else:
                        await ctx.send("Deletion cancelled.")
                        return

                if updates:
                    await self.db_manager.update_skill(entry_id, skill['user_id'], updates)
                    await ctx.send("✅ Skill updated.")
                    self.logger.warning(f"Admin {ctx.author} updated skill #{entry_id}: {updates}")

            elif entry_type == 'reminder':
                # --- EDIT REMINDER ---
                rem = await self.db_manager.get_reminder_by_id(entry_id)
                if not rem:
                    await ctx.send(f"No reminder found with ID {entry_id}.")
                    return

                embed = discord.Embed(
                    title=f"Edit Reminder #{entry_id}", color=discord.Color.orange())
                embed.add_field(name="1. Message",
                                value=rem['message'], inline=False)
                embed.add_field(
                    name="2. Time", value=f"<t:{rem['reminder_time']}:F>", inline=False)
                embed.add_field(name="3. DELETE",
                                value="⚠️ Delete this reminder", inline=False)
                embed.set_footer(text="Select an option to edit.")

                options = {"1️⃣ Message": "1",
                           "2️⃣ Time": "2", "🗑️ Delete": "3"}
                choice = await get_selection(ctx, embed, options)

                if not choice:
                    await ctx.send("Edit cancelled.")
                    return

                updates = {}
                if choice == '1':
                    await ctx.send(f"Current Message: `{rem['message']}`. Enter new message:")
                    msg = await self.bot.wait_for('message', check=check, timeout=60)
                    updates['message'] = msg.content.strip()
                elif choice == '2':
                    await ctx.send("Enter new time (e.g. 'in 5 mins', 'tomorrow 2pm'):")
                    msg = await self.bot.wait_for('message', check=check, timeout=60)
                    # Note: We'd ideally use the Reminders cog's parser here, but for admin override,
                    # we can just use dateparser directly or ask them to be precise.
                    # For simplicity in this admin tool, we'll assume they know what they are doing or use a simple parser.
                    import dateparser
                    from typing import cast, Any as TypingAny
                    dt = await asyncio.to_thread(
                        dateparser.parse, msg.content.strip(),
                        languages=['en'], settings=cast(TypingAny, {'PREFER_DATES_FROM': 'future'})
                    )
                    if dt:
                        updates['reminder_time'] = int(dt.timestamp())
                    else:
                        await ctx.send("Invalid time format. Cancelled.")
                        return
                elif choice == '3':
                    await ctx.send("Are you sure you want to DELETE this reminder? (yes/no)")
                    msg = await self.bot.wait_for('message', check=check, timeout=30)
                    if msg.content.lower() in ['yes', 'y']:
                        await self.db_manager.delete_reminders([entry_id])
                        await ctx.send("✅ Reminder deleted.")
                        self.logger.warning(f"Admin {ctx.author} deleted reminder #{entry_id} (user={rem['user_id']}).")
                        return
                    else:
                        await ctx.send("Deletion cancelled.")
                        return

                if updates:
                    await self.db_manager.update_reminder(entry_id, rem['user_id'], updates)
                    await ctx.send("✅ Reminder updated.")
                    self.logger.warning(f"Admin {ctx.author} updated reminder #{entry_id}: {updates}")

        except Exception as e:
            self.logger.error(
                f"Error editing {entry_type} #{entry_id}: {e}", exc_info=True)
            await ctx.send(f"An error occurred: {e}")

    @commands.hybrid_command(
        name="mood",
        hidden=True,
        description="View or change the bot's current mood.",
        help="View or change the bot's current mood. Use without arguments to see available moods."
    )
    @commands.is_owner()
    @app_commands.describe(mood_name="The mood to switch to (optional). Leave empty to see available moods.")
    async def mood(self, ctx: commands.Context, mood_name: typing.Optional[str] = None) -> None:
        """View or change the bot's current mood.

        When called without arguments, displays the current mood and a list
        of available moods to choose from.

        Args:
            ctx (commands.Context): The command context.
            mood_name (typing.Optional[str]): The mood to switch to.
        """
        from utils import ambience

        # Warn if ambience user-facing output is disabled
        ambience_warning = ""
        if not ambience.is_enabled():
            ambience_warning = (
                "⚠️ **Ambience is currently disabled** (`AMBIENCE_ENABLED=false` in info.env).\n"
                "Moods still cycle internally and affect playlist selection, but user-facing strings are silenced.\n"
                "A bot restart is required to change this setting.\n\n"
            )

        current_mood_id = ambience.get_current_mood_id()
        current_activity = ambience.get_current_activity()
        available_moods = list(ambience.MOODS.keys())

        # If no mood specified, show current state and available options
        if mood_name is None:
            embed = discord.Embed(
                title="🎭 Mood Management",
                description=ambience_warning if ambience_warning else None,
                color=discord.Color.purple()
            )

            # Current state
            activity_status = current_activity.status if current_activity else "None"
            embed.add_field(
                name="Current State",
                value=(
                    f"**Mood:** `{current_mood_id}`\n"
                    f"**Activity:** `{current_activity.id if current_activity else 'None'}`\n"
                    f"**Status:** {activity_status}"
                ),
                inline=False
            )

            # Available moods
            mood_list = "\n".join(f"• `{mood}`" for mood in available_moods)
            embed.add_field(
                name="Available Moods",
                value=mood_list,
                inline=False
            )

            embed.add_field(
                name="Usage",
                value=(
                    f"`{ctx.prefix}mood <mood_name>` - Switch to a specific mood\n"
                    f"Example: `{ctx.prefix}mood productive`"
                ),
                inline=False
            )

            await ctx.send(embed=embed)
            return

        # Normalize input
        mood_name = mood_name.lower().strip()

        # Check if valid mood
        if mood_name not in available_moods:
            await ctx.send(
                f"❌ Unknown mood `{mood_name}`.\n"
                f"Available moods: {', '.join(f'`{m}`' for m in available_moods)}"
            )
            return

        # Set the mood
        success = ambience.set_mood(mood_name)
        if success:
            new_activity = ambience.get_current_activity()
            activity_info = f" (now: {new_activity.status})" if new_activity else ""
            await ctx.send(f"✅ Mood changed to `{mood_name}`{activity_info}")
            self.logger.warning(f"Admin {ctx.author} changed mood to {mood_name}.")
        else:
            await ctx.send(f"❌ Failed to set mood to `{mood_name}`.")

    @commands.hybrid_command(
        name="status",
        hidden=True,
        description="Provides a comprehensive health and status check for the bot.",
        help="Provides a comprehensive health and status check for the bot."
    )
    @commands.is_owner()
    @app_commands.describe(mode="Optional: 'history' to view historical resource usage.")
    async def status(self, ctx: commands.Context, mode: typing.Optional[str] = None) -> None:
        """Provides a comprehensive health and status check for the bot.

        Displays an interactive paginated view with:
        - Overview: Health checks, quick stats, current activity
        - Performance: Latencies, resource usage, uptime
        - Music: Auth status, playback info
        - Storage: Cache and database stats
        - System: Extensions, environment, bot identity

        Usage: .status [history]

        Args:
            ctx (commands.Context): The command context.
            mode (typing.Optional[str]): 'history' to view historical resource usage.
        """
        # Get resource tracker from bot
        resource_tracker = getattr(self.bot, 'resource_tracker', None)

        if mode and mode.lower() == "history":
            if not resource_tracker:
                await ctx.send("Resource tracker is not available.")
                return

            history = resource_tracker.get_history()
            if not history:
                await ctx.send("No historical data recorded yet (updates every 15 mins).")
                return

            # Format history for export
            history_text = resource_tracker.format_history_for_export()

            with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", suffix="_usage_history.txt") as f:
                f.write(history_text)
                temp_path = f.name

            await ctx.send("Historical resource usage attached:", file=discord.File(temp_path, filename="usage_history.txt"))
            os.remove(temp_path)
            return

        # Send initial message
        message = await ctx.send("📊 Gathering status data...")

        # Gather all data for status view
        data = await self._gather_status_data(ctx, message)

        # Create refresh callback for the view
        async def refresh_callback() -> StatusData:
            return await self._gather_status_data(ctx)

        # Delete the loading message and show the status view
        await message.delete()
        await show_status(ctx, data, refresh_callback=refresh_callback)

    async def _gather_status_data(self, ctx: commands.Context, message: Optional[discord.Message] = None) -> StatusData:
        """Gather all data needed for the status view.

        Args:
            ctx: The command context.
            message: Optional loading message (used for roundtrip timing). If None, roundtrip is skipped.

        Returns:
            StatusData populated with all status information.
        """
        now = time.time()
        snapshot_timestamp = int(now)

        # =====================================================================
        # PERFORMANCE METRICS
        # =====================================================================

        # Latencies
        gateway_latency = self.bot.latency * 1000
        db_latency = await self.db_manager.ping() if self.db_manager else -1

        # Roundtrip only if we have a message to edit
        if message:
            start_time = time.monotonic()
            await message.edit(content="📊 Measuring latencies...")
            end_time = time.monotonic()
            roundtrip_latency = (end_time - start_time) * 1000
        else:
            roundtrip_latency = -1.0  # Not measured

        # Resource usage
        resource_tracker = getattr(self.bot, 'resource_tracker', None)
        if resource_tracker:
            usage = await resource_tracker.get_current_usage_async()
            cpu_percent = usage['cpu']
            ram_mb = usage['ram']
            ram_private_mb = usage['ram_private']
            ram_swap_mb = usage['ram_swap']
        else:
            cpu_percent = 0.0
            ram_mb = 0.0
            ram_private_mb = 0.0
            ram_swap_mb = 0.0

        # =====================================================================
        # UPTIME
        # =====================================================================

        start_timestamp = int(self.bot.start_time)
        uptime_delta = timedelta(seconds=now - self.bot.start_time)
        days, remainder = divmod(uptime_delta.total_seconds(), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"

        # =====================================================================
        # EXTENSIONS
        # =====================================================================

        loaded_extensions = list(self.bot.extensions.keys())
        loaded_cogs = [ext.replace('cogs.', '') for ext in loaded_extensions]
        total_cogs = len(discover_cogs(config.COGS_PATH))

        # Determine failed cogs by comparing discovered vs loaded
        # discover_cogs returns module names like 'cogs.admin', so strip the prefix
        discovered_cog_names = [mod.replace('cogs.', '') for mod in discover_cogs(config.COGS_PATH)]
        failed_cogs = [name for name in discovered_cog_names if name not in loaded_cogs]

        # =====================================================================
        # MUSIC AUTH STATUS
        # =====================================================================

        # Force refresh POT server status before reading
        # _detect_youtube_auth is sync (socket.connect_ex with 0.5s timeout)
        await asyncio.to_thread(_detect_youtube_auth)
        auth_status = get_youtube_auth_status()
        auth_method = auth_status.auth_method
        pot_server_running = auth_status.pot_server_running
        pot_plugin_error = auth_status.pot_plugin_error

        # Cookie age
        cookie_age_days: Optional[int] = None
        cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
        if cookie_path and await asyncio.to_thread(os.path.isfile, cookie_path):
            file_age = now - os.path.getmtime(cookie_path)
            cookie_age_days = int(file_age / 86400)

        # 403 error rate
        error_403_count, error_403_window = auth_status.get_403_rate()
        error_403_window_mins = int(error_403_window / 60)

        # =====================================================================
        # MUSIC PLAYBACK STATUS
        # =====================================================================

        music_cog = self.bot.get_cog('Music')
        music_status = "idle"
        music_channel: Optional[str] = None
        music_track: Optional[str] = None
        music_artist: Optional[str] = None
        music_queue_count = 0

        if music_cog:
            active_session = getattr(music_cog, 'active_session', None)
            if active_session:
                vc = getattr(active_session, 'voice_client', None)
                if vc and vc.channel:
                    music_channel = vc.channel.name
                    if vc.is_playing():
                        music_status = "playing"
                    elif vc.is_paused():
                        music_status = "paused"
                    else:
                        music_status = "connected"

            # Get current track info
            current_track = getattr(music_cog, '_get_current_track', lambda: None)()
            if current_track:
                music_track = getattr(current_track, 'title', None)
                music_artist = getattr(current_track, 'artist', None)

            # Get queue count
            playlist = getattr(music_cog, 'playlist', [])
            music_queue_count = len(playlist)

        # =====================================================================
        # CACHE STATISTICS
        # =====================================================================

        cache_manager = self._get_music_cache_manager()
        cache_playlists = 0
        cache_tracks_total = 0
        cache_tracks_downloaded = 0
        cache_size_mb = 0.0
        cache_orphaned_count = 0
        cache_orphaned_mb = 0.0
        cache_residential_count = 0
        cache_residential_mb = 0.0
        cache_residential_cost = 0.0
        cache_last_refresh_ago: Optional[float] = None
        cache_ytm_resolved = 0
        cache_ytdlp_fallback = 0
        cache_unresolved = 0
        cache_filenames_modified = 0
        cache_pending_downloads = 0

        if cache_manager:
            stats = cache_manager.get_stats()
            cache_playlists = stats.get('total_playlists', 0)
            cache_tracks_total = stats.get('total_tracks', 0)
            cache_tracks_downloaded = stats.get('downloaded_tracks', 0)
            cache_size_mb = stats.get('size_mb', 0.0)
            cache_orphaned_count = stats.get('orphaned_tracks', 0)
            cache_orphaned_mb = stats.get('orphaned_size_mb', 0.0)
            cache_last_refresh_ago = stats.get('last_refresh_ago')
            cache_ytm_resolved = stats.get('ytm_resolved_count', 0)
            cache_ytdlp_fallback = stats.get('ytdlp_fallback_count', 0)
            cache_unresolved = stats.get('unresolved_count', 0)
            cache_filenames_modified = stats.get('filenames_modified_count', 0)
            cache_pending_downloads = stats.get('pending_download_count', 0)

            residential_stats = cache_manager.get_residential_stats()
            cache_residential_count = residential_stats.get('file_count', 0)
            cache_residential_mb = residential_stats.get('size_mb', 0.0)
            cache_residential_cost = residential_stats.get('estimated_cost', 0.0)

        # =====================================================================
        # DATABASE
        # =====================================================================

        db_size_mb = 0.0
        if self.db_manager and hasattr(self.db_manager, 'db_path'):
            db_path = self.db_manager.db_path
            if os.path.isfile(db_path):
                db_size_mb = os.path.getsize(db_path) / (1024 * 1024)

        # =====================================================================
        # NEXT REMINDER
        # =====================================================================

        next_reminder_time: Optional[int] = None
        next_reminder_user: Optional[str] = None

        if self.db_manager:
            next_reminder = await self.db_manager.get_next_upcoming_reminder(int(now))
            if next_reminder:
                next_reminder_time = next_reminder.get('reminder_time')
                user_id = next_reminder.get('user_id')
                if user_id:
                    user = self.bot.get_user(user_id)
                    next_reminder_user = user.display_name if user else f"User {user_id}"

        # =====================================================================
        # SYSTEM INFO
        # =====================================================================

        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        discordpy_version = discord.__version__
        platform_str = platform.system()
        bot_name = config.BOT_NAME or "Unknown"
        bot_id = self.bot.user.id if self.bot.user else 0
        guild_count = len(self.bot.guilds)

        # =====================================================================
        # HEALTH CHECKS
        # =====================================================================

        health = StatusHealth(
            gateway=gateway_latency < 500,
            database=db_latency >= 0,
            music_auth=auth_method is not None,
            cache=cache_manager is not None,
            extensions=len(failed_cogs) == 0,
            ytdlp=YTDLP_AVAILABLE,
        )

        # =====================================================================
        # BUILD STATUS DATA
        # =====================================================================

        return StatusData(
            # Performance
            gateway_latency=gateway_latency,
            roundtrip_latency=roundtrip_latency,
            db_latency=db_latency,
            cpu_percent=cpu_percent,
            ram_mb=ram_mb,
            ram_private_mb=ram_private_mb,
            ram_swap_mb=ram_swap_mb,
            # Uptime
            start_timestamp=start_timestamp,
            uptime_str=uptime_str,
            # Extensions
            loaded_cogs=loaded_cogs,
            total_cogs=total_cogs,
            failed_cogs=failed_cogs,
            # Health
            health=health,
            # Music Auth
            auth_method=auth_method,
            pot_server_running=pot_server_running,
            pot_plugin_error=pot_plugin_error,
            cookie_age_days=cookie_age_days,
            error_403_count=error_403_count,
            error_403_window_mins=error_403_window_mins,
            ytdlp_available=YTDLP_AVAILABLE,
            mutagen_available=MUTAGEN_AVAILABLE,
            # Music Playback
            music_status=music_status,
            music_channel=music_channel,
            music_track=music_track,
            music_artist=music_artist,
            music_queue_count=music_queue_count,
            # Cache
            cache_playlists=cache_playlists,
            cache_tracks_total=cache_tracks_total,
            cache_tracks_downloaded=cache_tracks_downloaded,
            cache_size_mb=cache_size_mb,
            cache_orphaned_count=cache_orphaned_count,
            cache_orphaned_mb=cache_orphaned_mb,
            cache_residential_count=cache_residential_count,
            cache_residential_mb=cache_residential_mb,
            cache_residential_cost=cache_residential_cost,
            cache_last_refresh_ago=cache_last_refresh_ago,
            # Cache Provenance
            cache_ytm_resolved=cache_ytm_resolved,
            cache_ytdlp_fallback=cache_ytdlp_fallback,
            cache_unresolved=cache_unresolved,
            cache_filenames_modified=cache_filenames_modified,
            cache_pending_downloads=cache_pending_downloads,
            # Database
            db_size_mb=db_size_mb,
            # Next Reminder
            next_reminder_time=next_reminder_time,
            next_reminder_user=next_reminder_user,
            # System
            python_version=python_version,
            discordpy_version=discordpy_version,
            platform=platform_str,
            bot_name=bot_name,
            bot_id=bot_id,
            guild_count=guild_count,
            # Snapshot
            snapshot_timestamp=snapshot_timestamp,
        )

    # ==========================================================================
    # MUSIC CACHE MANAGEMENT COMMANDS
    # ==========================================================================

    def _get_music_cache_manager(self) -> Optional[MusicCacheManager]:
        """Gets the MusicCacheManager from the Music cog.

        Returns:
            MusicCacheManager instance, or None if Music cog not loaded.
        """
        music_cog = self.bot.get_cog('Music')
        if music_cog and hasattr(music_cog, 'cache_manager'):
            return typing.cast(MusicCacheManager, music_cog.cache_manager)  # type: ignore[attr-defined]
        return None

    @commands.hybrid_command(
        name="clear-orphaned",
        hidden=True,
        description="Clears all orphaned music files.",
        help="Removes tracks that are no longer in any playlist."
    )
    @commands.is_owner()
    async def clear_orphaned(self, ctx: commands.Context) -> None:
        """Clears all orphaned music files.

        Orphaned files are tracks that were removed from all playlists.
        Normally they're auto-deleted after 90 days, but this clears them immediately.

        Args:
            ctx: The command context.
        """
        cache_manager = self._get_music_cache_manager()
        if not cache_manager:
            await ctx.send("❌ Music cog is not loaded.")
            return

        stats = cache_manager.get_stats()
        if stats['orphaned_tracks'] == 0:
            await ctx.send("📋 No orphaned files to clear.")
            return

        # Confirm before clearing
        confirm_msg = await ctx.send(
            f"⚠️ This will delete **{stats['orphaned_tracks']} orphaned tracks** "
            f"({stats['orphaned_size_mb']:.1f} MB).\n"
            "React with ✅ to confirm or ❌ to cancel."
        )
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user == ctx.author
                and reaction.message.id == confirm_msg.id
                and str(reaction.emoji) in ["✅", "❌"]
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=30.0, check=check)
            if str(reaction.emoji) == "✅":
                deleted = await cache_manager.clear_orphaned()
                await confirm_msg.edit(content=f"✅ Cleared {deleted} orphaned files.")
                self.logger.warning(f"Admin {ctx.author} cleared {deleted} orphaned music files.")
            else:
                await confirm_msg.edit(content="❌ Clear cancelled.")
        except TimeoutError:
            await confirm_msg.edit(content="⏰ Timed out. Clear cancelled.")

    @commands.hybrid_command(
        name="refresh-cache",
        hidden=True,
        description="Forces immediate cache refresh from YouTube.",
        help="Re-fetches all playlists from YouTube and reconciles downloads."
    )
    @commands.is_owner()
    async def refresh_cache(self, ctx: commands.Context) -> None:
        """Forces immediate cache refresh from YouTube.

        This cancels the current 24-hour timer, fetches all playlists fresh,
        reconciles downloads (handles orphans), and restarts the timer.

        Args:
            ctx: The command context.
        """
        cache_manager = self._get_music_cache_manager()
        if not cache_manager:
            await ctx.send("❌ Music cog is not loaded.")
            return

        status_msg = await ctx.send("🔄 **Refreshing cache from YouTube...**")

        try:
            # Cancel current timer
            cache_manager.cancel_refresh_timer()

            # Refresh all playlists
            playlists, old_membership = await cache_manager.refresh_all_playlists()

            if not playlists:
                await status_msg.edit(content="⚠️ No playlists found in ambience.toml or all failed to fetch.")
                cache_manager.start_refresh_timer()
                return

            # Reconcile downloads
            await status_msg.edit(content="🔄 **Reconciling downloads...**")
            await cache_manager.reconcile_downloads(playlists, old_membership)

            # Cleanup expired orphans
            expired = await cache_manager.cleanup_expired_orphans()

            # Queue missing downloads
            queued = await cache_manager.queue_missing_downloads()

            # Restart timer
            cache_manager.start_refresh_timer()

            # Report results
            stats = cache_manager.get_stats()
            result_embed = discord.Embed(
                title="✅ Cache Refresh Complete",
                color=discord.Color.green()
            )
            result_embed.add_field(
                name="Playlists",
                value=f"{len(playlists)} refreshed",
                inline=True
            )
            result_embed.add_field(
                name="Downloads",
                value=f"{queued} queued",
                inline=True
            )
            if expired > 0:
                result_embed.add_field(
                    name="Cleanup",
                    value=f"{expired} expired orphans deleted",
                    inline=True
                )
            result_embed.add_field(
                name="Total Tracks",
                value=f"{stats['downloaded_tracks']}/{stats['total_tracks']} downloaded",
                inline=False
            )

            await status_msg.edit(content=None, embed=result_embed)
            self.logger.info(
                f"Cache refresh completed: {len(playlists)} playlists, "
                f"{queued} queued, {expired} expired orphans cleaned."
            )

        except Exception as e:
            self.logger.error(f"Cache refresh error: {e}", exc_info=True)
            await status_msg.edit(content=f"❌ Refresh failed: {e}")
            # Make sure timer is restarted even on error
            cache_manager.start_refresh_timer()

    @commands.hybrid_command(
        name='ytauth',
        hidden=True,
        description='Upload YouTube cookies for music playback authentication'
    )
    @commands.is_owner()
    async def youtube_cookie_upload(self, ctx: commands.Context) -> None:
        """Upload a cookies.txt file exported from your browser for YouTube auth.

        This is a fallback method if the PO token plugin isn't working.
        The cookie file should be in Netscape format (exported via browser extension).

        Steps:
        1. Use incognito mode in your browser
        2. Log into a burner Google account on youtube.com
        3. Use a cookie export extension (e.g., "Get cookies.txt LOCALLY")
        4. Export cookies and upload the file here
        """
        # Check if they attached a file
        if ctx.message.attachments:
            # Process the attachment directly
            attachment = ctx.message.attachments[0]
            await self._process_cookie_upload(ctx, attachment)
            return

        # No attachment - prompt for upload
        embed = discord.Embed(
            title="🍪 YouTube Cookie Upload",
            description="Upload a `cookies.txt` file to authenticate YouTube requests.",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="How to get cookies",
            value=(
                "1. Open **incognito/private** browser window\n"
                "2. Go to [youtube.com](https://youtube.com) and sign in with a **burner** account\n"
                "3. Install a cookie export extension:\n"
                "   • Chrome: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
                "   • Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)\n"
                "4. Export cookies for `youtube.com`\n"
                "5. Reply to this message with the `.txt` file attached"
            ),
            inline=False
        )

        embed.add_field(
            name="⚠️ Security Note",
            value=(
                "• Use a **burner account**, not your main Google account\n"
                "• Cookies grant full access to that account\n"
                "• They expire after ~30 days typically"
            ),
            inline=False
        )

        # Check current status
        # _detect_youtube_auth is sync (socket.connect_ex with 0.5s timeout)
        await asyncio.to_thread(_detect_youtube_auth)
        auth_status = get_youtube_auth_status()
        cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
        if cookie_path and await asyncio.to_thread(os.path.isfile, cookie_path):
            file_age = time.time() - os.path.getmtime(cookie_path)
            age_days = int(file_age / 86400)
            embed.add_field(
                name="Current Cookie",
                value=f"✅ Present ({age_days} days old)",
                inline=True
            )
        else:
            embed.add_field(
                name="Current Cookie",
                value="❌ Not found",
                inline=True
            )

        if auth_status.pot_server_running:
            embed.set_footer(text="💡 PO token server is running - cookies are optional backup")

        prompt_msg = await ctx.send(embed=embed)

        # Wait for a reply with attachment
        def check(m: discord.Message) -> bool:
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and len(m.attachments) > 0
            )

        try:
            reply = await self.bot.wait_for('message', check=check, timeout=120.0)
            await self._process_cookie_upload(ctx, reply.attachments[0], prompt_msg)
        except asyncio.TimeoutError:
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text="⏰ Timed out waiting for file upload")
            await prompt_msg.edit(embed=embed)

    async def _process_cookie_upload(
        self,
        ctx: commands.Context,
        attachment: discord.Attachment,
        status_msg: Optional[discord.Message] = None
    ) -> None:
        """Process an uploaded cookie file.

        Args:
            ctx: Command context.
            attachment: The uploaded file.
            status_msg: Optional message to edit with result.
        """
        # Validate file
        if not attachment.filename.endswith('.txt'):
            msg = "❌ File must be a `.txt` file (Netscape cookie format)"
            if status_msg:
                await status_msg.edit(content=msg, embed=None)
            else:
                await ctx.send(msg)
            return

        if attachment.size > 100_000:  # 100KB should be way more than enough
            msg = "❌ File too large. Cookie files are typically under 10KB."
            if status_msg:
                await status_msg.edit(content=msg, embed=None)
            else:
                await ctx.send(msg)
            return

        # Download and validate content
        try:
            content = await attachment.read()
            text = content.decode('utf-8')
        except Exception as e:
            msg = f"❌ Failed to read file: {e}"
            if status_msg:
                await status_msg.edit(content=msg, embed=None)
            else:
                await ctx.send(msg)
            return

        # Basic validation - should contain youtube.com cookies
        if 'youtube.com' not in text.lower() and '.youtube.com' not in text:
            msg = "❌ File doesn't appear to contain YouTube cookies."
            if status_msg:
                await status_msg.edit(content=msg, embed=None)
            else:
                await ctx.send(msg)
            return

        # Save the cookie file
        cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
        if not cookie_path:
            cookie_path = os.path.join(config.APP_PATH, 'youtube_cookies.txt')

        try:
            # Backup and write in thread — shutil.copy2 and open() are blocking I/O
            def _save_cookie_file() -> None:
                if os.path.isfile(cookie_path):
                    backup_path = cookie_path + '.backup'
                    shutil.copy2(cookie_path, backup_path)

                with open(cookie_path, 'w', encoding='utf-8') as f:
                    f.write(text)

            await asyncio.to_thread(_save_cookie_file)

            self.logger.warning(f"Admin {ctx.author} uploaded YouTube cookies to {cookie_path}.")

            # Reset auth detection cache
            auth_status = get_youtube_auth_status()
            auth_status.last_check = 0
            auth_status.auth_method = None

            # Force re-detection (_detect_youtube_auth is sync with socket timeout)
            from utils.musicutils.music_auth import _detect_youtube_auth
            await asyncio.to_thread(_detect_youtube_auth)

            # Success message
            embed = discord.Embed(
                title="✅ Cookies Uploaded",
                description="YouTube cookie file has been saved.",
                color=discord.Color.green()
            )

            embed.add_field(
                name="Status",
                value=f"Active auth: **{auth_status.auth_method or 'cookies'}**",
                inline=True
            )

            embed.add_field(
                name="Next Steps",
                value="Try playing a YouTube video to test.",
                inline=False
            )

            if status_msg:
                await status_msg.edit(content=None, embed=embed)
            else:
                await ctx.send(embed=embed)

            # Try to delete the user's message with the attachment (contains cookies!)
            try:
                if ctx.message.attachments:
                    await ctx.message.delete()
            except discord.Forbidden:
                pass  # Can't delete, not a big deal

        except Exception as e:
            self.logger.error(f"Failed to save cookies: {e}", exc_info=True)
            msg = f"❌ Failed to save cookies: {e}"
            if status_msg:
                await status_msg.edit(content=msg, embed=None)
            else:
                await ctx.send(msg)

    # ==========================================================================
    # BOD Fate System Commands
    # ==========================================================================

    @commands.hybrid_command(
        name="bod_bless",
        hidden=True,
        description="Silently add fate to a user's BOD bank."
    )
    @commands.is_owner()
    @app_commands.describe(
        user="The user to bless.",
        tier="Fate tier: SILENT, LUCKY, BLESSED, or GUARANTEED.",
        count="Amount of fate to add (default 1)."
    )
    async def bod_bless(
        self,
        ctx: commands.Context,
        user: discord.User,
        tier: str,
        count: int = 1
    ) -> None:
        """Silently add fate to a user's BOD bank.

        Args:
            ctx: The command context.
            user: The target user (works for any user, not just server members).
            tier: SILENT, LUCKY, BLESSED, or GUARANTEED.
            count: Amount to add.
        """
        tier_upper = tier.upper()
        if tier_upper not in ('SILENT', 'LUCKY', 'BLESSED', 'GUARANTEED'):
            await ctx.send("❌ Invalid tier. Use SILENT, LUCKY, BLESSED, or GUARANTEED.", ephemeral=True)
            return

        if count < 1:
            await ctx.send("❌ Count must be at least 1.", ephemeral=True)
            return

        try:
            await self.db_manager.add_bod_fate(user.id, tier_upper, count)
            display = getattr(user, 'display_name', None) or user.name
            await ctx.send(f"✅ Added {count}x {tier_upper} fate to {display}.", ephemeral=True)
            self.logger.warning(f"Admin {ctx.author} blessed {user} ({user.id}) with {count}x {tier_upper} fate.")
        except Exception as e:
            await ctx.send(f"❌ Failed to add fate: {e}", ephemeral=True)
            self.logger.error(f"Failed to add {count}x {tier_upper} fate to {user.id}: {e}", exc_info=True)

    @commands.hybrid_command(
        name="bod_fate",
        hidden=True,
        description="View a user's BOD fate bank."
    )
    @commands.is_owner()
    @app_commands.describe(
        user="The user to check (default: yourself)."
    )
    async def bod_fate(
        self,
        ctx: commands.Context,
        user: Optional[discord.User] = None
    ) -> None:
        """View a user's BOD fate bank.

        Args:
            ctx: The command context.
            user: User to check (works for any user), or self if None.
        """
        target = user or ctx.author

        try:
            fate = await self.db_manager.get_bod_fate(target.id)
            display = getattr(target, 'display_name', None) or target.name

            embed = discord.Embed(
                title=f"BOD Fate Bank: {display}",
                color=discord.Color.purple()
            )

            fate_lines = [
                f"🔇 **Silent** (100%, no flavor): {fate.get('silent', 0)}",
                f"⚡ **Guaranteed** (100%): {fate.get('guaranteed', 0)}",
                f"🌟 **Blessed** (75%): {fate.get('blessed', 0)}",
                f"✨ **Lucky** (50%): {fate.get('lucky', 0)}",
            ]
            embed.description = "\n".join(fate_lines)

            total = sum(fate.values())
            if total == 0:
                embed.set_footer(text="No fate stored - rolls will be normal (25%)")
            else:
                embed.set_footer(text=f"Total fate charges: {total}")

            await ctx.send(embed=embed, ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to get fate: {e}", ephemeral=True)
            self.logger.error(f"Failed to get fate for {target.id}: {e}", exc_info=True)

    @commands.hybrid_command(
        name="bod_clear",
        hidden=True,
        description="Clear fate from a user's BOD bank."
    )
    @commands.is_owner()
    @app_commands.describe(
        user="The user to clear.",
        tier="Specific tier to clear, or leave empty for all."
    )
    async def bod_clear(
        self,
        ctx: commands.Context,
        user: discord.User,
        tier: Optional[str] = None
    ) -> None:
        """Clear fate from a user's BOD bank.

        Args:
            ctx: The command context.
            user: The target user (works for any user, not just server members).
            tier: Specific tier or None for all.
        """
        tier_upper = tier.upper() if tier else None
        if tier_upper and tier_upper not in ('SILENT', 'LUCKY', 'BLESSED', 'GUARANTEED'):
            await ctx.send("❌ Invalid tier. Use SILENT, LUCKY, BLESSED, or GUARANTEED.", ephemeral=True)
            return

        try:
            await self.db_manager.clear_bod_fate(user.id, tier_upper)
            display = getattr(user, 'display_name', None) or user.name
            if tier_upper:
                await ctx.send(f"✅ Cleared {tier_upper} fate from {display}.", ephemeral=True)
            else:
                await ctx.send(f"✅ Cleared all fate from {display}.", ephemeral=True)
            self.logger.warning(f"Admin {ctx.author} cleared {'all' if not tier_upper else tier_upper} fate from {user} ({user.id}).")
        except Exception as e:
            await ctx.send(f"❌ Failed to clear fate: {e}", ephemeral=True)
            self.logger.error(f"Failed to clear {'all' if not tier_upper else tier_upper} fate from {user.id}: {e}", exc_info=True)

    @commands.hybrid_command(
        name="visibility",
        hidden=True,
        description="Set bot visibility status (online/idle/dnd/invisible).",
        help=(
            "Set the bot's Discord visibility status.\n\n"
            "Useful for dev bots to go invisible during testing to avoid "
            "online/offline notification spam. Music presence will not "
            "override an invisible status."
        )
    )
    @commands.is_owner()
    @app_commands.describe(
        status="The visibility status to set."
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Idle", value="idle"),
        app_commands.Choice(name="Do Not Disturb", value="dnd"),
        app_commands.Choice(name="Invisible", value="invisible"),
    ])
    async def visibility(
        self,
        ctx: commands.Context,
        status: str
    ) -> None:
        """Set the bot's Discord visibility status.

        Useful for dev bots to go invisible during testing to avoid
        online/offline notification spam. Music presence will not
        override an invisible status.

        Args:
            ctx: The command context.
            status: One of 'online', 'idle', 'dnd', 'invisible'.
        """
        status_lower = status.lower()
        if await self.bot.set_visibility(status_lower):
            status_emoji = {
                'online': '🟢',
                'idle': '🌙',
                'dnd': '⛔',
                'invisible': '👻'
            }
            emoji = status_emoji.get(status_lower, '')
            await ctx.send(f"{emoji} Visibility set to **{status_lower}**.", ephemeral=True)
            self.logger.warning(f"Admin {ctx.author} set visibility to {status_lower}.")
        else:
            await ctx.send(
                "❌ Invalid status. Use: online, idle, dnd, invisible.",
                ephemeral=True
            )


    # ========== LIMBUS DATA MANAGEMENT ==========

    @commands.hybrid_command(
        name="limbus-rescrape",
        hidden=True,
        description="Re-scrape Limbus identity data from the wiki.",
        help="Re-scrape Limbus identity data. Mode: 'parse' (cache only) or 'full' (re-download + parse).",
    )
    @commands.is_owner()
    @app_commands.describe(
        mode="'parse' (from cache, default) or 'full' (re-download + parse)"
    )
    async def limbus_rescrape(self, ctx: commands.Context, mode: str = "parse") -> None:
        """Re-scrape Limbus identity data.

        Args:
            ctx: The command context.
            mode: 'parse' to parse from cache, 'full' to re-download and parse.
        """
        mode = mode.lower()
        if mode not in ("parse", "full"):
            await ctx.send("Mode must be 'parse' (cache only) or 'full' (re-download + parse).")
            return

        limbus_cog: Limbus | None = self.bot.get_cog("Limbus")  # type: ignore[assignment]
        if not limbus_cog:
            await ctx.send("Limbus cog is not loaded.")
            return

        redownload = mode == "full"
        label = "full re-download + parse" if redownload else "parse from cache"
        status_msg = await ctx.send(f"Rescraping identity data ({label})...")

        log_lines: list[str] = []

        def log_fn(msg: str) -> None:
            log_lines.append(msg)

        try:
            downloaded, parsed, restored = await limbus_cog.rescrape(
                redownload=redownload, log=log_fn
            )

            summary_parts = []
            if redownload:
                summary_parts.append(f"{downloaded} pages downloaded")
            summary_parts.append(f"{parsed} identities parsed")
            if restored:
                summary_parts.append(f"{restored} manual edits preserved")

            await status_msg.edit(content=f"Rescrape complete: {', '.join(summary_parts)}.")
            self.logger.info(f"Admin {ctx.author} ran limbus rescrape ({mode}): {summary_parts}")

        except Exception as e:
            self.logger.error(f"Limbus rescrape failed: {e}", exc_info=True)
            tail = "\n".join(log_lines[-5:]) if log_lines else "No log output"
            await status_msg.edit(content=f"Rescrape failed: {e}\n```\n{tail}\n```")

    @commands.hybrid_command(
        name="limbus-edit",
        hidden=True,
        description="Edit a Limbus identity's skill data.",
        help="Edit a Limbus identity's skill data. Search by name.",
    )
    @commands.is_owner()
    @app_commands.describe(
        query="Identity name (or part of it) to search for."
    )
    async def limbus_edit(self, ctx: commands.Context, *, query: str) -> None:
        """Edit a Limbus identity's skill data via an interactive editor.

        Args:
            ctx: The command context.
            query: Identity name to search for.
        """
        limbus_cog: Limbus | None = self.bot.get_cog("Limbus")  # type: ignore[assignment]
        if not limbus_cog:
            await ctx.send("Limbus cog is not loaded.")
            return

        matches = limbus_cog._search_identities(query)
        if not matches:
            await ctx.send(f"No identities matching '{query}'.")
            return

        # If multiple matches, let admin pick
        if len(matches) > 1:
            top = matches[:5]
            options = {
                f"{m['name']} ({m['sinner']})": m["id"]
                for m in top
            }
            embed = discord.Embed(
                title="Multiple identities found",
                description="Select the identity to edit:",
                color=discord.Color.blue(),
            )
            for m in top:
                embed.add_field(
                    name=m["name"],
                    value=m["sinner"],
                    inline=False,
                )

            selected_id = await get_selection(ctx, embed, options)
            if not selected_id:
                await ctx.send("Edit cancelled.")
                return

            identity = limbus_cog.get_identity(selected_id)
        else:
            identity = matches[0]

        if not identity or not identity.get("skills"):
            await ctx.send("Identity has no skills to edit.")
            return

        from utils.views import SkillEditorState, show_skill_editor

        state = SkillEditorState(
            identity_id=identity["id"],
            identity_name=identity["name"],
            sinner=identity["sinner"],
            skills=identity["skills"],
        )

        async def save_callback(identity_id: str, skill_label: str, updates: dict) -> bool:
            return limbus_cog.update_skill(identity_id, skill_label, updates)

        await show_skill_editor(ctx, state, save_callback)
        self.logger.info(f"Admin {ctx.author} opened skill editor for {identity['name']}")


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(AdminCog(bot))

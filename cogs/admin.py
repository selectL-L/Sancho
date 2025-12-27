"""cogs/admin.py

This cog contains owner/admin-only commands for administrative tasks, such as
viewing bot status and managing configurations.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import typing
from datetime import timedelta
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.extensions import discover_cogs
from utils.music_helpers import (
    MUTAGEN_AVAILABLE,
    YTDLP_AVAILABLE,
    DownloadResult,
    MusicCacheManager,
    download_track_as_mp3,
    get_track_info_for_download,
    get_youtube_auth_status,
)
from utils.views import PaginatorView, get_selection


class DashboardView(discord.ui.View):
    """The main dashboard view for the admin report."""

    def __init__(
        self,
        ctx: commands.Context,
        skill_pages: List[discord.Embed],
        reminder_pages: List[discord.Embed],
        report_file_callback,
        dump_db_callback,
        dashboard_embed: Optional[discord.Embed] = None
    ):
        super().__init__(timeout=120.0)
        self.ctx = ctx
        self.skill_pages = skill_pages
        self.reminder_pages = reminder_pages
        self.report_file_callback = report_file_callback
        self.dump_db_callback = dump_db_callback
        self.dashboard_embed = dashboard_embed
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This dashboard is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📜 View Skills", style=discord.ButtonStyle.primary)
    async def view_skills(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.skill_pages:
            await interaction.response.send_message("No skills found in database.", ephemeral=True)
            return

        view = PaginatorView(self.ctx, self.skill_pages)
        # Add a "Back to Dashboard" button to the paginator
        back_button = discord.ui.Button(
            label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

        async def back_callback(interaction: discord.Interaction):
            if self.dashboard_embed:
                await interaction.response.edit_message(embed=self.dashboard_embed, view=self)
            view.stop()

        back_button.callback = back_callback
        view.add_item(back_button)

        await interaction.response.edit_message(embed=self.skill_pages[0], view=view)
        view.message = interaction.message

    @discord.ui.button(label="⏰ View Reminders", style=discord.ButtonStyle.primary)
    async def view_reminders(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.reminder_pages:
            await interaction.response.send_message("No reminders found in database.", ephemeral=True)
            return

        view = PaginatorView(self.ctx, self.reminder_pages)
        # Add a "Back to Dashboard" button to the paginator
        back_button = discord.ui.Button(
            label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

        async def back_callback(interaction: discord.Interaction):
            if self.dashboard_embed:
                await interaction.response.edit_message(embed=self.dashboard_embed, view=self)
            view.stop()

        back_button.callback = back_callback
        view.add_item(back_button)

        await interaction.response.edit_message(embed=self.reminder_pages[0], view=view)
        view.message = interaction.message

    @discord.ui.button(label="💾 Export to File", style=discord.ButtonStyle.secondary)
    async def export_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.report_file_callback(self.ctx)

    @discord.ui.button(label="🗄️ Dump Database", style=discord.ButtonStyle.danger)
    async def dump_database(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.dump_db_callback(self.ctx)

    async def on_timeout(self):
        if self.message:
            try:
                for child in self.children:
                    if hasattr(child, 'disabled'):
                        setattr(child, 'disabled', True)
                await self.message.edit(view=self)
            except Exception:
                pass


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
            def chunk_list(lst, n):
                for i in range(0, len(lst), n):
                    yield lst[i:i + n]

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

                await interaction_ctx.send("Database report attached:", file=discord.File(temp_path, filename="db_report.txt"))
                os.remove(temp_path)

            # 4. Define Database Dump Callback (full SQLite export for migrations)
            async def dump_db_callback(interaction_ctx):
                assert config.BOT_NAME is not None
                import json
                import shutil

                # Create a temp directory for the dump
                dump_dir = tempfile.mkdtemp(prefix="db_dump_")
                try:
                    # Copy the actual SQLite database file
                    db_path = self.db_manager.db_path
                    db_copy_path = os.path.join(
                        dump_dir, f"{config.BOT_NAME}_database.db")
                    shutil.copy2(db_path, db_copy_path)

                    # Also create a JSON export of all data for easy inspection
                    json_data = {
                        "exported_at": str(discord.utils.utcnow()),
                        "bot_name": config.BOT_NAME,
                        "tables": {}
                    }

                    # Export all tables as JSON
                    tables_to_export = [
                        ("skills", all_skills),
                        ("reminders", all_reminders),
                    ]

                    # Get additional tables
                    try:
                        skill_aliases = await self.db_manager.db_fetchall("SELECT * FROM skill_aliases")
                        tables_to_export.append(
                            ("skill_aliases", [dict(row) for row in skill_aliases]))
                    except Exception:
                        pass

                    try:
                        user_settings = await self.db_manager.db_fetchall("SELECT * FROM user_settings")
                        tables_to_export.append(
                            ("user_settings", [dict(row) for row in user_settings]))
                    except Exception:
                        pass

                    try:
                        bot_settings = await self.db_manager.db_fetchall("SELECT * FROM bot_settings")
                        tables_to_export.append(
                            ("bot_settings", [dict(row) for row in bot_settings]))
                    except Exception:
                        pass

                    try:
                        guild_settings = await self.db_manager.db_fetchall("SELECT * FROM guild_settings")
                        tables_to_export.append(
                            ("guild_settings", [dict(row) for row in guild_settings]))
                    except Exception:
                        pass

                    try:
                        starboard_entries = await self.db_manager.db_fetchall("SELECT * FROM starboard_entries")
                        tables_to_export.append(
                            ("starboard_entries", [dict(row) for row in starboard_entries]))
                    except Exception:
                        pass

                    try:
                        bod_usage = await self.db_manager.db_fetchall("SELECT * FROM bod_usage")
                        tables_to_export.append(
                            ("bod_usage", [dict(row) for row in bod_usage]))
                    except Exception:
                        pass

                    try:
                        bod_leaderboard = await self.db_manager.db_fetchall("SELECT * FROM bod_leaderboard")
                        tables_to_export.append(
                            ("bod_leaderboard", [dict(row) for row in bod_leaderboard]))
                    except Exception:
                        pass

                    for table_name, data in tables_to_export:
                        json_data["tables"][table_name] = {
                            "count": len(data),
                            "rows": data
                        }

                    json_path = os.path.join(
                        dump_dir, f"{config.BOT_NAME}_database.json")
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(json_data, f, indent=2, default=str)

                    # Create a zip archive containing both files
                    import zipfile
                    zip_path = os.path.join(
                        dump_dir, f"{config.BOT_NAME}_db_dump.zip")
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        zipf.write(
                            db_copy_path, os.path.basename(db_copy_path))
                        zipf.write(json_path, os.path.basename(json_path))

                    await interaction_ctx.send(
                        "**Full database dump attached.**\nContains:\n"
                        "• `.db` - SQLite database file (for migrations)\n"
                        "• `.json` - Human-readable JSON export",
                        file=discord.File(
                            zip_path, filename=f"{config.BOT_NAME}_db_dump.zip")
                    )
                finally:
                    # Cleanup temp directory
                    shutil.rmtree(dump_dir, ignore_errors=True)

            # 5. Create Dashboard Embed
            dashboard_embed = discord.Embed(
                title="Admin Dashboard",
                description="Select a category to view database entries.\n\n"
                            "**💾 Export to File** - Human-readable text report\n"
                            "**🗄️ Dump Database** - Full SQLite + JSON export for migrations",
                color=discord.Color.dark_grey()
            )
            dashboard_embed.add_field(
                name="Stats", value=f"Skills: **{len(all_skills)}**\nReminders: **{len(all_reminders)}**")

            # 6. Launch View
            view = DashboardView(ctx, skill_pages, reminder_pages,
                                 export_callback, dump_db_callback, dashboard_embed)
            sent_msg = await ctx.send(embed=dashboard_embed, view=view)
            view.message = sent_msg

        except Exception as e:
            logging.error("Error generating dashboard:", exc_info=True)
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
                        return
                    else:
                        await ctx.send("Deletion cancelled.")
                        return

                if updates:
                    await self.db_manager.update_skill(entry_id, skill['user_id'], updates)
                    await ctx.send("✅ Skill updated.")

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
                    dt = dateparser.parse(msg.content.strip(), languages=[
                                          'en'], settings={'PREFER_DATES_FROM': 'future'})
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
                        return
                    else:
                        await ctx.send("Deletion cancelled.")
                        return

                if updates:
                    await self.db_manager.update_reminder(entry_id, rem['user_id'], updates)
                    await ctx.send("✅ Reminder updated.")

        except Exception as e:
            logging.error(
                f"Error editing entry {entry_id}: {e}", exc_info=True)
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
            self.logger.info(f"Mood changed to {mood_name} by {ctx.author}")
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

        Includes latency, uptime, cog status, database health, and resource usage.
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

        # Send initial message.
        start_time = time.monotonic()
        message = await ctx.send("Checking status...")
        end_time = time.monotonic()

        # Gather metrics.
        # Latencies
        roundtrip_latency = (end_time - start_time) * 1000
        gateway_latency = self.bot.latency * 1000
        db_latency = await self.db_manager.ping() if self.db_manager else -1

        # Uptime & Start Time
        start_timestamp = int(self.bot.start_time)
        uptime_delta = timedelta(seconds=time.time() - self.bot.start_time)
        days, remainder = divmod(uptime_delta.total_seconds(), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"

        # Cogs
        loaded_cogs = self.bot.extensions.keys()
        total_cogs = len(discover_cogs(config.COGS_PATH))
        cogs_status = f"{len(loaded_cogs)}/{total_cogs}"

        # Resource Usage - Get LIVE values from resource tracker (async for accurate reading)
        if resource_tracker:
            usage = await resource_tracker.get_current_usage_async()
            cpu_str = f"{usage['cpu']:.1f}%"
            ram_str = f"{usage['ram']:.2f} MB"
        else:
            # Fallback if resource tracker is not available
            cpu_str = "N/A"
            ram_str = "N/A"

        # Create status embed.
        embed = discord.Embed(
            title=f"{config.BOT_NAME}'s Status Report",
            color=discord.Color.green() if gateway_latency < 200 else discord.Color.orange()
        )
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        embed.add_field(
            name="Timings",
            value=f"**Gateway:** `{gateway_latency:.2f}ms`\n"
            f"**Roundtrip:** `{roundtrip_latency:.2f}ms`\n"
            f"**Database:** `{db_latency:.2f}ms`",
            inline=True
        )

        embed.add_field(
            name="Status",
            value=f"**Uptime:** `{uptime_str}`\n"
            f"**Started:** <t:{start_timestamp}:f>\n"
            f"**Cogs Loaded:** `{cogs_status}`",
            inline=True
        )

        embed.add_field(
            name="Resource Usage",
            value=f"**CPU:** `{cpu_str}`\n"
            f"**RAM:** `{ram_str}`",
            inline=True
        )

        # Add a field for loaded cogs, formatted nicely
        if loaded_cogs:
            # Format cog names by removing 'cogs.' prefix and joining them
            cog_list_str = ", ".join([cog.replace('cogs.', '')
                                     for cog in sorted(loaded_cogs)])
            embed.add_field(
                name="Loaded Cogs",
                value=f"```{cog_list_str}```",
                inline=False
            )

        embed.set_footer(
            text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()

        # Update message.
        await message.edit(content=None, embed=embed)
        logging.info(f"Status command used by {ctx.author}.")

    # ==========================================================================
    # MUSIC DOWNLOAD COMMAND
    # ==========================================================================

    @commands.hybrid_command(
        name="download-mp3",
        hidden=True,
        description="Downloads a YouTube video as MP3 with full metadata.",
        help="Downloads a YouTube video as MP3 with full metadata including cover art."
    )
    @commands.is_owner()
    @app_commands.describe(
        url="YouTube URL to download",
        title="Override the track title (optional)",
        artist="Override the artist name (optional)",
        album="Album name to embed (optional)",
        genre="Genre tag to embed (optional)",
        year="Year tag to embed (optional)",
        track_num="Track number (e.g., '1' or '1/12') (optional)"
    )
    async def download_mp3(
        self,
        ctx: commands.Context,
        url: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        genre: Optional[str] = None,
        year: Optional[str] = None,
        track_num: Optional[str] = None
    ) -> None:
        """Downloads a YouTube video as MP3 with full embedded metadata.

        The file is saved to the music cache directory with embedded ID3 tags
        including cover art. Metadata can be customized or auto-populated from
        the source video.

        Args:
            ctx: The command context.
            url: YouTube URL to download.
            title: Override the track title.
            artist: Override the artist name.
            album: Album name to embed.
            genre: Genre tag to embed.
            year: Year tag to embed.
            track_num: Track number to embed.
        """
        # Check dependencies
        if not YTDLP_AVAILABLE:
            await ctx.send("❌ **yt-dlp** is not installed. Install with: `pip install yt-dlp`")
            return

        if not MUTAGEN_AVAILABLE:
            await ctx.send("❌ **mutagen** is not installed. Install with: `pip install mutagen`")
            return

        # Validate URL (basic check)
        if not any(domain in url.lower() for domain in ['youtube.com', 'youtu.be', 'music.youtube.com']):
            await ctx.send("❌ Please provide a valid YouTube URL.")
            return

        # Download directory - use a dedicated downloads folder
        download_dir = os.path.join(config.APP_PATH, 'downloads', 'music')

        # Show preview first
        status_msg = await ctx.send("🔍 **Fetching track info...**")

        track_info = await get_track_info_for_download(url, self.logger)
        if not track_info:
            await status_msg.edit(content="❌ Could not fetch track information. The video may be unavailable.")
            return

        # Build preview embed
        preview_embed = discord.Embed(
            title="📥 Download Preview",
            color=discord.Color.blue()
        )

        # Show what metadata will be used
        final_title = title or track_info['title']
        final_artist = artist or track_info['artist']
        final_album = album or track_info.get('album') or '*Not available*'
        final_year = year
        if not final_year and track_info.get('upload_date'):
            final_year = track_info['upload_date'][:4]

        preview_embed.add_field(
            name="Title",
            value=f"`{final_title}`",
            inline=True
        )
        preview_embed.add_field(
            name="Artist",
            value=f"`{final_artist}`",
            inline=True
        )
        preview_embed.add_field(
            name="Album",
            value=f"`{final_album}`",
            inline=True
        )

        if final_year:
            preview_embed.add_field(name="Year", value=f"`{final_year}`", inline=True)
        if genre:
            preview_embed.add_field(name="Genre", value=f"`{genre}`", inline=True)
        if track_num:
            preview_embed.add_field(name="Track #", value=f"`{track_num}`", inline=True)

        # Duration
        if track_info.get('duration'):
            mins, secs = divmod(track_info['duration'], 60)
            preview_embed.add_field(name="Duration", value=f"`{mins}:{secs:02d}`", inline=True)

        # Thumbnail preview
        if track_info.get('thumbnail'):
            preview_embed.set_thumbnail(url=track_info['thumbnail'])

        preview_embed.set_footer(text="Starting download...")

        await status_msg.edit(content=None, embed=preview_embed)

        # Start the download
        await status_msg.edit(content="⏳ **Downloading and converting to MP3...**", embed=preview_embed)

        result: DownloadResult = await download_track_as_mp3(
            url=url,
            output_dir=download_dir,
            logger=self.logger,
            custom_title=title,
            custom_artist=artist,
            custom_album=album,
            custom_genre=genre,
            custom_year=year,
            custom_track_num=track_num,
            embed_thumbnail=True
        )

        if not result.success:
            error_embed = discord.Embed(
                title="❌ Download Failed",
                description=result.error_message or "Unknown error occurred.",
                color=discord.Color.red()
            )
            await status_msg.edit(content=None, embed=error_embed)
            return

        # Success embed
        success_embed = discord.Embed(
            title="✅ Download Complete",
            color=discord.Color.green()
        )
        success_embed.add_field(name="Title", value=f"`{result.title}`", inline=True)
        success_embed.add_field(name="Artist", value=f"`{result.artist}`", inline=True)

        if result.album:
            success_embed.add_field(name="Album", value=f"`{result.album}`", inline=True)

        if result.duration:
            mins, secs = divmod(result.duration, 60)
            success_embed.add_field(name="Duration", value=f"`{mins}:{secs:02d}`", inline=True)

        success_embed.add_field(
            name="Cover Art",
            value="✅ Embedded" if result.thumbnail_embedded else "❌ Not embedded",
            inline=True
        )
        success_embed.add_field(
            name="File",
            value=f"```{result.filename}```",
            inline=False
        )
        success_embed.add_field(
            name="Location",
            value=f"```{result.file_path}```",
            inline=False
        )

        if track_info.get('thumbnail'):
            success_embed.set_thumbnail(url=track_info['thumbnail'])

        await status_msg.edit(content=None, embed=success_embed)
        self.logger.info(f"[Download] Completed download: {result.filename}")

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
        name="cache-stats",
        hidden=True,
        description="Shows statistics about the music cache.",
        help="Shows statistics about the music cache including downloads and orphans."
    )
    @commands.is_owner()
    async def cache_stats(self, ctx: commands.Context) -> None:
        """Shows statistics about the music cache.

        Displays total playlists, tracks, downloaded tracks, orphaned tracks,
        and disk usage.

        Args:
            ctx: The command context.
        """
        cache_manager = self._get_music_cache_manager()
        if not cache_manager:
            await ctx.send("❌ Music cog is not loaded.")
            return

        stats = cache_manager.get_stats()

        embed = discord.Embed(
            title="🎵 Music Cache Statistics",
            color=discord.Color.blue()
        )

        # Format last refresh time
        last_refresh_str = "Never"
        if stats.get('last_refresh_ago'):
            hours = stats['last_refresh_ago'] / 3600
            if hours < 1:
                last_refresh_str = f"{int(stats['last_refresh_ago'] / 60)} minutes ago"
            elif hours < 24:
                last_refresh_str = f"{hours:.1f} hours ago"
            else:
                last_refresh_str = f"{hours / 24:.1f} days ago"

        embed.add_field(
            name="Overview",
            value=(
                f"**Playlists:** {stats['total_playlists']}\n"
                f"**Total Tracks:** {stats['total_tracks']}\n"
                f"**Downloaded:** {stats['downloaded_tracks']}\n"
                f"**Orphaned:** {stats['orphaned_tracks']}"
            ),
            inline=True
        )

        embed.add_field(
            name="Storage",
            value=(
                f"**Active:** {stats['size_mb']:.1f} MB\n"
                f"**Orphaned:** {stats['orphaned_size_mb']:.1f} MB\n"
                f"**Total:** {stats['size_mb'] + stats['orphaned_size_mb']:.1f} MB"
            ),
            inline=True
        )

        embed.add_field(
            name="Last Refresh",
            value=last_refresh_str,
            inline=True
        )

        download_pct = (stats['downloaded_tracks'] / stats['total_tracks'] * 100) if stats['total_tracks'] > 0 else 0
        embed.set_footer(text=f"Download progress: {download_pct:.0f}% | Cache location: {config.MUSIC_CACHE_PATH}")
        await ctx.send(embed=embed)

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
                deleted = cache_manager.clear_orphaned()
                await confirm_msg.edit(content=f"✅ Cleared {deleted} orphaned files.")
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
            playlists = await cache_manager.refresh_all_playlists()

            if not playlists:
                await status_msg.edit(content="⚠️ No playlists found in ambience.toml or all failed to fetch.")
                cache_manager.start_refresh_timer()
                return

            # Reconcile downloads
            await status_msg.edit(content="🔄 **Reconciling downloads...**")
            cache_manager.reconcile_downloads(playlists)

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

        except Exception as e:
            self.logger.error(f"Cache refresh error: {e}", exc_info=True)
            await status_msg.edit(content=f"❌ Refresh failed: {e}")
            # Make sure timer is restarted even on error
            cache_manager.start_refresh_timer()

    @commands.hybrid_command(
        name='musicstatus',
        hidden=True,
        description='Shows YouTube authentication status and recent error rates'
    )
    @commands.is_owner()
    async def music_status(self, ctx: commands.Context) -> None:
        """Shows diagnostic info about YouTube authentication and 403 error rates.

        This helps identify if the PO token server is working or if cookies need refreshing.
        Owner-only command for debugging music playback issues.
        """
        auth_status = get_youtube_auth_status()

        # Force a fresh check of auth status
        auth_status.last_check = 0

        # Trigger detection to refresh all fields
        from utils.music_helpers import _detect_youtube_auth
        _detect_youtube_auth()

        # Build status embed
        embed = discord.Embed(
            title="🎵 Music Status",
            color=discord.Color.blue()
        )

        # PO Token Server status (most important)
        if auth_status.pot_server_running:
            server_text = "✅ Running (auto-generates tokens)"
        elif auth_status.pot_provider_ready:
            server_text = "⚠️ Ready but not running\n(restarts with bot)"
        elif auth_status.pot_plugin_installed:
            server_text = f"⚠️ Plugin installed\n`{auth_status.pot_plugin_error}`"
        else:
            server_text = "❌ Not set up"

        embed.add_field(
            name="PO Token Server",
            value=server_text,
            inline=False
        )

        # Active auth method
        if auth_status.auth_method == 'pot_server':
            auth_text = "✅ PO Token Server"
        elif auth_status.auth_method == 'cookies':
            auth_text = "✅ Cookies"
            if auth_status.po_token:
                auth_text += " + manual PO token"
        else:
            auth_text = "⚠️ None (403 errors likely)"

        embed.add_field(
            name="Active Auth",
            value=auth_text,
            inline=True
        )

        # Cookie file status (fallback option)
        cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
        if cookie_path and os.path.isfile(cookie_path):
            file_age = time.time() - os.path.getmtime(cookie_path)
            age_days = int(file_age / 86400)
            if age_days > 30:
                cookie_text = f"⚠️ Present ({age_days}d old)"
            else:
                cookie_text = f"✅ Present ({age_days}d old)"
        else:
            cookie_text = "❌ Not found"

        embed.add_field(
            name="Cookie Fallback",
            value=cookie_text,
            inline=True
        )

        # 403 error rate
        count_403, window = auth_status.get_403_rate()
        window_mins = int(window / 60)
        if count_403 == 0:
            error_text = "✅ No recent 403 errors"
        elif count_403 < 3:
            error_text = f"⚠️ {count_403} error(s) in last {window_mins}m"
        else:
            error_text = f"❌ {count_403} errors in last {window_mins}m"

        embed.add_field(
            name="403 Rate",
            value=error_text,
            inline=True
        )

        # yt-dlp availability
        embed.add_field(
            name="yt-dlp",
            value="✅ Available" if YTDLP_AVAILABLE else "❌ Missing",
            inline=True
        )

        # Get music cog for playback status
        music_cog = self.bot.get_cog('Music')
        active_session = getattr(music_cog, 'active_session', None) if music_cog else None
        if active_session:
            vc = active_session.voice_client
            embed.add_field(
                name="Playback",
                value=f"🔊 {vc.channel.mention if vc else 'Active'}",
                inline=True
            )
        else:
            embed.add_field(
                name="Playback",
                value="💤 Idle",
                inline=True
            )

        # Setup hints if server isn't working
        if not auth_status.pot_server_running:
            if not auth_status.pot_plugin_installed:
                hint = (
                    "**Setup PO Token Server:**\n"
                    "```pip install bgutil-ytdlp-pot-provider```\n"
                    "Then clone & build provider in `utils/pot_provider/`"
                )
            elif not auth_status.pot_provider_ready:
                hint = (
                    "**Build Provider Script:**\n"
                    "```\ncd utils/pot_provider/server\n"
                    "npm install && npx tsc\n```"
                )
            else:
                hint = "**Server will start when bot restarts.**"
            
            if not auth_status.auth_method:
                hint += "\n\n**Or upload cookies:** `/ytauth`"
            
            embed.add_field(
                name="🔧 Setup",
                value=hint,
                inline=False
            )

        await ctx.send(embed=embed)

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
        auth_status = get_youtube_auth_status()
        cookie_path = getattr(config, 'YOUTUBE_COOKIE_PATH', None)
        if cookie_path and os.path.isfile(cookie_path):
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
            # Backup existing file if present
            if os.path.isfile(cookie_path):
                backup_path = cookie_path + '.backup'
                shutil.copy2(cookie_path, backup_path)
                self.logger.info(f"Backed up existing cookies to {backup_path}")

            # Write new cookies
            with open(cookie_path, 'w', encoding='utf-8') as f:
                f.write(text)

            self.logger.info(f"Saved YouTube cookies to {cookie_path}")

            # Reset auth detection cache
            auth_status = get_youtube_auth_status()
            auth_status.last_check = 0
            auth_status.auth_method = None

            # Force re-detection
            from utils.music_helpers import _detect_youtube_auth
            _detect_youtube_auth()

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


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(AdminCog(bot))

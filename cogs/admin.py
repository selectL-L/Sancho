"""cogs/admin.py

This cog contains owner/admin-only commands for administrative tasks, such as
viewing bot status and managing configurations.
"""

import logging
import os
import tempfile
import time
import typing
import asyncio
from datetime import timedelta
from typing import List, Optional

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.extensions import discover_cogs
from utils.views import PaginatorView, get_selection


class DashboardView(discord.ui.View):
    """The main dashboard view for the admin report."""

    def __init__(
        self,
        ctx: commands.Context,
        skill_pages: List[discord.Embed],
        reminder_pages: List[discord.Embed],
        report_file_callback,
        dashboard_embed: Optional[discord.Embed] = None
    ):
        super().__init__(timeout=120.0)
        self.ctx = ctx
        self.skill_pages = skill_pages
        self.reminder_pages = reminder_pages
        self.report_file_callback = report_file_callback
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
        back_button = discord.ui.Button(label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

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
        back_button = discord.ui.Button(label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

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
        self.process = psutil.Process()
        self.process.cpu_percent()  # Initialize for accurate subsequent readings
        self.usage_history = []
        self.record_usage.start()

    async def cog_unload(self) -> None:
        """Cancels the usage recording task when the cog is unloaded."""
        self.take_snapshot(label="Shutdown")
        self.dump_usage_history()
        self.record_usage.cancel()

    def dump_usage_history(self) -> None:
        """Dumps the usage history to a file and manages old files."""
        if not self.usage_history:
            return

        try:
            # Calculate runtime
            uptime_seconds = time.time() - self.bot.start_time
            uptime_str = str(timedelta(seconds=int(uptime_seconds))).replace(":", "-")

            timestamp_str = discord.utils.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"status_history_{timestamp_str}_runtime-{uptime_str}.txt"

            # Generate content
            lines = [f"{'Timestamp':<25} | {'CPU (%)':<10} | {'RAM (MB)':<10} | {'Label':<10}"]
            lines.append("-" * 65)
            for entry in self.usage_history:
                ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
                label = entry.get('label') or ""
                lines.append(f"{ts:<25} | {entry['cpu']:<10.1f} | {entry['ram']:<10.2f} | {label:<10}")

            # Write to file
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            # Manage old files
            self.cleanup_old_history_files()
        except Exception as e:
            logging.error(f"Failed to dump usage history: {e}")

    def cleanup_old_history_files(self) -> None:
        """Keeps only the 5 most recent status history files."""
        try:
            files = [f for f in os.listdir('.') if f.startswith("status_history_") and f.endswith(".txt")]
            # Sort by modification time (oldest first)
            files.sort(key=lambda x: os.path.getmtime(x))

            while len(files) > 5:
                file_to_remove = files.pop(0)
                os.remove(file_to_remove)
        except Exception as e:
            logging.error(f"Failed to cleanup old history files: {e}")

    def take_snapshot(self, label: typing.Optional[str] = None) -> None:
        """Takes a snapshot of the current resource usage.

        Args:
            label (typing.Optional[str]): An optional label for the snapshot.
        """
        try:
            memory_info = self.process.memory_info()
            cpu_usage = self.process.cpu_percent(interval=None)
            ram_usage = memory_info.rss / (1024 * 1024)
            timestamp = discord.utils.utcnow()
            self.usage_history.append({
                'timestamp': timestamp,
                'cpu': cpu_usage,
                'ram': ram_usage,
                'label': label
            })
        except Exception as e:
            logging.error(f"Error recording usage stats: {e}")

    @tasks.loop(minutes=30)
    async def record_usage(self) -> None:
        """Records CPU and RAM usage every 30 minutes."""
        self.take_snapshot()

    @record_usage.before_loop
    async def before_record_usage(self) -> None:
        """Waits for the bot to be ready before starting the usage recording loop."""
        await self.bot.wait_until_ready()
        # Take startup snapshot
        self.take_snapshot(label="Startup")
        # Wait 5 minutes before starting the regular loop
        await asyncio.sleep(300)

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

            # --- Helper to generate pages ---
            def chunk_list(lst, n):
                for i in range(0, len(lst), n):
                    yield lst[i:i + n]

            # 1. Generate Skill Pages
            skill_pages = []
            if all_skills:
                chunks = list(chunk_list(all_skills, 8))  # 8 skills per page
                for i, chunk in enumerate(chunks):
                    embed = discord.Embed(title="Database: Skills", color=discord.Color.blue())
                    embed.set_footer(text=f"Page {i+1}/{len(chunks)} | Total Skills: {len(all_skills)}")
                    for skill in chunk:
                        user_id = skill['user_id']
                        user_display = f"User {user_id}"
                        # Try to resolve user name if cached
                        user = self.bot.get_user(user_id)
                        if user:
                            user_display = f"{user.name} ({user_id})"

                        embed.add_field(
                            name=f"ID: {skill['id']} | {skill['name']}",
                            value=f"**User:** {user_display}\n**Roll:** `{skill['dice_roll']}`",
                            inline=False
                        )
                    skill_pages.append(embed)

            # 2. Generate Reminder Pages
            reminder_pages = []
            if all_reminders:
                chunks = list(chunk_list(all_reminders, 8))
                for i, chunk in enumerate(chunks):
                    embed = discord.Embed(title="Database: Reminders", color=discord.Color.orange())
                    embed.set_footer(text=f"Page {i+1}/{len(chunks)} | Total Reminders: {len(all_reminders)}")
                    for rem in chunk:
                        user_id = rem['user_id']
                        user_display = f"User {user_id}"
                        user = self.bot.get_user(user_id)
                        if user:
                            user_display = f"{user.name} ({user_id})"

                        embed.add_field(
                            name=f"ID: {rem['id']} | Due: <t:{rem['reminder_time']}:R>",
                            value=f"**User:** {user_display}\n**Msg:** {rem['message'][:50]}...",
                            inline=False
                        )
                    reminder_pages.append(embed)

            # 3. Define Export Callback
            async def export_callback(interaction_ctx):
                bot_name = config.BOT_NAME or "NoName"
                report_lines = [f"--- {bot_name.upper()} DATABASE REPORT ---", f"Generated: {discord.utils.utcnow()}", ""]

                report_lines.append(f"\n--- SKILLS ({len(all_skills)}) ---")
                for s in all_skills:
                    report_lines.append(f"ID: {s['id']} | User: {s['user_id']} | Name: {s['name']} | Roll: {s['dice_roll']} | Type: {s['skill_type']}")

                report_lines.append(f"\n--- REMINDERS ({len(all_reminders)}) ---")
                for r in all_reminders:
                    report_lines.append(f"ID: {r['id']} | User: {r['user_id']} | Time: {r['reminder_time']} | Msg: {r['message']}")

                with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", suffix="_db_report.txt") as f:
                    f.write("\n".join(report_lines))
                    temp_path = f.name

                await interaction_ctx.send("Database report attached:", file=discord.File(temp_path, filename="db_report.txt"))
                os.remove(temp_path)

            # 4. Create Dashboard Embed
            dashboard_embed = discord.Embed(
                title="Admin Dashboard",
                description="Select a category to view database entries.",
                color=discord.Color.dark_grey()
            )
            dashboard_embed.add_field(name="Stats", value=f"Skills: **{len(all_skills)}**\nReminders: **{len(all_reminders)}**")

            # 5. Launch View
            view = DashboardView(ctx, skill_pages, reminder_pages, export_callback, dashboard_embed)
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

                embed = discord.Embed(title=f"Edit Skill #{entry_id}", color=discord.Color.blue())
                embed.add_field(name="1. Name", value=skill['name'], inline=False)
                embed.add_field(name="2. Roll", value=skill['dice_roll'], inline=False)
                embed.add_field(name="3. Description", value=skill['description'] or "None", inline=False)
                embed.add_field(name="4. DELETE", value="⚠️ Delete this skill", inline=False)
                embed.set_footer(text="Select an option to edit.")

                options = {"1️⃣ Name": "1", "2️⃣ Roll": "2", "3️⃣ Desc": "3", "🗑️ Delete": "4"}
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
                    updates['description'] = None if content.lower() == 'none' else content
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

                embed = discord.Embed(title=f"Edit Reminder #{entry_id}", color=discord.Color.orange())
                embed.add_field(name="1. Message", value=rem['message'], inline=False)
                embed.add_field(name="2. Time", value=f"<t:{rem['reminder_time']}:F>", inline=False)
                embed.add_field(name="3. DELETE", value="⚠️ Delete this reminder", inline=False)
                embed.set_footer(text="Select an option to edit.")

                options = {"1️⃣ Message": "1", "2️⃣ Time": "2", "🗑️ Delete": "3"}
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
                    dt = dateparser.parse(msg.content.strip(), settings={'PREFER_DATES_FROM': 'future'})
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
            logging.error(f"Error editing entry {entry_id}: {e}", exc_info=True)
            await ctx.send(f"An error occurred: {e}")

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
        if mode and mode.lower() == "history":
            if not self.usage_history:
                await ctx.send("No historical data recorded yet (updates every 30 mins).")
                return

            lines = [f"{'Timestamp':<25} | {'CPU (%)':<10} | {'RAM (MB)':<10} | {'Label':<10}"]
            lines.append("-" * 65)
            for entry in self.usage_history:
                ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
                label = entry.get('label') or ""
                lines.append(f"{ts:<25} | {entry['cpu']:<10.1f} | {entry['ram']:<10.2f} | {label:<10}")

            with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", suffix="_usage_history.txt") as f:
                f.write("\n".join(lines))
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
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"

        # Cogs
        loaded_cogs = self.bot.extensions.keys()
        total_cogs = len(discover_cogs(config.COGS_PATH))
        cogs_status = f"{len(loaded_cogs)}/{total_cogs}"

        # Resource Usage
        memory_info = self.process.memory_info()
        cpu_usage = self.process.cpu_percent(interval=None)  # Use interval=None for non-blocking call
        ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB

        # Create status embed.
        embed = discord.Embed(
            title=f"{config.BOT_NAME} Status Report",
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
            value=f"**CPU:** `{cpu_usage:.1f}%`\n"
            f"**RAM:** `{ram_usage:.2f} MB`",
            inline=True
        )

        # Add a field for loaded cogs, formatted nicely
        if loaded_cogs:
            # Format cog names by removing 'cogs.' prefix and joining them
            cog_list_str = ", ".join([cog.replace('cogs.', '') for cog in sorted(loaded_cogs)])
            embed.add_field(
                name="Loaded Cogs",
                value=f"```{cog_list_str}```",
                inline=False
            )

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()

        # Update message.
        await message.edit(content=None, embed=embed)
        logging.info(f"Status command used by {ctx.author}.")


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(AdminCog(bot))

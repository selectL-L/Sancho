"""cogs/admin.py

This cog contains owner-only commands for administrative tasks, such as
viewing bot status and managing configurations.
"""

import logging
import os
import tempfile
import time
import typing
from collections import defaultdict
from datetime import timedelta
from typing import List

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.extensions import discover_cogs


class StatusView(discord.ui.View):
    """A view for paginating through a status report.

    Shows skills and reminders for each user.
    """

    def __init__(self, bot: SanchoBot, user_pages: List[discord.Embed], author_id: int):
        """Initializes the StatusView.

        Args:
            bot (SanchoBot): The bot instance.
            user_pages (List[discord.Embed]): The list of embeds to paginate.
            author_id (int): The ID of the user who invoked the command.
        """
        super().__init__(timeout=60.0)
        self.bot = bot
        self.user_pages = user_pages
        self.author_id = author_id
        self.current_page = 0
        self.message: typing.Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the command author can use the buttons.

        Args:
            interaction (discord.Interaction): The interaction to check.

        Returns:
            bool: True if the user is authorized, False otherwise.
        """
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You are not authorized to use these buttons.", ephemeral=True)
            return False
        return True

    async def update_view(self, interaction: discord.Interaction) -> None:
        """Updates the message with the current page's embed.

        Args:
            interaction (discord.Interaction): The interaction to update.
        """
        previous_button = self.children[0]
        if isinstance(previous_button, discord.ui.Button):
            previous_button.disabled = self.current_page == 0

        next_button = self.children[1]
        if isinstance(next_button, discord.ui.Button):
            next_button.disabled = self.current_page == len(self.user_pages) - 1
        
        await interaction.response.edit_message(
            embed=self.user_pages[self.current_page],
            view=self
        )

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.grey)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Handles the previous button click.

        Args:
            interaction (discord.Interaction): The interaction.
            button (discord.ui.Button): The button that was clicked.
        """
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_view(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Handles the next button click.

        Args:
            interaction (discord.Interaction): The interaction.
            button (discord.ui.Button): The button that was clicked.
        """
        if self.current_page < len(self.user_pages) - 1:
            self.current_page += 1
            await self.update_view(interaction)


    async def on_timeout(self) -> None:
        """Handles the view timeout by disabling all buttons."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        # Try to edit the message to disable buttons
        # Note: discord.py 2.x requires storing the message reference
        if hasattr(self, 'message') and self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class AdminCog(BaseCog):
    """Administrative and owner-only commands."""

    def __init__(self, bot: SanchoBot):
        """Initializes the AdminCog.

        Args:
            bot (SanchoBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager = bot.db_manager
        self.process = psutil.Process()
        self.process.cpu_percent() # Initialize for accurate subsequent readings
        self.usage_history = []
        self.record_usage.start()

    async def cog_unload(self) -> None:
        """Cancels the usage recording task when the cog is unloaded."""
        self.record_usage.cancel()

    @tasks.loop(minutes=30)
    async def record_usage(self) -> None:
        """Records CPU and RAM usage every 30 minutes."""
        try:
            memory_info = self.process.memory_info()
            cpu_usage = self.process.cpu_percent(interval=None)
            ram_usage = memory_info.rss / (1024 * 1024)
            timestamp = discord.utils.utcnow()
            self.usage_history.append({
                'timestamp': timestamp,
                'cpu': cpu_usage,
                'ram': ram_usage
            })
        except Exception as e:
            logging.error(f"Error recording usage stats: {e}")

    @record_usage.before_loop
    async def before_record_usage(self) -> None:
        """Waits for the bot to be ready before starting the usage recording loop."""
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="global_limit", hidden=True, description="Set the global skill limit for all users.")
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

    @commands.hybrid_command(name="user_limit", hidden=True, description="Set the skill limit for a specific user.")
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


    @commands.hybrid_command(name="report", hidden=True, description="Display a report of all users' skills and reminders.")
    @commands.is_owner()
    @app_commands.describe(mode="Optional: 'full' to post all embeds, or 'print' to attach a text report.")
    async def report(self, ctx: commands.Context, mode: typing.Optional[str] = None) -> None:
        """Displays a status report of all users' skills and reminders.

        Usage: .report [full|print]

        Args:
            ctx (commands.Context): The command context.
            mode (typing.Optional[str]): 'full' to post all embeds, or 'print' to attach a text report.
        """
        await ctx.send("`Generating status report...`")

        try:
            all_skills = await self.db_manager.get_all_skills()
            all_reminders = await self.db_manager.get_all_reminders()

            user_data = defaultdict(lambda: {"skills": [], "reminders": []})

            for skill in all_skills:
                user_data[skill['user_id']]['skills'].append(skill)
            for reminder in all_reminders:
                user_data[reminder['user_id']]['reminders'].append(reminder)

            if not user_data:
                await ctx.send("No users with skills or reminders found.")
                return

            user_pages = []
            user_ids = sorted(user_data.keys())

            for i, user_id in enumerate(user_ids):
                try:
                    user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                    user_name = f"{user.name} ({user.id})"
                except discord.NotFound:
                    user_name = f"Unknown User ({user_id})"

                embed = discord.Embed(
                    title=f"Status for {user_name}",
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"User {i + 1}/{len(user_ids)}")

                # Add skills to embed
                skills_text = ""
                if user_data[user_id]['skills']:
                    for skill in user_data[user_id]['skills']:
                        aliases = skill.get('aliases')
                        alias_str = f" (aliases: {aliases})" if aliases else ""
                        skills_text += f"**{skill['name']}**: `{skill['dice_roll']}`{alias_str}\n"
                else:
                    skills_text = "No skills found."
                embed.add_field(name="Skills", value=skills_text, inline=False)

                # Add reminders to embed
                reminders_text = ""
                if user_data[user_id]['reminders']:
                    for reminder in user_data[user_id]['reminders']:
                        reminders_text += f"**ID {reminder['id']}**: '{reminder['message']}' @ <t:{reminder['reminder_time']}:f>\n"
                else:
                    reminders_text = "No reminders found."
                embed.add_field(name="Reminders", value=reminders_text, inline=False)
                user_pages.append(embed)

            if not user_pages:
                await ctx.send("Failed to generate report pages.")
                return

            if mode == "full":
                for embed in user_pages:
                    await ctx.send(embed=embed)
                return

            if mode == "print":
                report_lines = []
                for i, user_id in enumerate(user_ids):
                    try:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        user_name = f"{user.name} ({user.id})"
                    except discord.NotFound:
                        user_name = f"Unknown User ({user_id})"
                    report_lines.append(f"Status for {user_name}\n{'='*40}")
                    if user_data[user_id]['skills']:
                        for skill in user_data[user_id]['skills']:
                            aliases = skill.get('aliases')
                            alias_str = f" (aliases: {aliases})" if aliases else ""
                            report_lines.append(f"Skill: {skill['name']} | Dice: {skill['dice_roll']}{alias_str}")
                    else:
                        report_lines.append("No skills found.")
                    if user_data[user_id]['reminders']:
                        for reminder in user_data[user_id]['reminders']:
                            report_lines.append(f"Reminder ID {reminder['id']}: '{reminder['message']}' @ {reminder['reminder_time']}")
                    else:
                        report_lines.append("No reminders found.")
                    report_lines.append("\n")
                with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", suffix="_status_report.txt") as f:
                    f.write("\n".join(report_lines))
                    temp_path = f.name
                await ctx.send("Status report attached:", file=discord.File(temp_path, filename="status_report.txt"))
                os.remove(temp_path)
                return

            # Default: interactive view
            view = StatusView(self.bot, user_pages, ctx.author.id)
            previous_button = view.children[0]
            if isinstance(previous_button, discord.ui.Button):
                previous_button.disabled = True
            if len(user_pages) == 1:
                next_button = view.children[1]
                if isinstance(next_button, discord.ui.Button):
                    next_button.disabled = True
            sent_msg = await ctx.send(embed=user_pages[0], view=view)
            view.message = sent_msg

        except Exception as e:
            logging.error("Error generating status report:", exc_info=True)
            await ctx.send(f"An error occurred while generating the report: {e}")

    @commands.hybrid_command(name="status", hidden=True, description="Provides a comprehensive health and status check for the bot.")
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
            
            lines = [f"{'Timestamp':<25} | {'CPU (%)':<10} | {'RAM (MB)':<10}"]
            lines.append("-" * 50)
            for entry in self.usage_history:
                ts = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"{ts:<25} | {entry['cpu']:<10.1f} | {entry['ram']:<10.2f}")
            
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
        cpu_usage = self.process.cpu_percent(interval=None) # Use interval=None for non-blocking call
        ram_usage = memory_info.rss / (1024 * 1024)  # Convert bytes to MB

        # Create status embed.
        embed = discord.Embed(
            title="Sancho Status Report",
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


async def setup(bot: SanchoBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (SanchoBot): The bot instance.
    """
    await bot.add_cog(AdminCog(bot))
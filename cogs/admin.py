"""
cogs/admin.py

This cog contains owner-only commands for administrative tasks, such as
viewing bot status and managing configurations.
"""
import discord
from discord.ext import commands
from discord import app_commands
import logging
from collections import defaultdict
from typing import List, Dict, Any

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

class StatusView(discord.ui.View):
    """
    A view for paginating through a status report, showing skills and reminders
    for each user.
    """
    def __init__(self, bot: SanchoBot, user_pages: List[discord.Embed], author_id: int):
        super().__init__(timeout=120.0)
        self.bot = bot
        self.user_pages = user_pages
        self.author_id = author_id
        self.current_page = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the command author can use the buttons."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You are not authorized to use these buttons.", ephemeral=True)
            return False
        return True

    async def update_view(self, interaction: discord.Interaction):
        """Updates the message with the current page's embed."""
        
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
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_view(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.user_pages) - 1:
            self.current_page += 1
            await self.update_view(interaction)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stops the view and disables all buttons."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

class AdminCog(BaseCog):
    """
    Administrative and owner-only commands.
    """
    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.bot: SanchoBot = bot
        self.db_manager = self.bot.db_manager

    @commands.command(name="status", hidden=True)
    @commands.is_owner()
    async def status(self, ctx: commands.Context):
        """
        Displays an interactive status report of all users' skills and reminders.
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

            # We pass self.bot here, which is correctly typed as SanchoBot from the cog's __init__
            view = StatusView(self.bot, user_pages, ctx.author.id)
            
            previous_button = view.children[0]
            if isinstance(previous_button, discord.ui.Button):
                previous_button.disabled = True

            if len(user_pages) == 1:
                next_button = view.children[1]
                if isinstance(next_button, discord.ui.Button):
                    next_button.disabled = True

            await ctx.send(embed=user_pages[0], view=view)

        except Exception as e:
            logging.error("Error generating status report:", exc_info=True)
            await ctx.send(f"An error occurred while generating the report: {e}")


async def setup(bot: SanchoBot):
    await bot.add_cog(AdminCog(bot))

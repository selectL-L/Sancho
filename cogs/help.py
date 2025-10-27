"""
cogs/help.py

This cog implements a custom, user-friendly help command that replaces the
default discord.py help command. It is designed to provide clear and useful
information about both standard prefix commands and the bot's natural language
processing (NLP) capabilities.

Key Features:
- Overrides the default `help` command for a custom experience.
- `send_bot_help`: Displays a general overview of all available commands,
  grouped by their respective cogs (e.g., Math, Fun). It also provides a
  dedicated section explaining the NLP commands with varied examples.
- `send_command_help`: Provides detailed information for a specific command,
  including its description, aliases, and usage signature. (Largely redundant, we mostly use NLP)
- Dynamic Prefix Display: Automatically fetches and displays the correct
  command prefixes for the server it's being used in.
"""
import discord
from discord.ext import commands
import logging

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

class Help(BaseCog):
    """A custom, more detailed help command that overrides the default."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        # This is crucial to replace the default help command with our own.
        self.bot.remove_command('help')

    @commands.command(name='help', help="Shows this message.")
    async def custom_help(self, ctx: commands.Context, *, command_name: str | None = None):
        """
        The main help command entry point.
        If a command_name is provided, it shows detailed help for that command.
        Otherwise, it shows a general overview of all commands.
        """
        self.logger.info(f"Help command used by {ctx.author} for command: {command_name or 'general'}")
        if command_name:
            command = self.bot.get_command(command_name)
            # Ensure the command exists and is not hidden from the help menu.
            if command and not command.hidden:
                await self.send_command_help(ctx, command)
            else:
                await ctx.send(f"Sorry, I don't have a command called `{command_name}`.")
        else:
            await self.send_bot_help(ctx)

    async def send_bot_help(self, ctx: commands.Context):
        """Sends a general help embed listing all commands and NLP capabilities."""
        # The command_prefix can be a list or a callable. We need to get the
        # specific prefixes for the current context (server/message).
        prefixes = await self.bot.get_prefix(ctx.message)
        
        # Format prefixes for display, e.g., "`.s`", "`.sancho`"
        formatted_prefixes = ", ".join(f"`{p.strip()}`" for p in prefixes)
        # Get the first prefix to use in examples.
        example_prefix = prefixes[0] if prefixes else ''

        embed = discord.Embed(
            title="Hello, I'm Sancho!",
            description=(
                "I can respond to two kinds of instructions: **standard commands** and **natural language** hopefully the majority will be natural!\n\n"
                f"My standard command prefixes are {formatted_prefixes}. For example, `{example_prefix.strip()} help`."
            ),
            color=discord.Color.gold()
        )

        # Find all cogs that have visible commands to display.
        cogs_with_commands = [
            cog for cog_name, cog in self.bot.cogs.items()
            if cog.get_commands() and cog_name not in ["Help"]  # Exclude the Help cog itself
        ]

        # Add a field for each cog with its list of commands.
        for cog in cogs_with_commands:
            command_list = [f"`{command.name}`" for command in cog.get_commands() if not command.hidden]
            if command_list:
                embed.add_field(
                    name=f"**{cog.qualified_name} Commands!**",
                    value=' '.join(command_list),
                    inline=False
                )
        
        # This section is crucial for explaining the bot's primary functionality. (Giving examples helps a lot)
        embed.add_field(
            name="**Natural Language Commands!**",
            value=(
                "You don't always need a strict command! I can understand requests like these:\n\n"
                "**Reminders**\n"
                "• `Sancho remind me` (starts interactive setup)\n"
                "• `Sancho remind me to check the oven in 15 minutes`\n"
                "• `Sancho set a reminder to walk the dog every day at 8am`\n"
                "• `Sancho show my reminders` or `delete reminder 2`\n\n"
                "**Dice & Math**\n"
                "• `Sancho roll 2d20+5 with advantage`\n"
                "• `Sancho calculate (5 * 10) / 2`\n\n"
                "**Skills**\n"
                "• `Sancho save a new skill` (starts interactive setup)\n"
                "• `Sancho use my fireball skill + 3`\n"
                "• `Sancho list my skills`\n\n"
                "**Images** (You can reply to or attach an image!)\n"
                "• `Sancho resize this image to 50%`\n"
                "• `Sancho convert this image to webp`\n\n"
                "**Fun**\n"
                "• `Sancho 8ball should I have another coffee?`\n"
                "• `Sancho bod` (for the LOR experience)\n"
            ),
            inline=False
        )
        embed.set_footer(text=f"Use `{example_prefix.strip()} help [command]` for more info on a specific standard command.")
        await ctx.send(embed=embed)

    async def send_command_help(self, ctx: commands.Context, command: commands.Command):
        """Sends a detailed help embed for a specific standard command."""
        prefixes = await self.bot.get_prefix(ctx.message)
        example_prefix = prefixes[0] if prefixes else ''

        embed = discord.Embed(
            title=f"Help for: `{command.name}`",
            description=command.help or "No description available.",
            color=discord.Color.green()
        )
        
        # Show command aliases if they exist.
        if command.aliases:
            embed.add_field(name="Aliases", value=", ".join(f"`{alias}`" for alias in command.aliases), inline=False)
        
        # Construct the usage string, including the signature (e.g., <argument>).
        usage = f"{example_prefix.strip()} {command.name}"
        if command.signature:
            usage += f" {command.signature}"
        embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

        await ctx.send(embed=embed)

async def setup(bot: SanchoBot):
    """Standard setup function to add the cog to the bot."""
    await bot.add_cog(Help(bot))

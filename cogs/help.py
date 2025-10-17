import discord
from discord.ext import commands
import asyncio
import logging

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

class Help(BaseCog):
    """A custom, more detailed help command."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.bot.remove_command('help')

    @commands.command(name='help', help="Shows this message.")
    async def custom_help(self, ctx: commands.Context, *, command_name: str | None = None):
        """Shows help for a specific command or an overview of all commands."""
        logging.info(f"Help command used by {ctx.author} for command: {command_name or 'general'}")
        if command_name:
            command = self.bot.get_command(command_name)
            if command and not command.hidden:
                await self.send_command_help(ctx, command)
            else:
                await ctx.send(f"Sorry, I don't have a command called `{command_name}`.")
        else:
            await self.send_bot_help(ctx)

    async def send_bot_help(self, ctx: commands.Context):
        """Sends a general help embed listing all commands."""
        # The command_prefix can be a callable. We need to get the list of prefixes for the current context.
        prefixes = await self.bot.get_prefix(ctx.message)
        
        # Format prefixes for display, e.g., "`.s`", "`.sancho`"
        formatted_prefixes = ", ".join(f"`{p.strip()}`" for p in prefixes)
        # Get the first prefix for the example
        example_prefix = prefixes[0] if prefixes else ''

        embed = discord.Embed(
            title="Sancho Info",
            description=(
                "I'm Sancho, I can understand specific commands and natural language.\n"
                f"My prefixes for commands are {formatted_prefixes}. For example, `{example_prefix.strip()} ping`."
            ),
            color=discord.Color.gold()
        )

        cogs_with_commands = [
            cog for cog_name, cog in self.bot.cogs.items() 
            if cog.get_commands() and cog_name not in ["Help", "NLP"]
        ]

        for cog in cogs_with_commands:
            command_list = [f"`{command.name}`" for command in cog.get_commands() if not command.hidden]
            if command_list:
                embed.add_field(
                    name=f"**{cog.qualified_name} Commands**",
                    value=' '.join(command_list),
                    inline=False
                )
        
        embed.add_field(
            name="**Natural Language Commands**",
            value=(
                "I can also do things when you just talk to me. Try things like:\n"
                "• `Sancho calculate 5+5`\n"
                "• `Sancho roll a d20`\n"
                "• `Sancho 8ball should I have another coffee?`\n"
                "• `Sancho remind me to take out the trash in 1 hour`"
            ),
            inline=False
        )
        embed.set_footer(text=f"Use {example_prefix.strip()} help [command] for more info on a fixed (non-NLP) command.")
        await ctx.send(embed=embed)

    async def send_command_help(self, ctx: commands.Context, command: commands.Command):
        """Sends a detailed help embed for a specific command."""
        prefixes = await self.bot.get_prefix(ctx.message)
        example_prefix = prefixes[0] if prefixes else ''

        embed = discord.Embed(
            title=f"Help: `{command.name}`",
            description=command.help or "No description available.",
            color=discord.Color.green()
        )
        if command.aliases:
            embed.add_field(name="Aliases", value=", ".join(f"`{alias}`" for alias in command.aliases), inline=False)
        
        usage = f"{example_prefix.strip()}{command.name}"
        if command.signature:
            usage += f" {command.signature}"
        embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

        await ctx.send(embed=embed)

async def setup(bot: SanchoBot):
    await bot.add_cog(Help(bot))

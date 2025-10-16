import discord
from discord.ext import commands
import asyncio
from typing import Optional

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.skill_database import SkillDatabase
from cogs.math import Math

class Skills(BaseCog):
    """A cog for creating, managing, and using custom skills with dice rolls."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db = SkillDatabase(bot.db_path)
        self.bot.loop.create_task(self.db.setup_database())

    async def save_skill_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler to initiate the skill saving conversation."""
        
        def check(m: discord.Message):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # 1. Get Skill Name
            await ctx.send("What would you like to name this skill?")
            name_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            skill_name = name_msg.content.strip()

            # 2. Get Aliases
            await ctx.send(f"Got it: `{skill_name}`. What aliases should trigger this skill? (e.g., `smash, big hit, bonk`). You can also say `none`.")
            aliases_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            aliases_raw = aliases_msg.content.strip().lower()
            aliases = []
            if aliases_raw != 'none':
                aliases = [alias.strip() for alias in aliases_raw.split(',')]

            # 3. Get Dice Roll
            await ctx.send(f"What is the dice roll equation for `{skill_name}`? (e.g., `2d8 + 5`, `1d20kh1`)")
            roll_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            dice_roll = roll_msg.content.strip()

            # 4. Get Skill Type
            await ctx.send(f"Is this an `attack` or a `defense` skill?")
            type_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            skill_type = type_msg.content.strip().lower()
            if skill_type not in ['attack', 'defense']:
                await ctx.send("That's not a valid skill type. Please choose `attack` or `defense`. Aborting.")
                return

            # 5. Save to Database
            await self.db.save_skill(ctx.author.id, skill_name, aliases, dice_roll, skill_type)
            await ctx.send(f"✅ Skill saved! You can now use `.sancho skill {skill_name}`.")
            self.logger.info(f"User {ctx.author.id} saved skill '{skill_name}'.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Skill creation cancelled.")
        except Exception as e:
            self.logger.error(f"Error creating skill for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while creating the skill.")

    async def use_skill_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for using a saved skill."""
        # Extract the skill name from the query "skill <name>"
        skill_name = query.split(" ", 1)[-1].strip()
        if not skill_name:
            await ctx.send("You need to tell me which skill to use, like `.sancho skill smash`.")
            return

        skill = await self.db.get_skill(ctx.author.id, skill_name)
        if not skill:
            await ctx.send(f"I couldn't find a skill named or aliased as `{skill_name}` for you.")
            return

        # Get the Math cog to perform the roll
        math_cog: Optional[Math] = self.bot.get_cog('Math') # type: ignore
        if not math_cog:
            self.logger.error("Math cog not found, cannot perform skill roll.")
            await ctx.send("Internal error: The dice rolling module is not available.")
            return

        # Use the Math cog's roll method
        await math_cog.roll(ctx, query=skill['dice_roll'])

        # Add interactive message if replying to another user
        if ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
            target_user = ctx.message.reference.resolved.author
            if target_user != ctx.author and not target_user.bot:
                if skill['skill_type'] == 'attack':
                    await ctx.send(f"{ctx.author.mention} attacked {target_user.mention} with **{skill['name']}**!")
                elif skill['skill_type'] == 'defense':
                    await ctx.send(f"{ctx.author.mention} defended against {target_user.mention} using **{skill['name']}**!")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Skills(bot))

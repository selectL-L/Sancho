import discord
from discord.ext import commands
import asyncio
from typing import Optional
import re

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
from cogs.math import Math, DICE_NOTATION_REGEX, safe_eval_math

class Skills(BaseCog):
    """A cog for creating, managing, and using custom skills with dice rolls."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db: DatabaseManager = bot.db_manager # type: ignore

    def _evaluate_max_roll(self, dice_roll: str) -> int:
        """Evaluates the maximum possible result of a dice expression."""
        # Replace dice notation with max values (e.g., 2d6 -> 2*6)
        def max_dice_value(match: re.Match) -> str:
            num_dice = int(match.group(1) or 1)
            num_sides = int(match.group(2))
            return f"({num_dice * num_sides})"

        try:
            # Replace dice notations with their max possible roll
            expr = DICE_NOTATION_REGEX.sub(max_dice_value, dice_roll)
            # Remove keep/drop modifiers as they don't affect the max of the base dice
            expr = re.sub(r'kh\d+|kl\d+', '', expr)
            # Use the safe eval from the Math cog
            return int(safe_eval_math(expr))
        except Exception as e:
            self.logger.error(f"Could not evaluate max roll for '{dice_roll}': {e}")
            # If evaluation fails, return a high number to be safe.
            return 9999

    async def save_skill_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler to initiate the skill saving conversation."""
        
        user_skills = await self.db.get_user_skills(ctx.author.id)
        current_skills_count = len(user_skills)

        if current_skills_count >= self.db.skill_limit:
            await ctx.send(f"You have reached your skill limit of **{self.db.skill_limit}** skills. Please delete a skill before adding a new one.")
            return

        # --- Pre-gather all existing names and aliases for duplicate checking ---
        existing_names_and_aliases = set()
        for skill in user_skills:
            existing_names_and_aliases.add(skill['name'].lower())
            if skill['aliases']:
                for alias in skill['aliases'].split('|'):
                    if alias.strip():
                        existing_names_and_aliases.add(alias.strip().lower())

        def check(m: discord.Message):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # 1. Get Skill Name
            await ctx.send(f"What would you like to name this skill? You can say `exit` at any time to cancel.\nAfter this one, you will have **{self.db.skill_limit - current_skills_count - 1}** skill slot(s) remaining.")
            name_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            if name_msg.content.strip().lower() == 'exit':
                await ctx.send("Skill creation cancelled.")
                return
            skill_name = name_msg.content.strip()

            if skill_name.lower() == 'list':
                await ctx.send("`list` is a reserved keyword and cannot be used as a skill name. Please try again.")
                return
            
            if skill_name.lower() in existing_names_and_aliases:
                await ctx.send(f"You already have a skill or alias with the name `{skill_name}`. Skill names and aliases must be unique. Aborting.")
                return

            # 2. Get Aliases
            await ctx.send(f"Got it: `{skill_name}`. What aliases should trigger this skill? Please separate them with a `|` (e.g., `smash | big hit | bonk`). You can also say `none`.")
            aliases_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            if aliases_msg.content.strip().lower() == 'exit':
                await ctx.send("Skill creation cancelled.")
                return
            aliases_raw = aliases_msg.content.strip().lower()
            aliases = []
            if aliases_raw != 'none':
                aliases = [alias.strip() for alias in aliases_raw.split('|') if alias.strip()]
                
                # Check for duplicate aliases within the new skill and against existing ones
                newly_added_names = {skill_name.lower()}
                for alias in aliases:
                    if alias in existing_names_and_aliases or alias in newly_added_names:
                        await ctx.send(f"The name or alias `{alias}` is already in use or is duplicated in your input. Aborting.")
                        return
                    newly_added_names.add(alias)

            # 3. Get Dice Roll and validate
            while True:
                await ctx.send(f"What is the dice roll equation for `{skill_name}`? (e.g., `2d8 + 5`, `1d20kh1`)")
                roll_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                if roll_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                dice_roll = roll_msg.content.strip()

                match = DICE_NOTATION_REGEX.search(dice_roll)
                if not match:
                    await ctx.send("That doesn't look like a valid dice roll. Please include a dice notation like `d20` or `2d6`.")
                    continue

                num_dice = int(match.group(1) or 1)
                num_sides = int(match.group(2))

                if num_dice > 10 or num_sides > 40:
                    await ctx.send("Skill rolls are limited to a maximum of **10 dice** and **40 sides**. Please enter a different roll.")
                    continue
                
                max_roll = self._evaluate_max_roll(dice_roll)
                if max_roll > 2000:
                    await ctx.send(f"The maximum possible result of that roll is **{max_roll}**, which exceeds the limit of **2000**. Please enter a different roll.")
                    continue
                
                break # Roll is valid

            # 4. Get Skill Type
            await ctx.send(f"Is this an `attack` or a `defense` skill?")
            type_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
            if type_msg.content.strip().lower() == 'exit':
                await ctx.send("Skill creation cancelled.")
                return
            skill_type = type_msg.content.strip().lower()
            if skill_type not in ['attack', 'defense']:
                await ctx.send("That's not a valid skill type. Please choose `attack` or `defense`. Aborting.")
                return

            # 5. Save to Database
            await self.db.save_skill(ctx.author.id, skill_name, aliases, dice_roll, skill_type)
            
            confirmation_message = f"✅ Skill saved for you! You can now use `.sancho skill {skill_name}`."
            if aliases:
                confirmation_message += f"\nIt can also be called by: `{' | '.join(aliases)}`"

            await ctx.send(confirmation_message)
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

    async def list_skills_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for listing a user's skills."""
        skills = await self.db.get_user_skills(ctx.author.id)
        if not skills:
            await ctx.send("You have no saved skills. Use `.sancho save skill` to create one!")
            return

        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Skills",
            color=discord.Color.blue()
        )
        description = []
        for i, skill in enumerate(skills, 1):
            aliases_display = f" (aliases: {skill['aliases']})" if skill['aliases'] else ""
            description.append(f"**{i}. {skill['name'].title()}**{aliases_display}\n   - Roll: `{skill['dice_roll']}` | Type: `{skill['skill_type']}` | ID: `{skill['id']}`")
        
        embed.description = "\n".join(description)
        embed.set_footer(text=f"You are using {len(skills)}/{self.db.skill_limit} skill slots. Use '.sancho delete skill <number>' to remove one.")
        await ctx.send(embed=embed)

    async def delete_skill_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler for deleting a skill by number."""
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send("Please specify the number of the skill you want to delete. Use `.sancho skill list` to see the numbers.")
            return
        
        skill_num_to_delete = int(match.group(0))
        skills = await self.db.get_user_skills(ctx.author.id)

        if not (1 <= skill_num_to_delete <= len(skills)):
            await ctx.send(f"Invalid number. You only have {len(skills)} skills.")
            return
        
        skill_to_delete = skills[skill_num_to_delete - 1]
        rows_affected = await self.db.delete_skill(ctx.author.id, skill_to_delete['id'])

        if rows_affected > 0:
            await ctx.send(f"✅ Successfully deleted your skill: **{skill_to_delete['name'].title()}**.")
            self.logger.info(f"User {ctx.author.id} deleted skill '{skill_to_delete['name']}'.")
        else:
            await ctx.send("Something went wrong. I couldn't delete that skill.")

    @commands.command(name="setskilllimit")
    @commands.has_permissions(administrator=True)
    async def set_skill_limit_command(self, ctx: commands.Context, limit: int):
        """Sets the maximum number of skills a user can have."""
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        
        await self.db.set_skill_limit(limit)
        await ctx.send(f"✅ The global skill limit has been updated to **{limit}** per user.")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Skills(bot))

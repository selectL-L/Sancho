import discord
from discord.ext import commands
import asyncio
from typing import Optional
import re

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
from .math import Math, DICE_NOTATION_REGEX, COIN_FLIP_REGEX, safe_eval_math

class Skills(BaseCog):
    """A cog for creating, managing, and using custom skills with dice rolls."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.db: DatabaseManager = bot.db_manager # type: ignore

    def _sync_evaluate_max_roll(self, dice_roll: str) -> int:
        """Synchronous evaluation of the maximum possible result of a dice expression."""
        def max_coin_value(match: re.Match) -> str:
            num_coins = int(match.group(1) or 1)
            return f"({num_coins * 1})"

        # Replace dice notation with max values (e.g., 2d6 -> 2*6)
        def max_dice_value(match: re.Match) -> str:
            num_dice = int(match.group(1) or 1)
            num_sides = int(match.group(2))
            return f"({num_dice * num_sides})"

        try:
            # Pre-process the expression to handle 'x' as multiplication
            processed_roll = dice_roll.lower().replace('x', '*')

            # Replace coin notations first
            expr = COIN_FLIP_REGEX.sub(max_coin_value, processed_roll)
            # Then replace dice notations with their max possible roll
            expr = DICE_NOTATION_REGEX.sub(max_dice_value, expr)
            # Remove keep/drop modifiers as they don't affect the max of the base dice
            expr = re.sub(r'kh\d+|kl\d+', '', expr)
            # Use the safe eval from the Math cog
            return int(safe_eval_math(expr))
        except Exception as e:
            self.logger.error(f"Could not evaluate max roll for '{dice_roll}': {e}")
            # If evaluation fails, return a high number to be safe.
            return 9999

    async def _evaluate_max_roll(self, dice_roll: str) -> int:
        """
        Asynchronously evaluates the maximum possible result of a dice expression
        by running the synchronous evaluation in a separate thread.
        """
        return await asyncio.to_thread(self._sync_evaluate_max_roll, dice_roll)

    async def save_skill_nlp(self, ctx: commands.Context, *, query: str):
        """NLP handler to initiate the skill saving conversation."""
        
        user_skills = await self.db.get_user_skills(ctx.author.id)
        current_skills_count = len(user_skills)
        user_skill_limit = await self.db.get_user_skill_limit(ctx.author.id)

        if current_skills_count >= user_skill_limit:
            await ctx.send(f"You have reached your skill limit of **{user_skill_limit}** skills. Please delete a skill before adding a new one.")
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
            skill_name = ""
            while True:
                await ctx.send(f"What would you like to name this skill? You can say `exit` at any time to cancel.\nYou have **{user_skill_limit - current_skills_count}** skill slot(s) remaining.")
                name_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                
                if name_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                
                skill_name = name_msg.content.strip()

                if skill_name.lower() == 'list':
                    await ctx.send("`list` is a reserved keyword and cannot be used as a skill name. Please try again.")
                    continue
                
                if skill_name.lower() in existing_names_and_aliases:
                    await ctx.send(f"You already have a skill or alias with the name `{skill_name}`. Skill names and aliases must be unique. Please try again.")
                    continue
                
                break # Name is valid

            # 2. Get Aliases
            aliases = []
            while True:
                await ctx.send(f"Got it: `{skill_name}`. What aliases should trigger this skill? Please separate them with a `|` (e.g., `smash | big hit | bonk`). You can also say `none`.")
                aliases_msg = await self.bot.wait_for('message', check=check, timeout=20.0)

                if aliases_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                
                aliases_raw = aliases_msg.content.strip().lower()
                is_valid = True
                if aliases_raw != 'none':
                    aliases = [alias.strip() for alias in aliases_raw.split('|') if alias.strip()]
                    
                    newly_added_names = {skill_name.lower()}
                    for alias in aliases:
                        if alias in existing_names_and_aliases or alias in newly_added_names:
                            await ctx.send(f"The name or alias `{alias}` is already in use or is duplicated in your input. Please try again.")
                            is_valid = False
                            break
                        newly_added_names.add(alias)
                
                if is_valid:
                    break

            # 3. Get Dice Roll and validate
            dice_roll = ""
            while True:
                await ctx.send(f"What is the dice roll equation for `{skill_name}`? (e.g., `2d8 + 5`, `1d20kh1`, `4c + 2`)")
                roll_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                if roll_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                dice_roll = roll_msg.content.strip()

                dice_match = DICE_NOTATION_REGEX.search(dice_roll)
                coin_match = COIN_FLIP_REGEX.search(dice_roll)

                if not dice_match and not coin_match:
                    await ctx.send("That doesn't look like a valid dice or coin roll. Please include a notation like `d20`, `2d6`, or `4c`.")
                    continue

                if dice_match:
                    num_dice = int(dice_match.group(1) or 1)
                    num_sides = int(dice_match.group(2))

                    if num_dice > 10 or num_sides > 40:
                        await ctx.send("Skill rolls are limited to a maximum of **10 dice** and **40 sides**. Please enter a different roll.")
                        continue
                
                if coin_match:
                    num_coins_str = coin_match.group(1)
                    num_coins = int(num_coins_str) if num_coins_str else 1
                    if num_coins > 40:
                        await ctx.send("Skill rolls are limited to a maximum of **40 coins**. Please enter a different roll.")
                        continue

                max_roll = await self._evaluate_max_roll(dice_roll)
                if max_roll > 2000:
                    await ctx.send(f"The maximum possible result of that roll is **{max_roll}**, which exceeds the limit of **2000**. Please enter a different roll.")
                    continue
                
                break # Roll is valid

            # 4. Get Skill Type
            skill_type = ""
            while True:
                await ctx.send(f"Is this an `attack` or a `defense` skill?")
                type_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                if type_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                
                skill_type = type_msg.content.strip().lower()
                if skill_type not in ['attack', 'defense']:
                    await ctx.send("That's not a valid skill type. Please choose `attack` or `defense`.")
                    continue
                
                break # Type is valid

            # 5. Save to Database
            await self.db.save_skill(ctx.author.id, skill_name, aliases, dice_roll, skill_type)
            
            confirmation_message = f"✅ Skill saved for you! You can now use `.sancho skill {skill_name}`. Please note your skills are tied to your ID!"
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
        """NLP handler for using a saved skill with potential modifiers."""
        # 1. Get all user skills to find the correct one.
        user_skills = await self.db.get_user_skills(ctx.author.id)
        if not user_skills:
            await ctx.send("You have no saved skills to use. Use `.sancho save skill` to create one.")
            return

        # 2. Find the skill that matches the query.
        # We sort by length of name/alias descending to match "big attack" before "big".
        all_skill_names = []
        for s in user_skills:
            all_skill_names.append(s['name'])
            if s['aliases']:
                all_skill_names.extend(alias.strip() for alias in s['aliases'].split('|') if alias.strip())
        
        # Sort by length, longest first, to ensure more specific names are matched first.
        all_skill_names.sort(key=len, reverse=True)

        found_skill = None
        skill_name_in_query = ""
        rest_of_query = ""

        # The query from NLP dispatcher includes "skill", so we can remove it.
        cleaned_query = query.replace("skill", "", 1).strip()

        for name in all_skill_names:
            # Use word boundaries to avoid partial matches (e.g., 'fire' in 'fireball')
            pattern = r'\b' + re.escape(name) + r'\b'
            match = re.search(pattern, cleaned_query, re.IGNORECASE)
            if match:
                skill_name_in_query = match.group(0)
                # Find the full skill dictionary object
                for s in user_skills:
                    aliases = [alias.strip() for alias in s['aliases'].split('|')] if s['aliases'] else []
                    if s['name'].lower() == name.lower() or name.lower() in aliases:
                        found_skill = s
                        break
                
                # The rest of the query is everything after the skill name
                rest_of_query = cleaned_query[match.end():].strip()
                break
        
        if not found_skill:
            await ctx.send(f"I couldn't find a skill in your query: `{cleaned_query}`. Use `.sancho skill list` to see your skills.")
            return

        # 3. Get the Math cog to perform the roll.
        math_cog: Optional[Math] = self.bot.get_cog('Math') # type: ignore
        if not math_cog:
            self.logger.error("Math cog not found, cannot perform skill roll.")
            await ctx.send("Internal error: The dice rolling module is not available.")
            return

        # 4. Construct the final roll query and pass it to the Math cog.
        # The skill's dice roll must be wrapped in parentheses to ensure correct order of operations.
        final_roll_query = f"({found_skill['dice_roll']}) {rest_of_query}"
        
        self.logger.info(f"Executing skill '{found_skill['name']}' for {ctx.author.id}. Original query: '{query}', constructed roll: '{final_roll_query}'")

        # Use the Math cog's roll method, passing skill info for custom formatting
        await math_cog.roll(ctx, query=final_roll_query, skill_info=found_skill)

        # The response formatting is now handled entirely within the Math cog.

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
        
        skill_fields = []
        for i, skill in enumerate(skills, 1):
            name = f"**{i}. {skill['name'].title()}**"
            value = []
            if skill['aliases']:
                value.append(f"(aliases: {skill['aliases']})")
            value.append(f"**Roll:** `{skill['dice_roll']}` | **Type:** `{skill['skill_type']}` | **ID:** `{skill['id']}`")
            skill_fields.append({"name": name, "value": "\n".join(value), "inline": False})

        # Use a more compact description or add fields
        if len(skills) <= 5: # Use fields for shorter lists
            for field in skill_fields:
                embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
        else: # Use description for longer lists to save space
            description = []
            for field in skill_fields:
                description.append(f"{field['name']}\n{field['value']}")
            embed.description = "\n\n".join(description)

        user_skill_limit = await self.db.get_user_skill_limit(ctx.author.id)
        embed.set_footer(text=f"You are using {len(skills)}/{user_skill_limit} skill slots. Use '.sancho delete skill <id>' to remove one.")
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

    @commands.command(name="setskilllimit", hidden=True)
    @commands.has_permissions(administrator=True)
    async def set_skill_limit_command(self, ctx: commands.Context, limit: int):
        """Sets the maximum number of skills a user can have."""
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        
        await self.db.set_skill_limit(limit)
        await ctx.send(f"✅ The global skill limit has been updated to **{limit}** per user.")

    @commands.command(name="setuserskilllimit", hidden=True)
    @commands.has_permissions(administrator=True)
    async def set_user_skill_limit_command(self, ctx: commands.Context, user: discord.Member, limit: int):
        """Sets the maximum number of skills a specific user can have."""
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        
        await self.db.set_user_skill_limit(user.id, limit)
        await ctx.send(f"✅ {user.mention}'s skill limit has been updated to **{limit}**.")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Skills(bot))

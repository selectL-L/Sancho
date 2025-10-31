"""
cogs/skills.py

This cog manages the creation, use, and storage of user-defined "skills".
Skills are essentially named shortcuts for complex dice rolls or calculations.
This allows users to save a formula like "2d6+5" as "fireball" and then
execute it later with a simple command, even adding new modifiers on the fly.

Key Features:
- Interactive Skill Creation: A guided, conversational process to name a skill,
  define its formula, set aliases, and categorize it (e.g., 'attack').
- Formula Validation: Ensures that the dice roll formulas are valid and within
  safe limits (e.g., not too many dice) to prevent abuse.
- Database Integration: All skills are stored in the database, linked to the
  user's ID, making them persistent across sessions.
- Dynamic Skill Usage: When a user invokes a skill, this cog parses the command,
  retrieves the formula, applies any additional modifiers from the user's message
  (e.g., "fireball + 2"), and then passes the final expression to the Math cog
  for evaluation.
- Alias System: Skills can be given multiple names (aliases) for more flexible
  and natural invocation.
"""
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
    """
    The cog for creating, managing, and using custom user-defined skills.
    """

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        self.bot: SanchoBot = bot
        self.db_manager: DatabaseManager = self.bot.db_manager

    def _sync_evaluate_max_roll(self, dice_roll: str) -> int:
        """
        Synchronously evaluates the maximum possible result of a dice expression.
        This is a helper function designed to be run in a separate thread to avoid
        blocking the bot's main event loop. It replaces dice notation (e.g., 2d6)
        with their maximum possible value (e.g., 2*6) and then safely evaluates
        the resulting mathematical expression.

        Args:
            dice_roll (str): The dice roll string to evaluate.

        Returns:
            int: The maximum possible integer result of the roll. Returns 9999 on failure.
        """
        def max_coin_value(match: re.Match) -> str:
            """Replaces coin flip notation (e.g., 4c) with its max value (4*1)."""
            num_coins = int(match.group(1) or 1)
            return f"({num_coins * 1})"

        def max_dice_value(match: re.Match) -> str:
            """Replaces dice notation (e.g., 2d6) with its max value (2*6)."""
            num_dice = int(match.group(1) or 1)
            num_sides = int(match.group(2))
            return f"({num_dice * num_sides})"

        try:
            # Pre-process for 'x' as a multiplier and standardize to lowercase.
            processed_roll = dice_roll.lower().replace('x', '*')

            # Replace coin and dice notations with their maximum values.
            expr = COIN_FLIP_REGEX.sub(max_coin_value, processed_roll)
            expr = DICE_NOTATION_REGEX.sub(max_dice_value, expr)
            # Remove keep/drop modifiers as they don't affect the max of the base dice.
            expr = re.sub(r'kh\d+|kl\d+', '', expr)
            
            # Use the safe evaluation function from the Math cog.
            return int(safe_eval_math(expr))
        except Exception as e:
            self.logger.error(f"Could not evaluate max roll for '{dice_roll}': {e}")
            # Return a high number as a safe fallback if evaluation fails.
            return 9999

    async def _evaluate_max_roll(self, dice_roll: str) -> int:
        """
        Asynchronously evaluates the maximum possible result of a dice expression
        by running the synchronous evaluation in a separate thread. This prevents
        potentially complex calculations from blocking the bot's event loop.
        """
        return await asyncio.to_thread(self._sync_evaluate_max_roll, dice_roll)

    async def save_skill_nlp(self, ctx: commands.Context, *, query: str):
        """
        Initiates an interactive conversation with the user to create and save a new skill.
        This function guides the user through several steps:
        1.  Checking if they have available skill slots.
        2.  Naming the skill and ensuring the name is unique.
        3.  Adding optional aliases.
        4.  Defining and validating the dice roll formula.
        5.  Categorizing the skill type (e.g., 'attack').
        6.  Saving the completed skill to the database.
        """
        
        user_skills = await self.db_manager.get_user_skills(ctx.author.id)
        current_skills_count = len(user_skills)
        user_skill_limit = await self.db_manager.get_user_skill_limit(ctx.author.id)

        if current_skills_count >= user_skill_limit:
            await ctx.send(f"You have reached your skill limit of **{user_skill_limit}** skills. Please delete a skill before adding a new one.")
            return

        # Pre-gather all existing names and aliases for efficient duplicate checking.
        existing_names_and_aliases = set()
        for skill in user_skills:
            existing_names_and_aliases.add(skill['name'].lower())
            if skill['aliases']:
                for alias in skill['aliases'].split('|'):
                    if alias.strip():
                        existing_names_and_aliases.add(alias.strip().lower())

        def check(m: discord.Message):
            """A check function for message waits, ensuring the message is from the original author."""
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # --- Step 1: Get Skill Name ---
            skill_name = ""
            while True:
                await ctx.send(f"What would you like to name this skill? You can say `exit` at any time to cancel.\nYou have **{user_skill_limit - current_skills_count}** skill slot(s) remaining.")
                name_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                
                if name_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                
                skill_name = name_msg.content.strip()

                # Prevent using reserved keywords.
                if skill_name.lower() == 'list':
                    await ctx.send("`list` is a reserved keyword and cannot be used as a skill name. Please try again.")
                    continue
                
                # Check for uniqueness.
                if skill_name.lower() in existing_names_and_aliases:
                    await ctx.send(f"You already have a skill or alias with the name `{skill_name}`. Skill names and aliases must be unique. Please try again.")
                    continue
                
                break # Name is valid and unique.

            # --- Step 2: Get Aliases ---
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
                    
                    # Check for duplicates within the input and against existing names.
                    newly_added_names = {skill_name.lower()}
                    for alias in aliases:
                        if alias in existing_names_and_aliases or alias in newly_added_names:
                            await ctx.send(f"The name or alias `{alias}` is already in use or is duplicated in your input. Please try again.")
                            is_valid = False
                            break
                        newly_added_names.add(alias)
                
                if is_valid:
                    break # Aliases are valid.

            # --- Step 3: Get Dice Roll and Validate ---
            dice_roll = ""
            while True:
                await ctx.send(f"What is the dice roll equation for `{skill_name}`? (e.g., `2d8 + 5`, `1d20kh1`, `4c + 2`)")
                roll_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                if roll_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                dice_roll = roll_msg.content.strip()

                # Basic validation to ensure it contains dice or coin notation.
                dice_match = DICE_NOTATION_REGEX.search(dice_roll)
                coin_match = COIN_FLIP_REGEX.search(dice_roll)

                if not dice_match and not coin_match:
                    await ctx.send("That doesn't look like a valid dice or coin roll. Please include a notation like `d20`, `2d6`, or `4c`.")
                    continue

                # Validate against complexity limits to prevent abuse.
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

                # Validate the maximum possible outcome to prevent excessively large numbers.
                max_roll = await self._evaluate_max_roll(dice_roll)
                if max_roll > 2000:
                    await ctx.send(f"The maximum possible result of that roll is **{max_roll}**, which exceeds the limit of **2000**. Please enter a different roll.")
                    continue
                
                break # Roll is valid.

            # --- Step 4: Get Skill Type ---
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
                
                break # Type is valid.

            # --- Step 5: Save to Database ---
            await self.db_manager.save_skill(ctx.author.id, skill_name, aliases, dice_roll, skill_type)
            
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
        """
        Handles the NLP intent for using a saved skill. This function:
        1.  Fetches all of the user's skills from the database.
        2.  Parses the user's query to identify which skill is being invoked. It prioritizes
            longer, more specific names to resolve ambiguity (e.g., "big attack" vs. "attack").
        3.  Identifies any additional modifiers in the query (e.g., "+ 5", "- 1d4").
        4.  Constructs a final dice roll expression by combining the skill's base formula
            with the modifiers.
        5.  Delegates the actual roll and response formatting to the `Math` cog.
        """
        # 1. Fetch all user skills.
        user_skills = await self.db_manager.get_user_skills(ctx.author.id)
        if not user_skills:
            await ctx.send("You have no saved skills to use. Use `.sancho save skill` to create one.")
            return

        # 2. Create a sorted list of all names and aliases, from longest to shortest.
        # This ensures that more specific names (e.g., "heavy slash") are matched before
        # less specific ones (e.g., "slash").
        all_skill_names = []
        for s in user_skills:
            all_skill_names.append(s['name'])
            if s['aliases']:
                all_skill_names.extend(alias.strip() for alias in s['aliases'].split('|') if alias.strip())
        all_skill_names.sort(key=len, reverse=True)

        found_skill = None
        rest_of_query = ""

        # The query from the NLP dispatcher includes "skill", which we can remove for cleaner parsing.
        cleaned_query = query.replace("skill", "", 1).strip()

        # 3. Find the skill in the query.
        for name in all_skill_names:
            # Use word boundaries to prevent partial matches (e.g., matching 'fire' in 'fireball').
            pattern = r'\b' + re.escape(name) + r'\b'
            match = re.search(pattern, cleaned_query, re.IGNORECASE)
            if match:
                # Find the full skill dictionary object corresponding to the matched name/alias.
                for s in user_skills:
                    aliases = [alias.strip() for alias in s['aliases'].split('|')] if s['aliases'] else []
                    if s['name'].lower() == name.lower() or name.lower() in aliases:
                        found_skill = s
                        break
                
                # The rest of the query contains any modifiers (e.g., "+ 5").
                rest_of_query = cleaned_query[match.end():].strip()
                break
        
        if not found_skill:
            await ctx.send(f"I couldn't find a skill in your query: `{cleaned_query}`. Use `.sancho skill list` to see your skills.")
            return

        # 4. Get the Math cog to perform the roll.
        math_cog: Optional[Math] = self.bot.get_cog('Math') # type: ignore
        if not math_cog:
            self.logger.error("Math cog not found, cannot perform skill roll.")
            await ctx.send("Internal error: The dice rolling module is not available.")
            return

        # 5. Construct the final roll query and delegate to the Math cog.
        # The skill's base roll is wrapped in parentheses to ensure correct order of operations
        # when modifiers are added. Example: (2d6+2) + 5
        final_roll_query = f"({found_skill['dice_roll']}) {rest_of_query}"
        
        self.logger.info(f"Executing skill '{found_skill['name']}' for {ctx.author.id}. Original query: '{query}', constructed roll: '{final_roll_query}'")

        # The Math cog's roll method handles the calculation and response formatting.
        # We pass `skill_info` so the response can be customized with the skill's name.
        await math_cog.roll(ctx, query=final_roll_query, skill_info=found_skill)

    async def list_skills_nlp(self, ctx: commands.Context, *, query: str):
        """
        Handles the NLP intent for listing all of a user's saved skills.
        It formats the skills into a clean, readable embed, showing the name,
        aliases, roll formula, type, and a unique ID for deletion.
        """
        skills = await self.db_manager.get_user_skills(ctx.author.id)
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

        # Use a more compact description for long lists to avoid a cluttered embed.
        if len(skills) <= 5: # Use fields for shorter lists.
            for field in skill_fields:
                embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
        else: # Use the description for longer lists.
            description = []
            for field in skill_fields:
                description.append(f"{field['name']}\n{field['value']}")
            embed.description = "\n\n".join(description)

        user_skill_limit = await self.db_manager.get_user_skill_limit(ctx.author.id)
        embed.set_footer(text=f"You are using {len(skills)}/{user_skill_limit} skill slots. Use '.sancho delete skill <id>' to remove one.")
        await ctx.send(embed=embed)

    async def delete_skill_nlp(self, ctx: commands.Context, *, query: str):
        """
        Handles the NLP intent for deleting a skill. It parses the skill's number
        from the query, confirms it's a valid skill, and removes it from the database.
        """
        # Find the number in the query (e.g., "delete skill 3").
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send("Please specify the number of the skill you want to delete. Use `.sancho skill list` to see the numbers.")
            return
        
        skill_num_to_delete = int(match.group(0))
        skills = await self.db_manager.get_user_skills(ctx.author.id)

        # Validate the provided number against the user's actual skill list.
        if not (1 <= skill_num_to_delete <= len(skills)):
            await ctx.send(f"Invalid number. You only have {len(skills)} skills.")
            return
        
        skill_to_delete = skills[skill_num_to_delete - 1]
        rows_affected = await self.db_manager.delete_skill(ctx.author.id, skill_to_delete['id'])

        if rows_affected > 0:
            await ctx.send(f"✅ Successfully deleted your skill: **{skill_to_delete['name'].title()}**.")
            self.logger.info(f"User {ctx.author.id} deleted skill '{skill_to_delete['name']}'.")
        else:
            await ctx.send("Something went wrong. I couldn't delete that skill.")

    @commands.command(name="setskilllimit", hidden=True)
    @commands.has_permissions(administrator=True)
    async def set_skill_limit_command(self, ctx: commands.Context, limit: int):
        """Admin command to set the global maximum number of skills a user can have."""
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        
        await self.db_manager.set_skill_limit(limit)
        await ctx.send(f"✅ The global skill limit has been updated to **{limit}** per user.")

    @commands.command(name="setuserskilllimit", hidden=True)
    @commands.has_permissions(administrator=True)
    async def set_user_skill_limit_command(self, ctx: commands.Context, user: discord.Member, limit: int):
        """Admin command to set the skill limit for a specific user."""
        if not (0 < limit <= 100):
            await ctx.send("Please provide a limit between 1 and 100.")
            return
        
        await self.db_manager.set_user_skill_limit(user.id, limit)
        await ctx.send(f"✅ {user.mention}'s skill limit has been updated to **{limit}**.")

async def setup(bot: SanchoBot) -> None:
    """Standard setup function to add the cog to the bot."""
    await bot.add_cog(Skills(bot))

"""cogs/skills.py

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

import asyncio
import re
from typing import Any, Dict, Optional, Tuple, cast

import discord
from discord.ext import commands

import config
from utils.base_cog import BaseCog
from utils.bot_class import CoreBot
from utils.database import DatabaseManager
from utils.views import get_selection
from cogs.calc import Math, DiceLexer, DiceParser, DiceToken


class MaxDiceParser(DiceParser):
    """A specialized parser that calculates the maximum possible result of a dice expression."""

    async def _roll_dice(self, dice_str: str) -> int:
        # Re-parse to get components
        match = re.match(r'(\d+)?d(\d+)(kh|kl)?(\d+)?', dice_str, re.IGNORECASE)
        if not match:
            return 0

        num_dice_str, num_sides_str = match.group(1), match.group(2)
        num_dice = int(num_dice_str) if num_dice_str else 1
        num_sides = int(num_sides_str)

        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        # Calculate max possible value
        if keep_mode == 'kh' and keep_count > 0:
            # Max is keeping the highest N dice, all max value
            count_to_sum = min(num_dice, keep_count)
            return count_to_sum * num_sides
        elif keep_mode == 'kl' and keep_count > 0:
            # Max is keeping the lowest N dice.
            # To get the MAX possible result with 'keep lowest', we assume
            # the dice rolled as high as possible such that the 'lowest' are still high.
            # e.g. 4d6kl3 -> max is 6,6,6,6 -> keep 6,6,6 -> 18.
            count_to_sum = min(num_dice, keep_count)
            return count_to_sum * num_sides
        else:
            return num_dice * num_sides

    async def _flip_coin(self, coin_str: str) -> int:
        match = re.match(r'(\d*)c', coin_str, re.IGNORECASE)
        if not match:
            return 0
        num_coins_str = match.group(1)
        num_coins = int(num_coins_str) if num_coins_str else 1
        return num_coins

    def _apply_clamp(self, value: float, suffix: str, context_str: str = "") -> float:
        # We use the standard clamp logic, but since 'value' is the MAX possible roll,
        # applying the clamp to it correctly simulates the max possible outcome.
        # Even if min_val > value (e.g. max roll is 5, but min is 10),
        # the clamp logic `max(min_val, value)` will return 10, which is correct.
        return super()._apply_clamp(value, suffix, context_str)


class Skills(BaseCog):
    """The cog for creating, managing, and using custom user-defined skills."""

    def __init__(self, bot: CoreBot):
        """Initializes the Skills cog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager

    async def _validate_roll_logic(self, dice_roll: str) -> Tuple[bool, str]:
        """Validates a dice roll string against several criteria.

        Checks complexity and max value using the Lexer and MaxDiceParser.

        Args:
            dice_roll (str): The dice roll string to validate.

        Returns:
            Tuple[bool, str]: A tuple containing:
            - bool: True if the roll is valid, False otherwise.
            - str: A detailed error message if invalid, or an empty string if valid.
        """
        # Define limits
        max_dice_limit = 40
        max_sides_limit = 100
        max_coins_limit = 80
        max_total_roll_limit = 5000

        try:
            lexer = DiceLexer(dice_roll)
        except Exception as e:
            self.logger.debug(f"Roll validation parse error: {e}")
            return False, "Invalid syntax."

        # 1. Check Limits via Tokens
        error_messages = []
        has_dice_or_coin = False

        for token in lexer.tokens:
            if token.type == DiceToken.DICE:
                has_dice_or_coin = True
                match = re.match(r'(\d+)?d(\d+)', token.raw, re.IGNORECASE)
                if match:
                    num_dice = int(match.group(1) or 1)
                    num_sides = int(match.group(2))
                    if num_dice > max_dice_limit:
                        error_messages.append(f"exceeds the **{max_dice_limit}** dice limit")
                    if num_sides > max_sides_limit:
                        error_messages.append(f"exceeds the **{max_sides_limit}** sides limit")

            elif token.type == DiceToken.COIN:
                has_dice_or_coin = True
                match = re.match(r'(\d*)c', token.raw, re.IGNORECASE)
                if match:
                    num_coins = int(match.group(1) or 1)
                    if num_coins > max_coins_limit:
                        error_messages.append(f"exceeds the **{max_coins_limit}** coins limit")

        if not has_dice_or_coin:
            return False, "That doesn't look like a valid dice or coin roll. Please include a notation like `d20`, `2d6`, or `4c`."

        if error_messages:
            unique_errors = sorted(set(error_messages))
            error_summary = ", ".join(unique_errors)
            return False, (
                f"Your roll {error_summary}. "
                f"The limits are: **{max_dice_limit}** dice, **{max_sides_limit}** sides, **{max_coins_limit}** coins, "
                f"and a max total roll value of **{max_total_roll_limit}**."
            )

        # 2. Calculate Max Possible Roll
        try:
            # Re-initialize lexer for parsing since iteration consumed it
            lexer = DiceLexer(dice_roll)
            parser = MaxDiceParser(lexer)
            max_roll = await parser.parse()

            if max_roll > max_total_roll_limit:
                return False, f"The maximum possible result of that roll is **{int(max_roll)}**, which exceeds the limit of **{max_total_roll_limit}**."

        except Exception as e:
            self.logger.warning(f"Validation failed for '{dice_roll}': {e}")
            return False, "Invalid formula syntax."

        return True, ""

    async def save_skill_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Initiates an interactive conversation to create and save a new skill.

        This function guides the user through several steps:
        1.  Checking if they have available skill slots.
        2.  Naming the skill and ensuring the name is unique.
        3.  Adding optional aliases.
        4.  Defining and validating the dice roll formula.
        5.  Categorizing the skill type (e.g., 'attack').
        6.  Saving the completed skill to the database.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """

        user_skills = await self.db_manager.get_user_skills(ctx.author.id)
        current_skills_count = len(user_skills)
        user_skill_limit = await self.db_manager.get_user_skill_limit(ctx.author.id)

        if current_skills_count >= user_skill_limit:
            await ctx.send(f"You have reached your skill limit of **{user_skill_limit}** skills. Please delete a skill before adding a new one.")
            return

        # Cache names for duplicate checking.
        existing_names_and_aliases = set()
        for skill in user_skills:
            existing_names_and_aliases.add(skill['name'].lower())
            if skill['aliases']:
                for alias in skill['aliases'].split('|'):
                    if alias.strip():
                        existing_names_and_aliases.add(alias.strip().lower())

        def check(m: discord.Message) -> bool:
            """Verify message author."""
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # --- Step 1: Get Skill Name ---
            skill_name = ""
            while True:
                await ctx.send(
                    f"What would you like to name this skill? You can say `exit` at any time to cancel this process.\n"
                    f"You have **{user_skill_limit - current_skills_count}** skill slot(s) remaining."
                )
                name_msg = await self.bot.wait_for('message', check=check, timeout=45.0)

                if name_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return

                skill_name = name_msg.content.strip()

                if skill_name.lower() in existing_names_and_aliases:
                    await ctx.send(f"You already have a skill or alias with the name `{skill_name}`. Skill names and aliases must be unique. Please try again.")
                    continue

                break

            # --- Step 2: Get Aliases ---
            aliases = []
            while True:
                await ctx.send(
                    f"Got it: `{skill_name}`. What aliases should trigger this skill? "
                    f"Please separate them with a `|` (e.g., `smash | big hit | bonk`). You can also say `none`."
                )
                aliases_msg = await self.bot.wait_for('message', check=check, timeout=60.0)

                if aliases_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return

                aliases_raw = aliases_msg.content.strip()
                is_valid = True
                if aliases_raw.lower() != 'none':
                    aliases = [alias.strip() for alias in aliases_raw.split('|') if alias.strip()]

                    # Check for duplicates.
                    newly_added_names = {skill_name.lower()}
                    for alias in aliases:
                        if alias.lower() in existing_names_and_aliases or alias.lower() in newly_added_names:
                            await ctx.send(f"The name or alias `{alias}` is already in use or is duplicated in your input. Please try again.")
                            is_valid = False
                            break
                        newly_added_names.add(alias.lower())

                if is_valid:
                    break

            # --- Step 3: Get Dice Roll and Validate ---
            dice_roll = ""
            while True:
                await ctx.send(f"What is the dice roll equation for `{skill_name}`? (e.g., `2d8 + 5`, `4d20kh1`, `4c + 2`)")
                roll_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                if roll_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return
                dice_roll = roll_msg.content.strip()

                is_valid, error_message = await self._validate_roll_logic(dice_roll)
                if not is_valid:
                    await ctx.send(error_message)
                    continue

                break

            # --- Step 4: Get Skill Type ---
            skill_type = ""
            while True:
                await ctx.send("Is this an `attack` or a `defense` skill?")
                type_msg = await self.bot.wait_for('message', check=check, timeout=20.0)
                if type_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return

                skill_type = type_msg.content.strip().lower()
                if skill_type not in ['attack', 'defense']:
                    await ctx.send("That's not a valid skill type. Please choose `attack` or `defense`.")
                    continue

                break

            # --- Step 5: Get Description ---
            description = None
            while True:
                await ctx.send("Would you like to add a description? (max 400 chars). Reply with your description or `none` to skip.")
                desc_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                if desc_msg.content.strip().lower() == 'exit':
                    await ctx.send("Skill creation cancelled.")
                    return

                raw_desc = desc_msg.content.strip()
                if raw_desc.lower() == 'none':
                    description = None
                else:
                    if len(raw_desc) > 400:
                        await ctx.send(f"Description is too long ({len(raw_desc)}/400 chars). Please try again.")
                        continue
                    description = raw_desc
                break

            # --- Step 6: Save to Database ---
            await self.db_manager.save_skill(ctx.author.id, skill_name, aliases, dice_roll, skill_type, description)

            confirmation_message = f"✅ Skill saved for you! You can now use `.{config.BOT_NAME} cast {skill_name}`. Please note your skills are tied to your ID!"
            if aliases:
                confirmation_message += f"\nIt can also be called by: `{' | '.join(aliases)}`"

            await ctx.send(confirmation_message)
            self.logger.info(f"User {ctx.author.id} saved skill '{skill_name}'.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond! Skill creation cancelled...")
        except Exception as e:
            self.logger.error(f"Error creating skill for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while creating the skill.")

    async def use_skill_nlp(self, ctx: commands.Context, query: str) -> None:
        """Handles the NLP intent for using a saved skill.

        This function:
        1.  Fetches all of the user's skills from the database.
        2.  Parses the user's query to identify which skill is being invoked. It prioritizes
            longer, more specific names to resolve ambiguity (e.g., "big attack" vs. "attack").
        3.  Identifies any additional modifiers in the query (e.g., "+ 5", "- 1d4").
        4.  Constructs a final dice roll expression by combining the skill's base formula
            with the modifiers.
        5.  Delegates the actual roll and response formatting to the `Math` cog.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        user_skills = await self.db_manager.get_user_skills(ctx.author.id)
        if not user_skills:
            await ctx.send(f"You have no saved skills to use. Use `.{config.BOT_NAME} save skill` to create one.")
            return

        # Sort names by length for matching.
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

        # List of NLP trigger phrases. (Keep in sync with NLP dispatcher logic.)
        trigger_words = ["cast", "skill", "use"]

        temp_query = query.strip().lower()
        cleaned_query = ""
        for word in trigger_words:
            # Check for trigger word.
            if temp_query.startswith(word):
                if len(temp_query) == len(word) or temp_query[len(word)].isspace():
                    cleaned_query = query.strip()[len(word):].lstrip()
                    break

        if not cleaned_query:
            await ctx.send(f"You didn't specify a skill. Try `{ctx.prefix}cast <skill_name>`.")
            return

        # Match query to skill.
        # Handle multiple trigger words.
        while True:
            for name in all_skill_names:
                # Match name with word boundary.
                # (like a space, or the end of the string) or a non-word character that is
                # part of the name itself. This handles names with punctuation like "attack!".
                match = re.match(r'^' + re.escape(name) + r'(?=\b|\s|$)', cleaned_query, re.IGNORECASE)
                if match:
                    # Find matching skill object.
                    for s in user_skills:
                        aliases = [alias.strip().lower() for alias in s['aliases'].split('|')] if s['aliases'] else []
                        if s['name'].lower() == name.lower() or name.lower() in aliases:
                            found_skill = s
                            break

                    # Extract modifiers.
                    rest_of_query = cleaned_query[len(name):].strip()
                    break

            if found_skill:
                break

            # Retry stripping trigger word.
            stripped_again = False
            temp_search = cleaned_query.lower()
            for word in trigger_words:
                if temp_search.startswith(word):
                    if len(temp_search) == len(word) or temp_search[len(word)].isspace():
                        cleaned_query = cleaned_query[len(word):].lstrip()
                        stripped_again = True
                        break

            if not stripped_again or not cleaned_query:
                break

        if not found_skill:
            await ctx.send(f"I couldn't find the skill: `{cleaned_query}`. Use `{ctx.prefix}list skills` to see your available skills.")
            return

        math_cog: Optional[Math] = cast(Optional[Math], self.bot.get_cog('Math'))
        if not math_cog:
            self.logger.error("Math cog not found, cannot perform skill roll.")
            await ctx.send("Internal error: The dice rolling module is not available.")
            return

        # Construct final roll.
        # Wrap base roll in parens.
        # when modifiers are added. Example: (2d6+2) + 5
        final_roll_query = f"({found_skill['dice_roll']}) {rest_of_query}"

        # Delegate to Math cog for evaluation, but handle formatting here.
        try:
            result_data = await math_cog.evaluate_roll(final_roll_query)
        except (ValueError, TypeError, SyntaxError) as e:
            self.logger.debug(f"Skill roll failed for {ctx.author.id}: {e}")
            await ctx.send(f"Error executing skill: {e}")
            return

        result_display = result_data['total']
        self.logger.info(f"Skill '{found_skill['name']}' executed for {ctx.author.id}: result={result_display}, roll='{final_roll_query}'")
        roll_descriptions = result_data['breakdown']

        # Format the response
        response_parts = []

        if rest_of_query:
            display_formula = f"({found_skill['dice_roll']}) {rest_of_query}"
        else:
            display_formula = found_skill['dice_roll']

        # Handle reply targets.
        if ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
            target_user = ctx.message.reference.resolved.author
            if target_user != ctx.author and not target_user.bot:
                if found_skill['skill_type'] == 'attack':
                    header = f"{ctx.author.mention} attacked {target_user.mention} with **{found_skill['name']}**"
                else:  # defense
                    header = f"{ctx.author.mention} defended against {target_user.mention} with **{found_skill['name']}**"
                response_parts.append(header)
                response_parts.append(f"`{display_formula}`")

        # Handle untargeted skills.
        if not response_parts:
            response_parts.append(f"**{found_skill['name']}**")
            response_parts.append(f"-# `{display_formula}`")

        response_parts.append(f"{ctx.author.mention}, you rolled: **{result_display}**")

        # Add small text prefix to each breakdown line
        formatted_breakdown = [f"-# {line}" for line in roll_descriptions]
        response_parts.extend(formatted_breakdown)

        if found_skill['description']:
            response_parts.append(f"-----------\n{found_skill['description']}")

        response = "\n".join(response_parts)
        if len(response) > 3500:
            await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
            return
        await ctx.send(response)

    async def edit_skill_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Initiates an interactive conversation to edit an existing skill.

        The user can choose to edit the skill's name, aliases, dice roll, or type.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send(f"Please specify the number of the skill you want to edit. Use `.{config.BOT_NAME} skills` to see your list.")
            return

        try:
            skill_num_to_edit = int(match.group(0))
        except (ValueError, IndexError):
            await ctx.send("Invalid skill number provided.")
            return

        user_skills = await self.db_manager.get_user_skills(ctx.author.id)

        if not (1 <= skill_num_to_edit <= len(user_skills)):
            await ctx.send(f"Invalid number. You only have {len(user_skills)} skills.")
            return

        skill_to_edit = user_skills[skill_num_to_edit - 1]

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            embed = discord.Embed(
                title=f"Edit Skill: {skill_to_edit['name']}",
                description="What would you like to edit?",
                color=discord.Color.blue()
            )
            embed.add_field(name="1. Name", value=skill_to_edit['name'], inline=False)
            embed.add_field(name="2. Aliases", value=skill_to_edit['aliases'] or "None", inline=False)
            embed.add_field(name="3. Dice Roll", value=skill_to_edit['dice_roll'], inline=False)
            embed.add_field(name="4. Type", value=skill_to_edit['skill_type'], inline=False)
            embed.add_field(name="5. Description", value=skill_to_edit['description'] or "None", inline=False)
            embed.set_footer(text="Click a button or reply with the number.")

            options = {
                "1️⃣ Name": "1",
                "2️⃣ Aliases": "2",
                "3️⃣ Dice Roll": "3",
                "4️⃣ Type": "4",
                "5️⃣ Description": "5"
            }

            choice = await get_selection(ctx, embed, options, timeout=30.0)

            if not choice or choice.lower() in ['exit', 'cancel']:
                await ctx.send("Edit cancelled.")
                return

            updates: Dict[str, Any] = {}

            existing_names_and_aliases = set()
            for skill in user_skills:
                if skill['id'] == skill_to_edit['id']:
                    continue
                existing_names_and_aliases.add(skill['name'].lower())
                if skill['aliases']:
                    for alias in skill['aliases'].split('|'):
                        if alias.strip():
                            existing_names_and_aliases.add(alias.strip().lower())

            match choice:
                case '1':  # Edit Name
                    while True:
                        await ctx.send("What should the new name be?")
                        name_msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                        new_name = name_msg.content.strip()
                        if new_name.lower() in ['exit', 'cancel']:
                            await ctx.send("Edit cancelled.")
                            return
                        if new_name.lower() in existing_names_and_aliases:
                            await ctx.send(f"The name `{new_name}` is already in use. Please choose another.")
                            continue
                        updates['name'] = new_name
                        break

                case '2':  # Edit Aliases
                    while True:
                        await ctx.send("What should the new aliases be? Separate with `|` or say `none`.")
                        aliases_msg = await self.bot.wait_for('message', check=check, timeout=45.0)
                        raw_aliases = aliases_msg.content.strip()
                        if raw_aliases.lower() in ['exit', 'cancel']:
                            await ctx.send("Edit cancelled.")
                            return

                        new_aliases = [a.strip() for a in raw_aliases.split('|') if a.strip()] if raw_aliases.lower() != 'none' else []
                        is_valid = True
                        seen_aliases = set()
                        for alias in new_aliases:
                            if alias.lower() in existing_names_and_aliases or alias.lower() in seen_aliases:
                                await ctx.send(f"The alias `{alias}` is already in use or is duplicated. Please try again.")
                                is_valid = False
                                break
                            seen_aliases.add(alias.lower())

                        if is_valid:
                            updates['aliases'] = new_aliases
                            break

                case '3':  # Edit Dice Roll
                    while True:
                        await ctx.send(f"What is the new dice roll equation for `{skill_to_edit['name']}`? (e.g., `2d8 + 5`, `4d20kh1`, `4c + 2`)")
                        roll_msg = await self.bot.wait_for('message', check=check, timeout=35.0)
                        new_roll = roll_msg.content.strip()
                        if new_roll.lower() in ['exit', 'cancel']:
                            await ctx.send("Edit cancelled.")
                            return

                        is_valid, error_message = await self._validate_roll_logic(new_roll)
                        if not is_valid:
                            await ctx.send(error_message)
                            continue

                        updates['dice_roll'] = new_roll
                        break

                case '4':  # Edit Skill Type
                    while True:
                        embed = discord.Embed(title="Select Skill Type", description="Is this an `attack` or `defense` skill?", color=discord.Color.blue())
                        options = {"⚔️ Attack": "attack", "🛡️ Defense": "defense"}
                        new_type = await get_selection(ctx, embed, options, timeout=20.0)

                        if not new_type or new_type.lower() in ['exit', 'cancel']:
                            await ctx.send("Edit cancelled.")
                            return

                        new_type = new_type.lower()
                        if new_type not in ['attack', 'defense']:
                            await ctx.send("Invalid type. Please choose `attack` or `defense`.")
                            continue
                        updates['skill_type'] = new_type
                        break

                case '5':  # Edit Description
                    while True:
                        await ctx.send("What should the new description be? (max 200 chars) or say `none` to remove it.")
                        desc_msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                        raw_desc = desc_msg.content.strip()

                        if raw_desc.lower() in ['exit', 'cancel']:
                            await ctx.send("Edit cancelled.")
                            return

                        if raw_desc.lower() == 'none':
                            updates['description'] = None
                            break

                        if len(raw_desc) > 200:
                            await ctx.send(f"Description is too long ({len(raw_desc)}/200 chars). Please try again.")
                            continue

                        updates['description'] = raw_desc
                        break

                case _:
                    await ctx.send("Invalid choice. Edit cancelled.")
                    return

            if updates:
                rows_affected = await self.db_manager.update_skill(skill_to_edit['id'], ctx.author.id, updates)
                if rows_affected > 0:
                    await ctx.send(f"✅ Successfully updated your skill: **{skill_to_edit['name']}**.")
                    self.logger.info(f"User {ctx.author.id} updated skill '{skill_to_edit['name']}' (id={skill_to_edit['id']}): fields={list(updates.keys())}")
                else:
                    self.logger.warning(f"Update skill returned 0 rows for user {ctx.author.id}, skill id={skill_to_edit['id']} ('{skill_to_edit['name']}'). Fields attempted: {list(updates.keys())}.")  # noqa: E501
                    await ctx.send("Something went wrong. I couldn't update that skill.")
            else:
                await ctx.send("No changes were made.")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Edit cancelled.")
        except Exception as e:
            self.logger.error(f"Error editing skill for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while editing the skill.")

    async def list_skills_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Handles the NLP intent for listing all of a user's saved skills.

        It formats the skills into a clean, readable embed, showing the name,
        aliases, roll formula, type, and a unique ID for deletion.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        skills = await self.db_manager.get_user_skills(ctx.author.id)
        if not skills:
            await ctx.send(f"You have no saved skills. Use `.{config.BOT_NAME} save skill` to create one!")
            return

        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Skills",
            color=discord.Color.blue()
        )

        skill_fields = []
        for i, skill in enumerate(skills, 1):
            name = f"**{i}. {skill['name']}**"
            value = []
            if skill['aliases']:
                value.append(f"(aliases: {skill['aliases']})")
            value.append(f"**Roll:** `{skill['dice_roll']}` | **Type:** `{skill['skill_type']}` | **ID:** `{skill['id']}`")
            if skill['description']:
                value.append(f"_{skill['description']}_")
            skill_fields.append({"name": name, "value": "\n".join(value), "inline": False})

        # Compact display for long lists.
        if len(skills) <= 5:
            for field in skill_fields:
                embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
        else:
            description = [f"{field['name']}\n{field['value']}" for field in skill_fields]
            embed.description = "\n\n".join(description)

        user_skill_limit = await self.db_manager.get_user_skill_limit(ctx.author.id)
        embed.set_footer(text=f"You are using {len(skills)}/{user_skill_limit} skill slots. Use '.{config.BOT_NAME} delete skill <id>' to remove one.")
        await ctx.send(embed=embed)

    async def delete_skill_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Handles the NLP intent for deleting a skill.

        It parses the skill's number from the query, confirms it's a valid skill,
        and removes it from the database.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        # Extract skill index.
        match = re.search(r'\d+', query)
        if not match:
            await ctx.send(f"Please specify the number of the skill you want to delete. Use `.{config.BOT_NAME} skills` to see your list.")
            return

        skill_num_to_delete = int(match.group(0))

        try:
            skills = await self.db_manager.get_user_skills(ctx.author.id)

            # Validate index.
            if not (1 <= skill_num_to_delete <= len(skills)):
                await ctx.send(f"Invalid number. You only have {len(skills)} skills.")
                return

            skill_to_delete = skills[skill_num_to_delete - 1]
            rows_affected = await self.db_manager.delete_skill(ctx.author.id, skill_to_delete['id'])

            if rows_affected > 0:
                await ctx.send(f"✅ Successfully deleted your skill: **{skill_to_delete['name']}**.")
                self.logger.info(f"User {ctx.author.id} deleted skill '{skill_to_delete['name']}'.")
            else:
                self.logger.warning(f"Delete skill returned 0 rows for user {ctx.author.id}, skill id={skill_to_delete['id']} ('{skill_to_delete['name']}').")
                await ctx.send("Something went wrong. I couldn't delete that skill.")
        except Exception as e:
            self.logger.error(f"Error deleting skill for {ctx.author.id}: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while deleting the skill.")


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(Skills(bot))

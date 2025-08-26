import discord
from discord.ext import commands
import random
import re

# A strict regex to find all valid dice notations (e.g., 3d6, 4d6kh3, 2d20)
DICE_NOTATION_REGEX = re.compile(r'(\d+)?d(\d+)(kh|kl)?(\d+)?', re.IGNORECASE)
# A whitelist of characters allowed in the final mathematical expression
SAFE_EVAL_WHITELIST = re.compile(r'^[0-9+\-/*().\s]+$')

class DiceRoller(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _roll_and_parse_notation(self, match: re.Match) -> tuple[int, str]:
        """Parses a single dice notation, rolls the dice, and returns the sum and a description."""
        num_dice = int(match.group(1)) if match.group(1) else 1
        num_sides = int(match.group(2))
        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        # --- Input Validation ---
        if num_dice < 1 or num_sides < 1:
            raise ValueError("Dice and sides must be positive numbers.")
        if num_dice > 1000 or num_sides > 10000:
            raise ValueError("Dice or side count is too high.")
        if keep_count and keep_count > num_dice:
            raise ValueError("Cannot keep more dice than are rolled.")

        rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
        
        description = f"{match.group(0)}: ` {', '.join(map(str, rolls))} `"
        kept_rolls = rolls.copy()

        if keep_mode == 'kh' and keep_count > 0:
            kept_rolls = sorted(rolls, reverse=True)[:keep_count]
            discarded = sorted(rolls, reverse=True)[keep_count:]
            description += f" -> kept **{', '.join(map(str, kept_rolls))}**"
            if discarded:
                description += f" (discarded {', '.join(map(str, discarded))})"
        elif keep_mode == 'kl' and keep_count > 0:
            kept_rolls = sorted(rolls)[:keep_count]
            discarded = sorted(rolls)[keep_count:]
            description += f" -> kept **{', '.join(map(str, kept_rolls))}**"
            if discarded:
                description += f" (discarded {', '.join(map(str, discarded))})"

        return sum(kept_rolls), description

    async def roll(self, ctx, *, roll_string: str):
        """A powerful dice roller that supports complex mathematical expressions."""
        query = roll_string.lower().replace('roll', '').replace('x', '*').strip()

        # Handle advantage/disadvantage separately as they are not part of the math
        has_advantage = 'advantage' in query
        has_disadvantage = 'disadvantage' in query
        # Sanitize the query to prevent the roller from raising an error
        query = query.replace('with advantage', '').replace('advantage', '')
        query = query.replace('with disadvantage', '').replace('disadvantage', '')
        query = query.strip()

        if has_advantage and has_disadvantage:
            await ctx.send("You can't roll with both advantage and disadvantage at the same time!")
            return

        try:
            roll_descriptions = []
            
            # --- Advantage/Disadvantage Logic ---
            if has_advantage or has_disadvantage:
                first_roll_match = DICE_NOTATION_REGEX.search(query)
                if not first_roll_match:
                    await ctx.send("I couldn't find a dice roll to apply advantage/disadvantage to.")
                    return
                
                sum1, desc1 = self._roll_and_parse_notation(first_roll_match)
                sum2, desc2 = self._roll_and_parse_notation(first_roll_match)
                
                chosen_sum = max(sum1, sum2) if has_advantage else min(sum1, sum2)
                roll_descriptions.append(f"Advantage Rolls:\n- {desc1} (Total: {sum1})\n- {desc2} (Total: {sum2})")
                
                # Replace only the first occurrence of the dice roll with its result
                query = query.replace(first_roll_match.group(0), str(chosen_sum), 1)

            # --- Main Parsing Loop for all other dice ---
            # This loop finds all dice notations, rolls them, and replaces them with their sum.
            while match := DICE_NOTATION_REGEX.search(query):
                roll_sum, description = self._roll_and_parse_notation(match)
                roll_descriptions.append(description)
                query = query.replace(match.group(0), str(roll_sum), 1)

            # --- Safe Evaluation ---
            if not SAFE_EVAL_WHITELIST.match(query):
                await ctx.send("Your roll contains invalid characters. Only numbers and `+ - * / ( )` are allowed.")
                return

            final_result = eval(query)

            # --- Build and Send Response ---
            response_message = f"{ctx.author.mention}, you rolled: **{final_result}**\n"
            if roll_descriptions:
                response_message += "\n".join(roll_descriptions)
            
            await ctx.send(response_message)

        except ValueError as e:
            await ctx.send(f"Error: {e}")
        except Exception as e:
            await ctx.send("I couldn't understand that roll. Please check your format.")
            raise e

async def setup(bot):
    await bot.add_cog(DiceRoller(bot))
import discord
from discord.ext import commands
import random
import re
import ast
import operator as op
from typing import Optional
import re as _re
from utils.base_cog import BaseCog

# --- Secure Expression Evaluator ---

ALLOWED_OPERATORS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.USub: op.neg
}

def safe_eval_math(expr: str) -> float:
    """Safely evaluates a mathematical string expression using an AST walker."""
    tree = ast.parse(expr, mode='eval').body

    def _eval_node(node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError("Only numeric values are allowed.")
            return node.value
        elif isinstance(node, ast.Num):  # Legacy support for Python < 3.8
            # node.n can be a variety of constants; validate its type before
            # converting to float so Pylance can narrow the type safely.
            value = node.n
            if not isinstance(value, (int, float)):
                raise ValueError("Only numeric values are allowed.")
            return float(value)
        elif isinstance(node, (ast.BinOp, ast.UnaryOp)):
            op_type = type(node.op)
            if op_type not in ALLOWED_OPERATORS:
                raise ValueError(f"Operator not allowed: {op_type.__name__}")
            if isinstance(node, ast.BinOp):
                left = _eval_node(node.left)
                right = _eval_node(node.right)
                return ALLOWED_OPERATORS[op_type](left, right)
            else: # UnaryOp
                operand = _eval_node(node.operand)
                return ALLOWED_OPERATORS[op_type](operand)
        raise TypeError(f"Unsupported node type: {type(node).__name__}")
    
    return _eval_node(tree)

# --- Dice Roller Cog ---

DICE_NOTATION_REGEX = re.compile(r'(\d+)?d(\d+)(kh|kl)?(\d+)?', re.IGNORECASE)
BRACKETED_DICE_REGEX = re.compile(r'\(([^()]*?)\)d(\d+)', re.IGNORECASE)

class DiceRoller(BaseCog):
    """A cog for handling complex dice rolling commands."""
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    def _roll_and_parse_notation(self, match: _re.Match[str], advantage: bool = False, disadvantage: bool = False) -> tuple[int, str]:
        """
        Parses a regex match for a dice roll, rolls the dice, and returns the sum and a description.
        Handles advantage and disadvantage for the given roll.
        """
        num_dice_str = match.group(1)
        num_sides_str = match.group(2)

        if not num_dice_str or not num_sides_str:
            raise ValueError("Invalid dice notation format.")

        num_dice = int(num_dice_str)
        num_sides = int(num_sides_str)
        
        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        if not (1 <= num_dice <= 1000 and 1 <= num_sides <= 10000):
            raise ValueError("Dice or side count is out of range (1-1000 dice, 1-10000 sides).")
        if keep_count and keep_count > num_dice:
            raise ValueError("Cannot keep more dice than are rolled.")

        # --- Advantage/Disadvantage Logic ---
        if advantage or disadvantage:
            rolls1 = [random.randint(1, num_sides) for _ in range(num_dice)]
            rolls2 = [random.randint(1, num_sides) for _ in range(num_dice)]
            sum1, sum2 = sum(rolls1), sum(rolls2)

            if advantage:
                chosen_rolls, chosen_sum = (rolls1, sum1) if sum1 >= sum2 else (rolls2, sum2)
                other_rolls, other_sum = (rolls2, sum2) if sum1 >= sum2 else (rolls1, sum1)
            else: # Disadvantage
                chosen_rolls, chosen_sum = (rolls1, sum1) if sum1 <= sum2 else (rolls2, sum2)
                other_rolls, other_sum = (rolls2, sum2) if sum1 <= sum2 else (rolls1, sum1)

            description = (f"{match.group(0)} (Adv/Dis): Rolled `{', '.join(map(str, chosen_rolls))}` (Σ={chosen_sum}) "
                           f"and `{', '.join(map(str, other_rolls))}` (Σ={other_sum}). Kept **{chosen_sum}**.")
            return chosen_sum, description

        # --- Standard Roll Logic ---
        rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
        description = f"{match.group(0)}: ` {', '.join(map(str, rolls))} `"
        
        kept_rolls = rolls
        if keep_mode in ('kh', 'kl') and keep_count > 0:
            sorted_rolls = sorted(rolls, reverse=(keep_mode == 'kh'))
            kept_rolls = sorted_rolls[:keep_count]
            discarded = sorted_rolls[keep_count:]
            description += f" -> kept **{', '.join(map(str, kept_rolls))}** (discarded {', '.join(map(str, discarded))})"

        return sum(kept_rolls), description

    def _preprocess_bracketed_dice(self, query: str) -> str:
        """
        Recursively evaluates and replaces bracketed dice expressions like (2*3)d6.
        """
        while match := BRACKETED_DICE_REGEX.search(query):
            expression_in_brackets = match.group(1)
            try:
                # Safely evaluate the mathematical expression inside the brackets
                num_dice = int(safe_eval_math(expression_in_brackets))
                if num_dice <= 0:
                    raise ValueError("Number of dice from brackets must be positive.")
                
                # Replace the bracketed part with the calculated number of dice
                replacement = f"{num_dice}d{match.group(2)}"
                query = query.replace(match.group(0), replacement, 1)
                self.logger.info(f"Pre-processed bracketed dice: '{match.group(0)}' -> '{replacement}'")
            except (ValueError, TypeError, SyntaxError) as e:
                raise ValueError(f"Invalid expression in dice brackets '{expression_in_brackets}': {e}")
        return query

    async def roll(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all dice rolling requests."""
        try:
            # --- 1. Sanitize and Detect Keywords ---
            processed_query = " ".join(query.lower().split()).replace('x', '*')
            
            # Use regex to remove keywords safely to avoid mangling words
            adv = bool(re.search(r'\b(advantage|adv)\b', processed_query))
            dis = bool(re.search(r'\b(disadvantage|dis)\b', processed_query))
            
            # Remove keywords for clean processing
            processed_query = re.sub(r'\broll\b|\b(with\s+)?(advantage|adv|disadvantage|dis)\b', '', processed_query).strip()

            if adv and dis:
                await ctx.send("Cannot roll with both advantage and disadvantage.")
                return

            # --- 2. Pre-process Bracketed Dice ---
            processed_query = self._preprocess_bracketed_dice(processed_query)

            # --- 3. Resolve All Dice Rolls ---
            roll_descriptions = []
            final_query = processed_query
            
            # Loop to find and replace all dice notations
            while match := DICE_NOTATION_REGEX.search(final_query):
                roll_sum, description = self._roll_and_parse_notation(match, advantage=adv, disadvantage=dis)
                roll_descriptions.append(description)
                # Replace the matched dice notation with its calculated sum
                final_query = final_query.replace(match.group(0), str(roll_sum), 1)

            # --- 4. Final Calculation ---
            if not final_query.strip():
                # This happens if the query was just "roll 1d6" and nothing else.
                # The result is already in the description.
                if len(roll_descriptions) == 1:
                     # Extract the sum from the description for a clean final result
                    match = re.search(r': `.*`.*-> kept \*\*(.*?)\*\*|: ` (.*) `', roll_descriptions[0])
                    if match:
                        result_str = next((g for g in match.groups() if g is not None), "N/A")
                        result_display = result_str.split(' ')[0] # Handle cases with extra text
                    else:
                        result_display = "N/A" # Fallback
                    
                    response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
                    await ctx.send(response)
                    return
                else:
                    await ctx.send("Please specify what to roll!")
                    return
            
            # Evaluate the final mathematical expression
            result = safe_eval_math(final_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"
            
            response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
            await ctx.send(response)

        except (ValueError, TypeError, SyntaxError) as e:
            # These are expected errors from parsing or rolling, so we can give a direct response.
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in dice roller for query '{query}': {e}")
        # Any other exceptions will be caught by the global error handler in main.py
        # which will log the full traceback and notify the user.

async def setup(bot: commands.Bot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(DiceRoller(bot))
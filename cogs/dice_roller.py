import discord
from discord.ext import commands
import random
import re
import ast
import operator as op
from typing import Optional
import re as _re
import logging

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

class DiceRoller(commands.Cog):
    """A cog for handling complex dice rolling commands."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _roll_and_parse_notation(self, match: _re.Match[str]) -> tuple[int, str]:
        """Parses a regex match for a dice roll and returns the sum and a description."""
        num_dice = int(match.group(1)) if match.group(1) else 1
        num_sides = int(match.group(2))
        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        if not (1 <= num_dice <= 1000 and 1 <= num_sides <= 10000):
            raise ValueError("Dice or side count is out of range (1-1000 dice, 1-10000 sides).")
        if keep_count and keep_count > num_dice:
            raise ValueError("Cannot keep more dice than are rolled.")

        rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
        description = f"{match.group(0)}: ` {', '.join(map(str, rolls))} `"
        
        kept_rolls = rolls
        if keep_mode in ('kh', 'kl') and keep_count > 0:
            sorted_rolls = sorted(rolls, reverse=(keep_mode == 'kh'))
            kept_rolls = sorted_rolls[:keep_count]
            discarded = sorted_rolls[keep_count:]
            description += f" -> kept **{', '.join(map(str, kept_rolls))}** (discarded {', '.join(map(str, discarded))})"

        return sum(kept_rolls), description

    async def roll(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all dice rolling requests."""
        processed_query = " ".join(query.lower().split()).replace('roll', '').replace('x', '*').strip()
        
        adv = 'advantage' in processed_query
        dis = 'disadvantage' in processed_query
        processed_query = processed_query.replace('with advantage', '').replace('advantage', '').replace('with disadvantage', '').replace('disadvantage', '').strip()

        if adv and dis:
            await ctx.send("Cannot roll with both advantage and disadvantage.")
            return

        try:
            roll_descriptions = []
            if adv or dis:
                match = DICE_NOTATION_REGEX.search(processed_query)
                if not match:
                    await ctx.send("Couldn't find a dice roll for advantage/disadvantage.")
                    return
                sum1, desc1 = self._roll_and_parse_notation(match)
                sum2, desc2 = self._roll_and_parse_notation(match)
                chosen = max(sum1, sum2) if adv else min(sum1, sum2)
                roll_descriptions.append(f"Adv/Dis Rolls:\n- {desc1} (Total: {sum1})\n- {desc2} (Total: {sum2})")
                processed_query = processed_query.replace(match.group(0), str(chosen), 1)

            final_query = processed_query
            while match := DICE_NOTATION_REGEX.search(final_query):
                roll_sum, description = self._roll_and_parse_notation(match)
                roll_descriptions.append(description)
                final_query = final_query.replace(match.group(0), str(roll_sum), 1)

            if not final_query:
                await ctx.send("Please specify what to roll!")
                return
            
            result = safe_eval_math(final_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"
            
            response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
            await ctx.send(response)

        except (ValueError, TypeError, SyntaxError) as e:
            await ctx.send(f"Error: {e}")
        except Exception as e:
            await ctx.send("I couldn't understand that roll. Please check your format.")
            logging.getLogger('DiceRoller').error(f"Unexpected roll error: {e}", exc_info=True)

async def setup(bot: commands.Bot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(DiceRoller(bot))
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
    ast.Div: op.truediv, ast.USub: op.neg, ast.Pow: op.pow
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

# --- Math Cog ---

DICE_NOTATION_REGEX = re.compile(r'(\d+)?d(\d+)(kh|kl)?(\d+)?', re.IGNORECASE)

class Math(BaseCog):
    """A cog for handling complex dice rolling and mathematical calculations."""
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    async def calculate(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all basic math calculation requests."""
        try:
            # Clean the query for evaluation
            processed_query = " ".join(query.lower().split()).replace('x', '*').replace('^', '**')
            # Remove keywords
            processed_query = re.sub(r'\b(calculate|calc|compute|evaluate)\b', '', processed_query).strip()

            if not processed_query:
                await ctx.send("Please provide a mathematical expression to calculate.")
                return

            result = safe_eval_math(processed_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"
            
            await ctx.send(f"{ctx.author.mention}, the result is: **{result_display}**")

        except (ValueError, TypeError, SyntaxError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in calculator for query '{query}': {e}")

    def _roll_and_parse_notation(self, match: _re.Match[str], advantage: bool = False, disadvantage: bool = False) -> tuple[int, str]:
        """
        Parses a regex match for a dice roll, rolls the dice, and returns the sum and a description.
        Handles advantage and disadvantage for the given roll.
        """
        num_dice_str, num_sides_str = match.group(1), match.group(2)
        num_dice = int(num_dice_str) if num_dice_str else 1
        num_sides = int(num_sides_str)
        
        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        if not (1 <= num_dice <= 300 and 1 <= num_sides <= 5000):
            raise ValueError("Dice or side count is out of range (1-300 dice, 1-5000 sides).")
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

    def _preprocess_parentheses(self, query: str) -> str:
        """
        Recursively evaluates and replaces simple mathematical expressions within parentheses.
        """
        PARENTHESES_REGEX = re.compile(r'\(([^()]+)\)')
        
        while match := PARENTHESES_REGEX.search(query):
            expression = match.group(1)
            if 'd' in expression:
                break 

            try:
                result = safe_eval_math(expression)
                result_str = str(int(result)) if result == int(result) else f"{result:.2f}"
                query = query.replace(match.group(0), result_str, 1)
                self.logger.info(f"Pre-processed parentheses: '{match.group(0)}' -> '{result_str}'")
            except (ValueError, TypeError, SyntaxError):
                self.logger.warning(f"Could not resolve expression in parentheses '{expression}', moving on.")
                break
        return query

    async def roll(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all dice rolling requests."""
        try:
            # --- 1. Sanitize and Detect Keywords ---
            processed_query = " ".join(query.lower().split()).replace('x', '*').replace('^', '**')
            
            adv = bool(re.search(r'\b(advantage|adv)\b', processed_query))
            dis = bool(re.search(r'\b(disadvantage|dis)\b', processed_query))
            
            processed_query = re.sub(r'\broll\b|\b(with\s+)?(advantage|adv|disadvantage|dis)\b', '', processed_query).strip()

            if adv and dis:
                await ctx.send("Cannot roll with both advantage and disadvantage.")
                return

            # --- 2. Pre-process Parentheses ---
            processed_query = self._preprocess_parentheses(processed_query)

            # --- 3. Resolve All Dice Rolls ---
            roll_descriptions = []
            final_query = processed_query
            
            while match := DICE_NOTATION_REGEX.search(final_query):
                roll_sum, description = self._roll_and_parse_notation(match, advantage=adv, disadvantage=dis)
                roll_descriptions.append(description)
                final_query = final_query.replace(match.group(0), str(roll_sum), 1)

            # --- 4. Final Calculation ---
            if not final_query.strip():
                if len(roll_descriptions) == 1:
                    match = re.search(r'Kept \*\*(.*?)\*\*|: ` (.*?) `', roll_descriptions[0])
                    result_display = "N/A"
                    if match:
                        result_str = next((g for g in match.groups() if g is not None), "N/A")
                        result_display = result_str.split(' ')[0]
                    
                    response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
                    if len(response) > 4000:
                        await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
                        return
                    await ctx.send(response)
                    return
                else:
                    await ctx.send("Please specify what to roll!")
                    return
            
            result = safe_eval_math(final_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"
            
            response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
            if len(response) > 4000:
                await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
                return
            await ctx.send(response)

        except (ValueError, TypeError, SyntaxError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in dice roller for query '{query}': {e}")

async def setup(bot: commands.Bot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Math(bot))

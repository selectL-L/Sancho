import discord
from discord.ext import commands
import random
import re
import ast
import operator as op
import asyncio
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
COIN_FLIP_REGEX = re.compile(r'(\d*)c', re.IGNORECASE)

class Math(BaseCog):
    """A cog for handling complex dice rolling and mathematical calculations."""
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    async def limbus_roll_nlp(self, ctx: commands.Context, *, query: str):
        """
        Handles Limbus Company-style rolls using a sequential parser.
        It finds and consumes parameters one by one to avoid conflicts.
        If any are missing, it falls back to an interactive conversation.
        """
        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # --- 1. Sequential Parsing ---
            work_query = f" {query.lower()} "  # Pad with spaces for easier regex
            base_power, coin_power, num_coins, sp = None, None, None, None

            # Parser for SP
            sp_match = re.search(r'(?:at\s+)?(-?\d+)\s+sp\b', work_query, re.IGNORECASE)
            if sp_match:
                sp = int(sp_match.group(1))
                work_query = work_query.replace(sp_match.group(0), " ", 1)
            
            # Parser for Base Power
            base_match = re.search(r'(?:(\d+)\s+\b(base\s*power|bp)\b|\b(base\s*power|bp)\b\s+(\d+))', work_query, re.IGNORECASE)
            if base_match:
                base_power = int(base_match.group(1) or base_match.group(4))
                work_query = work_query.replace(base_match.group(0), " ", 1)

            # Parser for Coin Power
            cp_match = re.search(r'(?:([+-]?\d+)\s+\b(coin\s*power|cp)\b|\b(coin\s*power|cp)\b\s+([+-]?\d+))', work_query, re.IGNORECASE)
            if cp_match:
                coin_power = int(cp_match.group(1) or cp_match.group(4))
                work_query = work_query.replace(cp_match.group(0), " ", 1)

            # Parser for Number of Coins
            num_match = re.search(r'(?:(\d+)\s+\b(coins?|coin\s*count)\b|\b(coins?|coin\s*count)\b\s+(\d+))', work_query, re.IGNORECASE)
            if num_match:
                num_coins = int(num_match.group(1) or num_match.group(4))
                work_query = work_query.replace(num_match.group(0), " ", 1)

            # Parser for Modifier (any remaining signed number)
            mod_match = re.search(r'\s([+-]\d+)\s', work_query)
            modifier = int(mod_match.group(1)) if mod_match else 0

            # --- 2. Fallback to interactive mode if values are missing ---
            interactive_fallback_needed = any(v is None for v in [base_power, coin_power, num_coins, sp])
            if interactive_fallback_needed:
                await ctx.send(
                    "Switching to interactive mode, please input your values below.\n"
                    "*If you provided all the info, please let my author know something is broken!*"
                )

            if base_power is None:
                await ctx.send("Base power?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                base_power = int(msg.content)

            if coin_power is None:
                await ctx.send("Coin power?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                coin_power = int(msg.content)

            if num_coins is None:
                await ctx.send("How many coins?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                num_coins = int(msg.content)
            
            if sp is None:
                await ctx.send("SP? (optional, press enter to skip)")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                sp = int(msg.content) if msg.content else 0

            # --- 3. Validation ---
            if not (1 <= num_coins <= 15):
                raise ValueError("Coin count must be between 1 and 15.")
            if not (-50 <= coin_power <= 50):
                raise ValueError("Coin value must be between -50 and 50.")
            if not (-100 <= base_power <= 100) or not (-100 <= modifier <= 100):
                raise ValueError("Base power and modifiers must be between -100 and 100.")
            if not (-45 <= sp <= 45):
                raise ValueError("SP must be between -45 and 45.")

            # --- 4. Simulate Coin Flips ---
            heads_prob = 0.5 + (0.01 * sp)
            heads_count = 0
            coin_results_display = []
            for _ in range(num_coins):
                if random.random() < heads_prob: # True for Heads
                    heads_count += 1
                    coin_results_display.append("H")
                else: # Tails
                    coin_results_display.append("T")
            
            coin_total = heads_count * coin_power
            final_result = base_power + coin_total + modifier

            # --- 5. Format and Send Response ---
            coin_part_str = f"{heads_count}H {len(coin_results_display) - heads_count}T"
            coin_value_str = f"+{coin_power}" if coin_power >= 0 else str(coin_power)
            
            sp_info = f" at **{sp} SP** (Heads Chance: **{heads_prob:.0%}**)" if sp != 0 else ""

            description = (
                f"Flipping {num_coins} coins{sp_info} (Value: {coin_value_str}): `{' '.join(coin_results_display)}`\n"
                f"Result: {coin_part_str} -> **{coin_total}**"
            )

            response = (
                f"{ctx.author.mention}, your roll result is: **{final_result}**\n"
                f"Calculation: `(Base) {base_power} + (Coins) {coin_total} + (Mods) {modifier}`\n"
                f"{description}"
            )
            await ctx.send(response)
            self.logger.info(f"Limbus roll by {ctx.author}. Result: {final_result}")

        except asyncio.TimeoutError:
            await ctx.send("You took too long to answer, so I cancelled the roll.")
        except (ValueError, TypeError) as e:
            await ctx.send(f"Invalid input: {e}. Please enter a valid number.")
        except Exception as e:
            await ctx.send(f"An unexpected error occurred: {e}")
            self.logger.error(f"Error during limbus roll for {ctx.author}: {e}", exc_info=True)

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

            # Run the potentially blocking evaluation in a separate thread
            result = await asyncio.to_thread(safe_eval_math, processed_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"
            
            await ctx.send(f"{ctx.author.mention}, the result is: **{result_display}**")

        except (ValueError, TypeError, SyntaxError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in calculator for query '{query}': {e}")

    async def _roll_and_parse_notation(self, match: _re.Match[str], advantage: bool = False, disadvantage: bool = False) -> tuple[int, str]:
        """
        Parses a regex match for a dice roll, rolls the dice, and returns the sum and a description.
        Handles advantage and disadvantage for the given roll. This is run in a thread to avoid blocking.
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

        def _roll_dice_thread() -> tuple[list[int], list[int] | None]:
            """Synchronous function to handle the random number generation."""
            rolls1 = [random.randint(1, num_sides) for _ in range(num_dice)]
            if advantage or disadvantage:
                rolls2 = [random.randint(1, num_sides) for _ in range(num_dice)]
                return rolls1, rolls2
            return rolls1, None

        rolls1, rolls2 = await asyncio.to_thread(_roll_dice_thread)

        # --- Advantage/Disadvantage Logic ---
        if (advantage or disadvantage) and rolls2 is not None:
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
        rolls = rolls1
        description = f"{match.group(0)}: ` {', '.join(map(str, rolls))} `"
        
        kept_rolls = rolls
        if keep_mode in ('kh', 'kl') and keep_count > 0:
            sorted_rolls = sorted(rolls, reverse=(keep_mode == 'kh'))
            kept_rolls = sorted_rolls[:keep_count]
            discarded = sorted_rolls[keep_count:]
            description += f" -> kept **{', '.join(map(str, kept_rolls))}** (discarded {', '.join(map(str, discarded))})"

        return sum(kept_rolls), description

    async def _preprocess_parentheses(self, query: str) -> str:
        """
        Recursively evaluates and replaces simple mathematical expressions within parentheses.
        """
        PARENTHESES_REGEX = re.compile(r'\(([^()]+)\)')
        
        while match := PARENTHESES_REGEX.search(query):
            expression = match.group(1)
            if 'd' in expression or 'c' in expression:
                break 

            try:
                # Run the potentially blocking evaluation in a separate thread
                result = await asyncio.to_thread(safe_eval_math, expression)
                result_str = str(int(result)) if result == int(result) else f"{result:.2f}"
                query = query.replace(match.group(0), result_str, 1)
                self.logger.info(f"Pre-processed parentheses: '{match.group(0)}' -> '{result_str}'")
            except (ValueError, TypeError, SyntaxError):
                self.logger.warning(f"Could not resolve expression in parentheses '{expression}', moving on.")
                break
        return query

    async def _roll_and_parse_coins(self, match: _re.Match[str], sp: int) -> tuple[int, str]:
        """
        Parses a regex match for a coin flip, flips the coins with SP influence, and returns the sum and a description.
        """
        num_coins_str = match.group(1)
        num_coins = int(num_coins_str) if num_coins_str else 1

        if not (1 <= num_coins <= 40):
            raise ValueError("Coin count is out of range (1-40 coins).")

        heads_prob = sp / 100.0
        
        def _flip_coins_thread() -> list[int]:
            """Synchronous function to handle the random number generation for coin flips."""
            return [1 if random.random() < heads_prob else 0 for _ in range(num_coins)]

        flips = await asyncio.to_thread(_flip_coins_thread)
        heads_count = sum(flips)
        
        flip_results_display = "".join(['H' if r == 1 else 'T' for r in flips])
        
        description = f"{match.group(0)}: `{flip_results_display}` ({heads_count}H, {num_coins - heads_count}T)"
        return heads_count, description

    async def get_roll_result(self, dice_notation: str) -> int:
        """
        A simple utility to roll dice and get only the integer result back.
        Does not handle complex expressions, advantage, or send messages.
        """
        match = DICE_NOTATION_REGEX.fullmatch(dice_notation.strip())
        if not match:
            raise ValueError(f"Invalid simple dice notation provided: '{dice_notation}'")
        
        roll_sum, _ = await self._roll_and_parse_notation(match)
        return roll_sum

    async def roll(self, ctx: commands.Context, *, query: str, skill_info: Optional[dict] = None) -> None:
        """The NLP handler for all dice rolling requests."""
        try:
            # --- 1. Sanitize and Detect Keywords ---
            processed_query = " ".join(query.lower().split()).replace('x', '*').replace('^', '**')
            
            adv = bool(re.search(r'\b(advantage|adv)\b', processed_query))
            dis = bool(re.search(r'\b(disadvantage|dis)\b', processed_query))
            
            # Extract SP
            sp = 50 # Default to 50%
            sp_match = re.search(r'\b(at)\s+(\d+)\b', processed_query)
            if sp_match:
                sp = int(sp_match.group(2))
                if not (0 <= sp <= 100):
                    raise ValueError("SP must be between 0 and 100.")
                processed_query = processed_query.replace(sp_match.group(0), '', 1)

            processed_query = re.sub(r'\broll\b|\ba\b|\b(with\s+)?(advantage|adv|disadvantage|dis)\b', '', processed_query).strip()

            if adv and dis:
                await ctx.send("Cannot roll with both advantage and disadvantage.")
                return

            # --- 2. Pre-process Parentheses ---
            processed_query = await self._preprocess_parentheses(processed_query)

            # --- 3. Resolve All Rolls (Coins then Dice) ---
            roll_descriptions = []
            final_query = processed_query
            
            # Resolve coin flips first
            while match := COIN_FLIP_REGEX.search(final_query):
                roll_sum, description = await self._roll_and_parse_coins(match, sp=sp)
                roll_descriptions.append(description)
                final_query = final_query.replace(match.group(0), str(roll_sum), 1)

            # Then resolve dice rolls
            while match := DICE_NOTATION_REGEX.search(final_query):
                roll_sum, description = await self._roll_and_parse_notation(match, advantage=adv, disadvantage=dis)
                roll_descriptions.append(description)
                final_query = final_query.replace(match.group(0), str(roll_sum), 1)

            # --- 4. Final Calculation ---
            if not final_query.strip():
                if len(roll_descriptions) == 1:
                    match = re.search(r'Kept \*\*(.*?)\*\*|: ` (.*?) `|: `(.*?)`', roll_descriptions[0])
                    result_display = "N/A"
                    if match:
                        result_str = next((g for g in match.groups() if g is not None), "N/A")
                        # For dice, it's a list. For coins, it's a sum.
                        try:
                            result_display = str(sum(map(int, result_str.split(','))))
                        except (ValueError, TypeError):
                             # This will handle the coin flip case where the result is already a sum
                            result_display = result_str.split(' ')[0]

                    response = f"{ctx.author.mention}, you rolled: **{result_display}**\n" + "\n".join(roll_descriptions)
                    if len(response) > 3500:
                        await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
                        return
                    await ctx.send(response)
                    return
                else:
                    await ctx.send("Please specify what to roll!")
                    return
            
            result = safe_eval_math(final_query)
            result_display = int(result) if result == int(result) else f"{result:.2f}"

            # --- 5. Format Response ---
            response_parts = []
            # If a skill was used, format the header accordingly.
            if skill_info:
                display_formula = query.replace('(', '').replace(')', '').strip()
                
                # Case 1: Replying to another user (targeted skill)
                if ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
                    target_user = ctx.message.reference.resolved.author
                    if target_user != ctx.author and not target_user.bot:
                        if skill_info['skill_type'] == 'attack':
                            header = f"{ctx.author.mention} attacked {target_user.mention} with **{skill_info['name'].title()}**"
                        else: # defense
                            header = f"{ctx.author.mention} defended against {target_user.mention} with **{skill_info['name'].title()}**"
                        response_parts.append(header)
                        response_parts.append(f"`{display_formula}`")
                
                # Case 2: Skill used without a target
                else:
                    response_parts.append(f"**{skill_info['name'].title()}**")
                    response_parts.append(f"`{display_formula}`")

            response_parts.append(f"{ctx.author.mention}, you rolled: **{result_display}**")
            response_parts.extend(roll_descriptions)
            
            response = "\n".join(response_parts)
            if len(response) > 3500:
                await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
                return
            await ctx.send(response)

        except (ValueError, TypeError, SyntaxError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in dice roller for query '{query}': {e}")

async def setup(bot: commands.Bot) -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Math(bot))

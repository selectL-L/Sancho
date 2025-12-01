"""cogs/math.py

This cog provides a suite of mathematical and probabilistic commands for the bot.
It includes:
- A secure expression evaluator (`safe_eval_math`) that parses and computes mathematical
  strings without using `eval()`, preventing arbitrary code execution.
- A complex dice rolling command (`roll`) that supports standard notation (e.g., '2d20+5'),
  advantage/disadvantage, and keep highest/lowest modifiers.
- A Limbus Company-style coin flip simulator (`limbus_roll_nlp`) that models the game's
  unique probability mechanics based on Sanity Points (SP).
- NLP handlers that allow users to trigger these commands with natural language.
"""

import ast
import asyncio
import math
import operator as op
import random
import re
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

# --- Secure Expression Evaluator ---

# Whitelist safe operations.
ALLOWED_OPERATORS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.USub: op.neg, ast.Pow: op.pow,
    ast.Mod: op.mod
}

ALLOWED_FUNCTIONS = {
    'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
    'asin': math.asin, 'acos': math.acos, 'atan': math.atan,
    'sqrt': math.sqrt, 'log': math.log, 'log10': math.log10,
    'exp': math.exp, 'pow': math.pow, 'abs': abs,
    'ceil': math.ceil, 'floor': math.floor, 'round': round,
    'radians': math.radians, 'degrees': math.degrees
}

ALLOWED_NAMES = {
    'pi': math.pi,
    'e': math.e,
    'c': 299792458,  # Speed of light in m/s
    'avogadro': 6.02214076e23  # Avogadro's number
}


def safe_eval_math(expr: str) -> float:
    """Safely evaluates a mathematical string expression using an AST walker.

    This method is secure because it only processes a predefined set of
    mathematical operations and numeric constants, raising errors for any other
    type of node (like function calls or names).

    Args:
        expr (str): The mathematical expression to evaluate.

    Returns:
        float: The result of the evaluation.

    Raises:
        ValueError: If the expression contains disallowed values or operators.
        TypeError: If the expression contains unsupported node types.
    """
    tree = ast.parse(expr, mode='eval').body

    def _eval_node(node: ast.AST) -> float:
        # Handles numeric constants (e.g., 5, 3.14).
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError("Only numeric values are allowed.")
            return node.value
        # Legacy support for Python < 3.8.
        elif isinstance(node, ast.Num):
            value = node.n
            if not isinstance(value, (int, float)):
                raise ValueError("Only numeric values are allowed.")
            return float(value)
        # Handles binary operators (+, -, *, /) and unary operators (-).
        elif isinstance(node, (ast.BinOp, ast.UnaryOp)):
            op_type = type(node.op)
            if op_type not in ALLOWED_OPERATORS:
                raise ValueError(f"Operator not allowed: {op_type.__name__}")

            # Recursively evaluate the child nodes.
            if isinstance(node, ast.BinOp):
                left = _eval_node(node.left)
                right = _eval_node(node.right)
                return ALLOWED_OPERATORS[op_type](left, right)
            else:  # UnaryOp (e.g., -5)
                operand = _eval_node(node.operand)
                return ALLOWED_OPERATORS[op_type](operand)
        # Handles function calls (e.g., sin(pi)).
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
                func_name = node.func.id if isinstance(node.func, ast.Name) else 'unknown'
                raise ValueError(f"Function not allowed: {func_name}")

            args = [_eval_node(arg) for arg in node.args]
            return ALLOWED_FUNCTIONS[node.func.id](*args)
        # Handles named constants (e.g., pi, e).
        elif isinstance(node, ast.Name):
            if node.id not in ALLOWED_NAMES:
                raise ValueError(f"Name not allowed: {node.id}")
            return ALLOWED_NAMES[node.id]

        raise TypeError(f"Unsupported node type: {type(node).__name__}")

    return _eval_node(tree)


class Math(BaseCog):
    """A cog for handling complex dice rolling and mathematical calculations."""

    def __init__(self, bot: SanchoBot):
        """Initializes the Math cog.

        Args:
            bot (SanchoBot): The bot instance.
        """
        super().__init__(bot)

    async def limbus_roll_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Handles Limbus Company-style rolls using a sequential parser.

        It finds and consumes parameters one by one to avoid conflicts.
        If any are missing, it falls back to an interactive conversation.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # --- 1. Sequential Parsing ---
            # Pad query for reliable regex matching.
            # Each parameter (SP, Base Power, etc.) is searched for, its value extracted,
            # and the matched part is removed from the string to prevent it from being parsed again.
            # We replace with a space to maintain word boundaries.
            work_query = f" {query.lower()} "  # Pad with spaces for easier regex
            base_power, coin_power, num_coins, sp = None, None, None, None

            sp_match = re.search(r'(?:at\s+)?(-?\d+)\s+sp\b', work_query, re.IGNORECASE)
            if sp_match:
                sp = int(sp_match.group(1))
                work_query = work_query.replace(sp_match.group(0), " ", 1)

            base_match = re.search(r'(?:(\d+)\s+\b(base\s*power|bp)\b|\b(base\s*power|bp)\b\s+(\d+))', work_query, re.IGNORECASE)
            if base_match:
                base_power = int(base_match.group(1) or base_match.group(4))
                work_query = work_query.replace(base_match.group(0), " ", 1)

            cp_match = re.search(r'(?:([+-]?\d+)\s+\b(coin\s*power|cp)\b|\b(coin\s*power|cp)\b\s+([+-]?\d+))', work_query, re.IGNORECASE)
            if cp_match:
                coin_power = int(cp_match.group(1) or cp_match.group(4))
                work_query = work_query.replace(cp_match.group(0), " ", 1)

            num_match = re.search(r'(?:(\d+)\s+\b(coins?|coin\s*count)\b|\b(coins?|coin\s*count)\b\s+(\d+))', work_query, re.IGNORECASE)
            if num_match:
                num_coins = int(num_match.group(1) or num_match.group(4))
                work_query = work_query.replace(num_match.group(0), " ", 1)

            # Extract remaining signed number as modifier.
            mod_match = re.search(r'\s([+-]\d+)\s', work_query)
            modifier = int(mod_match.group(1)) if mod_match else 0

            # --- 2. Fallback to interactive mode if parameters missing ---
            # If any of the essential parameters were not found, the bot will ask for them one by one.
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
            # Adjust heads probability based on SP.
            heads_prob = 0.5 + (0.01 * sp)
            heads_count = 0
            coin_results_display = []
            for _ in range(num_coins):
                if random.random() < heads_prob:
                    heads_count += 1
                    coin_results_display.append("H")
                else:
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

    async def send_calc_help(self, ctx: commands.Context) -> None:
        """Sends a detailed help message for the calculator command.

        Args:
            ctx (commands.Context): The command context.
        """
        embed = discord.Embed(
            title="Calculator Help",
            description="The calculator supports a wide range of mathematical functions and constants. Here's how to use it:",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Basic Operations",
            value="`+` (add), `-` (subtract), `x or *` (multiply), `/` (divide), `^ or **` (power), `%` (modulo)",
            inline=False
        )

        functions_list = ", ".join(f"`{f}`" for f in sorted(ALLOWED_FUNCTIONS.keys()))
        embed.add_field(
            name="Available Functions",
            value=functions_list,
            inline=False
        )

        constants_list = ", ".join(f"`{c}`" for c in sorted(ALLOWED_NAMES.keys()))
        embed.add_field(
            name="Available Constants",
            value=constants_list,
            inline=False
        )

        embed.add_field(
            name="Usage Examples",
            value=(
                "**Basic Arithmetic:**\n"
                "`5 * (3 + 2)`\n\n"
                "**Functions:**\n"
                "`sqrt(64)` - Square Root\n"
                "`pow(3, 4)` - Power (X^Y)\n"
                "`abs(-15.5)` - Absolute Value\n"
                "`round(pi, 4)` - Rounding (Value, Precision)\n"
                "`ceil(4.2)` - Ceiling (round up)\n"
                "`floor(4.8)` - Floor (round down)\n\n"
                "**Trigonometry (angles in radians):**\n"
                "`sin(pi / 2)`\n"
                "`cos(0)`\n"
                "`tan(pi / 4)`\n\n"
                "**Inverse Trigonometry:**\n"
                "`asin(1)`\n"
                "`acos(-1)`\n"
                "`atan(0)`\n\n"
                "**Logarithms & Exponents:**\n"
                "`log(e)` - Natural Log\n"
                "`log10(1000)` - Base-10 Log\n"
                "`exp(2)` - e raised to the power of 2\n\n"
                "**Conversions:**\n"
                "`degrees(pi)` - Radians to Degrees\n"
                "`radians(180)` - Degrees to Radians\n\n"
                "**Constants:**\n"
                "`1/2 * 10 * c^2` - E=mc^2 example\n"
                "`avogadro * 2` - Using Avogadro's number\n\n"
                "**Combining Functions:**\n"
                "`sin(radians(90)) + cos(radians(180))`"
            ),
            inline=False
        )

        embed.set_footer(text="Expressions are parsed for safety. Only the functions and constants listed are available.")

        await ctx.send(embed=embed)

    async def calculate(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all basic math calculation requests.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        try:
            # Standardize the query: lowercase, collapse whitespace, and handle common operator aliases.
            # Replace 'x' with '*' only if it's not preceded by a letter (to preserve 'mx', 'exp', etc.)
            clean_query = " ".join(query.lower().split())
            original_query = re.sub(r'(?<![a-z])x', '*', clean_query).replace('^', '**')

            if 'help' in original_query:
                await self.send_calc_help(ctx)
                return

            # --- Extract Relevant Parts of the Expression ---
            # Extract valid math tokens.
            # This regex is designed to capture function names, numbers, and operators.
            token_pattern = re.compile(
                r'([a-zA-Z_][a-zA-Z0-9_]*|\d+(?:\.\d+)?|\*\*|[+\-*/%()]|\S)'
            )

            tokens = token_pattern.findall(original_query)

            valid_tokens = []
            for token in tokens:
                if token in ALLOWED_FUNCTIONS or \
                   token in ALLOWED_NAMES or \
                   token in "()+-*/%**" or \
                   re.fullmatch(r'\d+(?:\.\d+)?', token):
                    valid_tokens.append(token)

            processed_query = "".join(valid_tokens)

            if not processed_query:
                await ctx.send("Please provide a mathematical expression to calculate.")
                return

            # Run evaluation in thread.
            result = await asyncio.to_thread(safe_eval_math, processed_query)

            # Format result, removing trailing zeros.
            if result == int(result):
                result_display = str(int(result))
            else:
                result_display = f"{result:.15f}".rstrip('0').rstrip('.')

            await ctx.send(f"{ctx.author.mention}, the result is: **{result_display}**")

        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in calculator for query '{query}': {e}")

    async def get_roll_result(self, dice_notation: str) -> int:
        """A simple utility to roll dice and get only the integer result back.

        Args:
            dice_notation (str): The dice notation string (e.g., "2d20").

        Returns:
            int: The sum of the roll.

        Raises:
            ValueError: If the notation is invalid.
        """
        lexer = DiceLexer(dice_notation)
        parser = DiceParser(lexer)
        result = await parser.parse()
        return int(result)

    async def evaluate_roll(self, query: str) -> Dict[str, Any]:
        """Evaluates a dice roll query and returns the result and breakdown.

        Args:
            query (str): The roll query string.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - 'total': The final result (str).
                - 'breakdown': A list of roll description strings (List[str]).
                - 'processed_query': The query after processing (str).

        Raises:
            ValueError: If the query is invalid.
        """
        # --- 1. Sanitize and Detect Keywords ---
        original_query = " ".join(query.lower().split())

        # Check for advantage/disadvantage.
        adv = bool(re.search(r'\b(advantage|adv)\b', original_query))
        dis = bool(re.search(r'\b(disadvantage|dis)\b', original_query))

        if adv and dis:
            raise ValueError("Cannot roll with both advantage and disadvantage.")

        # Extract SP (default 50).
        sp = 50
        sp_match = re.search(r'\b(at|with)\s+(\d+)\s*[%]?', original_query)
        if sp_match:
            sp = int(sp_match.group(2))
            if not (0 <= sp <= 100):
                raise ValueError("SP must be between 0 and 100.")
            original_query = original_query.replace(sp_match.group(0), '', 1)

        # Remove keywords (advantage/disadvantage/roll/dice) from query before parsing
        original_query = re.sub(r'\b(advantage|adv|disadvantage|dis|roll|dice)\b', '', original_query)

        # --- 2. Parse and Evaluate ---
        lexer = DiceLexer(original_query)
        parser = DiceParser(lexer, advantage=adv, disadvantage=dis, sp=sp)

        result = await parser.parse()

        # --- 3. Format Result ---
        if result == int(result):
            result_display = str(int(result))
        else:
            result_display = f"{result:.2f}"

        return {
            'total': result_display,
            'breakdown': parser.breakdown,
            'processed_query': original_query.strip()
        }

    async def roll(self, ctx: commands.Context, *, query: str) -> None:
        """The NLP handler for all dice rolling requests.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string.
        """
        try:
            result_data = await self.evaluate_roll(query)
            result_display = result_data['total']
            roll_descriptions = result_data['breakdown']
            processed_query = result_data['processed_query']

            # Response formatting.
            response_parts = []
            response_parts.append(f"`{processed_query}`")
            response_parts.append(f"{ctx.author.mention}, you rolled: **{result_display}**")
            # Add roll breakdown.
            response_parts.extend(roll_descriptions)

            response = "\n".join(response_parts)
            if len(response) > 3500:
                await ctx.send(f"Sorry {ctx.author.mention}, the result of your roll is too long to display.")
                return
            await ctx.send(response)

        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as e:
            await ctx.send(f"Error: {e}")
            self.logger.warning(f"Handled error in dice roller for query '{query}': {e}")


class DiceToken:
    DICE = 'DICE'
    COIN = 'COIN'
    CLAMP = 'CLAMP'
    NUMBER = 'NUMBER'
    PLUS = 'PLUS'
    MINUS = 'MINUS'
    MULTIPLY = 'MULTIPLY'
    DIVIDE = 'DIVIDE'
    POWER = 'POWER'
    MODULO = 'MODULO'
    LPAREN = 'LPAREN'
    RPAREN = 'RPAREN'
    EOF = 'EOF'

    def __init__(self, type_: str, value: Any, raw: str = ""):
        self.type = type_
        self.value = value
        self.raw = raw

    def __repr__(self):
        return f"Token({self.type}, {self.value})"


class DiceLexer:
    def __init__(self, text: str):
        self.text = text
        self.tokens = []
        self.current = 0
        self._tokenize()

    def _tokenize(self):
        # Regex patterns - Order DOES matter here
        patterns = [
            (DiceToken.DICE, r'(\d+)?d(\d+)(?:kh|kl)?(?:\d+)?'),
            (DiceToken.COIN, r'(\d*)c'),
            (DiceToken.CLAMP, r'(?:mn\d+|mx\d+)+'),
            (DiceToken.NUMBER, r'\d+(?:\.\d+)?'),
            (DiceToken.POWER, r'\*\*|\^'),
            (DiceToken.PLUS, r'\+'),
            (DiceToken.MINUS, r'-'),
            (DiceToken.MULTIPLY, r'\*|x'),
            (DiceToken.DIVIDE, r'/'),
            (DiceToken.MODULO, r'%'),
            (DiceToken.LPAREN, r'\('),
            (DiceToken.RPAREN, r'\)'),
        ]

        regex_parts = []
        for type_, pattern in patterns:
            regex_parts.append(f'(?P<{type_}>{pattern})')

        full_regex = re.compile('|'.join(regex_parts), re.IGNORECASE)

        for match in full_regex.finditer(self.text):
            kind = match.lastgroup
            value = match.group()
            if kind:
                if kind == DiceToken.NUMBER:
                    self.tokens.append(DiceToken(kind, float(value), value))
                else:
                    self.tokens.append(DiceToken(kind, value, value))

        self.tokens.append(DiceToken(DiceToken.EOF, None))

    def next(self) -> DiceToken:
        if self.current < len(self.tokens):
            token = self.tokens[self.current]
            self.current += 1
            return token
        return self.tokens[-1]

    def peek(self) -> DiceToken:
        if self.current < len(self.tokens):
            return self.tokens[self.current]
        return self.tokens[-1]


class DiceParser:
    def __init__(self, lexer: DiceLexer, advantage: bool = False, disadvantage: bool = False, sp: int = 50):
        self.lexer = lexer
        self.advantage = advantage
        self.disadvantage = disadvantage
        self.sp = sp
        self.breakdown = []
        self.current_token = self.lexer.next()

    def eat(self, token_type: str):
        if self.current_token.type == token_type:
            self.current_token = self.lexer.next()
        else:
            raise ValueError(f"Unexpected token: {self.current_token.type}, expected {token_type}")

    async def parse(self) -> float:
        result = await self.expression()
        return result

    async def expression(self) -> float:
        node = await self.term()

        while self.current_token.type in (DiceToken.PLUS, DiceToken.MINUS):
            token = self.current_token
            if token.type == DiceToken.PLUS:
                self.eat(DiceToken.PLUS)
                node += await self.term()
            elif token.type == DiceToken.MINUS:
                self.eat(DiceToken.MINUS)
                node -= await self.term()

        return node

    async def term(self) -> float:
        node = await self.factor()

        while self.current_token.type in (DiceToken.MULTIPLY, DiceToken.DIVIDE, DiceToken.MODULO):
            token = self.current_token
            if token.type == DiceToken.MULTIPLY:
                self.eat(DiceToken.MULTIPLY)
                node *= await self.factor()
            elif token.type == DiceToken.DIVIDE:
                self.eat(DiceToken.DIVIDE)
                divisor = await self.factor()
                if divisor == 0:
                    raise ValueError("Division by zero")
                node /= divisor
            elif token.type == DiceToken.MODULO:
                self.eat(DiceToken.MODULO)
                divisor = await self.factor()
                if divisor == 0:
                    raise ValueError("Modulo by zero")
                node %= divisor

        return node

    async def factor(self) -> float:
        node = await self.atom()

        if self.current_token.type == DiceToken.POWER:
            self.eat(DiceToken.POWER)
            exponent = await self.factor()
            node = node ** exponent

        return node

    async def atom(self) -> float:
        token = self.current_token

        if token.type == DiceToken.NUMBER:
            self.eat(DiceToken.NUMBER)
            return token.value

        elif token.type == DiceToken.DICE:
            self.eat(DiceToken.DICE)
            return await self._roll_dice(token.raw)

        elif token.type == DiceToken.COIN:
            self.eat(DiceToken.COIN)
            return await self._flip_coin(token.raw)

        elif token.type == DiceToken.LPAREN:
            # Capture start index of the group (token after LPAREN)
            start_token_index = self.lexer.current

            self.eat(DiceToken.LPAREN)
            result = await self.expression()
            self.eat(DiceToken.RPAREN)

            # Check for Clamp immediately after closing parenthesis
            if self.current_token.type == DiceToken.CLAMP:
                clamp_token = self.current_token

                # Calculate the range of tokens inside the parentheses to reconstruct the string
                # The current token is CLAMP, so self.lexer.current points to the token AFTER CLAMP.
                # We want the tokens between LPAREN (start_token_index) and RPAREN (current - 2).
                end_token_index = self.lexer.current - 2

                group_tokens = self.lexer.tokens[start_token_index:end_token_index]
                group_str = "".join(t.raw for t in group_tokens)

                self.eat(DiceToken.CLAMP)
                result = self._apply_clamp(result, clamp_token.raw, context_str=f"({group_str})")

            return result

        elif token.type == DiceToken.PLUS:
            self.eat(DiceToken.PLUS)
            return await self.atom()

        elif token.type == DiceToken.MINUS:
            self.eat(DiceToken.MINUS)
            return -await self.atom()

        else:
            # If we hit EOF or something else unexpectedly
            if token.type == DiceToken.EOF:
                raise ValueError("Unexpected end of expression")
            raise ValueError(f"Unexpected token: {token.raw or token.type}")

    async def _roll_dice(self, dice_str: str) -> int:
        # Re-parse the specific dice string to get components
        match = re.match(r'(\d+)?d(\d+)(kh|kl)?(\d+)?', dice_str, re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid dice notation: {dice_str}")

        num_dice_str, num_sides_str = match.group(1), match.group(2)
        num_dice = int(num_dice_str) if num_dice_str else 1
        num_sides = int(num_sides_str)

        if num_dice <= 0 or num_sides <= 0:
            self.breakdown.append(f"{dice_str}: ` 0 ` -> Result **0**")
            return 0

        keep_mode = (match.group(3) or '').lower()
        keep_count = int(match.group(4)) if match.group(4) else 0

        if not (num_dice <= 300 and num_sides <= 5000):
            raise ValueError("Dice or side count is out of range (max 300 dice, max 5000 sides).")
        if keep_count and keep_count > num_dice:
            raise ValueError("Cannot keep more dice than are rolled.")

        def _roll_thread() -> Tuple[List[int], Optional[List[int]]]:
            rolls1 = [random.randint(1, num_sides) for _ in range(num_dice)]
            if self.advantage or self.disadvantage:
                rolls2 = [random.randint(1, num_sides) for _ in range(num_dice)]
                return rolls1, rolls2
            return rolls1, None

        rolls1, rolls2 = await asyncio.to_thread(_roll_thread)

        # Advantage/Disadvantage Logic
        if (self.advantage or self.disadvantage) and rolls2 is not None:
            sum1, sum2 = sum(rolls1), sum(rolls2)
            if self.advantage:
                chosen_rolls, chosen_sum = (rolls1, sum1) if sum1 >= sum2 else (rolls2, sum2)
                other_rolls, other_sum = (rolls2, sum2) if sum1 >= sum2 else (rolls1, sum1)
            else:
                chosen_rolls, chosen_sum = (rolls1, sum1) if sum1 <= sum2 else (rolls2, sum2)
                other_rolls, other_sum = (rolls2, sum2) if sum1 <= sum2 else (rolls1, sum1)

            description = (f"{dice_str} (Adv/Dis): Rolled `{', '.join(map(str, chosen_rolls))}` (Σ={chosen_sum}) "
                           f"and `{', '.join(map(str, other_rolls))}` (Σ={other_sum}). Kept **{chosen_sum}**.")
            self.breakdown.append(description)
            return chosen_sum

        # Standard Roll Logic
        rolls = rolls1
        description = f"{dice_str}: ` {', '.join(map(str, rolls))} `"

        kept_rolls = rolls
        if keep_mode in ('kh', 'kl') and keep_count > 0:
            sorted_rolls = sorted(rolls, reverse=(keep_mode == 'kh'))
            kept_rolls = sorted_rolls[:keep_count]
            discarded = sorted_rolls[keep_count:]

            kept_str = ', '.join(f"**{x}**" for x in kept_rolls)
            discarded_str = f" (Discarded {', '.join(f'**{x}**' for x in discarded)})" if discarded else ""
            description += f" -> Kept {kept_str}{discarded_str}"

        result_sum = sum(kept_rolls)
        description += f" -> Result **{result_sum}**"
        self.breakdown.append(description)
        return result_sum

    async def _flip_coin(self, coin_str: str) -> int:
        match = re.match(r'(\d*)c', coin_str, re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid coin notation: {coin_str}")

        num_coins_str = match.group(1)
        num_coins = int(num_coins_str) if num_coins_str else 1

        if not (1 <= num_coins <= 200):
            raise ValueError("Coin count is out of range (1-200 coins).")

        heads_prob = self.sp / 100.0

        def _flip_thread() -> List[int]:
            return [1 if random.random() < heads_prob else 0 for _ in range(num_coins)]

        flips = await asyncio.to_thread(_flip_thread)
        heads_count = sum(flips)
        flip_results_display = "".join(['H' if r == 1 else 'T' for r in flips])

        description = f"{coin_str}: `{flip_results_display}` ({heads_count}H, {num_coins - heads_count}T) -> Result **{heads_count}**"
        self.breakdown.append(description)
        return heads_count

    def _apply_clamp(self, value: float, suffix: str, context_str: str = "") -> float:
        min_val = None
        max_val = None

        mn_matches = re.findall(r'mn(\d+)', suffix, re.IGNORECASE)
        mx_matches = re.findall(r'mx(\d+)', suffix, re.IGNORECASE)

        if mn_matches:
            min_val = int(mn_matches[-1])
        if mx_matches:
            max_val = int(mx_matches[-1])

        if min_val is not None and max_val is not None and max_val < min_val:
            raise ValueError(f"Maximum ({max_val}) cannot be less than minimum ({min_val}).")

        original_value = value
        clamped_value = value

        if min_val is not None:
            clamped_value = max(min_val, clamped_value)
        if max_val is not None:
            clamped_value = min(max_val, clamped_value)

        limits = []
        if min_val is not None:
            limits.append(f"Min {min_val}")
        if max_val is not None:
            limits.append(f"Max {max_val}")

        if limits:
            # Format numbers nicely for display
            orig_str = str(int(original_value)) if original_value == int(original_value) else f"{original_value:.2f}"
            clamp_str = str(int(clamped_value)) if clamped_value == int(clamped_value) else f"{clamped_value:.2f}"

            prefix = f"`{context_str}`: " if context_str else ""

            if clamped_value != original_value:
                description = f"{prefix}Clamped **{orig_str}** to **{clamp_str}** ({', '.join(limits)})"
            else:
                description = f"{prefix}Result **{orig_str}** ({', '.join(limits)})"

            # Try to merge with previous line if it matches the value being clamped
            merged = False
            if self.breakdown and not context_str:  # Only merge if we don't have a specific context string
                last_line = self.breakdown[-1]
                expected_suffix = f" -> Result **{orig_str}**"

                if last_line.endswith(expected_suffix):
                    new_line = last_line[:-len(expected_suffix)] + f" -> {description}"
                    self.breakdown[-1] = new_line
                    merged = True

            if not merged:
                self.breakdown.append(description)

        return clamped_value


async def setup(bot: SanchoBot) -> None:
    """Standard setup function for the cog.

    Args:
        bot (SanchoBot): The bot instance.
    """
    await bot.add_cog(Math(bot))

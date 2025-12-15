"""Unit tests for the Math cog.

This module contains comprehensive tests for the mathematical system, including:
- safe_eval_math function tests (operators, functions, constants, security)
- Calculate command tests
- Dice rolling tests (DiceLexer, DiceParser)
- Dice notation tests (standard, keep highest/lowest, exploding, clamping)
- Coin flip tests
- Advantage/Disadvantage mechanics tests
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord.ext import commands

from cogs.math import Math, DiceToken, DiceLexer, DiceParser, safe_eval_math
from utils.bot_class import CoreBot
from utils.database import DatabaseManager


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_bot():
    """Create a mock CoreBot instance."""
    bot = MagicMock(spec=CoreBot)
    bot.db_manager = AsyncMock(spec=DatabaseManager)
    bot.loop = MagicMock()
    return bot


@pytest.fixture
def math_cog(mock_bot):
    """Create a Math cog instance with mocked bot."""
    cog = Math(mock_bot)
    return cog


@pytest.fixture
def mock_ctx(mock_bot):
    """Create a mock command context."""
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = mock_bot
    ctx.author.id = 12345
    ctx.author.display_name = "TestUser"
    ctx.author.mention = "<@12345>"
    ctx.channel.id = 67890

    # Mock send to return a mock message
    async def send_mock(*args, **kwargs):
        msg = MagicMock()
        return msg
    ctx.send = AsyncMock(side_effect=send_mock)

    return ctx


# =============================================================================
# SAFE_EVAL_MATH TESTS
# =============================================================================


class TestSafeEvalMath:
    """Tests for the safe_eval_math function."""

    # -------------------------------------------------------------------------
    # Basic Operators
    # -------------------------------------------------------------------------

    def test_addition(self):
        """Test basic addition."""
        assert safe_eval_math("2 + 3") == 5

    def test_subtraction(self):
        """Test basic subtraction."""
        assert safe_eval_math("10 - 4") == 6

    def test_multiplication(self):
        """Test basic multiplication."""
        assert safe_eval_math("6 * 7") == 42

    def test_division(self):
        """Test basic division."""
        assert safe_eval_math("20 / 4") == 5.0

    def test_power(self):
        """Test exponentiation."""
        assert safe_eval_math("2 ** 8") == 256

    def test_modulo(self):
        """Test modulo operator."""
        assert safe_eval_math("17 % 5") == 2

    def test_unary_minus(self):
        """Test unary negation."""
        assert safe_eval_math("-5") == -5

    def test_complex_expression(self):
        """Test complex expression with multiple operators."""
        assert safe_eval_math("(2 + 3) * 4 - 10 / 2") == 15.0

    def test_nested_parentheses(self):
        """Test nested parentheses."""
        assert safe_eval_math("((2 + 3) * (4 - 1)) ** 2") == 225

    # -------------------------------------------------------------------------
    # Mathematical Functions
    # -------------------------------------------------------------------------

    def test_sqrt(self):
        """Test square root function."""
        assert safe_eval_math("sqrt(16)") == 4.0

    def test_sin(self):
        """Test sine function."""
        result = safe_eval_math("sin(0)")
        assert result is not None
        assert abs(result) < 0.0001  # sin(0) = 0

    def test_cos(self):
        """Test cosine function."""
        result = safe_eval_math("cos(0)")
        assert result is not None
        assert abs(result - 1.0) < 0.0001  # cos(0) = 1

    def test_abs(self):
        """Test absolute value."""
        assert safe_eval_math("abs(-42)") == 42

    def test_ceil(self):
        """Test ceiling function."""
        assert safe_eval_math("ceil(4.2)") == 5

    def test_floor(self):
        """Test floor function."""
        assert safe_eval_math("floor(4.8)") == 4

    def test_round(self):
        """Test round function."""
        assert safe_eval_math("round(4.5)") == 4  # Python banker's rounding
        assert safe_eval_math("round(4.6)") == 5

    def test_log(self):
        """Test natural logarithm."""
        result = safe_eval_math("log(e)")
        assert result is not None
        assert abs(result - 1.0) < 0.0001  # ln(e) = 1

    def test_exp(self):
        """Test exponential function."""
        result = safe_eval_math("exp(0)")
        assert result is not None
        assert abs(result - 1.0) < 0.0001  # e^0 = 1

    def test_pow_function(self):
        """Test pow function (as opposed to ** operator)."""
        assert safe_eval_math("pow(2, 10)") == 1024

    # -------------------------------------------------------------------------
    # Mathematical Constants
    # -------------------------------------------------------------------------

    def test_pi_constant(self):
        """Test pi constant."""
        import math
        result = safe_eval_math("pi")
        assert result is not None
        assert abs(result - math.pi) < 0.0001

    def test_e_constant(self):
        """Test e constant."""
        import math
        result = safe_eval_math("e")
        assert result is not None
        assert abs(result - math.e) < 0.0001

    def test_c_constant(self):
        """Test speed of light constant."""
        result = safe_eval_math("c")
        assert result == 299792458  # Speed of light in m/s

    def test_avogadro_constant(self):
        """Test Avogadro's number constant."""
        result = safe_eval_math("avogadro")
        assert result == 6.02214076e23

    def test_expression_with_constants(self):
        """Test expression using constants."""
        import math
        result = safe_eval_math("2 * pi")
        assert result is not None
        assert abs(result - 2 * math.pi) < 0.0001

    # -------------------------------------------------------------------------
    # Error Handling
    # -------------------------------------------------------------------------

    def test_division_by_zero(self):
        """Test division by zero raises error."""
        with pytest.raises(ValueError, match="Math error"):
            safe_eval_math("1 / 0")

    def test_invalid_expression(self):
        """Test invalid expression raises error."""
        with pytest.raises(ValueError, match="Invalid expression"):
            safe_eval_math("2 +")

    def test_disallowed_function(self):
        """Test that disallowed functions raise errors."""
        with pytest.raises(ValueError, match="not allowed"):
            safe_eval_math("eval('1+1')")

    def test_disallowed_name(self):
        """Test that disallowed names raise errors."""
        with pytest.raises(ValueError, match="not allowed"):
            safe_eval_math("__import__('os')")

    def test_attribute_access_blocked(self):
        """Test that attribute access is blocked."""
        with pytest.raises(ValueError, match="not allowed"):
            safe_eval_math("().__class__")

    def test_subscript_access_blocked(self):
        """Test that subscript access is blocked."""
        with pytest.raises(ValueError, match="not allowed"):
            safe_eval_math("[1,2,3][0]")

    def test_empty_expression(self):
        """Test empty expression returns None."""
        result = safe_eval_math("")
        assert result is None

    def test_whitespace_only_expression(self):
        """Test whitespace-only expression returns None."""
        result = safe_eval_math("   ")
        assert result is None


# =============================================================================
# DICE LEXER TESTS
# =============================================================================


class TestDiceLexer:
    """Tests for the DiceLexer class."""

    def test_simple_dice(self):
        """Test lexing simple dice notation."""
        lexer = DiceLexer("1d20")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "1d20"
        assert tokens[1].type == DiceToken.EOF

    def test_implicit_one_dice(self):
        """Test lexing dice notation without count (d20)."""
        lexer = DiceLexer("d20")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "d20"

    def test_multiple_dice(self):
        """Test lexing multiple dice notation."""
        lexer = DiceLexer("4d6")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "4d6"

    def test_dice_with_keep_highest(self):
        """Test lexing dice with keep highest modifier."""
        lexer = DiceLexer("4d6kh3")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "4d6kh3"

    def test_dice_with_keep_lowest(self):
        """Test lexing dice with keep lowest modifier."""
        lexer = DiceLexer("2d20kl1")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "2d20kl1"

    def test_exploding_dice(self):
        """Test lexing exploding dice notation."""
        lexer = DiceLexer("2d6!")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.DICE
        assert tokens[0].raw == "2d6!"

    def test_coin_flip(self):
        """Test lexing coin flip notation."""
        lexer = DiceLexer("3c")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.COIN
        assert tokens[0].raw == "3c"

    def test_single_coin(self):
        """Test lexing single coin flip."""
        lexer = DiceLexer("c")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.COIN
        assert tokens[0].raw == "c"

    def test_number(self):
        """Test lexing plain numbers."""
        lexer = DiceLexer("42")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.NUMBER
        assert tokens[0].value == 42.0

    def test_decimal_number(self):
        """Test lexing decimal numbers."""
        lexer = DiceLexer("3.14")
        tokens = lexer.tokens
        assert tokens[0].type == DiceToken.NUMBER
        assert tokens[0].value == 3.14

    def test_operators(self):
        """Test lexing operators."""
        lexer = DiceLexer("1+2-3*4/5%6^7")
        types = [t.type for t in lexer.tokens]
        assert DiceToken.PLUS in types
        assert DiceToken.MINUS in types
        assert DiceToken.MULTIPLY in types
        assert DiceToken.DIVIDE in types
        assert DiceToken.MODULO in types
        assert DiceToken.POWER in types

    def test_power_double_star(self):
        """Test lexing ** as power operator."""
        lexer = DiceLexer("2**3")
        types = [t.type for t in lexer.tokens]
        assert DiceToken.POWER in types

    def test_parentheses(self):
        """Test lexing parentheses."""
        lexer = DiceLexer("(1+2)")
        types = [t.type for t in lexer.tokens]
        assert DiceToken.LPAREN in types
        assert DiceToken.RPAREN in types

    def test_clamp_notation(self):
        """Test lexing clamp notation."""
        lexer = DiceLexer("(1d20)mn5mx15")
        types = [t.type for t in lexer.tokens]
        assert DiceToken.CLAMP in types

    def test_complex_expression(self):
        """Test lexing complex expression."""
        lexer = DiceLexer("2d6+1d8+5")
        types = [t.type for t in lexer.tokens]
        assert types.count(DiceToken.DICE) == 2
        assert types.count(DiceToken.PLUS) == 2
        assert types.count(DiceToken.NUMBER) == 1

    def test_x_as_multiply(self):
        """Test that 'x' is recognized as multiply operator."""
        lexer = DiceLexer("2x3")
        types = [t.type for t in lexer.tokens]
        assert DiceToken.MULTIPLY in types


# =============================================================================
# DICE PARSER TESTS
# =============================================================================


class TestDiceParser:
    """Tests for the DiceParser class."""

    @pytest.mark.asyncio
    async def test_simple_number(self):
        """Test parsing a simple number."""
        lexer = DiceLexer("42")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 42.0

    @pytest.mark.asyncio
    async def test_addition(self):
        """Test parsing addition."""
        lexer = DiceLexer("10+5")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 15.0

    @pytest.mark.asyncio
    async def test_subtraction(self):
        """Test parsing subtraction."""
        lexer = DiceLexer("20-8")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 12.0

    @pytest.mark.asyncio
    async def test_multiplication(self):
        """Test parsing multiplication."""
        lexer = DiceLexer("6*7")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 42.0

    @pytest.mark.asyncio
    async def test_division(self):
        """Test parsing division."""
        lexer = DiceLexer("100/4")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 25.0

    @pytest.mark.asyncio
    async def test_division_by_zero(self):
        """Test that division by zero raises error."""
        lexer = DiceLexer("10/0")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="Division by zero"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_modulo(self):
        """Test parsing modulo."""
        lexer = DiceLexer("17%5")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 2.0

    @pytest.mark.asyncio
    async def test_modulo_by_zero(self):
        """Test that modulo by zero raises error."""
        lexer = DiceLexer("10%0")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="Modulo by zero"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_power(self):
        """Test parsing power."""
        lexer = DiceLexer("2^8")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 256.0

    @pytest.mark.asyncio
    async def test_unary_minus(self):
        """Test parsing unary minus."""
        lexer = DiceLexer("-5")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == -5.0

    @pytest.mark.asyncio
    async def test_unary_plus(self):
        """Test parsing unary plus."""
        lexer = DiceLexer("+5")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 5.0

    @pytest.mark.asyncio
    async def test_parentheses(self):
        """Test parsing parentheses."""
        lexer = DiceLexer("(2+3)*4")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 20.0

    @pytest.mark.asyncio
    async def test_order_of_operations(self):
        """Test correct order of operations."""
        lexer = DiceLexer("2+3*4")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 14.0  # Not 20

    @pytest.mark.asyncio
    async def test_unexpected_eof(self):
        """Test that unexpected EOF raises error."""
        lexer = DiceLexer("")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="Unexpected end"):
            await parser.parse()


# =============================================================================
# DICE ROLLING TESTS
# =============================================================================


class TestDiceRolling:
    """Tests for dice rolling functionality."""

    @pytest.mark.asyncio
    async def test_simple_dice_roll_range(self):
        """Test that dice rolls are within expected range."""
        lexer = DiceLexer("1d6")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert 1 <= result <= 6

    @pytest.mark.asyncio
    async def test_multiple_dice_roll_range(self):
        """Test that multiple dice sum is within range."""
        lexer = DiceLexer("2d6")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert 2 <= result <= 12

    @pytest.mark.asyncio
    async def test_dice_with_modifier(self):
        """Test dice roll with modifier."""
        with patch('random.randint', return_value=10):
            lexer = DiceLexer("1d20+5")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 15.0

    @pytest.mark.asyncio
    async def test_keep_highest(self):
        """Test keep highest mechanic (4d6kh3)."""
        # Patch random to return predictable values
        with patch('random.randint', side_effect=[6, 5, 4, 3]):  # First roll set
            lexer = DiceLexer("4d6kh3")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 15  # 6 + 5 + 4 = 15, drop the 3

    @pytest.mark.asyncio
    async def test_keep_lowest(self):
        """Test keep lowest mechanic (2d20kl1)."""
        with patch('random.randint', side_effect=[15, 8]):
            lexer = DiceLexer("2d20kl1")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 8  # Keep the lower roll

    @pytest.mark.asyncio
    async def test_exploding_dice(self):
        """Test exploding dice (reroll on max)."""
        # Mock: first roll is 6 (explodes), second roll is 3 (stops)
        with patch('random.randint', side_effect=[6, 3]):
            lexer = DiceLexer("1d6!")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 9  # 6 + 3 = 9

    @pytest.mark.asyncio
    async def test_dice_count_limit(self):
        """Test that excessive dice count raises error."""
        lexer = DiceLexer("500d6")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="out of range"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_dice_sides_limit(self):
        """Test that excessive sides raises error."""
        lexer = DiceLexer("1d10000")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="out of range"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_keep_more_than_rolled(self):
        """Test that keeping more dice than rolled raises error."""
        lexer = DiceLexer("2d6kh5")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="Cannot keep more"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_exploding_one_sided_dice(self):
        """Test that exploding 1-sided dice raises error."""
        lexer = DiceLexer("1d1!")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="at least 2 sides"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_zero_dice(self):
        """Test that zero dice returns zero."""
        lexer = DiceLexer("0d6")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result == 0

    @pytest.mark.asyncio
    async def test_breakdown_recorded(self):
        """Test that roll breakdown is recorded."""
        with patch('random.randint', return_value=4):
            lexer = DiceLexer("1d6")
            parser = DiceParser(lexer)
            await parser.parse()
            assert len(parser.breakdown) == 1
            assert "1d6" in parser.breakdown[0]


# =============================================================================
# ADVANTAGE/DISADVANTAGE TESTS
# =============================================================================


class TestAdvantageDisadvantage:
    """Tests for advantage and disadvantage mechanics."""

    @pytest.mark.asyncio
    async def test_advantage_picks_higher(self):
        """Test that advantage picks the higher roll."""
        # First set: sum=5, Second set: sum=12
        with patch('random.randint', side_effect=[3, 2, 7, 5]):
            lexer = DiceLexer("2d10")
            parser = DiceParser(lexer, advantage=True)
            result = await parser.parse()
            assert result == 12  # Higher sum is kept

    @pytest.mark.asyncio
    async def test_disadvantage_picks_lower(self):
        """Test that disadvantage picks the lower roll."""
        # First set: sum=5, Second set: sum=12
        with patch('random.randint', side_effect=[3, 2, 7, 5]):
            lexer = DiceLexer("2d10")
            parser = DiceParser(lexer, disadvantage=True)
            result = await parser.parse()
            assert result == 5  # Lower sum is kept

    @pytest.mark.asyncio
    async def test_advantage_breakdown_shows_both_rolls(self):
        """Test that advantage breakdown shows both roll sets."""
        with patch('random.randint', side_effect=[15, 8]):
            lexer = DiceLexer("1d20")
            parser = DiceParser(lexer, advantage=True)
            await parser.parse()
            assert len(parser.breakdown) == 1
            assert "Adv/Dis" in parser.breakdown[0]


# =============================================================================
# COIN FLIP TESTS
# =============================================================================


class TestCoinFlip:
    """Tests for coin flip functionality."""

    @pytest.mark.asyncio
    async def test_single_coin_flip(self):
        """Test single coin flip returns 0 or 1."""
        lexer = DiceLexer("c")
        parser = DiceParser(lexer)
        result = await parser.parse()
        assert result in [0, 1]

    @pytest.mark.asyncio
    async def test_multiple_coins(self):
        """Test multiple coin flips return sum of heads."""
        with patch('random.random', side_effect=[0.3, 0.7, 0.4]):  # H, T, H
            lexer = DiceLexer("3c")
            parser = DiceParser(lexer, sp=50)
            result = await parser.parse()
            assert result == 2  # 2 heads

    @pytest.mark.asyncio
    async def test_coin_count_limit(self):
        """Test that excessive coin count raises error."""
        lexer = DiceLexer("500c")
        parser = DiceParser(lexer)
        with pytest.raises(ValueError, match="out of range"):
            await parser.parse()

    @pytest.mark.asyncio
    async def test_sp_parameter_biased_coin(self):
        """Test that sp parameter biases coin flip."""
        # With sp=100, all flips should be heads
        with patch('random.random', return_value=0.99):
            lexer = DiceLexer("5c")
            parser = DiceParser(lexer, sp=100)
            result = await parser.parse()
            assert result == 5  # All heads

    @pytest.mark.asyncio
    async def test_coin_breakdown_recorded(self):
        """Test that coin flip breakdown is recorded."""
        with patch('random.random', side_effect=[0.3, 0.7]):
            lexer = DiceLexer("2c")
            parser = DiceParser(lexer, sp=50)
            await parser.parse()
            assert len(parser.breakdown) == 1
            assert "2c" in parser.breakdown[0]


# =============================================================================
# CLAMP TESTS
# =============================================================================


class TestClamp:
    """Tests for clamping functionality."""

    @pytest.mark.asyncio
    async def test_clamp_minimum(self):
        """Test clamping to minimum value."""
        # Roll a 2, clamp to minimum 5
        with patch('random.randint', return_value=2):
            lexer = DiceLexer("(1d6)mn5")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 5

    @pytest.mark.asyncio
    async def test_clamp_maximum(self):
        """Test clamping to maximum value."""
        # Roll a 6, clamp to maximum 4
        with patch('random.randint', return_value=6):
            lexer = DiceLexer("(1d6)mx4")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 4

    @pytest.mark.asyncio
    async def test_clamp_both_min_max(self):
        """Test clamping with both min and max."""
        with patch('random.randint', return_value=1):
            lexer = DiceLexer("(1d20)mn5mx15")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 5  # Clamped up to min

    @pytest.mark.asyncio
    async def test_clamp_within_range(self):
        """Test that value within clamp range is unchanged."""
        with patch('random.randint', return_value=10):
            lexer = DiceLexer("(1d20)mn5mx15")
            parser = DiceParser(lexer)
            result = await parser.parse()
            assert result == 10  # Unchanged

    @pytest.mark.asyncio
    async def test_clamp_invalid_range(self):
        """Test that invalid clamp range raises error."""
        with patch('random.randint', return_value=10):
            lexer = DiceLexer("(1d20)mn15mx5")
            parser = DiceParser(lexer)
            with pytest.raises(ValueError, match="cannot be less than"):
                await parser.parse()

    @pytest.mark.asyncio
    async def test_clamp_breakdown_shows_clamping(self):
        """Test that clamp is recorded in breakdown."""
        with patch('random.randint', return_value=2):
            lexer = DiceLexer("(1d6)mn5")
            parser = DiceParser(lexer)
            await parser.parse()
            # Should have breakdown showing the clamping
            assert any("Clamped" in b or "mn" in b.lower() for b in parser.breakdown)


# =============================================================================
# CALCULATE COMMAND TESTS
# =============================================================================


class TestCalculateCommand:
    """Tests for the calculate NLP command."""

    @pytest.mark.asyncio
    async def test_simple_calculation(self, math_cog, mock_ctx):
        """Test simple math calculation."""
        await math_cog.calculate(mock_ctx, query="2 + 2")
        mock_ctx.send.assert_called_once()
        call_args = mock_ctx.send.call_args[0][0]
        assert "4" in call_args

    @pytest.mark.asyncio
    async def test_calculation_with_function(self, math_cog, mock_ctx):
        """Test calculation with math function."""
        await math_cog.calculate(mock_ctx, query="sqrt(16)")
        mock_ctx.send.assert_called_once()
        call_args = mock_ctx.send.call_args[0][0]
        assert "4" in call_args

    @pytest.mark.asyncio
    async def test_calculation_error_handling(self, math_cog, mock_ctx):
        """Test that calculation errors are handled gracefully."""
        await math_cog.calculate(mock_ctx, query="1/0")
        mock_ctx.send.assert_called_once()
        call_args = mock_ctx.send.call_args[0][0]
        assert "error" in call_args.lower() or "Error" in call_args

    @pytest.mark.asyncio
    async def test_calculation_with_extract_pattern(self, math_cog, mock_ctx):
        """Test calculation extracts expression from natural language."""
        await math_cog.calculate(mock_ctx, query="what is 5 * 10?")
        mock_ctx.send.assert_called_once()
        call_args = mock_ctx.send.call_args[0][0]
        assert "50" in call_args


# =============================================================================
# ROLL COMMAND TESTS
# =============================================================================


class TestRollCommand:
    """Tests for the roll NLP command."""

    @pytest.mark.asyncio
    async def test_simple_roll(self, math_cog, mock_ctx):
        """Test simple dice roll."""
        with patch('random.randint', return_value=15):
            await math_cog.roll(mock_ctx, query="1d20")
            mock_ctx.send.assert_called_once()
            call_args = mock_ctx.send.call_args[0][0]
            assert "15" in call_args

    @pytest.mark.asyncio
    async def test_roll_with_modifier(self, math_cog, mock_ctx):
        """Test dice roll with modifier."""
        with patch('random.randint', return_value=10):
            await math_cog.roll(mock_ctx, query="1d20+5")
            mock_ctx.send.assert_called_once()
            call_args = mock_ctx.send.call_args[0][0]
            assert "15" in call_args

    @pytest.mark.asyncio
    async def test_roll_error_handling(self, math_cog, mock_ctx):
        """Test that roll errors are handled gracefully."""
        await math_cog.roll(mock_ctx, query="500d20")  # Too many dice
        mock_ctx.send.assert_called_once()
        call_args = mock_ctx.send.call_args[0][0]
        assert "Error" in call_args or "error" in call_args.lower()

    @pytest.mark.asyncio
    async def test_roll_too_long_response(self, math_cog, mock_ctx):
        """Test that excessively long roll results are handled."""
        # Roll many dice to generate a long response
        with patch.object(math_cog, 'evaluate_roll') as mock_eval:
            mock_eval.return_value = {
                'total': '500',
                'breakdown': ['x' * 4000],  # Very long breakdown
                'processed_query': '100d20'
            }
            await math_cog.roll(mock_ctx, query="100d20")
            mock_ctx.send.assert_called_once()
            call_args = mock_ctx.send.call_args[0][0]
            assert "too long" in call_args.lower()


# =============================================================================
# GET_ROLL_RESULT TESTS
# =============================================================================


class TestGetRollResult:
    """Tests for the get_roll_result helper method."""

    @pytest.mark.asyncio
    async def test_get_roll_result_returns_integer(self, math_cog):
        """Test that get_roll_result returns an integer."""
        with patch('random.randint', return_value=15):
            result = await math_cog.get_roll_result("1d20")
            assert isinstance(result, int)
            assert result == 15

    @pytest.mark.asyncio
    async def test_get_roll_result_with_modifier(self, math_cog):
        """Test get_roll_result with arithmetic."""
        with patch('random.randint', return_value=10):
            result = await math_cog.get_roll_result("1d20+5")
            assert result == 15

    @pytest.mark.asyncio
    async def test_get_roll_result_complex(self, math_cog):
        """Test get_roll_result with complex expression."""
        with patch('random.randint', side_effect=[3, 4]):
            result = await math_cog.get_roll_result("2d6")
            assert result == 7


# =============================================================================
# EVALUATE_ROLL TESTS
# =============================================================================


class TestEvaluateRoll:
    """Tests for the evaluate_roll method."""

    @pytest.mark.asyncio
    async def test_evaluate_roll_returns_dict(self, math_cog):
        """Test that evaluate_roll returns expected dict structure."""
        with patch('random.randint', return_value=10):
            result = await math_cog.evaluate_roll("1d20")
            assert 'total' in result
            assert 'breakdown' in result
            assert 'processed_query' in result

    @pytest.mark.asyncio
    async def test_evaluate_roll_with_advantage(self, math_cog):
        """Test evaluate_roll with advantage keyword."""
        with patch('random.randint', side_effect=[10, 15]):
            result = await math_cog.evaluate_roll("1d20 adv")
            assert int(result['total']) == 15  # Higher value kept

    @pytest.mark.asyncio
    async def test_evaluate_roll_with_disadvantage(self, math_cog):
        """Test evaluate_roll with disadvantage keyword."""
        with patch('random.randint', side_effect=[10, 15]):
            result = await math_cog.evaluate_roll("1d20 dis")
            assert int(result['total']) == 10  # Lower value kept

    @pytest.mark.asyncio
    async def test_evaluate_roll_with_sp(self, math_cog):
        """Test evaluate_roll with success probability for coins."""
        with patch('random.random', return_value=0.3):  # Below 75%
            result = await math_cog.evaluate_roll("1c sp75")
            assert int(result['total']) == 1  # Should be heads with sp75

    @pytest.mark.asyncio
    async def test_evaluate_roll_strips_keywords(self, math_cog):
        """Test that keywords are stripped from processed query."""
        with patch('random.randint', return_value=10):
            result = await math_cog.evaluate_roll("1d20 advantage")
            # The advantage keyword should be stripped from processed_query
            assert 'advantage' not in result['processed_query'].lower() or 'adv' not in result['processed_query'].lower()


# =============================================================================
# INTEGRATION TESTS
# =============================================================================


class TestIntegration:
    """Integration tests for the Math cog."""

    @pytest.mark.asyncio
    async def test_complex_dice_expression(self, math_cog):
        """Test complex dice expression with multiple components."""
        with patch('random.randint', side_effect=[6, 5, 4, 3, 8]):
            result = await math_cog.evaluate_roll("4d6kh3 + 1d8 + 5")
            total = int(result['total'])
            # 6+5+4 (keep highest 3 of 4d6) + 8 (1d8) + 5 = 28
            assert total == 28

    @pytest.mark.asyncio
    async def test_mixed_dice_and_math(self, math_cog):
        """Test mixing dice rolls with arithmetic."""
        with patch('random.randint', return_value=5):
            result = await math_cog.evaluate_roll("(1d6 + 2) * 2")
            total = float(result['total'])
            # (5 + 2) * 2 = 14
            assert total == 14.0

    @pytest.mark.asyncio
    async def test_coin_and_dice_together(self, math_cog):
        """Test coin flips and dice rolls in same expression."""
        with patch('random.randint', return_value=10):
            with patch('random.random', return_value=0.3):
                result = await math_cog.evaluate_roll("1d20 + 2c")
                total = int(result['total'])
                # 10 (d20) + 2 (both coins heads at sp50 with random=0.3)
                assert total == 12

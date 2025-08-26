import discord
from discord.ext import commands
import random
import re

class DiceRoller(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def roll(self, ctx, *, roll_string: str):
        """Rolls dice with support for advantage/disadvantage and modifiers."""
        try:
            clean_string = roll_string.lower().replace('roll', '').replace('dice', '').strip()
            has_advantage = 'advantage' in clean_string
            has_disadvantage = 'disadvantage' in clean_string
            clean_string = clean_string.replace('with advantage', '').replace('with disadvantage', '').strip()

            dice_match = re.search(r'(\d+)?d(\d+)', clean_string)
            if not dice_match:
                await ctx.send("I couldn't find a valid dice format like `2d6` or `d20` in your request.")
                return

            num_dice_str = dice_match.group(1)
            num_sides_str = dice_match.group(2)
            
            num_dice = int(num_dice_str) if num_dice_str else 1
            num_sides = int(num_sides_str) if num_sides_str else 0

            if num_dice < 1 or num_sides < 1:
                await ctx.send("You can't roll zero or negative dice or sides!")
                return
            if num_dice > 1000 or num_sides > 10000:
                await ctx.send("Please keep dice under 1000 and sides under 10000!")
                return

            modifier_match = re.search(r'([+-])\s*(\d+)', clean_string)
            modifier = 0
            if modifier_match:
                op = modifier_match.group(1)
                val = int(modifier_match.group(2))
                modifier = val if op == '+' else -val

            rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
            result = sum(rolls)
            roll_explanation = f"Rolls: {rolls}"

            if has_advantage and has_disadvantage:
                await ctx.send("You can't roll with both advantage and disadvantage at the same time!")
                return
            
            if has_advantage:
                rolls2 = [random.randint(1, num_sides) for _ in range(num_dice)]
                result2 = sum(rolls2)
                result = max(result, result2)
                roll_explanation = f"Advantage Rolls: {rolls} vs {rolls2}"

            if has_disadvantage:
                rolls2 = [random.randint(1, num_sides) for _ in range(num_dice)]
                result2 = sum(rolls2)
                result = min(result, result2)
                roll_explanation = f"Disadvantage Rolls: {rolls} vs {rolls2}"

            final_result = result + modifier
            
            response = f"{ctx.author.mention}, you rolled: **{final_result}**\n"
            response += f"*{roll_explanation}*"
            if modifier != 0:
                response += f" (Modifier: {modifier:+})"

            await ctx.send(response)

        except Exception as e:
            # This is a fallback for unexpected errors during parsing
            await ctx.send("I had trouble understanding that roll. Please try a format like `d20+5` or `2d6 with advantage`.")
            raise e # Re-raise the exception so the main error handler can log it

async def setup(bot):
    await bot.add_cog(DiceRoller(bot))
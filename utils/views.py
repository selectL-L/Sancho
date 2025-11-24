"""utils/views.py

This module contains reusable UI components (Views) for Discord interactions.
It provides standard selection menus and button interfaces used across multiple cogs.
"""

import discord
import asyncio
from typing import Optional, Dict, cast

class SelectionView(discord.ui.View):
    def __init__(self, ctx, options: Dict[str, str], timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.value: Optional[str] = None
        self.message = None

        for label, val in options.items():
            self.add_item(SelectionButton(label, val))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True
    
    async def on_timeout(self):
        self.stop()

class SelectionButton(discord.ui.Button):
    def __init__(self, label: str, value: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label)
        self.custom_value = value

    async def callback(self, interaction: discord.Interaction):
        assert self.view is not None
        view: SelectionView = cast(SelectionView, self.view)
        view.value = self.custom_value
        await interaction.response.defer()
        view.stop()

async def get_selection(ctx, embed: discord.Embed, options: Dict[str, str], timeout: float = 30.0) -> Optional[str]:
    """
    Sends an embed with buttons corresponding to the options.
    Waits for either a button click or a message from the user.
    Returns the value of the selected option (or the message content), or None on timeout.
    """
    view = SelectionView(ctx, options, timeout)
    message = await ctx.send(embed=embed, view=view)
    view.message = message

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    view_task = asyncio.create_task(view.wait())
    msg_task = asyncio.create_task(ctx.bot.wait_for('message', check=check, timeout=timeout))

    done, pending = await asyncio.wait([view_task, msg_task], return_when=asyncio.FIRST_COMPLETED)

    result = None
    
    if view_task in done:
        # View finished (button clicked or timeout)
        if view.value:
            result = view.value
        # If timeout (view.value is None), result remains None
    else:
        # Message received
        try:
            msg = msg_task.result()
            result = msg.content.strip()
            view.stop()
        except Exception:
            pass 

    for task in pending:
        task.cancel()
    
    # Cleanup / UI Update
    if result and result in options.values():
        # Find the label for this value
        selected_label = None
        for label, val in options.items():
            if val == result:
                selected_label = label
                break
        
        if selected_label:
            # Create a new view with just this button
            view.clear_items()
            b = discord.ui.Button(style=discord.ButtonStyle.primary, label=selected_label, disabled=True)
            view.add_item(b)
            try:
                await message.edit(view=view)
            except:
                pass
    else:
        # Timeout or invalid selection (not in options) -> Remove buttons
        try:
            await message.edit(view=None)
        except:
            pass

    return result

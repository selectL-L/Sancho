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
            except Exception:
                pass
    else:
        # Timeout or invalid selection (not in options) -> Remove buttons
        try:
            await message.edit(view=None)
        except Exception:
            pass

    return result


class FastConfirmModal(discord.ui.Modal):
    def __init__(self, future: asyncio.Future):
        super().__init__(title="Confirm Fast Mode")
        # Single short text field where the user must type the exact phrase
        self.add_item(discord.ui.TextInput(label="Confirm the use of fast mode.", style=discord.TextStyle.short, placeholder="I understand the risks"))
        self.future = future

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = getattr(self.children[0], 'value', '')
        try:
            value = value.strip().lower()
        except Exception:
            value = ""
        if value == 'i understand the risks':
            await interaction.response.send_message('Fast mode confirmed — proceeding without rate limits. This IS dangerous.', ephemeral=True)
            if not self.future.done():
                self.future.set_result(True)
        else:
            await interaction.response.send_message('Fast mode cancelled (incorrect confirmation).', ephemeral=True)
            if not self.future.done():
                self.future.set_result(False)


class ModalLauncherView(discord.ui.View):
    """A view that displays a button to launch a modal.
    Ensures consistent behavior for both text and slash commands.
    """

    def __init__(self, modal: discord.ui.Modal, author: discord.User | discord.Member, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.modal = modal
        self.author = author
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Open form", style=discord.ButtonStyle.danger)
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This form is not for you.", ephemeral=True)
            return
        # We must send the modal in response to THIS interaction (the button click)
        await interaction.response.send_modal(self.modal)

    async def on_timeout(self):
        if self.message:
            try:
                for child in self.children:
                    if hasattr(child, 'disabled'):
                        setattr(child, 'disabled', True)
                await self.message.edit(view=self)
            except Exception:
                pass


async def launch_modal(ctx, modal: discord.ui.Modal):
    """
    Sends a message with a button to launch the modal.
    This ensures consistent behavior between slash and text commands.
    """
    view = ModalLauncherView(modal, ctx.author)
    message = await ctx.send("This action requires strict confirmation. Click the button below to proceed to a form.", view=view)
    view.message = message
    return message


class PaginatorView(discord.ui.View):
    """A generic view for paginating through a list of embeds."""

    def __init__(self, ctx, pages: list[discord.Embed], timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.pages = pages
        self.current_page = 0
        self.message: Optional[discord.Message] = None

        # Update button states initially
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.grey)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def on_timeout(self):
        if self.message:
            try:
                for child in self.children:
                    if hasattr(child, 'disabled'):
                        setattr(child, 'disabled', True)
                await self.message.edit(view=self)
            except Exception:
                pass

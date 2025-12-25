"""utils/views.py

This module contains reusable UI components (Views) for Discord interactions.
It provides standard selection menus and button interfaces used across multiple cogs.
"""

import discord
from discord import ui
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Callable, Awaitable, cast


# =============================================================================
# Text Utility Functions
# =============================================================================

def visual_width(s: str) -> int:
    """Calculate visual width of string (CJK/full-width chars count as 2).

    Args:
        s: The string to measure.

    Returns:
        The visual width in character units.
    """
    width = 0
    for char in s:
        if ord(char) > 0x2E7F:  # CJK and full-width characters
            width += 2
        else:
            width += 1
    return width


def truncate_visual(s: str, max_width: int) -> str:
    """Truncate string to max visual width, adding ellipsis if needed.

    Args:
        s: The string to truncate.
        max_width: Maximum visual width (including ellipsis).

    Returns:
        The truncated string.
    """
    width = 0
    for i, char in enumerate(s):
        char_width = 2 if ord(char) > 0x2E7F else 1
        if width + char_width > max_width - 3:  # Reserve space for "..."
            return s[:i] + "..."
        width += char_width
    return s


# =============================================================================
# Now Playing View (Components V2)
# =============================================================================

@dataclass
class NowPlayingState:
    """State container for now playing widget."""
    track_title: str
    track_artist: str
    track_url: str
    elapsed_str: str
    duration_str: str
    progress: float  # 0.0 to 1.0
    loop_display: str
    is_playing: bool
    is_paused: bool
    in_voice: bool
    playlist_count: int
    thumbnail_url: Optional[str] = None


# Type alias for button callbacks
NowPlayingCallback = Callable[[discord.Interaction], Awaitable[None]]


class NowPlayingView(ui.LayoutView):
    """Components V2 now playing widget with interactive controls.

    This view displays current track information with a large thumbnail,
    progress bar, and playback control buttons.
    """

    def __init__(
        self,
        state: NowPlayingState,
        on_play_pause: Optional[NowPlayingCallback] = None,
        on_next: Optional[NowPlayingCallback] = None,
        on_shuffle: Optional[NowPlayingCallback] = None,
        on_loop: Optional[NowPlayingCallback] = None,
        timeout: float = 300.0,
    ):
        """Initialize the now playing view.

        Args:
            state: Current state of the player.
            on_play_pause: Callback for play/pause button.
            on_next: Callback for next track button.
            on_shuffle: Callback for shuffle button.
            on_loop: Callback for loop mode button.
            timeout: View timeout in seconds.
        """
        super().__init__(timeout=timeout)
        self.state = state

        # Build the view
        container = ui.Container(accent_colour=discord.Colour.purple())

        # Thumbnail
        if state.thumbnail_url:
            gallery = ui.MediaGallery(
                discord.MediaGalleryItem(media=state.thumbnail_url)
            )
            container.add_item(gallery)

        # Track info
        status_emoji = "🎵" if state.in_voice else "🎧"
        status_text = "Now Playing" if state.in_voice else "Currently Listening To"

        display_title = truncate_visual(state.track_title, 48) if visual_width(state.track_title) > 48 else state.track_title
        display_artist = truncate_visual(state.track_artist, 40) if visual_width(state.track_artist) > 40 else state.track_artist

        container.add_item(ui.TextDisplay(
            f"## {status_emoji} {status_text}\n"
            f"**[{display_title}]({state.track_url})**\n"
            f"by {display_artist}"
        ))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Progress bar
        filled = int(state.progress * 20)
        bar = "▓" * filled + "░" * (20 - filled)

        container.add_item(ui.TextDisplay(
            f"`{bar}`\n"
            f"⏱ **{state.elapsed_str}** / {state.duration_str}  •  🔁 {state.loop_display}"
        ))

        # Playback buttons
        action_row = ui.ActionRow()

        # Determine play/pause emoji
        if state.is_playing:
            play_emoji = "⏸️"
        elif state.is_paused:
            play_emoji = "▶️"
        else:
            play_emoji = "▶️" if not state.in_voice else "⏸️"

        play_pause_btn = ui.Button(style=discord.ButtonStyle.primary, emoji=play_emoji, custom_id="np_playpause")
        next_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="np_next")
        shuffle_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="np_shuffle")
        loop_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="np_loop")

        # Assign callbacks (type: ignore for discord.py dynamic callback signature)
        if on_play_pause:
            play_pause_btn.callback = on_play_pause  # type: ignore[method-assign]
        if on_next:
            next_btn.callback = on_next  # type: ignore[method-assign]
        if on_shuffle:
            shuffle_btn.callback = on_shuffle  # type: ignore[method-assign]
        if on_loop:
            loop_btn.callback = on_loop  # type: ignore[method-assign]

        action_row.add_item(play_pause_btn)
        action_row.add_item(next_btn)
        action_row.add_item(shuffle_btn)
        action_row.add_item(loop_btn)

        container.add_item(action_row)

        # Footer
        if state.in_voice:
            footer = f"-# Playing in voice | {state.playlist_count} tracks in playlist"
        else:
            footer = "-# Idle mode | Type 'listen along' to play in voice!"
        container.add_item(ui.TextDisplay(footer))

        self.add_item(container)


# =============================================================================
# Selection View
# =============================================================================


class SelectionView(discord.ui.View):
    """View with multiple selection buttons."""

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


async def get_selection(ctx, embed: discord.Embed, options: Dict[str, str], timeout: float = 30.0, buttons_only: bool = False) -> Optional[str]:
    """
    Sends an embed with buttons corresponding to the options.
    Waits for either a button click or a message from the user.
    Returns the value of the selected option (or the message content), or None on timeout.

    Args:
        ctx: The context object (or pseudo-context with author, channel, bot, send()).
        embed: The embed to display with the selection buttons.
        options: Dictionary mapping button labels to return values.
        timeout: How long to wait for a selection (default 30s).
        buttons_only: If True, only respond to button clicks, ignore text messages.
    """
    view = SelectionView(ctx, options, timeout)
    message = await ctx.send(embed=embed, view=view)
    view.message = message

    if buttons_only:
        # Only wait for button interaction
        await view.wait()
        result = view.value
    else:
        # Wait for either button click or text message
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

    def __init__(self, ctx, pages: list[discord.Embed], timeout: float = 60.0, start_index: int = 0):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.pages = pages
        self.current_page = max(0, min(start_index, len(pages) - 1))  # Clamp to valid range
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

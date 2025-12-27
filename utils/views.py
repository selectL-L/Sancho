"""utils/views.py

This module contains reusable UI components (Views) for Discord interactions.
It provides standard selection menus and button interfaces used across multiple cogs.
"""

import io
from dataclasses import dataclass
from enum import Enum, auto
from typing import Awaitable, Callable, Dict, List, Optional, cast

import asyncio
import discord
from discord import ui


# =============================================================================
# Track Failure View
# =============================================================================

class TrackFailureAction(Enum):
    """Actions a user can take when a track fails to play."""
    SKIP = auto()    # Skip but keep in playlist (might work later)
    REMOVE = auto()  # Remove from playlist entirely
    TIMEOUT = auto() # User didn't respond - defaults to REMOVE


class TrackFailedView(discord.ui.View):
    """View displayed when a track fails to play, giving users options.

    This view presents Retry, Skip, and Remove buttons when a track can't
    be played (e.g., 403 errors, region locks, unavailable videos).

    The view is single-use - once a button is clicked, buttons are disabled
    and the view stops.

    Usage:
        view = TrackFailedView(track_title, track_url)
        message = await channel.send(embed=view.create_embed(), view=view)
        await view.wait()
        action = view.action  # TrackFailureAction.SKIP / REMOVE / TIMEOUT
    """

    def __init__(
        self,
        track_title: str,
        track_url: str,
        timeout: float = 60.0,
    ):
        """Initialize the track failed view.

        Args:
            track_title: Title of the failed track.
            track_url: URL of the failed track.
            timeout: View timeout in seconds (default 60s).
        """
        super().__init__(timeout=timeout)
        self.track_title = track_title
        self.track_url = track_url
        self.action: TrackFailureAction = TrackFailureAction.TIMEOUT
        self.message: Optional[discord.Message] = None

    def create_embed(self) -> discord.Embed:
        """Create the failure notification embed.

        Returns:
            Embed describing the failure and available actions.
        """
        # Truncate title if too long
        display_title = self.track_title
        if len(display_title) > 50:
            display_title = display_title[:47] + "..."

        embed = discord.Embed(
            title="⚠️ Track Unavailable",
            description=(
                f"**[{display_title}]({self.track_url})**\n\n"
                "This track couldn't be played after multiple attempts. "
                "YouTube may be blocking playback, or the video is unavailable.\n\n"
                "**What would you like to do?**"
            ),
            color=discord.Color.orange()
        )

        embed.add_field(
            name="⏭️ Skip",
            value="Skip for now, keep in playlist (might work later)",
            inline=True
        )
        embed.add_field(
            name="🗑️ Remove",
            value="Remove from playlist entirely",
            inline=True
        )

        embed.set_footer(text="Auto-removes in 60 seconds if no response")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Allow anyone to interact - the music cog handles voice channel presence."""
        # We don't restrict here because:
        # 1. The failure view is sent to a text channel anyone can see
        # 2. The music cog ensures the view is only shown when users are in VC
        # 3. It's better UX to let anyone help when a song fails
        return True

    async def _disable_all_and_update(self, interaction: discord.Interaction, chosen_label: str) -> None:
        """Disable all buttons and update the message to show what was chosen."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                # Highlight the chosen action
                if child.label and chosen_label in child.label:
                    child.style = discord.ButtonStyle.success

        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed:
            embed.set_footer(text=f"✅ Action taken: {chosen_label}")
            embed.color = discord.Color.green()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle skip button click."""
        self.action = TrackFailureAction.SKIP
        await self._disable_all_and_update(interaction, "Skip")
        self.stop()

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle remove button click."""
        self.action = TrackFailureAction.REMOVE
        await self._disable_all_and_update(interaction, "Remove")
        self.stop()

    async def on_timeout(self) -> None:
        """Handle view timeout - auto-remove after 60s to keep playlist clean."""
        self.action = TrackFailureAction.TIMEOUT
        if self.message:
            try:
                for child in self.children:
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True

                embed = self.message.embeds[0] if self.message.embeds else None
                if embed:
                    embed.set_footer(text="⏱️ Timed out - removed from playlist")
                    embed.color = discord.Color.greyple()
                    await self.message.edit(embed=embed, view=self)
                else:
                    await self.message.edit(view=self)
            except Exception:
                pass  # Message may have been deleted


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


# Type aliases for NowPlayingView callbacks
# Action callbacks: do the action, return nothing (or new thumbnail bytes for skip)
PlayPauseAction = Callable[[], Awaitable[None]]
ShuffleAction = Callable[[], Awaitable[None]]
LoopAction = Callable[[], Awaitable[None]]
# Skip returns optional new thumbnail bytes (None = no change or unavailable)
SkipAction = Callable[[], Awaitable[Optional[bytes]]]
# State getter: returns fresh state given a thumbnail URL
StateGetter = Callable[[Optional[str]], NowPlayingState]


class NowPlayingView(ui.LayoutView):
    """Components V2 now playing widget with interactive controls.

    This view displays current track information with a large thumbnail,
    progress bar, and playback control buttons. It handles its own rebuilding
    after button interactions.

    The view accepts simple action callbacks that perform the action, plus a
    state-getter that fetches fresh state. The view handles all UI rebuild
    logic internally, keeping the caller's code clean.

    Usage:
        view = NowPlayingView(
            state=initial_state,
            get_state=lambda thumb_url: cog._build_now_playing_state(thumb_url),
            on_play_pause=cog._np_do_play_pause,
            on_skip=cog._np_do_skip,  # Returns new thumbnail bytes
            on_shuffle=cog._np_do_shuffle,
            on_loop=cog._np_do_loop,
        )
        await ctx.send(view=view, files=files)
    """

    def __init__(
        self,
        state: NowPlayingState,
        get_state: StateGetter,
        on_play_pause: Optional[PlayPauseAction] = None,
        on_skip: Optional[SkipAction] = None,
        on_shuffle: Optional[ShuffleAction] = None,
        on_loop: Optional[LoopAction] = None,
        timeout: float = 300.0,
    ):
        """Initialize the now playing view.

        Args:
            state: Initial state of the player.
            get_state: Callback to fetch fresh state. Accepts thumbnail URL,
                returns NowPlayingState.
            on_play_pause: Action callback for play/pause button.
            on_skip: Action callback for skip button. Returns new thumbnail
                bytes if track changed, None otherwise.
            on_shuffle: Action callback for shuffle button.
            on_loop: Action callback for loop mode toggle.
            timeout: View timeout in seconds.
        """
        super().__init__(timeout=timeout)
        self.state = state
        self._get_state = get_state
        self._on_play_pause = on_play_pause
        self._on_skip = on_skip
        self._on_shuffle = on_shuffle
        self._on_loop = on_loop
        self._thumbnail_url = state.thumbnail_url

        self._build_ui()

    def _build_ui(self) -> None:
        """Build or rebuild the UI from current state."""
        # Clear existing items
        self.clear_items()

        state = self.state
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
        skip_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="np_skip")
        shuffle_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="np_shuffle")
        loop_btn = ui.Button(style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="np_loop")

        # Bind callbacks to view methods
        play_pause_btn.callback = self._handle_play_pause  # type: ignore[method-assign]
        skip_btn.callback = self._handle_skip  # type: ignore[method-assign]
        shuffle_btn.callback = self._handle_shuffle  # type: ignore[method-assign]
        loop_btn.callback = self._handle_loop  # type: ignore[method-assign]

        action_row.add_item(play_pause_btn)
        action_row.add_item(skip_btn)
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

    async def _refresh_and_edit(self, interaction: discord.Interaction, new_files: Optional[List[discord.File]] = None) -> None:
        """Refresh state, rebuild UI, and edit the message.

        Args:
            interaction: The button interaction.
            new_files: Optional new attachments (e.g., new thumbnail).
        """
        self.state = self._get_state(self._thumbnail_url)
        self._build_ui()

        if new_files:
            await interaction.edit_original_response(view=self, attachments=new_files)
        else:
            await interaction.response.edit_message(view=self)

    async def _handle_play_pause(self, interaction: discord.Interaction) -> None:
        """Handle play/pause button click."""
        if self._on_play_pause:
            await self._on_play_pause()
        await self._refresh_and_edit(interaction)

    async def _handle_skip(self, interaction: discord.Interaction) -> None:
        """Handle skip button click."""
        if not self._on_skip:
            await interaction.response.send_message("Skip not available.", ephemeral=True)
            return

        await interaction.response.defer()
        new_thumbnail_bytes = await self._on_skip()

        # Update thumbnail if new one provided
        new_files: List[discord.File] = []
        if new_thumbnail_bytes:
            new_files.append(discord.File(io.BytesIO(new_thumbnail_bytes), filename="thumbnail.jpg"))
            self._thumbnail_url = "attachment://thumbnail.jpg"

        self.state = self._get_state(self._thumbnail_url)
        self._build_ui()
        await interaction.edit_original_response(view=self, attachments=new_files)

    async def _handle_shuffle(self, interaction: discord.Interaction) -> None:
        """Handle shuffle button click."""
        if self._on_shuffle:
            await self._on_shuffle()
            await interaction.response.send_message("🔀 Playlist shuffled!", ephemeral=True)
        else:
            await interaction.response.send_message("Shuffle not available.", ephemeral=True)

    async def _handle_loop(self, interaction: discord.Interaction) -> None:
        """Handle loop button click."""
        if self._on_loop:
            await self._on_loop()
        await self._refresh_and_edit(interaction)


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

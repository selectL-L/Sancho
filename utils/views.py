"""utils/views.py

This module contains reusable UI components (Views) for Discord interactions.
It provides standard selection menus and button interfaces used across multiple cogs.

Design Philosophy:
    If a user is clicking something, that logic belongs here. Cogs should call
    `await views.show_something(ctx, ...)` and receive a result back, without
    managing view lifecycle, callbacks, or Discord API details.

Exports:
    - Wrapper functions (preferred API): get_selection(), show_track_failed(),
      show_now_playing(), show_dashboard(), launch_modal(), get_track_selection()
    - Types: TrackFailureAction, NowPlayingState, MusicPlayerProtocol
    - View classes (for advanced use): PaginatorView, TrackSelectionView, etc.
"""

import io
import logging
import sys
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Protocol, cast

import asyncio
import discord
from discord import ui
from discord.ext import commands

if TYPE_CHECKING:
    from utils.musicutils import Track


# =============================================================================
# Track Failure View
# =============================================================================

class TrackFailureAction(Enum):
    """Actions a user can take when a track fails to play."""
    SKIP = auto()    # Skip but keep in playlist (might work later)
    REMOVE = auto()  # Remove from playlist entirely
    TIMEOUT = auto()  # User didn't respond - defaults to REMOVE


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
            except discord.NotFound:
                pass  # Message was deleted - expected
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"TrackFailedView timeout cleanup failed: {e}")


async def show_track_failed(
    channel: discord.abc.Messageable,
    track_title: str,
    track_url: str,
    timeout: float = 60.0
) -> TrackFailureAction:
    """Show a track failure dialog and return the user's choice.

    This is the preferred API for handling track failures. It creates the view,
    sends the message, waits for interaction, and handles cleanup automatically.

    Args:
        channel: The channel to send the failure message to.
        track_title: Title of the failed track.
        track_url: URL of the failed track.
        timeout: How long to wait for response (default 60s).

    Returns:
        TrackFailureAction indicating what the user chose (SKIP, REMOVE, or TIMEOUT).
    """
    view = TrackFailedView(
        track_title=track_title,
        track_url=track_url,
        timeout=timeout
    )

    embed = view.create_embed()
    message = await channel.send(embed=embed, view=view)
    view.message = message

    await view.wait()
    return view.action


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
    # NEW: Display metadata
    version_label: str = "Video"  # "Official Audio", "Music Video", etc.
    view_count_str: str = ""  # Pre-formatted: "1.2M views"
    album: Optional[str] = None
    is_explicit: Optional[bool] = None


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

        # Track info header
        status_emoji = "🎵" if state.in_voice else "🎧"
        status_text = "Now Playing" if state.in_voice else "Currently Listening To"

        display_title = truncate_visual(state.track_title, 48) if visual_width(state.track_title) > 48 else state.track_title
        display_artist = truncate_visual(state.track_artist, 40) if visual_width(state.track_artist) > 40 else state.track_artist

        # Build badges line
        badges = []
        if state.version_label and state.version_label != "Video":
            badges.append(f"🏷️ {state.version_label}")
        if state.is_explicit:
            badges.append("🅴")
        badge_line = " • ".join(badges) if badges else ""

        # Build metadata line (album, view count)
        meta_parts = []
        if state.album:
            meta_parts.append(f"*{state.album}*")
        if state.view_count_str:
            meta_parts.append(state.view_count_str)
        meta_line = " • ".join(meta_parts)

        # Compose track info
        track_info = f"## {status_emoji} {status_text}\n**[{display_title}]({state.track_url})**\nby {display_artist}"
        if badge_line:
            track_info += f"\n{badge_line}"
        if meta_line:
            track_info += f"\n-# {meta_line}"

        container.add_item(ui.TextDisplay(track_info))
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
        play_pause_btn.callback = self._handle_play_pause
        skip_btn.callback = self._handle_skip
        shuffle_btn.callback = self._handle_shuffle
        loop_btn.callback = self._handle_loop

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
# Music Player Protocol & Wrapper
# =============================================================================

class MusicPlayerProtocol(Protocol):
    """Protocol defining what show_now_playing() needs from a music controller.

    Any object implementing these methods can be passed to show_now_playing().
    This keeps views.py decoupled from the music cog's internals.
    """

    def get_now_playing_state(self, thumbnail_url: Optional[str] = None) -> NowPlayingState:
        """Build and return current player state.

        Args:
            thumbnail_url: Optional thumbnail attachment URL.

        Returns:
            NowPlayingState with current track info and playback status.
        """
        ...

    async def toggle_playback(self) -> None:
        """Toggle between play and pause states."""
        ...

    async def skip_track(self) -> Optional[bytes]:
        """Skip to next track.

        Returns:
            New thumbnail bytes if track changed, None otherwise.
        """
        ...

    async def shuffle_playlist(self) -> None:
        """Shuffle the playlist, preserving current track position."""
        ...

    async def cycle_loop_mode(self) -> None:
        """Cycle through loop modes (ALL -> ONE -> OFF -> ALL)."""
        ...

    async def get_current_thumbnail(self) -> Optional[bytes]:
        """Fetch thumbnail bytes for the current track.

        Returns:
            Thumbnail image bytes, or None if unavailable.
        """
        ...


async def show_now_playing(ctx: commands.Context, player: MusicPlayerProtocol) -> None:
    """Display an interactive now playing widget.

    This is the preferred API for showing the now playing view. It handles
    thumbnail fetching, file attachment management, view construction, and
    sending - the cog just needs to implement MusicPlayerProtocol.

    Args:
        ctx: The command context.
        player: Object implementing MusicPlayerProtocol (typically the Music cog).
    """
    # Fetch thumbnail
    thumbnail_bytes = await player.get_current_thumbnail()
    files: List[discord.File] = []
    thumbnail_url: Optional[str] = None

    if thumbnail_bytes:
        files.append(discord.File(io.BytesIO(thumbnail_bytes), filename="thumbnail.jpg"))
        thumbnail_url = "attachment://thumbnail.jpg"

    # Build initial state
    state = player.get_now_playing_state(thumbnail_url)

    # Create action callbacks that delegate to player
    async def do_play_pause() -> None:
        await player.toggle_playback()

    async def do_skip() -> Optional[bytes]:
        return await player.skip_track()

    async def do_shuffle() -> None:
        await player.shuffle_playlist()

    async def do_loop() -> None:
        await player.cycle_loop_mode()

    # Create view with callbacks
    view = NowPlayingView(
        state=state,
        get_state=player.get_now_playing_state,
        on_play_pause=do_play_pause,
        on_skip=do_skip,
        on_shuffle=do_shuffle,
        on_loop=do_loop,
    )

    if files:
        await ctx.send(view=view, files=files)
    else:
        await ctx.send(view=view)


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
            except asyncio.CancelledError:
                pass  # Task was cancelled - expected
            except Exception as e:
                logging.getLogger(__name__).debug(f"get_selection message result failed: {e}")

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
            except discord.NotFound:
                pass  # Message was deleted - expected
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"get_selection button update failed: {e}")
    else:
        # Timeout or invalid selection (not in options) -> Remove buttons
        try:
            await message.edit(view=None)
        except discord.NotFound:
            pass  # Message was deleted - expected
        except discord.HTTPException as e:
            logging.getLogger(__name__).debug(f"get_selection timeout cleanup failed: {e}")

    return result


# =============================================================================
# Track Selection View (YTM Integration) - Components V2
# =============================================================================

def _format_duration(seconds: int) -> str:
    """Format duration in seconds to MM:SS string."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def _format_view_count(count: Optional[int]) -> str:
    """Format view count for display (e.g., 1.2M views)."""
    if count is None:
        return ""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B views"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M views"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K views"
    return f"{count} views"


def _strip_leading_stars(title: str) -> str:
    """Strip leading star emojis from a title to avoid confusion with our recommendation badge."""
    return title.lstrip('⭐').lstrip()


@dataclass
class _SlottedTrack:
    """Internal wrapper pairing a track with its display slot number."""
    slot: int
    track: 'Track'
    is_recommended: bool = False


class TrackSelectionView(ui.LayoutView):
    """Components V2 view for selecting a track from search results.

    Displays tracks in vertical sections (YTM songs, YouTube videos) with
    numbered buttons that exactly match the displayed slot numbers.

    Layout:
        - Slot 0 (green): User's original URL (if URL mode)
        - Slots 1-3: YouTube Music results (songs preferred)
        - Slots 4-6: YouTube results (or renumbered if no YTM)
        - Cancel button

    Auto-Select:
        If a recommended track (⭐) is present, it will be auto-selected
        after a shorter timeout (15s) unless the user picks something else.

    Usage:
        view = TrackSelectionView(ctx.author, ytm_tracks, yt_tracks, user_track)
        message = await ctx.send(view=view)
        view.message = message
        await view.wait()
        selected = view.selected_track  # Track or None
    """

    # Auto-select timeout for recommended tracks (seconds)
    AUTO_SELECT_TIMEOUT = 15.0

    def __init__(
        self,
        author: discord.User | discord.Member,
        ytm_tracks: List['Track'],
        yt_tracks: List['Track'],
        user_track: Optional['Track'] = None,
        recommended_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """Initialize the track selection view.

        Args:
            author: The user who can interact with this view.
            ytm_tracks: YouTube Music results (songs/videos).
            yt_tracks: YouTube results (from yt-dlp).
            user_track: If provided, shown as slot 0 (user's original URL).
            recommended_id: Video ID of recommended track (gets ⭐ badge).
            timeout: View timeout in seconds.
        """
        # If there's a recommended track, use shorter auto-select timeout
        effective_timeout = self.AUTO_SELECT_TIMEOUT if recommended_id else timeout
        super().__init__(timeout=effective_timeout)
        self.author = author
        self.selected_track: Optional['Track'] = None
        self.message: Optional[discord.Message] = None
        self._cancelled = False
        self._auto_selected = False  # True if auto-selected on timeout

        # Build slot mapping
        self._slots: Dict[int, _SlottedTrack] = {}
        self._recommended_slot: Optional[int] = None  # Track which slot has recommended

        # Slot 0: User's URL (if provided)
        if user_track:
            self._slots[0] = _SlottedTrack(slot=0, track=user_track)

        # Slots 1-3: YTM results (always start at 1)
        ytm_start = 1
        for i, t in enumerate(ytm_tracks[:3]):
            s = ytm_start + i
            is_rec = recommended_id is not None and t.video_id == recommended_id
            self._slots[s] = _SlottedTrack(slot=s, track=t, is_recommended=is_rec)
            if is_rec:
                self._recommended_slot = s

        # Slots 4-6: YouTube results (or renumber to 1 if no YTM)
        yt_start = 4 if ytm_tracks else 1
        for i, t in enumerate(yt_tracks[:3]):
            s = yt_start + i
            self._slots[s] = _SlottedTrack(slot=s, track=t)

        self._ytm_tracks = ytm_tracks[:3]
        self._yt_tracks = yt_tracks[:3]
        self._user_track = user_track
        self._recommended_id = recommended_id
        self._effective_timeout = effective_timeout

        self._build_ui()

    def _build_ui(self) -> None:
        """Build the Components V2 UI."""
        self.clear_items()

        container = ui.Container(accent_colour=discord.Colour.blue())

        # Header
        container.add_item(ui.TextDisplay("## 🎵 Select a Track"))

        # User's URL section (slot 0)
        if self._user_track:
            t = self._user_track
            title_display = truncate_visual(_strip_leading_stars(t.title), 45)
            artist_display = truncate_visual(t.artist, 35)
            duration_str = _format_duration(t.duration)
            view_str = _format_view_count(t.view_count)

            # Build metadata line
            meta_parts = [duration_str]
            if view_str:
                meta_parts.append(view_str)
            meta_line = " • ".join(meta_parts)

            container.add_item(ui.TextDisplay(
                f"### 🔗 Your Link\n"
                f"**0** • **{title_display}**\n"
                f"by {artist_display} • {meta_line}"
            ))
            container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # YTM section
        if self._ytm_tracks:
            ytm_text = "### 🎵 YouTube Music\n"
            ytm_start = 1
            for i, t in enumerate(self._ytm_tracks):
                slot_num = ytm_start + i
                slotted = self._slots.get(slot_num)
                is_rec = slotted.is_recommended if slotted else False

                title_display = truncate_visual(_strip_leading_stars(t.title), 40)
                artist_display = truncate_visual(t.artist, 30)
                duration_str = _format_duration(t.duration)
                view_str = _format_view_count(t.view_count)

                # Version label emoji mapping
                version_emoji = "🎵" if t.version_label == "Official Audio" else "🎬" if t.version_label == "Music Video" else "📀"
                explicit_badge = " 🅴" if t.is_explicit else ""
                rec_badge = " ⭐" if is_rec else ""

                # Build metadata line (album, view count)
                meta_parts = []
                if t.album:
                    meta_parts.append(f"*{t.album}*")
                if view_str:
                    meta_parts.append(view_str)
                meta_line = f"\n-# {' • '.join(meta_parts)}" if meta_parts else ""

                ytm_text += (
                    f"**{slot_num}** {version_emoji}{explicit_badge}{rec_badge} **{title_display}**\n"
                    f"by {artist_display} • {duration_str}{meta_line}\n"
                )

            container.add_item(ui.TextDisplay(ytm_text.strip()))

            if self._yt_tracks:
                container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # YouTube section
        if self._yt_tracks:
            yt_start = 4 if self._ytm_tracks else 1
            yt_text = "### 📺 YouTube\n"
            for i, t in enumerate(self._yt_tracks):
                slot_num = yt_start + i
                title_display = truncate_visual(_strip_leading_stars(t.title), 40)
                artist_display = truncate_visual(t.artist, 30)
                duration_str = _format_duration(t.duration)
                view_str = _format_view_count(t.view_count)

                # Build metadata line
                meta_parts = [duration_str]
                if view_str:
                    meta_parts.append(view_str)
                meta_line = " • ".join(meta_parts)

                yt_text += (
                    f"**{slot_num}** 📺 **{title_display}**\n"
                    f"by {artist_display} • {meta_line}\n"
                )

            container.add_item(ui.TextDisplay(yt_text.strip()))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Buttons - create ActionRows
        # User URL button (slot 0) - separate row if present
        if self._user_track:
            url_row = ui.ActionRow()
            btn = ui.Button(
                style=discord.ButtonStyle.success,
                label="0",
                custom_id="track_0",
            )
            btn.callback = self._make_callback(0)
            url_row.add_item(btn)
            container.add_item(url_row)

        # YTM buttons
        if self._ytm_tracks:
            ytm_row = ui.ActionRow()
            ytm_start = 1
            for i in range(len(self._ytm_tracks)):
                slot_num = ytm_start + i
                btn = ui.Button(
                    style=discord.ButtonStyle.primary,
                    label=str(slot_num),
                    custom_id=f"track_{slot_num}",
                )
                btn.callback = self._make_callback(slot_num)
                ytm_row.add_item(btn)
            container.add_item(ytm_row)

        # YouTube buttons
        if self._yt_tracks:
            yt_row = ui.ActionRow()
            yt_start = 4 if self._ytm_tracks else 1
            for i in range(len(self._yt_tracks)):
                slot_num = yt_start + i
                btn = ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=str(slot_num),
                    custom_id=f"track_{slot_num}",
                )
                btn.callback = self._make_callback(slot_num)
                yt_row.add_item(btn)
            container.add_item(yt_row)

        # Cancel button
        cancel_row = ui.ActionRow()
        cancel_btn = ui.Button(
            style=discord.ButtonStyle.danger,
            label="Cancel",
            emoji="❌",
            custom_id="track_cancel",
        )
        cancel_btn.callback = self._handle_cancel
        cancel_row.add_item(cancel_btn)
        container.add_item(cancel_row)

        # Footer - different message if auto-select is enabled
        if self._recommended_id:
            footer_text = f"-# ⭐ Auto-selects recommended in {int(self._effective_timeout)}s • Pick another to override"
        else:
            footer_text = "-# Select by clicking a number • Times out in 30s"
        container.add_item(ui.TextDisplay(footer_text))

        self.add_item(container)

    def _make_callback(self, slot: int):
        """Create a callback for a specific slot."""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message(
                    "This selection is not for you.", ephemeral=True
                )
                return
            slotted = self._slots.get(slot)
            if slotted:
                self.selected_track = slotted.track
            await interaction.response.defer()
            self.stop()
        return callback

    async def _handle_cancel(self, interaction: discord.Interaction) -> None:
        """Handle cancel button."""
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This selection is not for you.", ephemeral=True
            )
            return
        self._cancelled = True
        self.selected_track = None
        await interaction.response.defer()
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the author can interact."""
        return interaction.user.id == self.author.id

    async def on_timeout(self) -> None:
        """Handle timeout - view cleanup handled by get_track_selection."""
        # Don't edit message here - let get_track_selection handle all UI updates
        # This includes auto-select logic which needs to happen synchronously


async def get_track_selection(
    ctx,
    ytm_tracks: List['Track'],
    yt_tracks: List['Track'],
    user_track: Optional['Track'] = None,
    recommended_id: Optional[str] = None,
    timeout: float = 30.0,
) -> Optional['Track']:
    """Display track selection UI and wait for user choice.

    This is the main entry point for the track selection flow.

    Args:
        ctx: Command context (or pseudo-context with author, channel, send()).
        ytm_tracks: YouTube Music results (songs/videos).
        yt_tracks: YouTube results (from yt-dlp).
        user_track: If provided, shown as slot 0 (user's original URL).
        recommended_id: Video ID of recommended track (gets ⭐ badge).
        timeout: Selection timeout in seconds.

    Returns:
        The selected Track, or None if cancelled/timed out.
    """
    # Count total options
    total = len(ytm_tracks) + len(yt_tracks) + (1 if user_track else 0)
    if total == 0:
        return None

    # If only one option total, return it directly (no UI needed)
    if total == 1:
        if user_track:
            return user_track
        return ytm_tracks[0] if ytm_tracks else yt_tracks[0]

    view = TrackSelectionView(
        ctx.author,
        ytm_tracks,
        yt_tracks,
        user_track,
        recommended_id,
        timeout=timeout,
    )

    # LayoutView requires explicit content/embed clearing
    message = await ctx.send(view=view, content=None, embed=None)
    view.message = message

    await view.wait()

    # Handle auto-select if timed out with a recommended track
    if not view.selected_track and not view._cancelled and view._recommended_slot is not None:
        slotted = view._slots.get(view._recommended_slot)
        if slotted:
            view.selected_track = slotted.track
            view._auto_selected = True

    # Update message after selection/timeout
    if view.selected_track:
        # Show selected track confirmation
        selected = view.selected_track
        title_display = truncate_visual(selected.title, 45)

        # Different header for auto-selected vs manual
        if view._auto_selected:
            header = "## ⭐ Auto-Selected"
        else:
            header = "## ✅ Selected"

        confirmation = ui.LayoutView()
        conf_container = ui.Container(accent_colour=discord.Colour.green())
        conf_container.add_item(ui.TextDisplay(
            f"{header}\n"
            f"**{title_display}**\n"
            f"by {selected.artist}"
        ))
        confirmation.add_item(conf_container)
        try:
            await message.edit(view=confirmation)
        except discord.HTTPException:
            pass
    elif view._cancelled:
        # User cancelled
        cancel_view = ui.LayoutView()
        cancel_container = ui.Container(accent_colour=discord.Colour.greyple())
        cancel_container.add_item(ui.TextDisplay("## ❌ Selection Cancelled"))
        cancel_view.add_item(cancel_container)
        try:
            await message.edit(view=cancel_view)
        except discord.HTTPException:
            pass
    else:
        # Timeout
        timeout_view = ui.LayoutView()
        timeout_container = ui.Container(accent_colour=discord.Colour.greyple())
        timeout_container.add_item(ui.TextDisplay("## ⏰ Selection Timed Out"))
        timeout_view.add_item(timeout_container)
        try:
            await message.edit(view=timeout_view)
        except discord.HTTPException:
            pass

    return view.selected_track


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
        except (AttributeError, TypeError) as e:
            logging.getLogger(__name__).debug(f"FastConfirmModal value processing failed: {e}")
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
            except discord.NotFound:
                pass  # Message was deleted - expected
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"ModalLauncherView timeout cleanup failed: {e}")


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
            except discord.NotFound:
                pass  # Message was deleted - expected
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"PaginatorView timeout cleanup failed: {e}")


# =============================================================================
# Dashboard View (Admin)
# =============================================================================

# Type alias for dashboard callbacks
DashboardCallback = Callable[[commands.Context], Awaitable[None]]


class DashboardView(discord.ui.View):
    """Interactive dashboard view with sub-pages and action buttons.

    Provides a main dashboard with buttons to:
    - View paginated skill entries
    - View paginated reminder entries
    - Export data to file
    - Dump database

    Each sub-view has a "Back to Dashboard" button to return to the main view.
    """

    def __init__(
        self,
        ctx: commands.Context,
        skill_pages: List[discord.Embed],
        reminder_pages: List[discord.Embed],
        report_file_callback: DashboardCallback,
        dump_db_callback: DashboardCallback,
        dashboard_embed: Optional[discord.Embed] = None,
        timeout: float = 120.0
    ):
        """Initialize the dashboard view.

        Args:
            ctx: The command context.
            skill_pages: List of embeds for skill pagination.
            reminder_pages: List of embeds for reminder pagination.
            report_file_callback: Async callback to export report file.
            dump_db_callback: Async callback to dump database.
            dashboard_embed: The main dashboard embed to return to.
            timeout: View timeout in seconds (default 120s).
        """
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.skill_pages = skill_pages
        self.reminder_pages = reminder_pages
        self.report_file_callback = report_file_callback
        self.dump_db_callback = dump_db_callback
        self.dashboard_embed = dashboard_embed
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the command author can interact."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This dashboard is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📜 View Skills", style=discord.ButtonStyle.primary)
    async def view_skills(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show paginated skill entries."""
        if not self.skill_pages:
            await interaction.response.send_message("No skills found in database.", ephemeral=True)
            return

        view = PaginatorView(self.ctx, self.skill_pages)
        # Add a "Back to Dashboard" button to the paginator
        back_button = discord.ui.Button(
            label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

        async def back_callback(interaction: discord.Interaction):
            if self.dashboard_embed:
                await interaction.response.edit_message(embed=self.dashboard_embed, view=self)
            view.stop()

        back_button.callback = back_callback
        view.add_item(back_button)

        await interaction.response.edit_message(embed=self.skill_pages[0], view=view)
        view.message = interaction.message

    @discord.ui.button(label="⏰ View Reminders", style=discord.ButtonStyle.primary)
    async def view_reminders(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show paginated reminder entries."""
        if not self.reminder_pages:
            await interaction.response.send_message("No reminders found in database.", ephemeral=True)
            return

        view = PaginatorView(self.ctx, self.reminder_pages)
        # Add a "Back to Dashboard" button to the paginator
        back_button = discord.ui.Button(
            label="↩ Back to Dashboard", style=discord.ButtonStyle.red, row=1)

        async def back_callback(interaction: discord.Interaction):
            if self.dashboard_embed:
                await interaction.response.edit_message(embed=self.dashboard_embed, view=self)
            view.stop()

        back_button.callback = back_callback
        view.add_item(back_button)

        await interaction.response.edit_message(embed=self.reminder_pages[0], view=view)
        view.message = interaction.message

    @discord.ui.button(label="💾 Export to File", style=discord.ButtonStyle.secondary)
    async def export_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Trigger the export callback."""
        await interaction.response.defer()
        await self.report_file_callback(self.ctx)

    @discord.ui.button(label="🗄️ Dump Database", style=discord.ButtonStyle.danger)
    async def dump_database(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Trigger the database dump callback."""
        await interaction.response.defer()
        await self.dump_db_callback(self.ctx)

    async def on_timeout(self):
        """Disable all buttons on timeout."""
        if self.message:
            try:
                for child in self.children:
                    if hasattr(child, 'disabled'):
                        setattr(child, 'disabled', True)
                await self.message.edit(view=self)
            except discord.NotFound:
                pass  # Message was deleted - expected
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"DashboardView timeout cleanup failed: {e}")


async def show_dashboard(
    ctx: commands.Context,
    skill_pages: List[discord.Embed],
    reminder_pages: List[discord.Embed],
    report_file_callback: DashboardCallback,
    dump_db_callback: DashboardCallback,
    dashboard_embed: discord.Embed,
    timeout: float = 120.0
) -> discord.Message:
    """Display an interactive admin dashboard.

    This is the preferred API for showing the admin dashboard. It handles
    view construction, message sending, and lifecycle management.

    Args:
        ctx: The command context.
        skill_pages: List of embeds for skill pagination.
        reminder_pages: List of embeds for reminder pagination.
        report_file_callback: Async function to call for file export.
        dump_db_callback: Async function to call for database dump.
        dashboard_embed: The main dashboard embed.
        timeout: View timeout in seconds (default 120s).

    Returns:
        The sent message containing the dashboard.
    """
    view = DashboardView(
        ctx=ctx,
        skill_pages=skill_pages,
        reminder_pages=reminder_pages,
        report_file_callback=report_file_callback,
        dump_db_callback=dump_db_callback,
        dashboard_embed=dashboard_embed,
        timeout=timeout
    )

    message = await ctx.send(embed=dashboard_embed, view=view)
    view.message = message
    return message


# =============================================================================
# Status View (Components V2)
# =============================================================================

@dataclass
class StatusHealth:
    """Health check results for status overview."""
    gateway: bool  # Gateway latency < 500ms
    database: bool  # Database responding
    music_auth: bool  # Music auth method available
    cache: bool  # Cache manifest valid
    extensions: bool  # All cogs loaded
    ytdlp: bool  # yt-dlp available


@dataclass
class StatusData:
    """All data needed to render status pages."""
    # Performance
    gateway_latency: float  # ms
    roundtrip_latency: float  # ms
    db_latency: float  # ms
    cpu_percent: float
    ram_mb: float  # RSS (resident in physical RAM)
    ram_private_mb: float  # USS (private bytes, unique to this process)
    ram_swap_mb: float  # Swap usage in MB (Linux only, 0 on Windows)

    # Uptime
    start_timestamp: int  # Unix timestamp
    uptime_str: str  # "0d 10h 31m"

    # Extensions
    loaded_cogs: List[str]
    total_cogs: int
    failed_cogs: List[str]

    # Health
    health: StatusHealth

    # Music Auth
    auth_method: Optional[str]  # 'pot_server', 'cookies', or None
    pot_server_running: bool
    pot_plugin_error: Optional[str]
    cookie_age_days: Optional[int]
    error_403_count: int
    error_403_window_mins: int
    ytdlp_available: bool
    mutagen_available: bool

    # Music Playback
    music_status: str  # "idle", "playing", "paused"
    music_channel: Optional[str]  # Channel name if in voice
    music_track: Optional[str]  # Current track title
    music_artist: Optional[str]  # Current track artist
    music_queue_count: int

    # Cache
    cache_playlists: int
    cache_tracks_total: int
    cache_tracks_downloaded: int
    cache_size_mb: float
    cache_orphaned_count: int
    cache_orphaned_mb: float
    cache_residential_count: int
    cache_residential_mb: float
    cache_residential_cost: float
    cache_last_refresh_ago: Optional[float]  # seconds

    # Database
    db_size_mb: float

    # Next Reminder
    next_reminder_time: Optional[int]  # Unix timestamp
    next_reminder_user: Optional[str]  # User display name

    # System
    python_version: str
    discordpy_version: str
    platform: str
    bot_name: str
    bot_id: int
    guild_count: int

    # Snapshot timestamp
    snapshot_timestamp: int


class StatusView(discord.ui.LayoutView):
    """Interactive status view using Components V2.

    Displays bot status across 5 pages:
    1. Overview - Health checks and quick stats
    2. Performance - Latencies and resource usage
    3. Music - Auth status and playback info
    4. Storage - Cache and database stats
    5. System - Extensions and environment

    Uses Container, Section, TextDisplay, Separator, and ActionRow components.
    """

    PAGE_NAMES = [
        ("🏠", "Overview"),
        ("⚡", "Performance"),
        ("🎵", "Music"),
        ("🗄️", "Storage"),
        ("🔧", "System"),
    ]

    def __init__(
        self,
        ctx: commands.Context,
        data: StatusData,
        timeout: float = 120.0,
        refresh_callback: Optional[Callable[[], Awaitable[StatusData]]] = None,
    ):
        """Initialize the status view.

        Args:
            ctx: The command context.
            data: StatusData containing all info to display.
            timeout: View timeout in seconds.
            refresh_callback: Optional async callback to fetch fresh StatusData.
        """
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.data = data
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        self.refresh_callback = refresh_callback
        self._initial_timeout = timeout

        self._build_ui()

    def _get_accent_color(self) -> discord.Colour:
        """Get accent color based on health status."""
        h = self.data.health
        all_healthy = all([h.gateway, h.database, h.music_auth, h.cache, h.extensions, h.ytdlp])
        any_critical = not h.gateway or not h.database

        if any_critical:
            return discord.Colour.red()
        elif all_healthy:
            return discord.Colour.green()
        else:
            return discord.Colour.orange()

    def _build_ui(self) -> None:
        """Build the UI for the current page."""
        self.clear_items()

        # Build page content
        container = ui.Container(accent_colour=self._get_accent_color())

        if self.current_page == 0:
            self._build_overview_page(container)
        elif self.current_page == 1:
            self._build_performance_page(container)
        elif self.current_page == 2:
            self._build_music_page(container)
        elif self.current_page == 3:
            self._build_storage_page(container)
        elif self.current_page == 4:
            self._build_system_page(container)

        # Footer with snapshot timestamp
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(f"-# Snapshot taken <t:{self.data.snapshot_timestamp}:R>"))

        self.add_item(container)

        # Navigation row with select menu
        select_row = ui.ActionRow()
        page_select = ui.Select(
            placeholder="Jump to page...",
            custom_id="status_page_select",
            options=[
                discord.SelectOption(
                    label=f"{emoji} {name}",
                    value=str(i),
                    default=(i == self.current_page)
                )
                for i, (emoji, name) in enumerate(self.PAGE_NAMES)
            ]
        )
        page_select.callback = self._handle_page_select
        select_row.add_item(page_select)
        self.add_item(select_row)

        # Navigation row with buttons
        nav_row = ui.ActionRow()

        prev_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="◀",
            custom_id="status_prev",
            disabled=(self.current_page == 0)
        )
        prev_btn.callback = self._handle_prev

        page_indicator = ui.Button(
            style=discord.ButtonStyle.secondary,
            label=f"{self.current_page + 1}/{len(self.PAGE_NAMES)}",
            custom_id="status_page_indicator",
            disabled=True
        )

        next_btn = ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="▶",
            custom_id="status_next",
            disabled=(self.current_page == len(self.PAGE_NAMES) - 1)
        )
        next_btn.callback = self._handle_next

        refresh_btn = ui.Button(
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            label="Refresh",
            custom_id="status_refresh"
        )
        refresh_btn.callback = self._handle_refresh

        nav_row.add_item(prev_btn)
        nav_row.add_item(page_indicator)
        nav_row.add_item(next_btn)
        nav_row.add_item(refresh_btn)
        self.add_item(nav_row)

    def _health_icon(self, healthy: bool) -> str:
        """Return health indicator emoji."""
        return "✅" if healthy else "⚠️"

    def _build_overview_page(self, container: ui.Container) -> None:
        """Build the Overview page content."""
        h = self.data.health
        d = self.data

        # Header
        container.add_item(ui.TextDisplay("## 🏠 Overview"))

        # Health section
        health_text = (
            f"### Health\n"
            f"{self._health_icon(h.gateway)} Gateway          "
            f"{self._health_icon(h.database)} Database\n"
            f"{self._health_icon(h.music_auth)} Music Auth        "
            f"{self._health_icon(h.cache)} Cache\n"
            f"{self._health_icon(h.extensions)} Extensions        "
            f"{self._health_icon(h.ytdlp)} yt-dlp"
        )
        container.add_item(ui.TextDisplay(health_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Quick Stats
        # Platform-aware memory display:
        # - Windows: Show private bytes (USS) - useful for paging detection
        # - Linux: Show swap if any (RSS vs USS gap is just shared libs)
        if sys.platform == 'win32':
            memory_str = f"{d.ram_mb:.0f} MB (private: {d.ram_private_mb:.0f} MB)"
        elif d.ram_swap_mb > 0:
            memory_str = f"{d.ram_mb:.0f} MB (paged: {d.ram_swap_mb:.0f} MB)"
        else:
            memory_str = f"{d.ram_mb:.0f} MB (paged: 0 MB)"
        quick_stats = (
            f"### Quick Stats\n"
            f"**Uptime:** {d.uptime_str}    **Latency:** {d.gateway_latency:.0f}ms\n"
            f"**Memory:** {memory_str}    **Cogs:** {len(d.loaded_cogs)}/{d.total_cogs}"
        )
        container.add_item(ui.TextDisplay(quick_stats))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Right Now section
        right_now_lines = ["### Right Now"]

        # Music status
        if d.music_status == "idle":
            right_now_lines.append("💤 Music idle")
        elif d.music_channel:
            status_emoji = "🎵" if d.music_status == "playing" else "⏸️"
            right_now_lines.append(f"{status_emoji} In **#{d.music_channel}**")
            if d.music_track:
                track_display = d.music_track[:40] + "..." if len(d.music_track) > 40 else d.music_track
                right_now_lines.append(f"    Playing: {track_display}")

        # Next reminder
        if d.next_reminder_time:
            right_now_lines.append(f"⏰ Next reminder: <t:{d.next_reminder_time}:R> for {d.next_reminder_user}")
        else:
            right_now_lines.append("⏰ No pending reminders")

        container.add_item(ui.TextDisplay("\n".join(right_now_lines)))

    def _build_performance_page(self, container: ui.Container) -> None:
        """Build the Performance page content."""
        d = self.data

        container.add_item(ui.TextDisplay("## ⚡ Performance"))

        # Latency section
        latency_text = (
            f"### Latency\n"
            f"**Gateway:** {d.gateway_latency:.2f}ms\n"
            f"**Roundtrip:** {d.roundtrip_latency:.2f}ms\n"
            f"**Database:** {d.db_latency:.2f}ms"
        )
        container.add_item(ui.TextDisplay(latency_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Resources section
        # Platform-aware memory display:
        # - Windows: Show private bytes (USS) - useful for paging detection
        # - Linux: Show swap if any (RSS vs USS gap is just shared libs)
        if sys.platform == 'win32':
            resources_text = (
                f"### Resources\n"
                f"**CPU:** {d.cpu_percent:.1f}%\n"
                f"**RAM:** {d.ram_mb:.2f} MB (resident)\n"
                f"**Private:** {d.ram_private_mb:.2f} MB"
            )
        elif d.ram_swap_mb > 0:
            resources_text = (
                f"### Resources\n"
                f"**CPU:** {d.cpu_percent:.1f}%\n"
                f"**RAM:** {d.ram_mb:.2f} MB\n"
                f"**Paged:** {d.ram_swap_mb:.2f} MB"
            )
        else:
            resources_text = (
                f"### Resources\n"
                f"**CPU:** {d.cpu_percent:.1f}%\n"
                f"**RAM:** {d.ram_mb:.2f} MB\n"
                f"**Paged:** 0 MB"
            )
        container.add_item(ui.TextDisplay(resources_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Uptime section
        uptime_text = (
            f"### Uptime\n"
            f"**Started:** <t:{d.start_timestamp}:f>\n"
            f"**Running:** {d.uptime_str}"
        )
        container.add_item(ui.TextDisplay(uptime_text))

    def _build_music_page(self, container: ui.Container) -> None:
        """Build the Music page content."""
        d = self.data

        container.add_item(ui.TextDisplay("## 🎵 Music System"))

        # Authentication section
        if d.auth_method == 'pot_server':
            method_text = "PO Token Server"
        elif d.auth_method == 'cookies':
            method_text = "Cookies"
        else:
            method_text = "None"

        server_status = "✅ Running" if d.pot_server_running else f"❌ {d.pot_plugin_error or 'Not running'}"

        cookie_text = f"{d.cookie_age_days}d old" if d.cookie_age_days is not None else "Not found"

        error_icon = "✅" if d.error_403_count == 0 else ("⚠️" if d.error_403_count < 3 else "❌")

        auth_text = (
            f"### Authentication\n"
            f"**Method:** {method_text}\n"
            f"**PO Server:** {server_status}\n"
            f"**Cookies:** {cookie_text}\n"
            f"**403 Rate:** {d.error_403_count}/{d.error_403_window_mins}m {error_icon}"
        )
        container.add_item(ui.TextDisplay(auth_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Playback section
        if d.music_status == "idle":
            playback_text = (
                "### Playback\n"
                "**Status:** 💤 Idle"
            )
        else:
            status_emoji = "🔊" if d.music_status == "playing" else "⏸️"
            playback_lines = [
                "### Playback",
                f"**Status:** {status_emoji} #{d.music_channel}"
            ]
            if d.music_track:
                playback_lines.append(f"**Track:** {d.music_track}")
            if d.music_artist:
                playback_lines.append(f"**Artist:** {d.music_artist}")
            playback_lines.append(f"**Queue:** {d.music_queue_count} tracks")
            playback_text = "\n".join(playback_lines)

        container.add_item(ui.TextDisplay(playback_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Dependencies section
        deps_text = (
            f"### Dependencies\n"
            f"**yt-dlp:** {'✅ Available' if d.ytdlp_available else '❌ Missing'}\n"
            f"**mutagen:** {'✅ Available' if d.mutagen_available else '❌ Missing'}"
        )
        container.add_item(ui.TextDisplay(deps_text))

    def _build_storage_page(self, container: ui.Container) -> None:
        """Build the Storage page content."""
        d = self.data

        container.add_item(ui.TextDisplay("## 🗄️ Storage"))

        # Music Cache section
        cache_text = (
            f"### Music Cache\n"
            f"**Playlists:** {d.cache_playlists}\n"
            f"**Tracks:** {d.cache_tracks_total} total ({d.cache_tracks_downloaded} downloaded)\n"
            f"**Size:** {d.cache_size_mb:.1f} MB"
        )
        container.add_item(ui.TextDisplay(cache_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Orphaned Files section
        orphan_text = (
            f"### Orphaned Files\n"
            f"**Count:** {d.cache_orphaned_count} tracks\n"
            f"**Size:** {d.cache_orphaned_mb:.1f} MB"
        )
        container.add_item(ui.TextDisplay(orphan_text))

        # Residential Cache (if any)
        if d.cache_residential_count > 0:
            container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
            residential_text = (
                f"### Residential Cache\n"
                f"**Files:** {d.cache_residential_count}\n"
                f"**Size:** {d.cache_residential_mb:.1f} MB\n"
                f"**Est. cost:** ${d.cache_residential_cost:.2f}"
            )
            container.add_item(ui.TextDisplay(residential_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Database section
        db_text = (
            f"### Database\n"
            f"**Size:** {d.db_size_mb:.2f} MB"
        )
        container.add_item(ui.TextDisplay(db_text))

        # Last refresh info
        if d.cache_last_refresh_ago is not None:
            if d.cache_last_refresh_ago < 3600:
                refresh_str = f"{int(d.cache_last_refresh_ago / 60)}m ago"
            elif d.cache_last_refresh_ago < 86400:
                refresh_str = f"{d.cache_last_refresh_ago / 3600:.1f}h ago"
            else:
                refresh_str = f"{d.cache_last_refresh_ago / 86400:.1f}d ago"
            container.add_item(ui.TextDisplay(f"\n-# Last cache refresh: {refresh_str}"))

    def _build_system_page(self, container: ui.Container) -> None:
        """Build the System page content."""
        d = self.data

        container.add_item(ui.TextDisplay("## 🔧 System"))

        # Extensions section
        cog_list = ", ".join(sorted(d.loaded_cogs))
        ext_text = (
            f"### Extensions ({len(d.loaded_cogs)}/{d.total_cogs})\n"
            f"{cog_list}"
        )
        container.add_item(ui.TextDisplay(ext_text))

        # Failed extensions
        if d.failed_cogs:
            failed_list = ", ".join(d.failed_cogs)
            container.add_item(ui.TextDisplay(f"\n**Failed:** {failed_list}"))
        else:
            container.add_item(ui.TextDisplay("\n**Failed:** None"))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Environment section
        env_text = (
            f"### Environment\n"
            f"**Python:** {d.python_version}\n"
            f"**discord.py:** {d.discordpy_version}\n"
            f"**Platform:** {d.platform}"
        )
        container.add_item(ui.TextDisplay(env_text))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))

        # Bot Identity section
        identity_text = (
            f"### Bot Identity\n"
            f"**Name:** {d.bot_name}\n"
            f"**ID:** {d.bot_id}\n"
            f"**Guilds:** {d.guild_count}"
        )
        container.add_item(ui.TextDisplay(identity_text))

    async def _handle_page_select(self, interaction: discord.Interaction) -> None:
        """Handle page selection from dropdown."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This status view is not for you.", ephemeral=True)
            return

        # Get selected value from the select component
        if interaction.data and 'values' in interaction.data:
            self.current_page = int(interaction.data['values'][0])
            self._build_ui()
            await interaction.response.edit_message(view=self)

    async def _handle_prev(self, interaction: discord.Interaction) -> None:
        """Handle previous button click."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This status view is not for you.", ephemeral=True)
            return

        if self.current_page > 0:
            self.current_page -= 1
            self._build_ui()
            await interaction.response.edit_message(view=self)

    async def _handle_next(self, interaction: discord.Interaction) -> None:
        """Handle next button click."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This status view is not for you.", ephemeral=True)
            return

        if self.current_page < len(self.PAGE_NAMES) - 1:
            self.current_page += 1
            self._build_ui()
            await interaction.response.edit_message(view=self)

    async def _handle_refresh(self, interaction: discord.Interaction) -> None:
        """Handle refresh button click - fetches fresh data and rebuilds UI."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This status view is not for you.", ephemeral=True)
            return

        if self.refresh_callback is None:
            await interaction.response.send_message("🔄 Use the command again for fresh data.", ephemeral=True)
            return

        # Defer while we fetch fresh data
        await interaction.response.defer()

        try:
            # Fetch fresh data
            self.data = await self.refresh_callback()

            # Reset timeout
            self.timeout = self._initial_timeout

            # Rebuild UI with new data
            self._build_ui()
            await interaction.edit_original_response(view=self)
        except Exception as e:
            logging.getLogger(__name__).error(f"Status refresh failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Refresh failed: {e}", ephemeral=True)

    async def on_timeout(self) -> None:
        """Disable navigation on timeout."""
        if self.message:
            try:
                # Rebuild with disabled state
                self.clear_items()

                # Rebuild current page container (read-only)
                container = ui.Container(accent_colour=self._get_accent_color())

                if self.current_page == 0:
                    self._build_overview_page(container)
                elif self.current_page == 1:
                    self._build_performance_page(container)
                elif self.current_page == 2:
                    self._build_music_page(container)
                elif self.current_page == 3:
                    self._build_storage_page(container)
                elif self.current_page == 4:
                    self._build_system_page(container)

                container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
                container.add_item(ui.TextDisplay(f"-# Snapshot taken <t:{self.data.snapshot_timestamp}:R> (view expired)"))

                self.add_item(container)
                await self.message.edit(view=self)
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logging.getLogger(__name__).debug(f"StatusView timeout cleanup failed: {e}")


async def show_status(
    ctx: commands.Context,
    data: StatusData,
    timeout: float = 120.0,
    refresh_callback: Optional[Callable[[], Awaitable[StatusData]]] = None,
) -> discord.Message:
    """Display an interactive status view.

    This is the preferred API for showing the status view. It handles
    view construction, message sending, and lifecycle management.

    Args:
        ctx: The command context.
        data: StatusData containing all status information.
        timeout: View timeout in seconds (default 120s).
        refresh_callback: Optional async callback to fetch fresh StatusData on refresh.

    Returns:
        The sent message containing the status view.
    """
    view = StatusView(ctx=ctx, data=data, timeout=timeout, refresh_callback=refresh_callback)
    message = await ctx.send(view=view)
    view.message = message
    return message

"""cogs/files.py

This cog contains commands for file manipulation, such as image resizing
and converting formats. It uses the Pillow (PIL) library for processing.

A key feature of this cog is the use of `asyncio.to_thread` to run the
synchronous, blocking processing functions in a separate thread. This
prevents the bot's main event loop from being blocked, ensuring the bot
remains responsive while handling potentially time-consuming file operations.

This cog also provides utilities to fetch user assets (avatars, banners) at
both global and guild levels, with helper functions designed to be reusable
for other operations like applying masks or overlays.
"""

import asyncio
import io
import re
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import aiohttp
import discord
from discord.ext import commands
from PIL import Image as PILImage

from utils.base_cog import BaseCog
from utils.bot_class import CoreBot


@dataclass
class UserAsset:
    """Represents a fetched user asset (avatar or banner).

    Attributes:
        url: The CDN URL of the asset.
        image_bytes: The raw image data as bytes (None if not yet fetched).
        source: A description of where this asset came from (e.g., "Global", "Server").
    """
    url: str
    image_bytes: Optional[bytes] = None
    source: str = "Unknown"

    async def fetch(self, session: aiohttp.ClientSession) -> bytes:
        """Fetches the image bytes from the URL.

        Args:
            session: An aiohttp client session to use for the request.

        Returns:
            The raw image data as bytes.

        Raises:
            aiohttp.ClientError: If the fetch fails.
        """
        async with session.get(self.url) as response:
            response.raise_for_status()
            self.image_bytes = await response.read()
            return self.image_bytes

    def to_pil_image(self) -> PILImage.Image:
        """Converts the fetched bytes to a PIL Image.

        Returns:
            A PIL Image object.

        Raises:
            ValueError: If image_bytes has not been fetched yet.
        """
        if self.image_bytes is None:
            raise ValueError("Image bytes not fetched. Call fetch() first.")
        return PILImage.open(io.BytesIO(self.image_bytes))


class FilesCog(BaseCog):
    """A cog for handling file manipulation commands."""

    def __init__(self, bot: CoreBot):
        """Initializes the FilesCog.

        Args:
            bot (CoreBot): The bot instance.
        """
        super().__init__(bot)

    # -------------------------------------------------------------------------
    # User Asset Helpers (Reusable for other operations)
    # -------------------------------------------------------------------------

    async def get_avatar(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> UserAsset:
        """Gets a user's global avatar as a UserAsset.

        Args:
            user: The user or member to get the avatar for.
            size: The size of the avatar to fetch (default 1024).

        Returns:
            A UserAsset containing the global avatar URL.
        """
        avatar = user.avatar or user.default_avatar
        url = avatar.replace(size=size, format='png').url
        return UserAsset(url=url, source="Global")

    async def get_guild_avatar(
        self,
        member: discord.Member,
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a member's guild-specific avatar as a UserAsset.

        Args:
            member: The member to get the guild avatar for.
            size: The size of the avatar to fetch (default 1024).

        Returns:
            A UserAsset containing the guild avatar URL, or None if not set.
        """
        if member.guild_avatar is None:
            return None
        url = member.guild_avatar.replace(size=size, format='png').url
        return UserAsset(url=url, source="Server")

    async def get_all_avatars(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> list[UserAsset]:
        """Gets all available avatars (global and guild) for a user.

        Args:
            user: The user or member to get avatars for.
            size: The size of the avatars to fetch (default 1024).

        Returns:
            A list of UserAsset objects for each available avatar.
        """
        assets = [await self.get_avatar(user, size=size)]

        if isinstance(user, discord.Member):
            guild_avatar = await self.get_guild_avatar(user, size=size)
            if guild_avatar:
                assets.append(guild_avatar)

        return assets

    async def get_banner(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a user's global banner as a UserAsset.

        Note: Requires fetching the full user object to access banner data.

        Args:
            user: The user or member to get the banner for.
            size: The size of the banner to fetch (default 1024).

        Returns:
            A UserAsset containing the global banner URL, or None if not set.
        """
        # Fetch the full user object to access banner data
        try:
            fetched_user = await self.bot.fetch_user(user.id)
        except discord.HTTPException:
            return None

        if fetched_user.banner is None:
            return None

        # Banners can be animated (GIF), so we check and preserve the format
        fmt = 'gif' if fetched_user.banner.is_animated() else 'png'
        url = fetched_user.banner.replace(size=size, format=fmt).url
        return UserAsset(url=url, source="Global")

    async def get_guild_banner(
        self,
        member: discord.Member,
        *,
        size: int = 1024
    ) -> Optional[UserAsset]:
        """Gets a member's guild-specific banner as a UserAsset.

        Args:
            member: The member to get the guild banner for.
            size: The size of the banner to fetch (default 1024).

        Returns:
            A UserAsset containing the guild banner URL, or None if not set.
        """
        # Guild banners require fetching the member with the guild profile
        try:
            # Refetch the member to ensure we have the latest data
            fetched_member = await member.guild.fetch_member(member.id)
        except discord.HTTPException:
            return None

        # Check if the member has a guild-specific banner
        # Note: Guild banners are a Nitro feature and require Server Boosting
        if not hasattr(fetched_member, 'guild_banner') or fetched_member.guild_banner is None:
            return None

        fmt = 'gif' if fetched_member.guild_banner.is_animated() else 'png'
        url = fetched_member.guild_banner.replace(size=size, format=fmt).url
        return UserAsset(url=url, source="Server")

    async def get_all_banners(
        self,
        user: Union[discord.User, discord.Member],
        *,
        size: int = 1024
    ) -> list[UserAsset]:
        """Gets all available banners (global and guild) for a user.

        Args:
            user: The user or member to get banners for.
            size: The size of the banners to fetch (default 1024).

        Returns:
            A list of UserAsset objects for each available banner.
        """
        assets = []

        global_banner = await self.get_banner(user, size=size)
        if global_banner:
            assets.append(global_banner)

        if isinstance(user, discord.Member):
            guild_banner = await self.get_guild_banner(user, size=size)
            if guild_banner:
                assets.append(guild_banner)

        return assets

    async def fetch_asset_bytes(self, asset: UserAsset) -> UserAsset:
        """Fetches the image bytes for a UserAsset.

        Args:
            asset: The UserAsset to fetch bytes for.

        Returns:
            The same UserAsset with image_bytes populated.
        """
        async with aiohttp.ClientSession() as session:
            await asset.fetch(session)
        return asset

    async def fetch_all_asset_bytes(self, assets: list[UserAsset]) -> list[UserAsset]:
        """Fetches the image bytes for multiple UserAssets concurrently.

        Args:
            assets: The list of UserAssets to fetch bytes for.

        Returns:
            The same list of UserAssets with image_bytes populated.
        """
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*[asset.fetch(session) for asset in assets])
        return assets

    def _extract_user_from_query(self, ctx: commands.Context, query: str) -> Optional[discord.Member]:
        """Extracts a mentioned user from the query string.

        Args:
            ctx: The command context.
            query: The user's input string.

        Returns:
            The mentioned Member, or None if no valid mention found.
        """
        # Check for user mentions in the message
        if ctx.message.mentions:
            return ctx.message.mentions[0] if isinstance(ctx.message.mentions[0], discord.Member) else None

        # Try to extract a user ID from the query
        user_id_match = re.search(r'(\d{17,19})', query)
        if user_id_match and ctx.guild:
            try:
                return ctx.guild.get_member(int(user_id_match.group(1)))
            except (ValueError, AttributeError):
                pass

        return None

    # -------------------------------------------------------------------------
    # NLP Handlers for User Assets
    # -------------------------------------------------------------------------

    async def pfp(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for fetching a user's profile picture(s).

        Returns both global and guild-specific avatars if available.

        Args:
            ctx: The command context.
            query: The user's input string (should contain a mention or user ID).
        """
        # Determine target user
        target = self._extract_user_from_query(ctx, query)
        if target is None:
            # If no mention, default to the author
            if ctx.guild:
                target = ctx.guild.get_member(ctx.author.id)
            if target is None:
                await ctx.send("Please mention a user or provide their ID to fetch their profile picture.")
                return

        try:
            async with ctx.typing():
                avatars = await self.get_all_avatars(target, size=1024)

                embeds = []

                for avatar in avatars:
                    embed = discord.Embed(
                        title=f"{target.display_name}'s {avatar.source} Avatar",
                        color=target.color if hasattr(target, 'color') else discord.Color.blurple()
                    )
                    embed.set_image(url=avatar.url)
                    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    embeds.append(embed)

                if len(avatars) == 1:
                    await ctx.send(embed=embeds[0])
                else:
                    # Send both avatars
                    await ctx.send(content=f"Here are {target.display_name}'s avatars:", embeds=embeds)
        except Exception as e:
            self.logger.error(f"Failed to fetch avatar: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to fetch that profile picture.")

    async def banner(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for fetching a user's banner(s).

        Returns both global and guild-specific banners if available.

        Args:
            ctx: The command context.
            query: The user's input string (should contain a mention or user ID).
        """
        # Determine target user
        target = self._extract_user_from_query(ctx, query)
        if target is None:
            # If no mention, default to the author
            if ctx.guild:
                target = ctx.guild.get_member(ctx.author.id)
            if target is None:
                await ctx.send("Please mention a user or provide their ID to fetch their banner.")
                return

        try:
            async with ctx.typing():
                banners = await self.get_all_banners(target, size=1024)

                if not banners:
                    await ctx.send(f"{target.display_name} doesn't have any banners set.")
                    return

                embeds = []
                for banner_asset in banners:
                    embed = discord.Embed(
                        title=f"{target.display_name}'s {banner_asset.source} Banner",
                        color=target.color if hasattr(target, 'color') else discord.Color.blurple()
                    )
                    embed.set_image(url=banner_asset.url)
                    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    embeds.append(embed)

                if len(banners) == 1:
                    await ctx.send(embed=embeds[0])
                else:
                    # Send both banners
                    await ctx.send(content=f"Here are {target.display_name}'s banners:", embeds=embeds)
        except Exception as e:
            self.logger.error(f"Failed to fetch banner: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to fetch that banner.")

    async def _find_image_attachments(self, message: discord.Message) -> list[discord.Attachment]:
        """Finds all valid image attachments in the message or its reply context.

        Checks the current message first. If no images are found there, falls
        back to checking the replied-to message.

        Args:
            message: The message to check.

        Returns:
            A list of image attachments found, which may be empty.
        """
        # Check current message attachments.
        attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith('image/')]
        if attachments:
            return attachments

        # Fall back to reply attachments.
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            return [a for a in message.reference.resolved.attachments if a.content_type and a.content_type.startswith('image/')]

        return []

    async def resize(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for resizing an image.

        It parses dimensions (e.g., "500x500") from the query and resizes the
        attached or replied-to image.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string containing dimensions.
        """
        # Parse dimensions like "500x500" or "500 x 500" from the query.
        match = re.search(r'(\d+)\s*x\s*(\d+)', query)
        if not match:
            await ctx.send("I couldn't find the dimensions. Please specify the size like `500x500`.")
            return

        new_size = (int(match.group(1)), int(match.group(2)))
        # Enforce size limits to prevent abuse.
        if not (0 < new_size[0] <= 4000 and 0 < new_size[1] <= 4000):
            await ctx.send("Invalid dimensions. Both width and height must be between 1 and 4000 pixels.")
            return

        attachments = await self._find_image_attachments(ctx.message)
        if not attachments:
            await ctx.send("Please attach an image or reply to a message with an image to resize.")
            return

        def _processing_thread(image_bytes: bytes, size: Tuple[int, int]) -> io.BytesIO:
            """Contains the synchronous, blocking image processing code.

            This function is intended to be run in a separate thread via asyncio.to_thread
            to avoid blocking the main event loop during heavy image operations.

            Args:
                image_bytes (bytes): The raw image data.
                size (Tuple[int, int]): The target width and height.

            Returns:
                io.BytesIO: The resized image data.
            """
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                # Capture format before resize (PIL clears it on transform)
                original_format = img.format or 'PNG'
                img = img.resize(size)

                buffer = io.BytesIO()
                img.save(buffer, format=original_format)
                buffer.seek(0)
                return buffer

        try:
            async with ctx.typing():  # Show a "typing..." indicator.
                # Fetch all image bytes concurrently.
                all_bytes = await asyncio.gather(*[a.read() for a in attachments])

                # Process all images concurrently in threads.
                buffers = await asyncio.gather(*[
                    asyncio.to_thread(_processing_thread, img_bytes, new_size)
                    for img_bytes in all_bytes
                ])

                files = [
                    discord.File(buf, filename=f"resized_{att.filename}")
                    for att, buf in zip(attachments, buffers, strict=True)
                ]

                label = f"{new_size[0]}x{new_size[1]}"
                if len(files) == 1:
                    await ctx.send(f"Here is the image resized to {label}:", file=files[0])
                else:
                    await ctx.send(f"Here are {len(files)} images resized to {label}:", files=files)
        except Exception as e:
            self.logger.error(f"Failed to resize image: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to resize that image.")

    async def convert(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for converting an image's format.

        It parses the target format (e.g., "png") from the query and converts
        the image.

        Args:
            ctx (commands.Context): The command context.
            query (str): The user's input string containing the target format.
        """
        # Alias 'jpg' to 'jpeg' to handle common user input.
        query = re.sub(r'\bjpg\b', 'jpeg', query, flags=re.IGNORECASE)

        supported_formats = {"png", "jpeg", "webp", "gif", "bmp", "tiff", "ico", "pdf"}

        # Find all unique format mentions in the query.
        found_formats = set(re.findall(r'\b(' + '|'.join(supported_formats) + r')\b', query, re.IGNORECASE))

        if len(found_formats) > 1:
            await ctx.send("I found multiple formats in your request. I can't convert one file into multiple formats!.")
            return
        elif not found_formats:
            await ctx.send(f"I couldn't figure out what format to convert to. Supported formats are: `{', '.join(supported_formats)}`.")
            return

        # Get the single target format and make it uppercase.
        target_format = found_formats.pop().upper()

        attachments = await self._find_image_attachments(ctx.message)
        if not attachments:
            await ctx.send("Please attach an image or reply to a message with an image to convert.")
            return

        def _processing_thread(image_bytes: bytes, format_str: str) -> io.BytesIO:
            """Contains the synchronous, blocking image conversion code.

            This function is intended to be run in a separate thread via asyncio.to_thread
            to avoid blocking the main event loop during heavy image operations.

            Args:
                image_bytes (bytes): The raw image data.
                format_str (str): The target format (e.g., 'PNG', 'JPEG').

            Returns:
                io.BytesIO: The converted image data.
            """
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                # Handle transparency for formats that don't support it (like JPEG and PDF).
                if format_str in ('JPEG', 'PDF') and img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                buffer = io.BytesIO()
                img.save(buffer, format=format_str)
                buffer.seek(0)
                return buffer

        try:
            async with ctx.typing():
                # Fetch all image bytes concurrently.
                all_bytes = await asyncio.gather(*[a.read() for a in attachments])

                # Process all images concurrently in threads.
                buffers = await asyncio.gather(*[
                    asyncio.to_thread(_processing_thread, img_bytes, target_format)
                    for img_bytes in all_bytes
                ])

                ext = target_format.lower()
                files = [
                    discord.File(buf, filename=f"{att.filename.rsplit('.', 1)[0]}.{ext}")
                    for att, buf in zip(attachments, buffers, strict=True)
                ]

                if len(files) == 1:
                    await ctx.send(f"Here is the image converted to {target_format}:", file=files[0])
                else:
                    await ctx.send(f"Here are {len(files)} images converted to {target_format}:", files=files)
        except Exception as e:
            self.logger.error(f"Failed to convert image: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to convert that image.")


async def setup(bot: CoreBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (CoreBot): The bot instance.
    """
    await bot.add_cog(FilesCog(bot))

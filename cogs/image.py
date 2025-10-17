import discord
from discord.ext import commands
import io
import re
import asyncio
from PIL import Image as PILImage
from typing import Optional, Tuple

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot

class Image(BaseCog):
    """A cog for handling image manipulation commands."""

    def __init__(self, bot: SanchoBot):
        super().__init__(bot)

    async def _find_image_attachment(self, message: discord.Message) -> Optional[discord.Attachment]:
        """Finds a valid image attachment in the message or its reply context."""
        # 1. Check attachments on the current message
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('image/'):
                return attachment

        # 2. If it's a reply, check the referenced message's attachments
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            for attachment in message.reference.resolved.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    return attachment
        
        return None

    async def resize(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for resizing an image."""
        match = re.search(r'(\d+)\s*x\s*(\d+)', query)
        if not match:
            await ctx.send("I couldn't find the dimensions. Please specify the size like `500x500`.")
            return

        new_size = (int(match.group(1)), int(match.group(2)))
        if not (0 < new_size[0] <= 4000 and 0 < new_size[1] <= 4000):
            await ctx.send("Invalid dimensions. Both width and height must be between 1 and 4000 pixels.")
            return

        attachment = await self._find_image_attachment(ctx.message)
        if not attachment:
            await ctx.send("Please attach an image or reply to a message with an image to resize.")
            return

        def _processing_thread(image_bytes: bytes, size: tuple[int, int]) -> io.BytesIO:
            """Contains the synchronous, blocking image processing code."""
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                img = img.resize(size)
                
                buffer = io.BytesIO()
                # Preserve original format, but save as PNG if format is unknown
                original_format = img.format or 'PNG'
                img.save(buffer, format=original_format)
                buffer.seek(0)
                return buffer

        try:
            async with ctx.typing():
                image_bytes = await attachment.read()
                
                # Run the blocking code in a separate thread
                buffer = await asyncio.to_thread(_processing_thread, image_bytes, new_size)
                
                filename = f"resized_{attachment.filename}"
                await ctx.send(f"Here is the image resized to {new_size[0]}x{new_size[1]}:", file=discord.File(buffer, filename=filename))
            
            self.logger.info(f"Resized image for {ctx.author} to {new_size[0]}x{new_size[1]}.")
        except Exception as e:
            self.logger.error(f"Failed to resize image: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to resize that image.")

    async def convert(self, ctx: commands.Context, *, query: str) -> None:
        """NLP handler for converting an image's format."""
        supported_formats = {"png", "jpeg", "webp", "gif", "bmp"}
        match = re.search(r'\bto\s+(' + '|'.join(supported_formats) + r')\b', query, re.IGNORECASE)
        if not match:
            await ctx.send(f"I couldn't figure out what format to convert to. Supported formats are: `{', '.join(supported_formats)}`.")
            return
        
        target_format = match.group(1).upper()
        
        attachment = await self._find_image_attachment(ctx.message)
        if not attachment:
            await ctx.send("Please attach an image or reply to a message with an image to convert.")
            return

        def _processing_thread(image_bytes: bytes, format_str: str) -> io.BytesIO:
            """Contains the synchronous, blocking image conversion code."""
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                # Handle RGBA for formats that don't support it (like JPEG)
                if format_str == 'JPEG' and img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                buffer = io.BytesIO()
                img.save(buffer, format=format_str)
                buffer.seek(0)
                return buffer

        try:
            async with ctx.typing():
                image_bytes = await attachment.read()

                # Run the blocking code in a separate thread
                buffer = await asyncio.to_thread(_processing_thread, image_bytes, target_format)
                
                # Create a new filename with the correct extension
                base_filename = attachment.filename.rsplit('.', 1)[0]
                new_filename = f"{base_filename}.{target_format.lower()}"

                await ctx.send(f"Here is the image converted to {target_format}:", file=discord.File(buffer, filename=new_filename))
            
            self.logger.info(f"Converted image for {ctx.author} to {target_format}.")
        except Exception as e:
            self.logger.error(f"Failed to convert image: {e}", exc_info=True)
            await ctx.send("Sorry, I encountered an error trying to convert that image.")

async def setup(bot: SanchoBot):
    """Sets up the Image cog."""
    await bot.add_cog(Image(bot))

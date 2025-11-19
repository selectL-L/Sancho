import discord
from discord.ext import commands
from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
import logging
from typing import Optional
import io
import aiohttp
import asyncio

logger = logging.getLogger(__name__)

class Starboard(BaseCog):
    def __init__(self, bot: SanchoBot):
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager
        self.starboard_emoji = "⭐"
        self.starboard_threshold = 3
        self.http_session = aiohttp.ClientSession()
        self._locks = {} # For preventing race conditions

    async def cog_unload(self):
        await self.http_session.close()

    async def get_starboard_config(self, guild_id: int) -> tuple[Optional[int], str, int]:
        """Fetches starboard configuration for a guild, with defaults."""
        channel_id_str = await self.db_manager.get_guild_config(guild_id, "starboard_channel_id")
        emoji = await self.db_manager.get_guild_config(guild_id, "starboard_emoji") or self.starboard_emoji
        threshold_str = await self.db_manager.get_guild_config(guild_id, "starboard_threshold")
        
        channel_id = int(channel_id_str) if channel_id_str and channel_id_str.isdigit() else None
        threshold = int(threshold_str) if threshold_str and threshold_str.isdigit() else self.starboard_threshold
        
        return channel_id, emoji, threshold

    @commands.group(name="starboard", invoke_without_command=True, hidden=True)
    @commands.has_permissions(manage_guild=True)
    async def starboard_group(self, ctx: commands.Context):
        """Manages starboard settings."""
        await ctx.send_help(ctx.command)

    @starboard_group.command(name="channel")
    async def set_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Sets the channel for the starboard."""
        if ctx.guild:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_channel_id", str(channel.id))
            await ctx.send(f"Starboard channel set to {channel.mention}")

    @starboard_group.command(name="emoji")
    async def set_emoji(self, ctx: commands.Context, emoji: str):
        """Sets the emoji for the starboard."""
        if ctx.guild:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_emoji", emoji)
            await ctx.send(f"Starboard emoji set to {emoji}")

    @starboard_group.command(name="threshold")
    async def set_threshold(self, ctx: commands.Context, threshold: int):
        """Sets the reaction threshold for the starboard."""
        if ctx.guild and threshold > 0:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_threshold", str(threshold))
            await ctx.send(f"Starboard threshold set to {threshold}")

    @starboard_group.command(name="reload")
    async def reload_starboard(self, ctx: commands.Context):
        """
        DEV COMMAND: Deletes and remakes all starboard messages in the guild.
        Only usable by the bot owner when DEV_MODE is True.
        """
        from config import DEV_MODE
        if not DEV_MODE:
            await ctx.send("This command is only available in developer mode.")
            return

        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        await ctx.send("Starting starboard reload...")
        logger.info(f"DEV_MODE: Starting starboard reload for guild {ctx.guild.id} triggered by {ctx.author.id}.")

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(ctx.guild.id)
        if not starboard_channel_id:
            await ctx.send("Starboard channel is not configured.")
            return
        
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            await ctx.send("Starboard channel not found.")
            return

        all_entries = await self.db_manager.get_all_starboard_entries_for_guild(ctx.guild.id)
        if not all_entries:
            await ctx.send("No starboard entries found to reload.")
            return
            
        # Store original message info before deleting
        messages_to_recreate = [
            {'message_id': entry['original_message_id'], 'channel_id': entry['original_channel_id']}
            for entry in all_entries if entry.get('original_channel_id')
        ]

        # --- Deletion Phase ---
        logger.info(f"Deleting {len(all_entries)} existing starboard messages...")
        deleted_count = 0
        for entry in all_entries:
            try:
                # Delete the main starboard message
                msg = await starboard_channel.fetch_message(entry['starboard_message_id'])
                await msg.delete()
                deleted_count += 1
            except discord.NotFound:
                pass # Message already gone
            except discord.HTTPException as e:
                logger.error(f"Failed to delete starboard message {entry['starboard_message_id']}: {e}")

            # Delete the context message if it exists
            if entry.get('starboard_reply_id'):
                try:
                    reply_msg = await starboard_channel.fetch_message(entry['starboard_reply_id'])
                    await reply_msg.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    logger.error(f"Failed to delete starboard reply context {entry['starboard_reply_id']}: {e}")
        
        await self.db_manager.clear_starboard_for_guild(ctx.guild.id)
        await ctx.send(f"Deleted {deleted_count} starboard messages and cleared database entries.")

        # --- Recreation Phase ---
        logger.info(f"Attempting to recreate {len(messages_to_recreate)} posts...")
        recreated_count = 0
        failed_count = 0

        for msg_info in messages_to_recreate:
            original_channel = self.bot.get_channel(msg_info['channel_id'])
            if not isinstance(original_channel, discord.TextChannel):
                logger.warning(f"Could not find original channel {msg_info['channel_id']}. Skipping message {msg_info['message_id']}.")
                failed_count += 1
                continue
            
            try:
                message = await original_channel.fetch_message(msg_info['message_id'])
                star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)

                if star_reaction and star_reaction.count >= starboard_threshold:
                    await self.post_to_starboard(message, starboard_channel_id, starboard_emoji, star_reaction.count)
                    recreated_count += 1
                    await asyncio.sleep(1) # Avoid rate limits
                else:
                    logger.info(f"Message {message.id} no longer meets threshold. Not recreating.")
            except discord.NotFound:
                logger.warning(f"Original message {msg_info['message_id']} not found in channel {msg_info['channel_id']}. Skipping.")
                failed_count += 1
            except Exception as e:
                logger.error(f"Failed to recreate starboard post for message {msg_info['message_id']}: {e}")
                failed_count += 1

        await ctx.send(f"Starboard reload complete. Recreated {recreated_count} posts. Failed to recreate {failed_count} posts.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or not self.bot.user or payload.user_id == self.bot.user.id:
            return

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(payload.guild_id)

        if not starboard_channel_id or str(payload.emoji) != starboard_emoji:
            return

        # Use a lock to prevent race conditions from multiple simultaneous reactions
        lock = self._locks.setdefault(payload.message_id, asyncio.Lock())
        async with lock:
            channel = self.bot.get_channel(payload.channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.id == starboard_channel_id:
                return
                
            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                logger.warning(f"Starboard: Message {payload.message_id} not found.")
                return

            # Find the reaction count for the correct emoji
            star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)
            if not star_reaction:
                return

            if star_reaction.count >= starboard_threshold:
                await self.post_to_starboard(message, starboard_channel_id, starboard_emoji, star_reaction.count)
        
        # Clean up lock if no longer needed
        if lock.locked() is False:
            self._locks.pop(payload.message_id, None)

    async def post_to_starboard(self, message: discord.Message, starboard_channel_id: int, starboard_emoji: str, star_count: int):
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            logger.error(f"Starboard channel with ID {starboard_channel_id} not found or is not a text channel.")
            return

        existing_entry = await self.db_manager.get_starboard_entry(message.id)
        content = f"{starboard_emoji} **{star_count}** in <#{message.channel.id}>"
        logger.info(f"Starboard post content: {content}")

        if existing_entry:
            try:
                starboard_message = await starboard_channel.fetch_message(existing_entry['starboard_message_id'])
                await starboard_message.edit(content=content)
            except discord.NotFound:
                # The message was deleted from the starboard channel, so we should remove the entry and recreate it.
                logger.warning(f"Starboard message for {message.id} not found. Removing entry and recreating.")
                await self.db_manager.remove_starboard_entry(message.id)
                await self.create_new_starboard_post(message, starboard_channel, content)
        else:
            await self.create_new_starboard_post(message, starboard_channel, content)

    async def create_new_starboard_post(self, message: discord.Message, starboard_channel: discord.TextChannel, content: str):
        """
        Creates a new starboard post. If the message is a reply, it posts the replied-to message first,
        then replies to that with the starred message.
        """
        # If it's a reply, handle the two-message system
        if message.reference and message.reference.message_id and isinstance(message.channel, discord.TextChannel):
            try:
                replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                
                # 1. Post the context of the replied-to message.
                reply_embed, reply_files = await self.create_starboard_embed_and_files(replied_to_message)
                reply_context_message = await starboard_channel.send(embed=reply_embed, files=reply_files)
                for file in reply_files:
                    file.close()

                # 2. Post the main starred message as a reply to the context message.
                main_embed, main_files = await self.create_starboard_embed_and_files(message)
                starboard_message = await reply_context_message.reply(content=content, embed=main_embed, files=main_files)
                for file in main_files:
                    file.close()

                # 3. Save to DB with both IDs
                if message.guild:
                    await self.db_manager.add_starboard_entry(
                        message.id, starboard_message.id, message.guild.id, message.channel.id, reply_context_message.id
                    )

            except discord.NotFound:
                # If the replied-to message is gone, just post the main message as a normal post.
                await self.create_single_starboard_post(message, starboard_channel, content)
            except discord.HTTPException as e:
                logger.error(f"Failed to create two-part starboard post: {e}")
        
        # If it's not a reply, just post it directly
        else:
            await self.create_single_starboard_post(message, starboard_channel, content)

    async def create_single_starboard_post(self, message: discord.Message, starboard_channel: discord.TextChannel, content: str):
        """Creates a single starboard post, used for non-reply messages or as a fallback."""
        embed, files = await self.create_starboard_embed_and_files(message)
        try:
            starboard_message = await starboard_channel.send(content=content, embed=embed, files=files)
            if message.guild:
                await self.db_manager.add_starboard_entry(message.id, starboard_message.id, message.guild.id, message.channel.id)
        except discord.HTTPException as e:
            logger.error(f"Failed to create single starboard post: {e}")
        finally:
            for file in files:
                file.close()

    async def create_starboard_embed_and_files(self, message: discord.Message) -> tuple[discord.Embed, list[discord.File]]:
        """Creates an embed and a list of discord.File objects for a starboard message, handling regular content, attachments, and embeds."""
        
        description_parts = []
        files = []

        # 1. Always start with the message's direct text content, if any.
        if message.content:
            description_parts.append(message.content)

        # 2. Process direct attachments on the main message.
        for attachment in message.attachments:
            try:
                async with self.http_session.get(attachment.url) as resp:
                    if resp.status == 200:
                        data = io.BytesIO(await resp.read())
                        files.append(discord.File(data, filename=attachment.filename, spoiler=attachment.is_spoiler()))
            except Exception as e:
                logger.error(f"Failed to download direct attachment for starboard: {e}")

        # 3. Process message snapshots for forwarded content.
        if hasattr(message, 'message_snapshots') and message.message_snapshots:
            for snapshot in message.message_snapshots:
                if snapshot.content:
                    description_parts.append(snapshot.content)
                # Process attachments within the snapshot.
                for attachment in snapshot.attachments:
                    try:
                        async with self.http_session.get(attachment.url) as resp:
                            if resp.status == 200:
                                data = io.BytesIO(await resp.read())
                                files.append(discord.File(data, filename=attachment.filename, spoiler=attachment.is_spoiler()))
                    except Exception as e:
                        logger.error(f"Failed to download snapshot attachment for starboard: {e}")
        
        # 4. Handle embeds as a fallback or for link previews.
        elif message.embeds:
            embed = message.embeds[0]
            if embed.description:
                description_parts.append(embed.description)
            # Download image from the embed if it exists.
            if embed.image and embed.image.url:
                try:
                    async with self.http_session.get(embed.image.url) as resp:
                        if resp.status == 200:
                            data = io.BytesIO(await resp.read())
                            filename = embed.image.url.split('/')[-1].split('?')[0] or "embedded_image.png"
                            files.append(discord.File(data, filename=filename))
                except Exception as e:
                    logger.error(f"Failed to download embedded image for starboard: {e}")

        # Join all collected parts into a single description string.
        description = "\n\n".join(description_parts)

        # Truncate the final description if it's too long for an embed.
        if len(description) > 4096:
            description = description[:4093] + "..."

        new_embed = discord.Embed(
            description=description,
            color=discord.Color.gold(),
            timestamp=message.created_at
        )
        new_embed.set_author(name=f"{message.author.display_name} ({message.author.name})", icon_url=message.author.display_avatar.url)
        new_embed.set_footer(text=f"ID: {message.id}")
        new_embed.add_field(name="Original Message", value=f"[Jump to Message]({message.jump_url})", inline=False)
        
        return new_embed, files

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(payload.guild_id)

        if not starboard_channel_id or str(payload.emoji) != starboard_emoji:
            return

        existing_entry = await self.db_manager.get_starboard_entry(payload.message_id)
        if not existing_entry:
            return

        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            return
        
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
            star_count = 0
            star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)
            if star_reaction:
                star_count = star_reaction.count
            
            starboard_message = await starboard_channel.fetch_message(existing_entry['starboard_message_id'])
            
            if star_count < starboard_threshold:
                await starboard_message.delete()
                # If there's a related reply context message, delete it too.
                if existing_entry.get('starboard_reply_id'):
                    try:
                        reply_context_message = await starboard_channel.fetch_message(existing_entry['starboard_reply_id'])
                        await reply_context_message.delete()
                    except discord.NotFound:
                        logger.warning(f"Starboard reply context message {existing_entry['starboard_reply_id']} not found for deletion.")
                
                await self.db_manager.remove_starboard_entry(message.id)
            else:
                content = f"{starboard_emoji} **{star_count}** in <#{message.channel.id}>"
                await starboard_message.edit(content=content)
        except discord.NotFound:
            # This can happen if the original message, the starboard message, or the channel is deleted.
            # In any case, the entry is now invalid.
            await self.db_manager.remove_starboard_entry(payload.message_id)

async def setup(bot: SanchoBot):
    await bot.add_cog(Starboard(bot))
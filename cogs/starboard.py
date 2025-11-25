"""cogs/starboard.py

This cog implements a "Starboard" feature, which is a popular way to highlight
interesting or funny messages in a server. When a message receives a certain
number of "star" (⭐) reactions, it is reposted to a designated starboard channel.

Key Features:
- Configurable Settings: Guild admins can set the target channel, the emoji to
  use (defaulting to ⭐), and the reaction threshold required to post.
- Automatic Posting: Monitors reactions and automatically posts messages that
  meet the threshold.
- Updates and Deletions: Updates the star count on the starboard post as more
  reactions are added. Removes the post if the reaction count drops below the
  threshold.
- Rich Content Support: Handles text, images, attachments, and even forwarded
  message snapshots, ensuring the starboard post faithfully represents the original.
- Reply Context: If the starred message is a reply to another message, the
  starboard post attempts to show that context by posting the parent message first.
- Maintenance Tools: Includes a powerful `reload` command to rebuild the starboard
  from history or fix database inconsistencies, with support for a "fast mode"
  to bypass rate limits in emergencies.
"""

import asyncio
import datetime
import inspect
import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.base_cog import BaseCog
from utils.bot_class import SanchoBot
from utils.database import DatabaseManager
from utils.views import FastConfirmModal

logger = logging.getLogger(__name__)


class Starboard(BaseCog):
    """The cog for managing the Starboard feature."""

    def __init__(self, bot: SanchoBot):
        """Initializes the Starboard cog.

        Args:
            bot (SanchoBot): The bot instance.
        """
        super().__init__(bot)
        assert bot.db_manager is not None
        self.db_manager: DatabaseManager = bot.db_manager
        self.starboard_emoji = "⭐"
        self.starboard_threshold = 3
        self.http_session = aiohttp.ClientSession()
        self._locks: Dict[int, asyncio.Lock] = {}  # For preventing race conditions
        # Rate-limiting controls for slow 'fix' operations
        self._fix_semaphore = asyncio.Semaphore(1)
        self._fix_delay = 0.6  # seconds between external calls
        self._fix_retries = 4
        # Fast-mode override (disabled by default). When True, bypass rate-limits and thresholds.
        self._fast_mode = False

    async def cog_unload(self) -> None:
        """Clean up resources when the cog is unloaded."""
        await self.http_session.close()

    async def get_starboard_config(self, guild_id: int) -> Tuple[Optional[int], str, int]:
        """Fetches starboard configuration for a guild, with defaults.

        Args:
            guild_id (int): The ID of the guild.

        Returns:
            Tuple[Optional[int], str, int]: A tuple containing:
                - The starboard channel ID (or None if not set).
                - The starboard emoji string.
                - The reaction threshold.
        """
        channel_id_str = await self.db_manager.get_guild_config(guild_id, "starboard_channel_id")
        emoji = await self.db_manager.get_guild_config(guild_id, "starboard_emoji") or self.starboard_emoji
        threshold_str = await self.db_manager.get_guild_config(guild_id, "starboard_threshold")

        channel_id = int(channel_id_str) if channel_id_str and channel_id_str.isdigit() else None
        threshold = int(threshold_str) if threshold_str and threshold_str.isdigit() else self.starboard_threshold

        return channel_id, emoji, threshold

    @commands.hybrid_group(name="starboard", hidden=True, usage="<subcommand>")
    @commands.has_guild_permissions(manage_channels=True)
    async def starboard_group(self, ctx: commands.Context) -> None:
        """Manages starboard settings.

        Args:
            ctx (commands.Context): The command context.
        """
        if ctx.invoked_subcommand is None:
            help_cog: Any = self.bot.get_cog('Help')
            if help_cog and hasattr(help_cog, 'send_command_help'):
                await help_cog.send_command_help(ctx, ctx.command)
            else:
                await ctx.send_help(ctx.command)

    @starboard_group.command(name="channel")
    async def set_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Sets the channel for the starboard.

        Args:
            ctx (commands.Context): The command context.
            channel (discord.TextChannel): The channel to use for the starboard.
        """
        if ctx.guild:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_channel_id", str(channel.id))
            await ctx.send(f"Starboard channel set to {channel.mention}")

    @starboard_group.command(name="emoji")
    async def set_emoji(self, ctx: commands.Context, emoji: str) -> None:
        """Sets the emoji for the starboard.

        Args:
            ctx (commands.Context): The command context.
            emoji (str): The emoji to use.
        """
        if ctx.guild:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_emoji", emoji)
            await ctx.send(f"Starboard emoji set to {emoji}")

    @starboard_group.command(name="threshold")
    async def set_threshold(self, ctx: commands.Context, threshold: int) -> None:
        """Sets the reaction threshold for the starboard.

        Args:
            ctx (commands.Context): The command context.
            threshold (int): The minimum number of reactions required.
        """
        if ctx.guild and threshold > 0:
            await self.db_manager.set_guild_config(ctx.guild.id, "starboard_threshold", str(threshold))
            await ctx.send(f"Starboard threshold set to {threshold}")

    async def _confirm_fast_mode(self, ctx: commands.Context, fast: bool) -> bool:
        """Handles the confirmation logic for fast mode."""
        if not ctx.guild:
            return False
        guild = ctx.guild

        # Support an explicit `fast` boolean option for slash commands or prefix callers.
        msg = getattr(ctx, 'message', None)
        msg_content = msg.content.lower() if msg and getattr(msg, 'content', None) else ''
        fast_requested = bool(fast) or ('--fast' in msg_content)

        if not fast_requested:
            self._fast_mode = False
            return True

        # Present a modal to the caller for explicit confirmation
        # TODO: Rewrite the modal interaction logic in utils/views.py to be more robust and reusable.
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        modal = FastConfirmModal(future)

        # Send the modal and wait for the future to be set by the modal submit handler
        send_modal = None
        if getattr(ctx, 'interaction', None):
            send_modal = getattr(ctx.interaction, 'response', None)
        # Fallback to Context.send_modal (older shims / wrappers)
        if not send_modal:
            send_modal = getattr(ctx, 'send_modal', None)

        if callable(send_modal):
            try:
                # If we have an interaction response, use `send_modal` via that interface.
                res = send_modal(modal)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                # If something goes wrong with modal sending, fall back to text confirmation
                send_modal = None

        if not callable(send_modal):
            await ctx.send("WARNING: Modals unavailable — please reply with 'I understand the risks' to confirm fast mode.")
            try:
                def _check(m: discord.Message) -> bool:
                    return m.author == ctx.author and m.channel == ctx.channel and m.content.strip().lower() == 'i understand the risks'

                await self.bot.wait_for('message', check=_check, timeout=30.0)
                confirmed = True
            except asyncio.TimeoutError:
                await ctx.send('Fast mode cancelled (no confirmation).')
                return False
            if not confirmed:
                return False
            self._fast_mode = True
            return True
        else:
            try:
                confirmed = await asyncio.wait_for(future, timeout=30.0)
            except asyncio.TimeoutError:
                await ctx.send('Fast mode cancelled (no confirmation).')
                return False
            if not confirmed:
                return False
            # Mark fast mode and write an audit log entry
            self._fast_mode = True
            logger.warning(f"FAST MODE ENABLED by {ctx.author} ({ctx.author.id}) in guild {guild.id} at {datetime.datetime.utcnow().isoformat()}")
            return True

    @starboard_group.command(name="remake")
    @commands.is_owner()
    @app_commands.describe(
        fast="If True, skips rate limits and confirmations (Dangerous!)."
    )
    async def remake_starboard(self, ctx: commands.Context, fast: bool = False) -> None:
        """Recreates starboard posts from history. Only callable by the bot owner.

        Usage: /starboard remake [fast]

        Args:
            ctx (commands.Context): The command context.
            fast (bool): Whether to enable fast mode. Defaults to False.
        """
        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        # Safety Gate: Check for invalid entries
        all_entries = await self.db_manager.get_all_starboard_entries_for_guild(ctx.guild.id)
        invalid_count = 0
        for entry in all_entries:
            if not (entry.get('original_message_id') and entry.get('starboard_message_id') and entry.get('guild_id') and entry.get('original_channel_id')):
                invalid_count += 1

        if invalid_count > 0:
            msg = (
                f"⚠️ **WARNING**: Found {invalid_count} invalid/incomplete starboard entries.\n"
                "These entries will be **IGNORED** (effectively deleted) during the remake process.\n"
                "It is highly recommended to run `/starboard fix` first to attempt recovery.\n\n"
                "Do you want to proceed anyway?"
            )

            # Reuse the confirmation logic but with a custom message if possible,
            # or just rely on the standard confirmation flow.
            # Since _confirm_fast_mode is specific to fast mode, let's do a simple confirmation here.

            await ctx.send(msg)
            try:
                def _check(m: discord.Message) -> bool:
                    return m.author == ctx.author and m.channel == ctx.channel and m.content.strip().lower() in ('yes', 'y', 'confirm')

                await self.bot.wait_for('message', check=_check, timeout=30.0)
            except asyncio.TimeoutError:
                await ctx.send("Remake cancelled.")
                return

        if not await self._confirm_fast_mode(ctx, fast):
            return

        await self._remake_impl(ctx)

    @starboard_group.command(name="fix")
    @commands.is_owner()
    @app_commands.describe(
        fast="If True, skips rate limits and confirmations (Dangerous!)."
    )
    async def fix_starboard(self, ctx: commands.Context, fast: bool = False) -> None:
        """Repairs starboard DB entries. Only callable by the bot owner.

        Usage: /starboard fix [fast]

        Args:
            ctx (commands.Context): The command context.
            fast (bool): Whether to enable fast mode. Defaults to False.
        """
        if not ctx.guild:
            await ctx.send("This command must be used in a guild.")
            return

        if not await self._confirm_fast_mode(ctx, fast):
            return

        await self._fix_impl(ctx)

    async def _remake_impl(self, ctx: commands.Context) -> None:
        """Implementation of the remake logic."""
        if not ctx.guild:
            return
        guild = ctx.guild

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(guild.id)
        if not starboard_channel_id:
            await ctx.send("Starboard channel is not configured.")
            return
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            await ctx.send("Starboard channel not found.")
            return

        all_entries = await self.db_manager.get_all_starboard_entries_for_guild(guild.id)
        if not all_entries:
            await ctx.send("No starboard entries found.")
            return

        await ctx.send("Starting starboard remake...")
        logger.info(f"Starboard remake for guild {guild.id} triggered by {ctx.author.id}.")

        # Strict Filter: Only remake entries that have complete stored information
        # We require original_message_id, starboard_message_id, guild_id, and original_channel_id.
        # If any are missing, 'fix' should be run first.
        valid_entries = [
            entry for entry in all_entries
            if entry.get('original_message_id') and entry.get('starboard_message_id') and entry.get('guild_id') and entry.get('original_channel_id')
        ]

        logger.info(f"Deleting {len(valid_entries)} existing starboard messages and their reply contexts...")
        deleted_count = 0
        # We'll collect recreation targets from the valid entries before clearing DB
        recreation_targets = []
        for entry in valid_entries:
            logger.debug(f"Remake processing DB entry id={entry.get('original_message_id')} starboard_id={entry.get('starboard_message_id')}")
            recreation_targets.append({
                'original_message_id': entry['original_message_id'],
                'original_channel_id': entry['original_channel_id'],
                'guild_id': entry['guild_id']
            })

            # Delete the main starboard message if it exists
            try:
                if entry.get('starboard_message_id'):
                    logger.debug(f"Fetching starboard message {entry['starboard_message_id']} for deletion")
                    msg = await starboard_channel.fetch_message(entry['starboard_message_id'])
                    logger.debug(f"Deleting starboard message {msg.id}")
                    await msg.delete()
                    logger.info(f"Deleted starboard message {entry['starboard_message_id']}")
                    deleted_count += 1
            except (discord.NotFound, KeyError):
                logger.debug(f"Starboard message {entry.get('starboard_message_id')} not found when attempting deletion")
                pass
            except discord.HTTPException as e:
                logger.error(f"Failed to delete starboard message {entry.get('starboard_message_id')}: {e}")

            # Delete the reply context message if present
            reply_id = entry.get('starboard_reply_id')
            if reply_id is not None:
                try:
                    logger.debug(f"Fetching starboard reply context {reply_id} for deletion")
                    reply_msg = await starboard_channel.fetch_message(reply_id)
                    await reply_msg.delete()
                    logger.info(f"Deleted starboard reply context {reply_id}")
                except (discord.NotFound, KeyError):
                    logger.debug(f"Starboard reply context {reply_id} not found during deletion")
                    pass
                except discord.HTTPException as e:
                    logger.error(f"Failed to delete starboard reply context {reply_id}: {e}")

        # Clear DB entries for this guild so we can recreate fresh
        await self.db_manager.clear_starboard_for_guild(guild.id)
        await ctx.send(f"Deleted {deleted_count} starboard messages and cleared database entries.")
        logger.info(f"Cleared starboard entries for guild {guild.id}; preparing to recreate {len(recreation_targets)} entries.")

        # --- Recreation Phase ---
        logger.info(f"Attempting to recreate {len(recreation_targets)} posts (ignoring current reaction counts)...")
        recreated_count = 0
        failed_count = 0
        tombstone_count = 0

        for tgt in recreation_targets:
            logger.debug(f"Recreation target: {tgt}")
            original_channel = self.bot.get_channel(tgt['original_channel_id'])

            # If channel is missing, we can't fetch the message -> Tombstone
            if not isinstance(original_channel, discord.TextChannel):
                logger.warning(f"Original channel {tgt['original_channel_id']} not found. Creating tombstone for {tgt['original_message_id']}.")
                tomb = await self._create_tombstone(starboard_channel, tgt['original_message_id'])
                if tomb:
                    await self.db_manager.add_starboard_entry(tgt['original_message_id'], tomb.id, tgt['guild_id'], tgt['original_channel_id'])
                    tombstone_count += 1
                else:
                    failed_count += 1
                continue

            try:
                logger.debug(f"Fetching original message {tgt['original_message_id']} from channel {original_channel.id}")
                message = await original_channel.fetch_message(tgt['original_message_id'])
                logger.debug(f"Fetched original message {message.id} (author_id={getattr(message.author, 'id', None)})")

                # Only recreate if the message still meets the starboard threshold
                star_reaction = discord.utils.get(message.reactions, emoji=starboard_emoji)
                current_count = star_reaction.count if star_reaction else 0
                logger.debug(f"Original message {message.id} has {current_count} '{starboard_emoji}' reactions; threshold={starboard_threshold}")

                # If fast mode requested, recreate regardless of the current reaction count
                if self._fast_mode or (star_reaction and current_count >= starboard_threshold):
                    logger.info(f"Recreating starboard post for original message {message.id}")
                    await self.post_to_starboard(message, starboard_channel_id, starboard_emoji, current_count)
                    logger.debug(f"Requested creation of starboard post for {message.id}")
                    recreated_count += 1
                    await asyncio.sleep(0.5)
                else:
                    logger.info(f"Message {message.id} no longer meets threshold ({current_count} < {starboard_threshold}). Skipping recreation.")
                    # Do not create a tombstone for messages that are simply under threshold; skip.
                    continue
            except discord.NotFound:
                # Original message deleted -> create a tombstone
                try:
                    logger.info(f"Original message {tgt['original_message_id']} not found — creating tombstone.")
                    tomb = await self._create_tombstone(starboard_channel, tgt['original_message_id'])
                    if tomb:
                        await self.db_manager.add_starboard_entry(tgt['original_message_id'], tomb.id, tgt['guild_id'], tgt['original_channel_id'])
                        logger.debug(f"Tombstone created with id {tomb.id} for original {tgt['original_message_id']}")
                        tombstone_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.error(f"Failed to create tombstone for missing original {tgt['original_message_id']}: {e}")
                    failed_count += 1
            except Exception as e:
                logger.error(f"Failed to recreate starboard post for message {tgt['original_message_id']}: {e}")
                failed_count += 1

        await ctx.send(f"Starboard remake complete. Recreated: {recreated_count}, Tombstones: {tombstone_count}, Failed: {failed_count}.")
        # Reset fast mode to avoid affecting future operations
        self._fast_mode = False

    async def _create_tombstone(self, starboard_channel: discord.TextChannel, original_message_id: int) -> Optional[discord.Message]:
        """Creates a tombstone message for a lost original message."""
        try:
            return await starboard_channel.send(f"🪦 Original Message {original_message_id} Lost")
        except Exception as e:
            logger.error(f"Failed to create tombstone for {original_message_id}: {e}")
            return None

    async def _fix_impl(self, ctx: commands.Context) -> None:
        """Implementation of the fix logic."""
        if not ctx.guild:
            return
        guild = ctx.guild

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(guild.id)
        if not starboard_channel_id:
            await ctx.send("Starboard channel is not configured.")
            return
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            await ctx.send("Starboard channel not found.")
            return

        all_entries = await self.db_manager.get_all_starboard_entries_for_guild(guild.id)
        if not all_entries:
            await ctx.send("No starboard entries found.")
            return

        await ctx.send("Starting starboard fix and verification...")
        logger.info(f"Starboard fix for guild {guild.id} triggered by {ctx.author.id}.")
        fixed_count = 0
        failed_count = 0
        verified_count = 0
        tombstone_count = 0

        # Start a periodic status notifier so the caller sees progress for long runs
        stop_event = asyncio.Event()
        progress = {'done': 0, 'total': len(all_entries), 'elapsed': 0}
        status_msg = await ctx.send(f"Starboard fix started. Processed 0/{len(all_entries)}. Elapsed: 0s. Please wait.")
        status_task = asyncio.create_task(self._status_editor(status_msg, progress, stop_event, interval=30.0))
        logger.debug("Status editor task started for fix operation")

        for entry in all_entries:
            try:
                original_id = entry.get('original_message_id')
                starboard_id = entry.get('starboard_message_id')
                entry_guild_id = entry.get('guild_id')
                entry_channel_id = entry.get('original_channel_id')

                # We need at least an original_message_id OR a starboard_message_id to do anything meaningful
                if not original_id and not starboard_id:
                    logger.warning(f"Skipping corrupt entry with no IDs: {entry}")
                    failed_count += 1
                    progress['done'] += 1
                    continue

                sb_msg = None
                missing_sb = True

                # --- Scenario A: Starboard Message ID exists ---
                if starboard_id:
                    try:
                        sb_msg = await self._run_rate_limited(starboard_channel.fetch_message, starboard_id)
                        missing_sb = False
                    except discord.NotFound:
                        logger.info(f"Starboard message {starboard_id} not found (404). Treating as missing.")
                        missing_sb = True
                    except Exception as e:
                        logger.warning(f"Error fetching starboard message {starboard_id}: {e}. Treating as missing.")
                        missing_sb = True

                if not missing_sb and sb_msg:
                    # Validate metadata from Embed
                    updated = False
                    if sb_msg.embeds:
                        embed = sb_msg.embeds[0]
                        # Parse Jump URL
                        # Expected format: https://discord.com/channels/{guild_id}/{channel_id}/{message_id}
                        # We look for the "Original Message" field
                        jump_url = None
                        found_channel_id = None
                        for field in embed.fields:
                            if field.name == 'Original Message' and field.value:
                                match = re.search(r"/channels/(\d+)/(\d+)/(\d+)", field.value)
                                if match:
                                    found_guild_id = int(match.group(1))
                                    found_channel_id = int(match.group(2))
                                    found_msg_id = int(match.group(3))

                                    if entry_guild_id != found_guild_id:
                                        entry['guild_id'] = found_guild_id
                                        updated = True
                                    if entry_channel_id != found_channel_id:
                                        entry['original_channel_id'] = found_channel_id
                                        updated = True

                                    # Update original_id if we recovered it
                                    if original_id != found_msg_id:
                                        entry['original_message_id'] = found_msg_id
                                        original_id = found_msg_id  # Update local var for later use
                                        updated = True

                                    jump_url = field.value
                                    # Extract URL from markdown [Jump to Message](url)
                                    url_match = re.search(r"\((http[^\)]+)\)", jump_url)
                                    if url_match:
                                        jump_url = url_match.group(1)
                                break

                        # Check if Jump URL target is valid
                        target_valid = False
                        if jump_url:
                            # We can try to fetch the message to see if it exists
                            # We have found_channel_id from the regex
                            if found_channel_id and original_id:
                                try:
                                    ch = self.bot.get_channel(found_channel_id)
                                    if isinstance(ch, discord.abc.Messageable):
                                        await self._run_rate_limited(ch.fetch_message, original_id)
                                        target_valid = True
                                    else:
                                        # If channel type is wrong/unknown, assume valid to be safe
                                        target_valid = True
                                except discord.NotFound:
                                    target_valid = False
                                except Exception:
                                    # If we can't check, assume valid to avoid destructive tombstoning on transient errors
                                    target_valid = True
                            else:
                                # If we couldn't parse channel ID, we can't verify.
                                target_valid = True

                        if not target_valid and original_id:
                            logger.info(f"Original message {original_id} seems dead (Jump URL invalid). Tombstoning.")
                            await sb_msg.delete()
                            tomb = await self._create_tombstone(starboard_channel, original_id)
                            if tomb:
                                entry['starboard_message_id'] = tomb.id
                                await self.db_manager.update_starboard_entry(entry)
                                tombstone_count += 1
                                fixed_count += 1
                        else:
                            # Update reply ID if needed
                            found_reply_id = sb_msg.reference.message_id if sb_msg.reference else None
                            if entry.get('starboard_reply_id') != found_reply_id:
                                entry['starboard_reply_id'] = found_reply_id
                                updated = True

                            if updated:
                                await self.db_manager.update_starboard_entry(entry)
                                fixed_count += 1
                            else:
                                verified_count += 1

                # --- Scenario B: Starboard Message Missing ---
                else:
                    # Goal: Find original_message_id
                    # If we don't have an original_id by now, we can't do anything
                    if not original_id:
                        logger.warning(f"Entry {entry} has no original_message_id and no starboard message to recover from.")
                        failed_count += 1
                        progress['done'] += 1
                        continue

                    found_msg = None
                    found_channel = None

                    # Step 1: Targeted Channel Lookup
                    if entry_channel_id:
                        ch = self.bot.get_channel(entry_channel_id)
                        if isinstance(ch, discord.abc.Messageable):
                            try:
                                found_msg = await self._run_rate_limited(ch.fetch_message, original_id)
                                found_channel = ch
                            except discord.NotFound:
                                pass
                            except Exception as e:
                                logger.warning(f"Error fetching from original channel {entry_channel_id}: {e}")

                    # Step 2: Guild-Wide Scan (Fallback) - NO ctx.guild fallback
                    if not found_msg and entry_guild_id:
                        search_guild = self.bot.get_guild(entry_guild_id)
                        if search_guild:
                            for ch in search_guild.channels:
                                if not isinstance(ch, discord.abc.Messageable):
                                    continue
                                try:
                                    found_msg = await self._run_rate_limited(ch.fetch_message, original_id)
                                    found_channel = ch
                                    break  # Found it
                                except discord.NotFound:
                                    continue
                                except Exception:
                                    continue

                    # Step 3: Fix via Repost
                    if found_msg:
                        # We found it!
                        # Update DB with correct location if changed
                        if found_channel and found_channel.id != entry_channel_id:
                            entry['original_channel_id'] = found_channel.id
                            if found_msg.guild:
                                entry['guild_id'] = found_msg.guild.id
                            await self.db_manager.update_starboard_entry(entry)

                        # Post to starboard
                        star_reaction = discord.utils.get(found_msg.reactions, emoji=starboard_emoji)
                        star_count = star_reaction.count if star_reaction else 0

                        # We need to ensure post_to_starboard handles the missing starboard_message_id correctly
                        # It will see the entry, see missing ID (if we fix it), remove entry, and create new.
                        # Or we can manually remove entry here to force creation.
                        # To be safe, let's remove the broken entry so post_to_starboard creates a fresh one.
                        await self.db_manager.remove_starboard_entry(original_id)

                        await self.post_to_starboard(found_msg, starboard_channel_id, starboard_emoji, star_count)
                        fixed_count += 1

                    # Step 4: Tombstone Creation
                    else:
                        logger.info(f"Original message {original_id} lost. Creating tombstone.")
                        tomb = await self._create_tombstone(starboard_channel, original_id)
                        if tomb:
                            entry['starboard_message_id'] = tomb.id
                            await self.db_manager.update_starboard_entry(entry)
                            tombstone_count += 1
                            fixed_count += 1

            except Exception as e:
                logger.error(f"Failed to fix entry {entry}: {e}")
                failed_count += 1

            progress['done'] += 1

        # Stop the periodic status task and await it to finish
        stop_event.set()
        try:
            await status_task
        except Exception:
            # If the status task was cancelled or errored, ignore
            pass

        await ctx.send(f"Starboard fix complete. Fixed: {fixed_count}, Verified: {verified_count}, Tombstones: {tombstone_count}, Failed: {failed_count}.")
        # Reset fast mode to avoid affecting future operations
        self._fast_mode = False

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Handles raw reaction add events to check for starboard triggers.

        Args:
            payload (discord.RawReactionActionEvent): The reaction event payload.
        """
        if not payload.guild_id or not self.bot.user or payload.user_id == self.bot.user.id:
            return

        starboard_channel_id, starboard_emoji, starboard_threshold = await self.get_starboard_config(payload.guild_id)

        if not starboard_channel_id or str(payload.emoji) != starboard_emoji:
            return

        # Use a lock to prevent race conditions from multiple simultaneous reactions.
        # This ensures we don't post the same message multiple times or desync the count.
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

    async def post_to_starboard(self, message: discord.Message, starboard_channel_id: int, starboard_emoji: str, star_count: int) -> None:
        """Posts or updates a message on the starboard.

        Args:
            message (discord.Message): The original message.
            starboard_channel_id (int): The ID of the starboard channel.
            starboard_emoji (str): The emoji used for the starboard.
            star_count (int): The current number of reactions.
        """
        starboard_channel = self.bot.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            logger.error(f"Starboard channel with ID {starboard_channel_id} not found or is not a text channel.")
            return

        existing_entry = await self.db_manager.get_starboard_entry(message.id)
        content = f"{starboard_emoji} **{star_count}** in <#{message.channel.id}>"
        logger.info(f"Starboard post content: {content}")

        if existing_entry:
            # Safety check: if starboard_message_id is missing, we can't fetch it.
            # Treat it as missing and recreate.
            if not existing_entry.get('starboard_message_id'):
                logger.warning(f"Starboard entry for {message.id} exists but has no starboard_message_id. Recreating.")
                await self.db_manager.remove_starboard_entry(message.id)
                await self.create_new_starboard_post(message, starboard_channel, content)
                return

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

    async def create_new_starboard_post(self, message: discord.Message, starboard_channel: discord.TextChannel, content: str) -> None:
        """Creates a new starboard post.

        If the message is a reply, it posts the replied-to message first,
        then replies to that with the starred message.

        Args:
            message (discord.Message): The original message.
            starboard_channel (discord.TextChannel): The starboard channel.
            content (str): The content string (e.g., "⭐ 5 in #general").
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

    async def create_single_starboard_post(self, message: discord.Message, starboard_channel: discord.TextChannel, content: str) -> None:
        """Creates a single starboard post, used for non-reply messages or as a fallback.

        Args:
            message (discord.Message): The original message.
            starboard_channel (discord.TextChannel): The starboard channel.
            content (str): The content string.
        """
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

    async def create_starboard_embed_and_files(self, message: discord.Message) -> Tuple[discord.Embed, List[discord.File]]:
        """Creates an embed and a list of discord.File objects for a starboard message.

        Handles regular content, attachments, and embeds.

        Args:
            message (discord.Message): The message to convert.

        Returns:
            Tuple[discord.Embed, List[discord.File]]: The embed and list of files.
        """

        description_parts = []
        files = []

        # Add message content.
        if message.content:
            description_parts.append(message.content)

        # Process attachments.
        for attachment in message.attachments:
            try:
                async with self.http_session.get(attachment.url) as resp:
                    if resp.status == 200:
                        data = io.BytesIO(await resp.read())
                        files.append(discord.File(data, filename=attachment.filename, spoiler=attachment.is_spoiler()))
            except Exception as e:
                logger.error(f"Failed to download direct attachment for starboard: {e}")

        # Process forwarded snapshots.
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

        # Handle embeds.
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

    async def _run_rate_limited(self, coro_func: Any, *args: Any, delay: Optional[float] = None, retries: Optional[int] = None) -> Any:
        """Run the provided coroutine-callable under the fix semaphore with simple backoff.

        Args:
            coro_func (Any): A callable that returns an awaitable when called with *args.
            *args (Any): Arguments to pass to coro_func.
            delay (Optional[float]): Delay in seconds after success. Defaults to self._fix_delay.
            retries (Optional[int]): Number of retries. Defaults to self._fix_retries.

        Returns:
            Any: The result of the coroutine.
        """
        if delay is None:
            delay = self._fix_delay
        if retries is None:
            retries = self._fix_retries

        async with self._fix_semaphore:
            backoff = 1.0
            last_exc = None
            for attempt in range(retries):
                try:
                    logger.debug(f"_run_rate_limited attempt {attempt+1}/{retries} for {getattr(coro_func, '__name__', repr(coro_func))} args={args}")
                    result = await coro_func(*args)
                    # gentle delay after a successful call
                    try:
                        await asyncio.sleep(delay)
                    except Exception:
                        pass
                    return result
                except discord.NotFound:
                    # Do not retry on 404 Not Found
                    raise
                except (discord.HTTPException, aiohttp.ClientError) as e:
                    last_exc = e
                    # exponential backoff
                    wait = backoff
                    logger.debug(f"_run_rate_limited HTTP error on attempt {attempt+1}: {e}; backing off {wait}s")
                    backoff = min(backoff * 2, 30)
                    await asyncio.sleep(wait)
                    continue
                except Exception:
                    # Non-http error — re-raise
                    raise
            # If we exhausted retries, raise the last HTTP-related exception
            if last_exc:
                raise last_exc
            return None

    async def _status_editor(self, status_message: discord.Message, progress: Dict[str, Any], stop_event: asyncio.Event, interval: float = 30.0) -> None:
        """Edit a single status message every `interval` seconds until `stop_event` is set.

        Args:
            status_message (discord.Message): The message to edit.
            progress (Dict[str, Any]): A mutable dict with keys 'done', 'total', and 'elapsed'.
            stop_event (asyncio.Event): Event to signal stopping.
            interval (float): Update interval in seconds.
        """
        try:
            while not stop_event.is_set():
                await asyncio.sleep(interval)
                progress['elapsed'] = progress.get('elapsed', 0) + int(interval)
                done = progress.get('done', 0)
                total = progress.get('total', '?')
                elapsed = progress.get('elapsed', 0)
                try:
                    logger.debug(f"Editing status message: processed {done}/{total}, elapsed {elapsed}s")
                    await status_message.edit(content=f"Starboard fix running... processed {done}/{total}. Elapsed: {elapsed}s. Please wait.")
                except Exception:
                    # Ignore edit/send errors; keep looping until stop_event is set
                    pass
        except asyncio.CancelledError:
            return

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Handles raw reaction remove events to check for starboard deletions.

        Args:
            payload (discord.RawReactionActionEvent): The reaction event payload.
        """
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


async def setup(bot: SanchoBot) -> None:
    """Standard setup function to add the cog to the bot.

    Args:
        bot (SanchoBot): The bot instance.
    """
    await bot.add_cog(Starboard(bot))

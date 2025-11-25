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
        # Only remake entries that have complete stored information
        # (original_message_id, starboard_message_id, guild_id, original_channel_id)
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

        for tgt in recreation_targets:
            logger.debug(f"Recreation target: {tgt}")
            original_channel = self.bot.get_channel(tgt['original_channel_id'])
            if not isinstance(original_channel, discord.TextChannel):
                logger.warning(f"Could not find original channel {tgt['original_channel_id']}. Skipping message {tgt['original_message_id']}.")
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
                    tomb = await starboard_channel.send("🪦")
                    await self.db_manager.add_starboard_entry(tgt['original_message_id'], tomb.id, tgt.get('guild_id'), None)
                    logger.debug(f"Tombstone created with id {tomb.id} for original {tgt['original_message_id']}")
                    recreated_count += 1
                except Exception as e:
                    logger.error(f"Failed to create tombstone for missing original {tgt['original_message_id']}: {e}")
                    failed_count += 1
            except Exception as e:
                logger.error(f"Failed to recreate starboard post for message {tgt['original_message_id']}: {e}")
                failed_count += 1

        await ctx.send(f"Starboard remake complete. Recreated {recreated_count} posts. Failed to recreate {failed_count} posts.")
        # Reset fast mode to avoid affecting future operations
        self._fast_mode = False

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
        missing_reports = []

        # Start a periodic status notifier so the caller sees progress for long runs
        stop_event = asyncio.Event()
        progress = {'done': 0, 'total': len(all_entries), 'elapsed': 0}
        status_msg = await ctx.send(f"Starboard fix started. Processed 0/{len(all_entries)}. Elapsed: 0s. Please wait.")
        status_task = asyncio.create_task(self._status_editor(status_msg, progress, stop_event, interval=30.0))
        logger.debug("Status editor task started for fix operation")

        for entry in all_entries:
            try:
                logger.debug(f"Fix processing DB entry: {entry}")
                # 1) If we have a starboard_message_id, use that message to recover original metadata
                missing_sb = False
                if entry.get('starboard_message_id'):
                    try:
                        logger.debug(f"Attempting to fetch starboard message id {entry['starboard_message_id']} via rate-limited runner")
                        sb_msg = await self._run_rate_limited(starboard_channel.fetch_message, entry['starboard_message_id'])
                        logger.debug(f"Fetched starboard message id {entry['starboard_message_id']} successfully")
                    except discord.NotFound:
                        # Treat the entry as if it has no starboard message when it cannot be found
                        sb_msg = None
                        missing_sb = True
                        logger.info(f"Starboard message {entry.get('starboard_message_id')} not found; will attempt recovery")
                    except Exception as e:
                        # For other errors, log and mark missing so we can try recovering from original
                        logger.warning(f"Error fetching starboard message {entry.get('starboard_message_id')}: {e}")
                        sb_msg = None
                        missing_sb = True

                    # If the starboard message was missing, build a report of other missing fields
                    if missing_sb:
                        missing_fields = []
                        # guild_id
                        if not entry.get('guild_id'):
                            missing_fields.append('guild_id')
                        # original_channel_id
                        if not entry.get('original_channel_id'):
                            missing_fields.append('original_channel_id')

                        # Check whether the original message can be found
                        orig_id = entry.get('original_message_id')
                        original_found = False
                        if orig_id:
                            # Try stored channel first
                            if entry.get('original_channel_id'):
                                ch = self.bot.get_channel(entry['original_channel_id'])
                                logger.debug(f"Trying stored original_channel {entry['original_channel_id']} to find original message {orig_id}")

                                if isinstance(ch, discord.abc.Messageable):
                                    try:
                                        # Bypass rate-limiting in fast mode
                                        if self._fast_mode:
                                            logger.debug(f"Fast mode: fetching original {orig_id} directly from channel {ch.id}")
                                            await ch.fetch_message(orig_id)
                                        else:
                                            logger.debug(f"Rate-limited fetch of original {orig_id} from channel {ch.id}")
                                            await self._run_rate_limited(ch.fetch_message, orig_id)
                                        original_found = True
                                        logger.debug(f"Found original {orig_id} in stored channel {entry['original_channel_id']}")
                                    except Exception as e:
                                        original_found = False
                                        logger.debug(f"Failed to fetch original {orig_id} from stored channel {entry.get('original_channel_id')}: {e}")

                            # If not found yet, try scanning the stored guild's channels (admin operation - acceptable)
                            if not original_found:
                                target_guild_for_lookup = None
                                if entry.get('guild_id'):
                                    target_guild_for_lookup = self.bot.get_guild(entry['guild_id'])
                                if not target_guild_for_lookup:
                                    target_guild_for_lookup = ctx.guild

                                if target_guild_for_lookup:
                                    logger.debug(f"Scanning guild {getattr(target_guild_for_lookup, 'id', None)} channels to find original {orig_id}")
                                    for ch in target_guild_for_lookup.channels:
                                        if not isinstance(ch, discord.abc.Messageable):
                                            continue
                                        try:
                                            if self._fast_mode:
                                                logger.debug(f"Fast mode: attempting fetch in channel {ch.id} for message {orig_id}")
                                                await ch.fetch_message(orig_id)
                                            else:
                                                logger.debug(f"Rate-limited attempt to fetch message {orig_id} in channel {ch.id}")
                                                await self._run_rate_limited(ch.fetch_message, orig_id)
                                            original_found = True
                                            logger.debug(f"Found original {orig_id} in channel {ch.id}")
                                            break
                                        except Exception as e:
                                            logger.debug(f"Channel {ch.id} did not contain message {orig_id}: {e}")
                                            continue

                        if not original_found:
                            missing_fields.append('original_message')

                        missing_reports.append({'original_message_id': orig_id, 'missing': missing_fields})

                    if sb_msg and sb_msg.embeds:
                        # Parse jump URL from the embed's 'Original Message' field
                        embed = sb_msg.embeds[0]
                        original_id = None
                        original_channel_id = None
                        guild_id = None
                        for field in embed.fields:
                            if field.name == 'Original Message' and field.value:
                                m = re.search(r"/channels/(\d+)/(\d+)/(\d+)", field.value)
                                if m:
                                    guild_id = int(m.group(1))
                                    original_channel_id = int(m.group(2))
                                    original_id = int(m.group(3))
                                break

                        updated = False
                        if original_id and entry.get('original_message_id') != original_id:
                            entry['original_message_id'] = original_id
                            updated = True
                        if guild_id and entry.get('guild_id') != guild_id:
                            entry['guild_id'] = guild_id
                            updated = True
                        if original_channel_id and entry.get('original_channel_id') != original_channel_id:
                            entry['original_channel_id'] = original_channel_id
                            updated = True

                        # starboard_reply_id: may be present as a message reference on the starboard post
                        found_reply_id = sb_msg.reference.message_id if sb_msg.reference else None
                        if entry.get('starboard_reply_id') != found_reply_id:
                            entry['starboard_reply_id'] = found_reply_id
                            updated = True

                        if updated:
                            await self.db_manager.update_starboard_entry(entry)
                            fixed_count += 1
                        else:
                            verified_count += 1
                        progress['done'] += 1
                        continue

                # 2) If we only have an original_message_id (no starboard_message_id), try to locate it and repost
                # If the DB lacks a starboard_message_id OR the stored starboard message was not found,
                # attempt to locate the original message and recreate the starboard post.
                if entry.get('original_message_id') and (not entry.get('starboard_message_id') or missing_sb):
                    original_id = entry['original_message_id']
                    original_channel = None
                    original_message = None

                    # Try stored channel first
                    if entry.get('original_channel_id'):
                        ch = self.bot.get_channel(entry['original_channel_id'])
                        if isinstance(ch, discord.abc.Messageable):
                            try:
                                if self._fast_mode:
                                    original_message = await ch.fetch_message(original_id)
                                else:
                                    original_message = await self._run_rate_limited(ch.fetch_message, original_id)
                                original_channel = ch
                            except discord.NotFound:
                                original_message = None
                            except discord.Forbidden:
                                original_message = None
                            except Exception:
                                original_message = None

                    # Fallback: scan the stored guild's channels (prefer entry.guild_id) to find the message
                    if not original_message:
                        target_guild_for_lookup = None
                        if entry.get('guild_id'):
                            target_guild_for_lookup = self.bot.get_guild(entry['guild_id'])
                        if not target_guild_for_lookup:
                            target_guild_for_lookup = ctx.guild

                        if target_guild_for_lookup:
                            for ch in target_guild_for_lookup.channels:
                                if not isinstance(ch, discord.abc.Messageable):
                                    continue
                                try:
                                    if self._fast_mode:
                                        original_message = await ch.fetch_message(original_id)
                                    else:
                                        original_message = await self._run_rate_limited(ch.fetch_message, original_id)
                                    original_channel = ch
                                    break
                                except discord.NotFound:
                                    continue
                                except discord.Forbidden:
                                    continue
                                except Exception:
                                    continue

                    if original_message:
                        star_reaction = discord.utils.get(original_message.reactions, emoji=starboard_emoji)
                        star_count = star_reaction.count if star_reaction else 0
                        if self._fast_mode:
                            await self.post_to_starboard(original_message, starboard_channel_id, starboard_emoji, star_count)
                        else:
                            await self._run_rate_limited(self.post_to_starboard, original_message, starboard_channel_id, starboard_emoji, star_count)
                        fixed_count += 1
                    else:
                        failed_count += 1

                    progress['done'] += 1

            except Exception as e:
                logger.error(f"Failed to fix/verify starboard entry for row {entry}: {e}")
                failed_count += 1
                progress['done'] += 1

        # Stop the periodic status task and await it to finish
        stop_event.set()
        try:
            await status_task
        except Exception:
            # If the status task was cancelled or errored, ignore
            pass

        await ctx.send(f"Starboard fix complete. Fixed {fixed_count} entries, verified {verified_count} entries, failed {failed_count} entries.")
        # Reset fast mode to avoid affecting future operations
        self._fast_mode = False

        # If there were missing starboard messages, send a helpful summary of what other data is missing
        if missing_reports:
            lines = ["Missing starboard messages detected. Summary per original message ID:"]
            for rep in missing_reports:
                orig = rep.get('original_message_id')
                missing = rep.get('missing') or []
                if not missing:
                    lines.append(f"- {orig}: only starboard message missing")
                else:
                    lines.append(f"- {orig}: missing {', '.join(missing)}")

            # Send as one message (may be long); trim if necessary
            report_text = "\n".join(lines)
            if len(report_text) > 1900:
                # If too long, send in chunks
                for i in range(0, len(report_text), 1900):
                    await ctx.send(report_text[i:i+1900])
            else:
                await ctx.send(report_text)

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

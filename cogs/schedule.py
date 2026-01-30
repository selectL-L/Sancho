"""Cog for weekly availability scheduling with web UI.

This cog provides:
- A FastAPI web server (started in cog_ready) for users to manage availability
- Discord OAuth2 authentication for the web UI
- NLP handlers for querying availability via Discord chat

The web server runs in-process, sharing the event loop with the Discord bot.
It's started via Uvicorn when the cog is ready and stopped on cog unload.

Web UI Features:
- View your weekly availability in a heatmap grid
- Set availability by clicking/dragging time slots
- Control which guilds can see your availability
- Block specific users from viewing your availability

NLP Query Features:
- "when is @user free" / "is @user free at 3pm Saturday" - Check availability
- "who's free" / "who's free Saturday afternoon" - Find available people
- "schedule link" / "edit my availability" - Get web UI link
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

import dateparser
import discord
import pytz
from discord.ext import commands

import config
from utils.base_cog import BaseCog

if TYPE_CHECKING:
    from utils.bot_class import CoreBot

logger = logging.getLogger(__name__)

# Day name mappings
DAY_NAMES = {
    0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"
}
DAY_NAMES_FULL = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"
}


class Schedule(BaseCog):
    """Availability scheduling with web-based management.

    This cog owns the schedule web server lifecycle:
    - Server starts in cog_ready()
    - Server stops in cog_unload()

    Attributes:
        _server_task: The asyncio task running Uvicorn.
        _server_started: Event signaling server startup complete.
    """

    def __init__(self, bot: "CoreBot") -> None:
        """Initialize the Schedule cog.

        Args:
            bot: The Discord bot instance.
        """
        super().__init__(bot)
        self._server_task: Optional[asyncio.Task[None]] = None
        self._server_started = asyncio.Event()
        self._uvicorn_server: Any = None  # uvicorn.Server, typed as Any for lazy import

    async def cog_ready(self) -> None:
        """Called when bot is ready. Start the web server if enabled."""
        # Idempotency guard: Don't start another server if one is already running
        if self._server_task is not None and not self._server_task.done():
            self.logger.info("Web server already running, skipping start")
            return

        if not config.WEB_ENABLED:
            self.logger.info("Web server disabled (WEB_ENABLED=False)")
            return

        # Check for required OAuth config
        if not config.OAUTH_CLIENT_ID or not config.OAUTH_CLIENT_SECRET:
            self.logger.warning(
                "Web server enabled but OAuth not configured. "
                "Set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET in info.env"
            )
            return

        if not config.WEB_SESSION_SECRET:
            self.logger.warning(
                "Web server enabled but WEB_SESSION_SECRET not set. "
                "Generate a random secret for session signing."
            )
            return

        # Start web server
        self._server_task = asyncio.create_task(
            self._run_web_server(),
            name="schedule-web-server"
        )

    async def cog_unload(self) -> None:
        """Called when cog is unloaded. Stop the web server."""
        await self._stop_web_server()

    async def _run_web_server(self) -> None:
        """Run the Uvicorn web server.

        This runs in-process, sharing the event loop with the Discord bot.
        The server serves static files and API endpoints for the schedule UI.
        """
        try:
            import uvicorn
            from utils.web import create_app

            app = create_app(self.bot)

            # Custom server class to allow graceful shutdown
            uvicorn_config = uvicorn.Config(
                app=app,
                host=config.WEB_HOST,
                port=config.WEB_PORT,
                log_level="warning" if not config.DEV_MODE else "info",
                # Don't reload in production
                reload=False,
                # Access log only in dev
                access_log=config.DEV_MODE,
            )

            self._uvicorn_server = uvicorn.Server(uvicorn_config)

            self.logger.info(
                f"Starting web server on http://{config.WEB_HOST}:{config.WEB_PORT}"
            )
            self._server_started.set()

            try:
                await self._uvicorn_server.serve()
            except SystemExit as e:
                # Uvicorn calls sys.exit(1) on port binding failure - don't let it crash the bot
                if e.code == 1:
                    self.logger.error(
                        f"Web server failed to start (port {config.WEB_PORT} likely in use). "
                        "The bot will continue without the web interface."
                    )
                else:
                    raise

        except ImportError as e:
            self.logger.error(
                f"Failed to import web server dependencies: {e}. "
                "Install with: pip install fastapi uvicorn aiohttp"
            )
        except Exception as e:
            self.logger.error(f"Web server error: {e}", exc_info=True)
        finally:
            self._server_started.clear()

    async def _stop_web_server(self) -> None:
        """Stop the Uvicorn web server gracefully."""
        if self._uvicorn_server is not None:
            self.logger.info("Stopping web server...")
            self._uvicorn_server.should_exit = True

        if self._server_task is not None:
            try:
                # Wait for server to finish with timeout
                await asyncio.wait_for(self._server_task, timeout=5.0)
                self.logger.info("Web server stopped gracefully")
            except asyncio.TimeoutError:
                self.logger.warning("Web server stop timed out, cancelling task")
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                self.logger.error(f"Error stopping web server: {e}")
            finally:
                self._server_task = None
                self._uvicorn_server = None

    # ========== NLP HANDLERS ==========
    # These methods are called by the NLP dispatcher in bot_class.py
    # Patterns are defined in config.NLP_COMMANDS

    async def _get_user_timezone(self, user_id: int) -> str:
        """Fetches a user's timezone string.

        Args:
            user_id: The user's Discord ID.

        Returns:
            The pytz-compatible timezone string, defaulting to 'UTC'.
        """
        return await self.bot.db_manager.get_user_timezone(user_id) or "UTC"  # type: ignore[union-attr]

    async def _parse_time_from_query(self, query: str, user_id: int) -> Optional[datetime]:
        """Parse a time expression from the query string.

        Uses dateparser with PREFER_DATES_FROM: future to get the next
        occurrence of relative dates like "Saturday" or "tomorrow".

        Args:
            query: The query string potentially containing a time expression.
            user_id: The user's ID for timezone lookup.

        Returns:
            A timezone-aware datetime if parsing succeeded, None otherwise.
        """
        # Strip mentions and common prefixes
        clean = re.sub(r'<@!?\d+>', '', query)
        clean = re.sub(r'\b(when|is|are|free|available|at|on)\b', ' ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\s+', ' ', clean).strip()

        if not clean or len(clean) < 2:
            return None

        tz_str = await self._get_user_timezone(user_id)
        settings: Dict[str, Any] = {
            'PREFER_DATES_FROM': 'future',
            'TIMEZONE': tz_str,
            'RETURN_AS_TIMEZONE_AWARE': True,
        }

        result = await asyncio.to_thread(
            dateparser.parse, clean, languages=['en'], settings=cast(Any, settings)
        )

        if result:
            self.logger.debug(f"Parsed time '{clean}' -> {result}")

        return result

    def _datetime_to_slot(self, dt: datetime) -> str:
        """Convert a datetime to a slot string.

        Args:
            dt: The datetime to convert.

        Returns:
            Slot string in format "day-HHMM" (e.g., "sat-1500").
        """
        day = DAY_NAMES[dt.weekday()]
        return f"{day}-{dt.hour:02d}{dt.minute:02d}"

    def _datetime_to_slot_range(self, dt: datetime, window_minutes: int = 60) -> List[str]:
        """Convert a datetime to a range of slots covering a time window.

        Args:
            dt: The center datetime.
            window_minutes: Total window size in minutes (default 60).

        Returns:
            List of slot strings covering the window.
        """
        slots = []
        day = DAY_NAMES[dt.weekday()]
        half_window = window_minutes // 2

        # Generate slots at 15-minute intervals
        for offset in range(-half_window, half_window + 1, 15):
            total_minutes = dt.hour * 60 + dt.minute + offset
            if 0 <= total_minutes < 24 * 60:
                h = total_minutes // 60
                m = (total_minutes // 15) * 15 % 60  # Round to 15-min boundary
                slots.append(f"{day}-{h:02d}{m:02d}")

        return list(dict.fromkeys(slots))  # Remove duplicates, preserve order

    def _times_to_ranges(self, times: List[str]) -> List[str]:
        """Convert a list of HHMM times to human-readable ranges.

        Args:
            times: List of time strings in HHMM format.

        Returns:
            List of formatted time ranges like "9AM-12PM".
        """
        if not times:
            return []

        times = sorted(times)
        ranges = []
        start = times[0]
        prev = times[0]

        for time in times[1:]:
            prev_minutes = int(prev[:2]) * 60 + int(prev[2:])
            curr_minutes = int(time[:2]) * 60 + int(time[2:])

            if curr_minutes - prev_minutes == 15:
                prev = time
            else:
                ranges.append(self._format_range(start, prev))
                start = time
                prev = time

        ranges.append(self._format_range(start, prev))
        return ranges

    def _format_range(self, start: str, end: str) -> str:
        """Format a time range for display.

        Args:
            start: Start time in HHMM format.
            end: End time in HHMM format.

        Returns:
            Formatted string like "9AM-12PM" or "9AM" if single slot.
        """
        def format_time(t: str) -> str:
            hour = int(t[:2])
            minute = int(t[2:])

            if hour == 0:
                h, ampm = 12, "AM"
            elif hour < 12:
                h, ampm = hour, "AM"
            elif hour == 12:
                h, ampm = 12, "PM"
            else:
                h, ampm = hour - 12, "PM"

            return f"{h}:{minute:02d}{ampm}" if minute else f"{h}{ampm}"

        start_fmt = format_time(start)

        # Add 15 min to end to show exclusive end time
        end_minutes = int(end[:2]) * 60 + int(end[2:]) + 15
        end_hour = end_minutes // 60
        end_min = end_minutes % 60

        if end_hour >= 24:
            end_hour = 0

        end_fmt = format_time(f"{end_hour:02d}{end_min:02d}")

        return start_fmt if start_fmt == end_fmt else f"{start_fmt}-{end_fmt}"

    def _format_user_availability(self, slots: List[str], user_name: str) -> str:
        """Format a user's availability slots for display.

        Args:
            slots: List of slot strings.
            user_name: Display name of the user.

        Returns:
            Formatted message string.
        """
        if not slots:
            return f"{user_name} hasn't set any availability yet."

        by_day: Dict[str, List[str]] = {d: [] for d in DAY_NAMES.values()}

        for slot in slots:
            day, time = slot.split("-")
            if day in by_day:
                by_day[day].append(time)

        lines = [f"📅 **{user_name}'s Availability**\n"]

        for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            times = by_day[day]
            if times:
                times.sort()
                ranges = self._times_to_ranges(times)
                lines.append(f"**{DAY_NAMES_FULL[day]}:** {', '.join(ranges)}")

        return "\n".join(lines) if len(lines) > 1 else f"{user_name} hasn't set any availability yet."

    async def check_availability_nlp(self, ctx: commands.Context, query: str) -> None:
        """Check availability for mentioned users.

        Handles two modes:
        1. Open-ended: "when is @user free" → shows full availability
        2. Specific time: "is @user free Saturday 3pm" → checks that slot

        Multiple mentions are supported for finding overlapping availability.

        Args:
            ctx: Command context from Discord.
            query: The full query string from the user.
        """
        if ctx.guild is None:
            await ctx.send("This command only works in servers.")
            return

        # Get mentioned users
        mentions = ctx.message.mentions
        if not mentions:
            await ctx.send("Please mention one or more users to check their availability.")
            return

        requester_id = ctx.author.id
        guild_id = ctx.guild.id

        # Try to parse a specific time from the query
        parsed_time = await self._parse_time_from_query(query, requester_id)

        # Gather availability for all mentioned users
        user_slots: Dict[int, Tuple[discord.User | discord.Member, List[str]]] = {}
        blocked_users: List[str] = []

        for user in mentions:
            can_view = await self.bot.db_manager.schedule_can_view(  # type: ignore[union-attr]
                requester_id=requester_id,
                target_id=user.id,
                guild_id=guild_id
            )

            if not can_view:
                blocked_users.append(user.display_name)
                continue

            slots = await self.bot.db_manager.schedule_get_availability(user.id)  # type: ignore[union-attr]
            user_slots[user.id] = (user, slots)

        # Handle blocked users
        if blocked_users and not user_slots:
            names = ", ".join(blocked_users)
            await ctx.send(
                f"{names} {'have' if len(blocked_users) > 1 else 'has'}n't shared "
                "availability in this server, or blocked you from viewing it."
            )
            return

        # Single user, no specific time → show full availability
        if len(user_slots) == 1 and parsed_time is None:
            user, slots = next(iter(user_slots.values()))
            message = self._format_user_availability(slots, user.display_name)
            if blocked_users:
                message += f"\n\n⚠️ Couldn't view: {', '.join(blocked_users)}"
            await ctx.send(message)
            return

        # Single user, specific time → check that slot
        if len(user_slots) == 1 and parsed_time is not None:
            user, slots = next(iter(user_slots.values()))

            # Convert query time to user's timezone for comparison
            if parsed_time.tzinfo is None:
                query_utc = parsed_time.replace(tzinfo=pytz.UTC)
            else:
                query_utc = parsed_time.astimezone(pytz.UTC)

            user_tz_str = await self._get_user_timezone(user.id)
            try:
                user_tz = pytz.timezone(user_tz_str)
            except pytz.UnknownTimeZoneError:
                user_tz = pytz.UTC

            query_in_user_tz = query_utc.astimezone(user_tz)
            target_slots = self._datetime_to_slot_range(query_in_user_tz)

            matching = [s for s in slots if s in target_slots]

            if matching:
                time_str = parsed_time.strftime("%A at %H:%M")
                await ctx.send(f"✅ **{user.display_name}** is available around {time_str}!")
            else:
                time_str = parsed_time.strftime("%A at %H:%M")
                await ctx.send(f"❌ **{user.display_name}** is not available around {time_str}.")
            return

        # Multiple users → find overlap
        if parsed_time is not None:
            # Convert query time to UTC first
            if parsed_time.tzinfo is None:
                query_utc = parsed_time.replace(tzinfo=pytz.UTC)
            else:
                query_utc = parsed_time.astimezone(pytz.UTC)

            # Check specific time for all users (in their respective timezones)
            available_users: List[str] = []
            unavailable_users: List[str] = []

            for user, slots in user_slots.values():
                user_tz_str = await self._get_user_timezone(user.id)
                try:
                    user_tz = pytz.timezone(user_tz_str)
                except pytz.UnknownTimeZoneError:
                    user_tz = pytz.UTC

                query_in_user_tz = query_utc.astimezone(user_tz)
                target_slots = self._datetime_to_slot_range(query_in_user_tz)

                if any(s in target_slots for s in slots):
                    available_users.append(user.display_name)
                else:
                    unavailable_users.append(user.display_name)

            time_str = parsed_time.strftime("%A at %H:%M")
            lines = [f"📅 **Availability for {time_str}**\n"]

            if available_users:
                lines.append(f"✅ Available: {', '.join(available_users)}")
            if unavailable_users:
                lines.append(f"❌ Unavailable: {', '.join(unavailable_users)}")
            if blocked_users:
                lines.append(f"⚠️ Couldn't view: {', '.join(blocked_users)}")

            await ctx.send("\n".join(lines))
        else:
            # Find overlapping slots across all users
            all_slot_sets = [set(slots) for _, slots in user_slots.values()]
            if all_slot_sets:
                overlap = all_slot_sets[0].intersection(*all_slot_sets[1:])
            else:
                overlap = set()

            names = ", ".join(u.display_name for u, _ in user_slots.values())

            if not overlap:
                await ctx.send(f"😕 No overlapping availability found for {names}.")
                return

            # Group overlap by day
            by_day: Dict[str, List[str]] = {d: [] for d in DAY_NAMES.values()}
            for slot in overlap:
                day, time = slot.split("-")
                if day in by_day:
                    by_day[day].append(time)

            lines = [f"📅 **Overlapping Availability** for {names}\n"]

            for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
                times = by_day[day]
                if times:
                    times.sort()
                    ranges = self._times_to_ranges(times)
                    lines.append(f"**{DAY_NAMES_FULL[day]}:** {', '.join(ranges)}")

            if blocked_users:
                lines.append(f"\n⚠️ Couldn't view: {', '.join(blocked_users)}")

            await ctx.send("\n".join(lines))

    async def who_available_nlp(self, ctx: commands.Context, query: str) -> None:
        """Find who is available, optionally at a specific time.

        Examples:
        - "who's free" → people available right now
        - "who's free Saturday afternoon" → people available then

        Args:
            ctx: Command context from Discord.
            query: The full query string from the user.
        """
        if ctx.guild is None:
            await ctx.send("This command only works in servers.")
            return

        requester_id = ctx.author.id
        guild_id = ctx.guild.id

        # Try to parse a specific time, default to now
        parsed_time = await self._parse_time_from_query(query, requester_id)
        if parsed_time is None:
            # Use current time in user's timezone
            tz_str = await self._get_user_timezone(requester_id)
            settings: Dict[str, Any] = {
                'TIMEZONE': tz_str,
                'RETURN_AS_TIMEZONE_AWARE': True,
            }
            parsed_time = await asyncio.to_thread(
                dateparser.parse, "now", languages=['en'], settings=cast(Any, settings)
            )

        if parsed_time is None:
            await ctx.send("Couldn't determine the time to check. Please try again.")
            return

        # Get all visible availability in guild
        all_availability = await self.bot.db_manager.schedule_get_guild_availability(  # type: ignore[union-attr]
            guild_id=guild_id,
            requester_id=requester_id
        )

        if not all_availability:
            await ctx.send(
                "No one in this server has shared their availability yet.\n"
                "Use the schedule link command to get started!"
            )
            return

        # Check who's available at the target time
        # Convert query time to UTC first for consistent comparison
        if parsed_time.tzinfo is None:
            # If somehow not tz-aware, assume UTC
            query_utc = parsed_time.replace(tzinfo=pytz.UTC)
        else:
            query_utc = parsed_time.astimezone(pytz.UTC)

        available_users: List[str] = []

        for user_id, slots in all_availability.items():
            # Get this user's timezone and convert query time to their local time
            user_tz_str = await self._get_user_timezone(user_id)
            try:
                user_tz = pytz.timezone(user_tz_str)
            except pytz.UnknownTimeZoneError:
                user_tz = pytz.UTC

            # Convert query time to this user's timezone
            query_in_user_tz = query_utc.astimezone(user_tz)

            # Generate slots based on the time in the user's timezone
            target_slots = self._datetime_to_slot_range(query_in_user_tz, window_minutes=30)

            if any(s in target_slots for s in slots):
                member = ctx.guild.get_member(user_id)
                if member:
                    available_users.append(member.display_name)

        time_str = parsed_time.strftime("%A at %H:%M")

        if available_users:
            await ctx.send(
                f"📅 **Available {time_str}:**\n"
                f"{', '.join(available_users)}"
            )
        else:
            await ctx.send(f"😕 No one is available around {time_str}.")

    async def schedule_link_nlp(self, ctx: commands.Context, query: str) -> None:
        """Send the link to the schedule web UI.

        Args:
            ctx: Command context from Discord.
            query: The full query string (unused).
        """
        if not config.WEB_ENABLED:
            await ctx.send("The schedule web interface is not currently enabled.")
            return

        base_url = config.OAUTH_REDIRECT_URI.rsplit("/callback", 1)[0]
        guild_param = f"?guild={ctx.guild.id}" if ctx.guild else ""

        await ctx.send(
            f"📅 **Schedule Manager**\n"
            f"Set your availability: {base_url}/settings.html\n"
            f"View others' schedules: {base_url}/{guild_param}"
        )


async def setup(bot: "CoreBot") -> None:
    """Load the Schedule cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(Schedule(bot))

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

NLP Query Features (patterns to be added in config.py):
- "when is @user free" / "is @user available"
- "show my schedule"
- User must have visibility enabled in the queried guild
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from discord.ext import commands

import config
from utils.base_cog import BaseCog

if TYPE_CHECKING:
    from utils.bot_class import CoreBot

logger = logging.getLogger(__name__)


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

            await self._uvicorn_server.serve()

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
    # Patterns are defined in config.NLP_COMMANDS but left empty for now

    async def schedule_check(self, ctx: commands.Context, query: str) -> None:
        """Check if a mentioned user is available.

        This handler responds to queries like "when is @user free" or
        "is @user available". It looks up the mentioned user's availability
        and shows matching time slots.

        Args:
            ctx: Command context from Discord.
            query: The full query string from the user.
        """
        # Get mentioned user
        if not ctx.message.mentions:
            await ctx.send("Please mention a user to check their availability.")
            return

        target = ctx.message.mentions[0]
        requester_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None

        if guild_id is None:
            await ctx.send("This command only works in servers.")
            return

        # Check if requester can view target's availability
        can_view = await self.bot.db_manager.schedule_can_view(  # type: ignore[union-attr]
            requester_id=requester_id,
            target_id=target.id,
            guild_id=guild_id
        )

        if not can_view:
            await ctx.send(
                f"{target.display_name} hasn't shared their availability in this server, "
                "or has blocked you from viewing it."
            )
            return

        # Get availability
        slots = await self.bot.db_manager.schedule_get_availability(target.id)  # type: ignore[union-attr]

        if not slots:
            await ctx.send(f"{target.display_name} hasn't set any availability yet.")
            return

        # Format availability for display
        # Group by day
        by_day: dict[str, list[str]] = {
            "mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []
        }

        for slot in slots:
            day, time = slot.split("-")
            if day in by_day:
                by_day[day].append(time)

        # Format output
        day_names = {
            "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
            "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"
        }

        lines = [f"📅 **{target.display_name}'s Availability**\n"]

        for day, times in by_day.items():
            if times:
                times.sort()
                # Convert times to ranges
                ranges = self._times_to_ranges(times)
                lines.append(f"**{day_names[day]}:** {', '.join(ranges)}")

        if len(lines) == 1:
            await ctx.send(f"{target.display_name} hasn't set any availability yet.")
            return

        await ctx.send("\n".join(lines))

    async def schedule_show(self, ctx: commands.Context, query: str) -> None:
        """Show the requesting user's own availability.

        Args:
            ctx: Command context from Discord.
            query: The full query string (unused).
        """
        slots = await self.bot.db_manager.schedule_get_availability(ctx.author.id)  # type: ignore[union-attr]

        if not slots:
            await ctx.send(
                "You haven't set any availability yet. "
                f"Visit {config.OAUTH_REDIRECT_URI.rsplit('/callback', 1)[0]}/settings.html to set it up!"
            )
            return

        # Group by day
        by_day: dict[str, list[str]] = {
            "mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []
        }

        for slot in slots:
            day, time = slot.split("-")
            if day in by_day:
                by_day[day].append(time)

        day_names = {
            "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
            "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"
        }

        lines = ["📅 **Your Availability**\n"]

        for day, times in by_day.items():
            if times:
                times.sort()
                ranges = self._times_to_ranges(times)
                lines.append(f"**{day_names[day]}:** {', '.join(ranges)}")

        await ctx.send("\n".join(lines))

    async def schedule_link(self, ctx: commands.Context, query: str) -> None:
        """Send the link to the schedule web UI.

        Args:
            ctx: Command context from Discord.
            query: The full query string (unused).
        """
        if not config.WEB_ENABLED:
            await ctx.send("The schedule web interface is not currently enabled.")
            return

        # Construct base URL from redirect URI
        base_url = config.OAUTH_REDIRECT_URI.rsplit("/callback", 1)[0]

        # Include guild param if run from a server
        guild_param = f"?guild={ctx.guild.id}" if ctx.guild else ""

        await ctx.send(
            f"📅 **Schedule Manager**\n"
            f"Set your availability here: {base_url}/settings.html\n"
            f"View others' schedules: {base_url}/{guild_param}"
        )

    async def schedule_guild(self, ctx: commands.Context, query: str) -> None:
        """Show guild-wide availability for planning.

        This aggregates availability from all users who have enabled
        visibility in this guild.

        Args:
            ctx: Command context from Discord.
            query: The full query string (unused).
        """
        if ctx.guild is None:
            await ctx.send("This command only works in servers.")
            return

        # Get all availability visible in this guild
        all_availability = await self.bot.db_manager.schedule_get_guild_availability(  # type: ignore[union-attr]
            guild_id=ctx.guild.id,
            requester_id=ctx.author.id
        )

        if not all_availability:
            await ctx.send(
                "No one in this server has shared their availability yet.\n"
                "Be the first! Use the schedule link command to get started."
            )
            return

        # Count users available at each slot
        slot_counts: dict[str, int] = {}
        user_count = len(all_availability)

        for user_slots in all_availability.values():
            for slot in user_slots:
                slot_counts[slot] = slot_counts.get(slot, 0) + 1

        # Find slots where everyone/most are available
        everyone_slots = [s for s, c in slot_counts.items() if c == user_count]
        most_slots = [s for s, c in slot_counts.items() if c >= user_count * 0.75]

        lines = [f"📅 **Server Availability** ({user_count} members sharing)\n"]

        if everyone_slots:
            everyone_slots.sort()
            lines.append(f"✅ **Everyone available:** {len(everyone_slots)} slots")

            # Group by day and show ranges
            by_day: dict[str, list[str]] = {d: [] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
            for slot in everyone_slots[:20]:  # Limit output
                day, time = slot.split("-")
                if day in by_day:
                    by_day[day].append(time)

            day_names = {
                "mon": "Mon", "tue": "Tue", "wed": "Wed",
                "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"
            }

            for day, times in by_day.items():
                if times:
                    times.sort()
                    ranges = self._times_to_ranges(times)
                    lines.append(f"  {day_names[day]}: {', '.join(ranges)}")

        elif most_slots:
            lines.append(f"⚠️ No times when everyone is free, but {len(most_slots)} slots where 75%+ are available.")
        else:
            lines.append("⚠️ Schedules don't overlap much. Try checking individual availability.")

        await ctx.send("\n".join(lines))

    def _times_to_ranges(self, times: list[str]) -> list[str]:
        """Convert a list of HHMM times to human-readable ranges.

        Args:
            times: List of time strings in HHMM format.

        Returns:
            List of formatted time ranges like "9AM-12PM".
        """
        if not times:
            return []

        # Sort times
        times = sorted(times)

        ranges = []
        start = times[0]
        prev = times[0]

        for time in times[1:]:
            prev_minutes = int(prev[:2]) * 60 + int(prev[2:])
            curr_minutes = int(time[:2]) * 60 + int(time[2:])

            # Check if consecutive (15 min apart)
            if curr_minutes - prev_minutes == 15:
                prev = time
            else:
                # End current range
                ranges.append(self._format_range(start, prev))
                start = time
                prev = time

        # Add final range
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
                h = 12
                ampm = "AM"
            elif hour < 12:
                h = hour
                ampm = "AM"
            elif hour == 12:
                h = 12
                ampm = "PM"
            else:
                h = hour - 12
                ampm = "PM"

            if minute == 0:
                return f"{h}{ampm}"
            else:
                return f"{h}:{minute:02d}{ampm}"

        start_fmt = format_time(start)

        # Add 15 min to end to show exclusive end time
        end_minutes = int(end[:2]) * 60 + int(end[2:]) + 15
        end_hour = end_minutes // 60
        end_min = end_minutes % 60

        if end_hour >= 24:
            end_hour = 0  # Wrap to midnight

        end_time = f"{end_hour:02d}{end_min:02d}"
        end_fmt = format_time(end_time)

        if start_fmt == end_fmt:
            return start_fmt
        else:
            return f"{start_fmt}-{end_fmt}"


async def setup(bot: "CoreBot") -> None:
    """Load the Schedule cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(Schedule(bot))

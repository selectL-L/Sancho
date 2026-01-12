"""API routes for schedule availability management.

This module provides REST API endpoints for:
- /api/config: Public config (bot name, theme color)
- /api/availability: Get/set user's availability slots
- /api/guilds: List user's guilds with visibility settings
- /api/guilds/{guild_id}/visibility: Toggle guild visibility
- /api/guilds/{guild_id}/viewable-users: Users visible in a specific guild (for heatmap)
- /api/all-viewable-users: All users visible across any guild (for Everyone sidebar)
- /api/blacklist: Manage blocked users
- /api/account: Delete all user data

All routes except /api/config require authentication.
When DEV_MODE is enabled, mock data is returned for UI testing.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import config
from utils.web.auth import get_user_guilds, get_user_id, require_auth

router = APIRouter()
logger = logging.getLogger(__name__)

# Days of the week for timezone conversion (Monday = 0)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def convert_slots_timezone(
    slots: list[str],
    from_tz: str,
    to_tz: str,
) -> list[str]:
    """Convert availability slots from one timezone to another.

    Handles day wrapping (e.g., Friday 11 PM NYC → Saturday 4 AM London).

    Args:
        slots: List of slot strings like "mon-0900", "fri-2300".
        from_tz: Source IANA timezone (e.g., "America/New_York").
        to_tz: Target IANA timezone (e.g., "Europe/London").

    Returns:
        List of converted slot strings in the target timezone.
    """
    if from_tz == to_tz:
        return slots

    try:
        source_tz = ZoneInfo(from_tz)
        target_tz = ZoneInfo(to_tz)
    except Exception:
        # If timezone parsing fails, return unchanged
        logger.warning(f"Failed to parse timezones: {from_tz} -> {to_tz}")
        return slots

    converted = []

    # Use a reference week (Monday of a recent week)
    # We use a fixed date to get consistent behavior
    reference_monday = datetime(2025, 1, 6, 0, 0, 0)  # A Monday

    for slot in slots:
        try:
            parts = slot.split("-")
            if len(parts) != 2:
                continue

            day, time = parts
            if day not in DAYS or len(time) != 4:
                continue

            day_idx = DAYS.index(day)
            hour = int(time[:2])
            minute = int(time[2:])

            # Create datetime in source timezone
            slot_date = reference_monday + timedelta(days=day_idx)
            source_dt = datetime(
                slot_date.year, slot_date.month, slot_date.day,
                hour, minute, 0,
                tzinfo=source_tz
            )

            # Convert to target timezone
            target_dt = source_dt.astimezone(target_tz)

            # Calculate new day index (may have shifted)
            # Days since reference Monday
            days_diff = (target_dt.date() - reference_monday.date()).days
            new_day_idx = days_diff % 7  # Wrap around week

            new_day = DAYS[new_day_idx]
            new_time = f"{target_dt.hour:02d}{target_dt.minute:02d}"

            converted.append(f"{new_day}-{new_time}")

        except (ValueError, IndexError):
            # Skip malformed slots
            continue

    return converted


@router.get("/config")
async def get_config() -> JSONResponse:
    """Get public configuration for the web UI.

    Returns bot name and theme color for dynamic UI customization.
    This endpoint does not require authentication.

    Returns:
        JSON with botName and optional themeColor.
    """
    # DEV_MODE: Return mock config
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_config())

    response: dict[str, Any] = {
        "botName": config.BOT_NAME,
    }

    # Only include theme color if configured
    if config.THEME_COLOR:
        response["themeColor"] = config.THEME_COLOR

    return JSONResponse(content=response)


@router.get("/users/resolve")
async def resolve_user(request: Request, query: str = "") -> JSONResponse:
    """Resolve a username or user ID to user info.

    Searches all guilds the bot is in for a matching user.
    Accepts either a numeric user ID or a username/display name.

    Args:
        request: The incoming request.
        query: Username or user ID to search for.

    Returns:
        JSON with user info if found, or suggestion to use user ID if not.
    """
    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    if not query or not query.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "Query parameter required"},
        )

    query = query.strip()
    bot = request.app.state.bot

    # Check if it's a numeric user ID
    if query.isdigit():
        target_id = int(query)
        # Try to find in cache first
        user = bot.get_user(target_id)
        if user:
            return JSONResponse(content={
                "found": True,
                "id": target_id,
                "username": user.display_name,
                "avatar": user.display_avatar.url,
            })
        else:
            # User ID is valid format but not in cache - still usable for blocking
            return JSONResponse(content={
                "found": True,
                "id": target_id,
                "username": f"User {target_id}",
                "avatar": None,
                "note": "User isn't currently in any shared servers, but the ID is still valid for blocking!",
            })

    # Search by username/display name across all bot guilds
    query_lower = query.lower()
    matches = []

    for guild in bot.guilds:
        for member in guild.members:
            # Check username, display name, and global name
            if (query_lower in member.name.lower() or
                query_lower in member.display_name.lower() or
                    (member.global_name and query_lower in member.global_name.lower())):
                # Avoid duplicates
                if not any(m["id"] == member.id for m in matches):
                    matches.append({
                        "id": member.id,
                        "username": member.display_name,
                        "avatar": member.display_avatar.url,
                    })

        # Limit results to avoid huge responses
        if len(matches) >= 10:
            break

    if matches:
        if len(matches) == 1:
            return JSONResponse(content={
                "found": True,
                **matches[0],
            })
        else:
            return JSONResponse(content={
                "found": True,
                "multiple": True,
                "matches": matches,
                "message": "I found multiple users found. Please select one or use their user ID if you have it!",
            })

    # No matches found
    return JSONResponse(content={
        "found": False,
        "message": "No user found with that name. If you have their user ID, you can enter it directly to block them before you even share a server!",
    })


@router.get("/availability")
async def get_availability(request: Request) -> JSONResponse:
    """Get the current user's availability slots.

    Returns a list of time slot strings in format "day-HHMM" where:
    - day is one of: mon, tue, wed, thu, fri, sat, sun
    - HHMM is 24-hour time in 15-minute increments (0000, 0015, 0030, ...)

    Args:
        request: The incoming request.

    Returns:
        JSON with slots array, or 401 if not authenticated.
    """
    # DEV_MODE: Return mock availability
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_availability())

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    bot = request.app.state.bot
    slots = await bot.db_manager.schedule_get_availability(user_id)  # type: ignore[arg-type]
    updated_at = await bot.db_manager.schedule_get_availability_updated_at(user_id)  # type: ignore[arg-type]

    return JSONResponse(content={"slots": slots, "updated_at": updated_at})


@router.post("/availability")
async def set_availability(request: Request) -> JSONResponse:
    """Set the current user's availability slots.

    Expects JSON body with "slots" array of time slot strings.
    Replaces all existing availability for the user.

    Requires the user to have a timezone set (availability is timezone-aware).

    Args:
        request: The incoming request.

    Returns:
        JSON success message, or 401/400/403 on error.
    """
    try:
        body = await request.json()
        slots = body.get("slots", [])
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    if not isinstance(slots, list):
        return JSONResponse(
            status_code=400,
            content={"error": "slots must be an array"},
        )

    # Validate slot format
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    valid_slots = []

    for slot in slots:
        if not isinstance(slot, str):
            continue

        parts = slot.split("-")
        if len(parts) != 2:
            continue

        day, time_str = parts
        if day not in valid_days:
            continue

        if len(time_str) != 4 or not time_str.isdigit():
            continue

        hour = int(time_str[:2])
        minute = int(time_str[2:])

        if hour < 0 or hour > 23:
            continue
        if minute not in (0, 15, 30, 45):
            continue

        valid_slots.append(slot)

    # DEV_MODE: Save to ephemeral mock store
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.set_mock_availability(valid_slots))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Require timezone to be set before saving availability
    bot = request.app.state.bot
    user_tz = await bot.db_manager.get_user_timezone(user_id)
    if not user_tz:
        return JSONResponse(
            status_code=403,
            content={"error": "You must set your timezone before saving availability. Use the timezone command in Discord."},
        )

    await bot.db_manager.schedule_set_availability(user_id, valid_slots)  # type: ignore[arg-type]

    logger.info(f"User {user_id} saved {len(valid_slots)} availability slots")

    return JSONResponse(content={"success": True, "count": len(valid_slots)})


@router.delete("/availability")
async def clear_availability(request: Request) -> JSONResponse:
    """Clear all availability for the current user.

    Args:
        request: The incoming request.

    Returns:
        JSON success message, or 401 if not authenticated.
    """
    # DEV_MODE: Clear ephemeral mock store
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.clear_mock_availability())

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    bot = request.app.state.bot
    await bot.db_manager.schedule_clear_availability(user_id)  # type: ignore[arg-type]

    logger.info(f"User {user_id} cleared all availability")

    return JSONResponse(content={"success": True})


@router.get("/guilds")
async def get_guilds(request: Request) -> JSONResponse:
    """Get the user's guilds with visibility settings.

    Returns guilds the user is a member of that the bot is also in,
    along with whether the user's availability is visible in each guild.

    Args:
        request: The incoming request.

    Returns:
        JSON with guilds array, or 401 if not authenticated.
    """
    # DEV_MODE: Return mock guilds
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_guilds())

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    bot = request.app.state.bot
    user_guild_ids = await get_user_guilds(request)

    # Get guilds the bot is in
    bot_guild_ids = {g.id for g in bot.guilds}

    # Find overlap (user + bot both in guild)
    shared_guild_ids = set(user_guild_ids) & bot_guild_ids

    # Get visibility settings from database
    visibility_settings = await bot.db_manager.schedule_get_guild_visibility(user_id)  # type: ignore[arg-type]
    visibility_map = {v["guild_id"]: bool(v["enabled"]) for v in visibility_settings}

    # Build guild list
    # NOTE: IDs are strings to avoid JavaScript number precision loss (snowflakes are 64-bit)
    guilds = []
    for guild_id in shared_guild_ids:
        guild = bot.get_guild(guild_id)
        if guild:
            guilds.append({
                "id": str(guild_id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
                "enabled": visibility_map.get(guild_id, False),
            })

    # Sort by name
    guilds.sort(key=lambda g: g["name"].lower())

    return JSONResponse(content={"guilds": guilds})


@router.post("/guilds/{guild_id}/visibility")
async def set_guild_visibility(request: Request, guild_id: str) -> JSONResponse:
    """Set visibility for a specific guild.

    Args:
        request: The incoming request.
        guild_id: The guild to update (as string to preserve precision).

    Returns:
        JSON success message, or 401/400 on error.
    """
    # DEV_MODE: Toggle in ephemeral mock store
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.toggle_mock_guild_visibility(guild_id))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Convert string guild_id to int (strings used in API to avoid JS precision loss)
    try:
        guild_id_int = int(guild_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid guild ID"},
        )

    try:
        body = await request.json()
        visible = body.get("visible", False)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    if not isinstance(visible, bool):
        return JSONResponse(
            status_code=400,
            content={"error": "visible must be a boolean"},
        )

    # Verify user is in this guild
    user_guild_ids = await get_user_guilds(request)
    if guild_id_int not in user_guild_ids:
        return JSONResponse(
            status_code=403,
            content={"error": "You are not a member of this guild"},
        )

    bot = request.app.state.bot
    await bot.db_manager.schedule_set_guild_visibility(user_id, guild_id_int, visible)  # type: ignore[arg-type]

    logger.info(f"User {user_id} set visibility in guild {guild_id_int} to {visible}")

    return JSONResponse(content={"success": True})


@router.get("/blacklist")
async def get_blacklist(request: Request) -> JSONResponse:
    """Get the user's blacklist (blocked users).

    Args:
        request: The incoming request.

    Returns:
        JSON with users array (id, username), or 401 if not authenticated.
    """
    # DEV_MODE: Return mock blacklist
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_blacklist())

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    bot = request.app.state.bot
    blacklist = await bot.db_manager.schedule_get_blacklist(user_id)  # type: ignore[arg-type]

    # Enrich with usernames from Discord
    users = []
    for blocked_id in blacklist:
        user = bot.get_user(blocked_id)

        users.append({
            "id": blocked_id,
            "username": user.display_name if user else f"User {blocked_id}",
            "avatar": user.display_avatar.url if user else None,
        })

    return JSONResponse(content={"users": users})


@router.post("/blacklist/{blocked_user_id}")
async def add_to_blacklist(request: Request, blocked_user_id: str) -> JSONResponse:
    """Add a user to the blacklist.

    Args:
        request: The incoming request.
        blocked_user_id: The user ID to block (as string to preserve precision).

    Returns:
        JSON success message, or 401/400 on error.
    """
    # DEV_MODE: Add to ephemeral mock blacklist
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.add_mock_blacklist(blocked_user_id))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Convert string ID to int (strings used in API to avoid JS precision loss)
    try:
        blocked_user_id_int = int(blocked_user_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid user ID"},
        )

    if blocked_user_id_int == user_id:
        return JSONResponse(
            status_code=400,
            content={"error": "You cannot block yourself"},
        )

    bot = request.app.state.bot
    await bot.db_manager.schedule_add_to_blacklist(user_id, blocked_user_id_int)  # type: ignore[arg-type]

    logger.info(f"User {user_id} blocked user {blocked_user_id_int}")

    return JSONResponse(content={"success": True})


@router.delete("/blacklist/{blocked_user_id}")
async def remove_from_blacklist(request: Request, blocked_user_id: str) -> JSONResponse:
    """Remove a user from the blacklist.

    Args:
        request: The incoming request.
        blocked_user_id: The user ID to unblock (as string to preserve precision).

    Returns:
        JSON success message, or 401 if not authenticated.
    """
    # DEV_MODE: Remove from ephemeral mock blacklist
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.remove_mock_blacklist(blocked_user_id))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Convert string ID to int (strings used in API to avoid JS precision loss)
    try:
        blocked_user_id_int = int(blocked_user_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid user ID"},
        )

    bot = request.app.state.bot
    await bot.db_manager.schedule_remove_from_blacklist(user_id, blocked_user_id_int)  # type: ignore[arg-type]

    logger.info(f"User {user_id} unblocked user {blocked_user_id_int}")

    return JSONResponse(content={"success": True})


@router.delete("/account")
async def delete_account(request: Request) -> JSONResponse:
    """Delete all user data (availability, visibility, blacklist).

    This is a destructive operation that cannot be undone.

    Args:
        request: The incoming request.

    Returns:
        JSON success message, or 401 if not authenticated.
    """
    # DEV_MODE: Reset ephemeral mock state
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.delete_mock_account())

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    bot = request.app.state.bot
    await bot.db_manager.schedule_delete_all_user_data(user_id)  # type: ignore[arg-type]

    # Clear session
    request.session.clear()

    logger.info(f"User {user_id} deleted all their schedule data")

    return JSONResponse(content={"success": True})


@router.get("/guilds/{guild_id}/viewable-users")
async def get_viewable_users(request: Request, guild_id: str) -> JSONResponse:
    """Get users whose availability is viewable in a specific guild.

    Returns users who:
    - Are members of the specified guild
    - Have enabled visibility for that guild
    - Have not blocked the requesting user
    - Are not blocked by the requesting user

    Args:
        request: The incoming request.
        guild_id: The guild to get viewable users for (as string to preserve precision).

    Returns:
        JSON with users array, or 401/403 on error.
    """
    # DEV_MODE: Return mock viewable users
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_viewable_users(guild_id))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Convert string guild_id to int (strings used in API to avoid JS precision loss)
    try:
        guild_id_int = int(guild_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid guild ID"},
        )

    # Verify user is in this guild
    user_guild_ids = await get_user_guilds(request)
    logger.debug(f"viewable-users: user {user_id} requesting guild {guild_id_int}, user guilds: {user_guild_ids}")
    if guild_id_int not in user_guild_ids:
        logger.warning(f"viewable-users: user {user_id} not in guild {guild_id_int}")
        return JSONResponse(
            status_code=403,
            content={"error": "You are not a member of this guild"},
        )

    bot = request.app.state.bot
    guild = bot.get_guild(guild_id_int)
    logger.debug(f"viewable-users: bot.get_guild({guild_id_int}) = {guild}")
    if not guild:
        return JSONResponse(
            status_code=404,
            content={"error": "Guild not found"},
        )

    logger.debug(f"viewable-users: guild has {guild.member_count} members, cached: {len(guild.members)}")

    # Get users who have enabled visibility for this guild
    visible_user_ids = await bot.db_manager.schedule_get_visible_users_in_guild(guild_id_int)  # type: ignore[arg-type]
    logger.debug(f"viewable-users: visible_user_ids for guild {guild_id_int}: {visible_user_ids}")

    # Get requester's blacklist
    my_blacklist = set(await bot.db_manager.schedule_get_blacklist(user_id))  # type: ignore[arg-type]

    # Build user list (include self for heatmap, marked with isSelf flag)
    users = []
    for target_id in visible_user_ids:
        # Skip users the requester has blocked (but not self)
        if target_id != user_id and target_id in my_blacklist:
            logger.debug(f"viewable-users: skipping {target_id} (in my_blacklist)")
            continue

        # Check if target has blocked requester (but not self)
        if target_id != user_id:
            target_blacklist = set(await bot.db_manager.schedule_get_blacklist(target_id))  # type: ignore[arg-type]
            if user_id in target_blacklist:
                logger.debug(f"viewable-users: skipping {target_id} (blocked me)")
                continue

        # Get user info - try cache first, then fetch from Discord API
        member = guild.get_member(target_id)
        if not member:
            try:
                member = await guild.fetch_member(target_id)
                logger.debug(f"viewable-users: fetched member {target_id} from API")
            except Exception as e:
                logger.warning(f"viewable-users: failed to fetch member {target_id}: {e}")
                member = None
        else:
            logger.debug(f"viewable-users: got member {target_id} from cache")

        if member:
            # Get last-modified timestamp
            updated_at = await bot.db_manager.schedule_get_availability_updated_at(target_id)
            users.append({
                "id": str(target_id),  # String to avoid JS precision loss
                "username": member.display_name,
                "avatar": member.display_avatar.url,
                "updated_at": updated_at,
                "isSelf": target_id == user_id,
            })
        else:
            logger.warning(f"viewable-users: member {target_id} not found in guild {guild_id_int} cache")

    logger.debug(f"viewable-users: returning {len(users)} users: {[u['id'] for u in users]}")

    # Sort by username
    users.sort(key=lambda u: u["username"].lower())

    return JSONResponse(content={"users": users, "guild_name": guild.name})


@router.get("/all-viewable-users")
async def get_all_viewable_users(request: Request, focus_guild: str = "0") -> JSONResponse:
    """Get ALL users whose availability is viewable across ANY shared guild.

    This endpoint aggregates users from all guilds where:
    - Requester is a member
    - Target user has enabled visibility
    - Neither has blocked the other

    For each user, returns the list of shared guilds (where both are members
    AND target has visibility enabled), sorted with focus_guild first if applicable,
    then alphabetically.

    Args:
        request: The incoming request.
        focus_guild: Guild ID to prioritize in shared_guilds ordering (optional).

    Returns:
        JSON with users array including shared_guilds for each user.
    """
    # DEV_MODE: Return mock all viewable users
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_all_viewable_users(focus_guild))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Parse focus guild ID
    focus_guild_int = 0
    if focus_guild and focus_guild != "0":
        try:
            focus_guild_int = int(focus_guild)
        except ValueError:
            pass

    bot = request.app.state.bot
    user_guild_ids = await get_user_guilds(request)

    # Get requester's blacklist once
    my_blacklist = set(await bot.db_manager.schedule_get_blacklist(user_id))  # type: ignore[arg-type]

    # Aggregate: user_id -> {info, shared_guilds: []}
    user_data: dict[int, dict[str, Any]] = {}

    for guild_id in user_guild_ids:
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        # Get users who have enabled visibility for this guild
        visible_user_ids = await bot.db_manager.schedule_get_visible_users_in_guild(guild_id)  # type: ignore[arg-type]

        for target_id in visible_user_ids:
            # Skip self (we'll handle self separately at the end)
            if target_id == user_id:
                continue

            # Skip users in my blacklist
            if target_id in my_blacklist:
                continue

            # Check if target has blocked me (cache this check per-user)
            if target_id not in user_data:
                target_blacklist = set(await bot.db_manager.schedule_get_blacklist(target_id))  # type: ignore[arg-type]
                if user_id in target_blacklist:
                    continue

            # Get member info from this guild
            member = guild.get_member(target_id)
            if not member:
                try:
                    member = await guild.fetch_member(target_id)
                except Exception:
                    continue

            if not member:
                continue

            # Initialize user entry if not seen before
            if target_id not in user_data:
                updated_at = await bot.db_manager.schedule_get_availability_updated_at(target_id)
                user_data[target_id] = {
                    "id": str(target_id),
                    "username": member.display_name,
                    "avatar": member.display_avatar.url,
                    "updated_at": updated_at,
                    "isSelf": False,
                    "shared_guilds": [],
                }

            # Add this guild to their shared_guilds
            user_data[target_id]["shared_guilds"].append({
                "id": str(guild_id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
            })

    # Sort shared_guilds for each user: focus guild first, then alphabetical
    for data in user_data.values():
        data["shared_guilds"].sort(key=lambda g: (
            0 if int(g["id"]) == focus_guild_int else 1,  # Focus guild first
            g["name"].lower()  # Then alphabetical
        ))

    # Convert to list and sort by username
    users = list(user_data.values())
    users.sort(key=lambda u: u["username"].lower())

    return JSONResponse(content={"users": users})


@router.get("/users/{target_user_id}/availability")
async def get_user_availability(request: Request, target_user_id: str, guild_id: str = "0") -> JSONResponse:
    """Get another user's availability.

    Permission checks:
    - Requester must share a guild with target where target has visibility enabled
    - Target must not have blocked requester
    - Requester must not have blocked target (mutual respect)

    Args:
        request: The incoming request.
        target_user_id: The user whose availability to view (as string to preserve precision).
        guild_id: Optional guild context for permission check (as string).

    Returns:
        JSON with availability slots, or 401/403 on error.
    """
    # DEV_MODE: Return mock user availability
    if config.DEV_MODE:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_user_availability(target_user_id))

    user_id = await get_user_id(request)
    if (error := require_auth(user_id)):
        return error

    # Convert string IDs to int (strings used in API to avoid JS precision loss)
    try:
        target_user_id_int = int(target_user_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid user ID"},
        )

    # Note: guild_id parameter is reserved for future per-guild permission checks
    # Currently permissions are checked via get_user_guilds() overlap

    if target_user_id_int == user_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Use /api/availability to view your own schedule"},
        )

    bot = request.app.state.bot

    # Check blacklists (mutual)
    my_blacklist = set(await bot.db_manager.schedule_get_blacklist(user_id))  # type: ignore[arg-type]
    target_blacklist = set(await bot.db_manager.schedule_get_blacklist(target_user_id_int))  # type: ignore[arg-type]

    if target_user_id_int in my_blacklist:
        return JSONResponse(
            status_code=403,
            content={"error": "You have blocked this user"},
        )

    if user_id in target_blacklist:
        return JSONResponse(
            status_code=403,
            content={"error": "This user has blocked you"},
        )

    # Get requester's guilds
    user_guild_ids = set(await get_user_guilds(request))

    # Get target's visibility settings
    target_visibility = await bot.db_manager.schedule_get_guild_visibility(target_user_id_int)  # type: ignore[arg-type]
    enabled_guild_ids = {v["guild_id"] for v in target_visibility if v["enabled"]}

    # Find shared guilds where target has visibility enabled
    viewable_guilds = user_guild_ids & enabled_guild_ids

    if not viewable_guilds:
        return JSONResponse(
            status_code=403,
            content={"error": "You don't share any guilds where this user has enabled visibility"},
        )

    # Get availability
    slots = await bot.db_manager.schedule_get_availability(target_user_id_int)  # type: ignore[arg-type]
    updated_at = await bot.db_manager.schedule_get_availability_updated_at(target_user_id_int)  # type: ignore[arg-type]

    # Convert slots to viewer's timezone
    target_tz = await bot.db_manager.get_user_timezone(target_user_id_int)
    viewer_tz = await bot.db_manager.get_user_timezone(user_id)

    # Track if conversion happened (for UI to show warning if not)
    converted = False
    if target_tz and viewer_tz:
        slots = convert_slots_timezone(slots, target_tz, viewer_tz)
        converted = True

    # Get target's user info
    user = bot.get_user(target_user_id_int)

    return JSONResponse(content={
        "slots": slots,
        "username": user.display_name if user else f"User {target_user_id_int}",
        "avatar": user.display_avatar.url if user else None,
        "updated_at": updated_at,
        "timezone_converted": converted,
        "source_timezone": target_tz,  # So viewer knows whose timezone if not converted
    })

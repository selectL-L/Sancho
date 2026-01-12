"""Discord OAuth2 authentication routes.

This module provides OAuth2 authentication with Discord, including:
- Login redirect to Discord's authorization page
- OAuth callback to exchange code for tokens
- User info endpoint (who am I?)
- Logout endpoint

Session cookie stores only user_id and token_expires_at (minimal footprint).
User profile data (username, avatar) is cached in the database for persistence.
Guild membership is determined from the bot's cache at runtime.
"""

import logging
import re
import secrets
import time

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

import config

router = APIRouter()


def _to_display_format(tz_str: str | None) -> str | None:
    """Convert pytz/IANA timezone string to user-friendly display format.

    Converts Etc/GMT format back to familiar GMT notation.
    IANA names are returned unchanged.

    Args:
        tz_str: The timezone string from the database.

    Returns:
        A user-friendly timezone string for display, or None if input is None.
    """
    if tz_str is None:
        return None

    match = re.match(r'^Etc/GMT([+-]?)(\d+)$', tz_str)
    if match:
        sign_part = match.group(1)
        hour = int(match.group(2))
        if hour == 0:
            return "GMT"
        # Invert back: Etc/GMT-5 -> GMT+5 (POSIX sign inversion)
        if sign_part == '-' or (not sign_part and hour > 0):
            return f"GMT+{hour}"
        else:
            return f"GMT-{hour}"

    return tz_str
logger = logging.getLogger(__name__)

# Discord OAuth2 endpoints
DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"

# Required OAuth2 scopes
# - identify: Get user's Discord ID, username, avatar
# We no longer need guilds scope - guild membership comes from bot's cache
OAUTH_SCOPES = "identify"


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Redirect user to Discord OAuth2 authorization page.

    Generates a state token to prevent CSRF attacks and stores it in the session.

    Args:
        request: The incoming request.

    Returns:
        Redirect to Discord's authorization URL.
    """
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    # Build authorization URL
    params = {
        "client_id": config.OAUTH_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
    }

    query = "&".join(f"{k}={v}" for k, v in params.items())
    auth_url = f"{DISCORD_AUTHORIZE_URL}?{query}"

    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    """Handle OAuth2 callback from Discord.

    Validates state token, exchanges authorization code for tokens,
    fetches user info, and stores user data in session.

    Args:
        request: The incoming request.
        code: Authorization code from Discord.
        state: State token for CSRF validation.
        error: Error message if authorization was denied.

    Returns:
        Redirect to settings page on success, or home with error on failure.
    """
    # Handle authorization denied
    if error:
        logger.warning(f"OAuth callback received error: {error}")
        return RedirectResponse(url="/?error=access_denied")

    # Validate state to prevent CSRF
    stored_state = request.session.pop("oauth_state", None)
    if not stored_state or stored_state != state:
        logger.warning("OAuth callback state mismatch (possible CSRF)")
        return RedirectResponse(url="/?error=invalid_state")

    if not code:
        logger.warning("OAuth callback missing authorization code")
        return RedirectResponse(url="/?error=no_code")

    # Exchange code for tokens
    try:
        async with aiohttp.ClientSession() as session:
            # Token exchange
            token_data = {
                "client_id": config.OAUTH_CLIENT_ID,
                "client_secret": config.OAUTH_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.OAUTH_REDIRECT_URI,
            }

            async with session.post(
                DISCORD_TOKEN_URL,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Token exchange failed: {resp.status} - {error_text}")
                    return RedirectResponse(url="/?error=token_failed")

                tokens = await resp.json()

            access_token = tokens.get("access_token")
            expires_in = tokens.get("expires_in", 604800)  # Default 7 days
            if not access_token:
                logger.error("No access token in response")
                return RedirectResponse(url="/?error=no_token")

            # Fetch user info
            headers = {"Authorization": f"Bearer {access_token}"}

            async with session.get(f"{DISCORD_API_BASE}/users/@me", headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to fetch user info: {resp.status}")
                    return RedirectResponse(url="/?error=user_fetch_failed")

                user_data = await resp.json()

    except aiohttp.ClientError as e:
        logger.error(f"Network error during OAuth: {e}")
        return RedirectResponse(url="/?error=network_error")
    except Exception as e:
        logger.error(f"Unexpected error during OAuth: {e}", exc_info=True)
        return RedirectResponse(url="/?error=unknown_error")

    # Extract user info
    user_id = int(user_data["id"])
    username = user_data.get("global_name") or user_data["username"]
    avatar_hash = user_data.get("avatar")

    if avatar_hash:
        avatar_ext = "gif" if avatar_hash.startswith("a_") else "png"
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{avatar_ext}"
    else:
        # Default avatar based on user ID
        default_index = (user_id >> 22) % 6
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"

    # Save user to database (permanent memory!)
    bot = request.app.state.bot
    await bot.db_manager.upsert_user(user_id, username, avatar_url)

    # Store minimal data in session cookie
    # Only user_id and token_expires_at - this fits easily in 4KB
    request.session["user_id"] = user_id
    request.session["token_expires_at"] = int(time.time()) + expires_in

    logger.info(f"User {user_id} ({username}) logged in successfully")

    return RedirectResponse(url="/settings.html", status_code=302)


@router.get("/me")
async def get_current_user(request: Request) -> JSONResponse:
    """Get the currently logged-in user's info.

    Fetches user profile from database (permanent memory).
    Checks if Discord auth has expired based on token_expires_at.

    Args:
        request: The incoming request.

    Returns:
        JSON with user info (id, username, avatar, needs_reauth) or 401 if not logged in.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
        )

    # Fetch user from database
    bot = request.app.state.bot
    user = await bot.db_manager.get_user(user_id)

    if not user:
        # User ID in cookie but not in database - shouldn't happen, but handle it
        logger.warning(f"User {user_id} in session but not in database")
        request.session.clear()
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
        )

    # Check if Discord auth has expired
    token_expires_at = request.session.get("token_expires_at", 0)
    needs_reauth = time.time() > token_expires_at

    # Get user's timezone from reminders settings
    user_tz = await bot.db_manager.get_user_timezone(user_id)

    response = {
        "id": user["user_id"],
        "username": user["username"],
        "avatar": user["avatar"],
        "needs_reauth": needs_reauth,
        "timezone": _to_display_format(user_tz),
        "hasTimezone": user_tz is not None,
    }

    if needs_reauth:
        response["reauth_message"] = "Discord wants to check it's still you - please log in again!"

    return JSONResponse(content=response)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Log out the current user by clearing their session.

    Args:
        request: The incoming request.

    Returns:
        Redirect to home page.
    """
    request.session.clear()
    return RedirectResponse(url="/")


async def get_user_id(request: Request) -> int | None:
    """Extract the user ID from the session.

    This is a helper function for use in route handlers.

    Args:
        request: The incoming request.

    Returns:
        User ID if logged in, None otherwise.
    """
    return request.session.get("user_id")


async def get_user_guilds(request: Request) -> list[int]:
    """Get the list of guild IDs the user shares with the bot.

    Queries the bot's cache for guilds where this user is a member.
    Uses multiple lookup methods to handle member cache limitations.
    Results are cached on the request to avoid repeated API calls.

    Args:
        request: The incoming request.

    Returns:
        List of guild IDs, or empty list if not logged in.
    """
    # Check if we've already computed this for this request
    if hasattr(request.state, '_user_guilds'):
        return request.state._user_guilds

    user_id = request.session.get("user_id")
    if not user_id:
        return []

    bot = request.app.state.bot
    shared_guilds = []

    for guild in bot.guilds:
        # Try get_member first (cached lookup)
        member = guild.get_member(user_id)
        if member:
            shared_guilds.append(guild.id)
            continue

        # If not in cache, check if user is in the guild's member list
        # This handles cases where member cache isn't fully populated
        # but the user has interacted with the bot before
        try:
            member = await guild.fetch_member(user_id)
            if member:
                shared_guilds.append(guild.id)
        except Exception:
            # User is not in this guild, skip
            pass

    # Cache on request state for subsequent calls in same request
    request.state._user_guilds = shared_guilds
    logger.debug(f"get_user_guilds for {user_id}: {shared_guilds}")
    return shared_guilds


def require_auth(user_id: int | None) -> JSONResponse | None:
    """Check if user is authenticated, return error response if not.

    Args:
        user_id: The user ID from session, or None if not logged in.

    Returns:
        JSONResponse with 401 error if not authenticated, None otherwise.
    """
    if user_id is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Authentication required"},
        )
    return None

"""Discord OAuth2 authentication routes.

This module provides OAuth2 authentication with Discord, including:
- Login redirect to Discord's authorization page
- OAuth callback to exchange code for tokens
- User info endpoint (who am I?)
- Logout endpoint

Session data (tokens) is stored server-side in the database.
Only an opaque session_id is sent to the client in a cookie.
User profile data (username, avatar) is cached in the database for persistence.
Guild membership is determined from the bot's cache at runtime.
When WEB_MOCK_DATA is enabled, /auth/me returns mock user data for UI testing.
"""

import logging
import re
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

import config
from utils.web.session_middleware import (
    clear_session_cookie,
    generate_session_id,
    set_session_cookie,
)

router = APIRouter()

# OAuth state cookie (temporary, short-lived for CSRF protection)
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_MAX_AGE = 300  # 5 minutes - plenty of time for OAuth flow


def _to_display_format(tz_str: str | None) -> str | None:
    """Convert IANA/pytz timezone string to user-friendly display format.

    Examples:
        - "Etc/GMT-5" -> "GMT+5" (POSIX sign inversion)
        - "Etc/GMT+0" -> "GMT"
        - "America/New_York" -> "New York"
        - "Europe/London" -> "London"
        - "America/Indiana/Indianapolis" -> "Indianapolis"
        - "GMT+3" -> "GMT+3" (pass-through)

    Args:
        tz_str: The timezone string from the database.

    Returns:
        A user-friendly timezone string for display, or None if input is None.
    """
    if tz_str is None:
        return None

    # Handle Etc/GMT format (POSIX sign inversion)
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

    # Handle GMT/UTC offset strings - pass through as-is
    if re.match(r'^(GMT|UTC)([+-]\d+)?$', tz_str, re.IGNORECASE):
        return tz_str

    # IANA timezone: extract city name and format
    # "America/New_York" -> "New York"
    # "America/Indiana/Indianapolis" -> "Indianapolis"
    if '/' in tz_str:
        city = tz_str.split('/')[-1]
        return city.replace('_', ' ')

    # Unknown format, return as-is
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

    Generates a state token to prevent CSRF attacks and stores it in a
    signed, short-lived cookie.

    Args:
        request: The incoming request.

    Returns:
        Redirect to Discord's authorization URL.
    """
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Sign the state with the session secret
    serializer = URLSafeTimedSerializer(config.WEB_SESSION_SECRET)
    signed_state = serializer.dumps(state)

    # Build authorization URL
    params = {
        "client_id": config.OAUTH_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
    }

    query = urlencode(params)
    auth_url = f"{DISCORD_AUTHORIZE_URL}?{query}"

    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        signed_state,
        max_age=OAUTH_STATE_MAX_AGE,
        path="/",
        secure=not config.DEV_MODE,
        httponly=True,
        samesite="lax"
    )
    return response


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

    # Validate state to prevent CSRF (stored in signed cookie)
    signed_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not signed_state:
        logger.warning("OAuth callback missing state cookie (possible CSRF)")
        return RedirectResponse(url="/?error=invalid_state")

    try:
        serializer = URLSafeTimedSerializer(config.WEB_SESSION_SECRET)
        stored_state = serializer.loads(signed_state, max_age=OAUTH_STATE_MAX_AGE)
    except BadSignature:
        logger.warning("OAuth callback state signature invalid or expired (possible CSRF)")
        return RedirectResponse(url="/?error=invalid_state")

    if stored_state != state:
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

    # Create server-side session
    session_id = generate_session_id()
    token_expires_at = int(time.time()) + expires_in

    # Get refresh token from response
    refresh_token = tokens.get("refresh_token", "")

    await bot.db_manager.create_session(
        session_id=session_id,
        user_id=user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=token_expires_at
    )

    logger.info(f"User {user_id} ({username}) logged in successfully, session {session_id[:8]}...")

    # Set session cookie, clear OAuth state cookie, and redirect to index
    response = RedirectResponse(url="/index.html", status_code=302)
    set_session_cookie(response, session_id)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@router.get("/me")
async def get_current_user(request: Request) -> JSONResponse:
    """Get the currently logged-in user's info.

    Fetches user profile from database (permanent memory).
    Session validity and token refresh is handled by middleware.

    Args:
        request: The incoming request.

    Returns:
        JSON with user info (id, username, avatar) or 401 if not logged in.
    """
    # WEB_MOCK_DATA: Return mock current user for UI testing
    if config.WEB_MOCK_DATA:
        from utils.web import mock_data
        return JSONResponse(content=mock_data.get_mock_current_user())

    # user_id is set by session middleware
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
        )

    # Fetch user from database
    bot = request.app.state.bot
    user = await bot.db_manager.get_user(user_id)

    if not user:
        # User ID in session but not in database - shouldn't happen, but handle it
        logger.warning(f"User {user_id} in session but not in database")
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
        )

    # Get user's timezone from reminders settings
    user_tz = await bot.db_manager.get_user_timezone(user_id)

    response = {
        "id": str(user["user_id"]),  # String to avoid JS precision loss
        "username": user["username"],
        "avatar": user["avatar"],
        "timezone": _to_display_format(user_tz),
        "hasTimezone": user_tz is not None,
    }

    return JSONResponse(content=response)


@router.get("/logout")
async def logout(request: Request, clear: str = "") -> RedirectResponse:
    """Log out the current user.

    With clear=true, deletes ALL sessions for the user.
    Otherwise, only deletes the current session.

    Args:
        request: The incoming request.
        clear: If "true", delete all user sessions.

    Returns:
        Redirect to home page.
    """
    bot = request.app.state.bot
    user_id = getattr(request.state, "user_id", None)
    session_id = getattr(request.state, "session_id", None)

    if user_id and clear == "true":
        # Clear all sessions for user
        await bot.db_manager.delete_user_sessions(user_id)
        logger.info(f"User {user_id} logged out and cleared all sessions")
    elif session_id:
        # Delete only this session
        await bot.db_manager.delete_session(session_id)
        logger.info(f"Session {session_id[:8]}... logged out")

    response = RedirectResponse(url="/")
    clear_session_cookie(response)
    return response


async def get_user_id(request: Request) -> int | None:
    """Extract the user ID from the request state.

    This is a helper function for use in route handlers.
    The user_id is set by the session middleware.

    Args:
        request: The incoming request.

    Returns:
        User ID if logged in, None otherwise.
    """
    user_id = getattr(request.state, "user_id", None)
    return user_id


async def get_user_guilds(request: Request) -> list[int]:
    """Get the list of guild IDs the user shares with the bot.

    Queries the bot's cache for guilds where this user is a member.
    Uses cache-first lookup with API fallback for uncached members.
    Results are cached on the request to avoid repeated API calls.

    Note on performance: This function may make up to N API calls where N is
    the number of bot guilds where the user isn't in the member cache. With
    the members intent enabled, most users should be cached. The fallback
    handles edge cases like very large guilds or recently joined members.
    Rate limit errors are caught silently (user won't see that guild).

    Args:
        request: The incoming request.

    Returns:
        List of guild IDs, or empty list if not logged in.
    """
    # Check if we've already computed this for this request
    if hasattr(request.state, '_user_guilds'):
        return request.state._user_guilds

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return []

    bot = request.app.state.bot
    shared_guilds = []
    uncached_count = 0

    for guild in bot.guilds:
        # Try get_member first (cached lookup - instant, no API call)
        member = guild.get_member(user_id)
        if member:
            shared_guilds.append(guild.id)
            continue

        # If not in cache, fetch from Discord API
        # This handles cases where member cache isn't fully populated
        uncached_count += 1
        try:
            member = await guild.fetch_member(user_id)
            if member:
                shared_guilds.append(guild.id)
        except Exception:
            # User is not in this guild, or rate limited - skip silently
            pass

    if uncached_count > 0:
        logger.debug(f"get_user_guilds: {uncached_count} guilds required API lookup for user {user_id}")

    # Cache on request state for subsequent calls in same request
    request.state._user_guilds = shared_guilds
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

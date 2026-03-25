"""Server-side session middleware for web authentication.

This middleware replaces Starlette's SessionMiddleware with a proper server-side
session system. Session data (including Discord tokens) is stored in the database,
and only an opaque session ID is sent to the client in a cookie.

Benefits:
- Tokens never leave the server (more secure)
- Sessions can be extended indefinitely via token refresh
- Server can invalidate sessions at any time
- Multiple concurrent sessions per user supported

Cookie: schedule_session = UUID v4 string
Database: web_sessions table stores tokens and metadata
"""

import logging
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

import config
from utils.web.token_refresh import TokenRevokedException, refresh_discord_token

logger = logging.getLogger(__name__)

# Cookie configuration
COOKIE_NAME = "schedule_session"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year
COOKIE_PATH = "/"


class SessionMiddleware(BaseHTTPMiddleware):
    """Middleware that handles server-side session management.

    On each request:
    1. Reads session_id from cookie
    2. Looks up session in database
    3. Refreshes token if expired
    4. Attaches user_id to request.state
    5. Updates last_seen_at

    If session is invalid or token refresh fails, clears the cookie.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process the request, handling session lookup and token refresh."""
        # Initialize request state
        request.state.user_id = None
        request.state.session_id = None

        # Get session ID from cookie
        session_id = request.cookies.get(COOKIE_NAME)

        if session_id:
            await self._process_session(request, session_id)

        # Process the request
        response = await call_next(request)

        # If session was invalidated during processing, clear the cookie
        if hasattr(request.state, "_clear_session") and request.state._clear_session:
            response.delete_cookie(
                COOKIE_NAME,
                path=COOKIE_PATH,
                secure=not config.DEV_MODE,
                httponly=True,
                samesite="lax"
            )

        return response

    async def _process_session(self, request: Request, session_id: str) -> None:
        """Look up session and refresh token if needed.

        Args:
            request: The incoming request.
            session_id: The session UUID from the cookie.
        """
        bot = request.app.state.bot
        session = await bot.db_manager.get_session(session_id)

        if not session:
            # Session not in database - cookie is stale
            logger.debug(f"Session {session_id[:8]}... not found in database")
            request.state._clear_session = True
            return

        user_id = session["user_id"]
        token_expires_at = session["token_expires_at"]

        # Check if token needs refresh
        if time.time() > token_expires_at:
            try:
                await self._refresh_session_token(request, session)
            except TokenRevokedException:
                # User revoked access - delete session
                logger.info(f"Session {session_id[:8]}... revoked by user, deleting")
                await bot.db_manager.delete_session(session_id)
                request.state._clear_session = True
                return
            except Exception as e:
                # Refresh failed but not revoked - let request proceed with stale token
                # The actual API call might still work, or will fail gracefully
                logger.warning(f"Token refresh failed for session {session_id[:8]}...: {e}")

        # Session is valid - attach to request
        request.state.user_id = user_id
        request.state.session_id = session_id

        # Update last_seen_at (fire and forget - don't block request)
        try:
            await bot.db_manager.update_session_last_seen(session_id)
        except Exception as e:
            logger.warning(f"Failed to update last_seen_at: {e}")

    async def _refresh_session_token(self, request: Request, session: dict) -> None:
        """Refresh the Discord token for a session.

        Args:
            request: The incoming request.
            session: The session data from database.

        Raises:
            TokenRevokedException: If the refresh token is invalid/revoked.
            Exception: If refresh fails for other reasons.
        """
        bot = request.app.state.bot
        session_id = session["session_id"]
        refresh_token = session["refresh_token"]

        logger.debug(f"Refreshing token for session {session_id[:8]}...")

        new_access, new_refresh, expires_in = await refresh_discord_token(refresh_token)
        new_expires_at = int(time.time()) + expires_in

        await bot.db_manager.update_session_tokens(
            session_id,
            new_access,
            new_refresh,
            new_expires_at
        )

        logger.info(f"Token refreshed for session {session_id[:8]}...")


def generate_session_id() -> str:
    """Generate a new UUID v4 session ID.

    Returns:
        A UUID v4 string suitable for use as a session identifier.
    """
    return str(uuid.uuid4())


def set_session_cookie(response: Response, session_id: str) -> None:
    """Set the session cookie on a response.

    Args:
        response: The response to add the cookie to.
        session_id: The session UUID to store in the cookie.
    """
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=COOKIE_MAX_AGE,
        path=COOKIE_PATH,
        secure=not config.DEV_MODE,
        httponly=True,
        samesite="lax"
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the session cookie from a response.

    Args:
        response: The response to clear the cookie from.
    """
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        secure=not config.DEV_MODE,
        httponly=True,
        samesite="lax"
    )

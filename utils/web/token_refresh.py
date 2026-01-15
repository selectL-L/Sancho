"""Discord OAuth2 token refresh utilities.

This module handles refreshing Discord access tokens using refresh tokens.
When an access token expires, we can use the refresh token to get a new pair
without requiring the user to re-authenticate.

Typical Discord token lifetimes:
- Access token: 604,800 seconds (7 days)
- Refresh token: Does not expire, but can be revoked by user
"""

import logging

import aiohttp

import config

logger = logging.getLogger(__name__)

# Discord OAuth2 endpoints
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"


class TokenRevokedException(Exception):
    """Raised when Discord returns invalid_grant (user revoked access or token corrupted)."""


class TokenRefreshException(Exception):
    """Raised when token refresh fails for a non-revocation reason."""


async def refresh_discord_token(refresh_token: str) -> tuple[str, str, int]:
    """Refresh a Discord access token using a refresh token.

    Calls Discord's token endpoint with grant_type=refresh_token to obtain
    a new access token and refresh token pair.

    Args:
        refresh_token: The current refresh token.

    Returns:
        Tuple of (new_access_token, new_refresh_token, expires_in_seconds).

    Raises:
        TokenRevokedException: If Discord returns invalid_grant (user revoked access).
        TokenRefreshException: If the refresh fails for any other reason.
    """
    token_data = {
        "client_id": config.OAUTH_CLIENT_ID,
        "client_secret": config.OAUTH_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DISCORD_TOKEN_URL,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body = await resp.json()

                if resp.status == 400:
                    error = body.get("error", "")
                    if error == "invalid_grant":
                        logger.warning("Token refresh failed: invalid_grant (user revoked access or token expired)")
                        raise TokenRevokedException("Discord refresh token is invalid or revoked")
                    else:
                        logger.error(f"Token refresh failed with 400: {body}")
                        raise TokenRefreshException(f"Discord returned error: {error}")

                if resp.status != 200:
                    logger.error(f"Token refresh failed: {resp.status} - {body}")
                    raise TokenRefreshException(f"Discord returned status {resp.status}")

                access_token = body.get("access_token")
                new_refresh_token = body.get("refresh_token")
                expires_in = body.get("expires_in", 604800)  # Default 7 days

                if not access_token or not new_refresh_token:
                    logger.error(f"Token refresh response missing tokens: {body}")
                    raise TokenRefreshException("Missing tokens in response")

                logger.debug("Successfully refreshed Discord token")
                return access_token, new_refresh_token, expires_in

    except aiohttp.ClientError as e:
        logger.error(f"Network error during token refresh: {e}")
        raise TokenRefreshException(f"Network error: {e}") from e

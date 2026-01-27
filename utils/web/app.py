"""FastAPI application factory for the schedule web server.

This module creates and configures the FastAPI application with:
- Static file serving for the web UI assets
- Server-side session management (tokens stored in database)
- Rate limiting (60 requests/minute per IP)
- CORS blocking (same-origin only)
- OAuth2 routes for Discord authentication
- API routes for availability, guilds, and blacklist management

The app shares the event loop with the Discord bot when run via Uvicorn.
"""

import logging
import os
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from utils.web.auth import router as auth_router
from utils.web.routes import router as api_router
from utils.web.session_middleware import SessionMiddleware as ServerSessionMiddleware

if TYPE_CHECKING:
    from utils.bot_class import CoreBot

logger = logging.getLogger(__name__)

# Rate limiting storage: IP -> list of request timestamps
_rate_limit_storage: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_REQUESTS = 60  # requests per window
RATE_LIMIT_WINDOW = 60  # seconds


def create_app(bot: "CoreBot") -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        bot: The Discord bot instance, used for database access and
             Discord API interactions.

    Returns:
        Configured FastAPI application ready to be run with Uvicorn.

    Raises:
        ValueError: If WEB_SESSION_SECRET is not configured.
    """
    if not config.WEB_SESSION_SECRET:
        raise ValueError("WEB_SESSION_SECRET must be configured for web server")

    app = FastAPI(
        title=f"{config.BOT_NAME} Schedule",
        docs_url=None,  # Disable Swagger UI
        redoc_url=None,  # Disable ReDoc
        openapi_url=None,  # Disable OpenAPI schema
    )

    # Store bot reference in app state for route access
    app.state.bot = bot

    # Rate limiting middleware
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        """Limit requests to 60/minute per IP."""
        # Skip rate limiting for static files
        if not request.url.path.startswith(("/api/", "/auth/")):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean old timestamps and add current
        _rate_limit_storage[client_ip] = [
            ts for ts in _rate_limit_storage[client_ip]
            if now - ts < RATE_LIMIT_WINDOW
        ]

        if len(_rate_limit_storage[client_ip]) >= RATE_LIMIT_REQUESTS:
            logger.warning(f"Rate limited {client_ip}: {request.url.path}")
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests. Please slow down."},
            )

        _rate_limit_storage[client_ip].append(now)
        return await call_next(request)

    # CORS blocking middleware - reject cross-origin requests
    @app.middleware("http")
    async def cors_blocking_middleware(request: Request, call_next):
        """Block cross-origin requests to API endpoints."""
        origin = request.headers.get("origin")

        if origin and request.url.path.startswith(("/api/", "/auth/")):
            # Check if origin matches our host
            host = request.headers.get("host", "")
            # Origin format: "https://example.com" or "http://localhost:8080"
            # Extract host from origin
            origin_host = origin.split("://")[-1].rstrip("/")

            if origin_host != host:
                logger.warning(f"Blocked cross-origin request from {origin} to {host}")
                return JSONResponse(
                    status_code=403,
                    content={"error": "Cross-origin requests not allowed"},
                )

        return await call_next(request)

    # Server-side session middleware
    # Stores tokens in database, only session_id in cookie
    app.add_middleware(ServerSessionMiddleware)

    # API routes
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(api_router, prefix="/api", tags=["api"])

    # ============================================================================
    # Web UI serving
    # ============================================================================
    from fastapi.responses import FileResponse

    # Base path for web assets
    web_assets_dir = os.path.join(config.ASSETS_PATH, "web")

    # Serve web-core JS modules
    @app.get("/web-core/{file_path:path}")
    async def serve_web_core(file_path: str):
        """Serve web-core module files."""
        full_path = os.path.join(web_assets_dir, "web-core", file_path)
        if os.path.exists(full_path) and full_path.endswith('.js'):
            return FileResponse(full_path, media_type="application/javascript")
        return JSONResponse(status_code=404, content={"error": "Not found"})

    # Serve web-css stylesheets
    @app.get("/web-css/{file_path:path}")
    async def serve_web_css(file_path: str):
        """Serve web-css stylesheet files."""
        full_path = os.path.join(web_assets_dir, "web-css", file_path)
        if os.path.exists(full_path) and full_path.endswith('.css'):
            return FileResponse(full_path, media_type="text/css")
        return JSONResponse(status_code=404, content={"error": "Not found"})

    # ============================================================================
    # Web UI Routing - UA-based desktop/mobile detection
    # ============================================================================

    def is_mobile_request(request: Request) -> bool:
        """Check if request is from a mobile device based on User-Agent."""
        user_agent = request.headers.get("user-agent", "").lower()
        mobile_keywords = ["mobile", "android", "iphone", "ipad", "ipod", "blackberry", "windows phone"]
        return any(keyword in user_agent for keyword in mobile_keywords)

    @app.get("/")
    async def serve_root():
        """Redirect root to /index for future homepage flexibility."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/index", status_code=302)

    @app.get("/index")
    @app.get("/index.html")
    async def serve_index(request: Request):
        """Serve appropriate index.html based on device type."""
        if is_mobile_request(request):
            return FileResponse(
                os.path.join(web_assets_dir, "mobile", "index.html"),
                media_type="text/html"
            )
        return FileResponse(
            os.path.join(web_assets_dir, "desktop", "index.html"),
            media_type="text/html"
        )

    @app.get("/settings")
    @app.get("/settings.html")
    async def serve_settings(request: Request):
        """Serve appropriate settings.html based on device type."""
        if is_mobile_request(request):
            return FileResponse(
                os.path.join(web_assets_dir, "mobile", "settings.html"),
                media_type="text/html"
            )
        return FileResponse(
            os.path.join(web_assets_dir, "desktop", "settings.html"),
            media_type="text/html"
        )

    # ============================================================================
    # END Web UI serving
    # ============================================================================

    # Static files (web UI) - mounted last so API routes take precedence
    app.mount("/", StaticFiles(directory=web_assets_dir, html=True), name="static")

    @app.on_event("startup")
    async def on_startup() -> None:
        """Log server startup and clean stale sessions."""
        logger.info(f"Web server started on {config.WEB_HOST}:{config.WEB_PORT}")
        # Clean up sessions that haven't been used in 90 days
        try:
            deleted = await bot.db_manager.cleanup_stale_sessions(max_age_days=90)  # type: ignore[union-attr]
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} stale web sessions")
        except Exception as e:
            logger.warning(f"Failed to clean stale sessions: {e}")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        """Log server shutdown."""
        logger.info("Web server shutting down")

    return app

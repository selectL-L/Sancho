"""Cog for managing the web server lifecycle.

This cog owns the Uvicorn web server that serves all web features
(schedule UI, limbus calculator, etc.). It starts the server in
cog_ready() and stops it gracefully in cog_unload().

The web server runs in-process, sharing the event loop with the Discord bot.
"""

import asyncio
from typing import TYPE_CHECKING, Any, Optional

import config
from utils.base_cog import BaseCog

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


class Web(BaseCog):
    """Web server lifecycle management.

    Attributes:
        _server_task: The asyncio task running Uvicorn.
    """

    def __init__(self, bot: "CoreBot") -> None:
        super().__init__(bot)
        self._server_task: Optional[asyncio.Task[None]] = None
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

        self._server_task = asyncio.create_task(
            self._run_web_server(),
            name="web-server"
        )

    async def cog_unload(self) -> None:
        """Called when cog is unloaded. Stop the web server."""
        await self._stop_web_server()

    async def _run_web_server(self) -> None:
        """Run the Uvicorn web server.

        This runs in-process, sharing the event loop with the Discord bot.
        The server serves static files and API endpoints for all web features.
        """
        try:
            import uvicorn
            from utils.web import create_app

            app = create_app(self.bot)

            uvicorn_config = uvicorn.Config(
                app=app,
                host=config.WEB_HOST,
                port=config.WEB_PORT,
                log_level="warning" if not config.DEV_MODE else "info",
                reload=False,
                access_log=config.DEV_MODE,
            )

            self._uvicorn_server = uvicorn.Server(uvicorn_config)

            self.logger.info(
                f"Starting web server on http://{config.WEB_HOST}:{config.WEB_PORT}"
            )

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
            self.logger.info("Web server task exited.")

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


async def setup(bot: "CoreBot") -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Web(bot))

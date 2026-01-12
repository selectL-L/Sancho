"""Web server module for the availability scheduler.

This module provides a FastAPI-based web server for the schedule feature,
allowing users to manage their availability via a web UI with Discord OAuth2.
"""

from utils.web.app import create_app

__all__ = ['create_app']

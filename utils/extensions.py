"""utils/extensions.py

This module provides utility functions for discovering and managing bot extensions (cogs).
Placing this logic here separates it from the main configuration file, improving
the separation of concerns.
"""

import os
from typing import List


def discover_cogs(cogs_path: str) -> List[str]:
    """Scans the `cogs` directory and returns a list of all valid cog modules.

    This allows for dynamic loading of cogs without having to manually list them.

    Args:
        cogs_path (str): The path to the cogs directory.

    Returns:
        List[str]: A list of cog module names (e.g., 'cogs.math').
    """
    cogs = []
    if not os.path.exists(cogs_path):
        return cogs
        
    for filename in os.listdir(cogs_path):
        # Ensure the file is a Python file and not a special file like __init__.py
        if filename.endswith('.py') and not filename.startswith('__'):
            cogs.append(f'cogs.{filename[:-3]}')
    return cogs

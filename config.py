"""
Centralized configuration for the Sancho bot.
All paths, constants, and other settings should be defined here.
"""
import os
import sys
import logging

# --- Pathing ---

def get_application_path() -> str:
    """Returns the base path for the application, whether running from source or bundled."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running as a bundled executable
        return os.path.dirname(sys.executable)
    # Running as a script
    return os.path.dirname(os.path.abspath(__file__))

def discover_cogs(cogs_path: str) -> list[str]:
    """Scans the cogs directory and returns a list of cog module names."""
    cogs = []
    for filename in os.listdir(cogs_path):
        if filename.endswith('.py') and not filename.startswith('__'):
            cogs.append(f'cogs.{filename[:-3]}')
    return cogs

# --- Core Paths ---
APP_PATH = get_application_path()
ENV_PATH = os.path.join(APP_PATH, 'info.env')
LOG_PATH = os.path.join(APP_PATH, 'sancho.log')
DB_PATH = os.path.join(APP_PATH, 'reminders.db')
COGS_PATH = os.path.join(APP_PATH, 'cogs')

# --- Bot Configuration ---
BOT_PREFIX = ".sancho "

# Dynamically discover cogs and create a static list for the application to use.
# This list is used by both main.py (at runtime) and build.py (at build time).
try:
    COGS_TO_LOAD = discover_cogs(COGS_PATH)
except FileNotFoundError:
    # This handles the case where the script is run from a location where the cogs/ dir isn't present
    # (like the PyInstaller executable context), preventing a crash.
    # The build script ensures the necessary modules are frozen anyway, so we can use a placeholder.
    COGS_TO_LOAD = [
        'cogs.math',
        'cogs.reminders',
        'cogs.image',
        'cogs.fun',
        'cogs.skills'
    ]

# --- Logging Configuration ---
LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 5

# --- NLP Command Registry ---
# To add a new command, add a tuple to this list.
# Format: ( (keywords), 'CogClassName', 'method_name' )
NLP_COMMANDS: list[tuple[tuple[str, ...], str, str]] = [
    # Dice rolling (should be checked before basic calculation)
    ((r'\broll\b', r'\bdice\b', r'd\d'), 'Math', 'roll'),

    # Basic calculation
    ((r'\bcalculate\b', r'\bcalc\b', r'\bcompute\b', r'\bevaluate\b'), 'Math', 'calculate'),
    
    # --- Skill Commands ---
    ((r'\b(delete|remove)\s.*skill\b',), 'Skills', 'delete_skill_nlp'),
    # This pattern now handles "skill list", "list skills", and "skilllist"
    ((r'\b(list|show|check)\s.*skill(s)?\b', r'\bskill\s.*(list|show|check)\b', r'\bskilllist\b'), 'Skills', 'list_skills_nlp'),
    # This should be checked before more general "skill" queries.
    ((r'\bsave\s.*skill\b', r'\bskill\s.*save\b', r'create.*skill'), 'Skills', 'save_skill_nlp'),
    # This is the general "use skill" command, and should be last.
    ((r'\bskill\b',), 'Skills', 'use_skill_nlp'),

    # Deleting reminders (catches "delete/remove reminder 1", etc.)
    # This should be checked BEFORE setting reminders, to avoid conflict on the word "remind"
    ((r'\b(delete|remove)\b.*\breminder',), 'Reminders', 'delete_reminders_nlp'),

    # Setting reminders
    ((r'\bremind\b', r'\breminder\b', r'\bremember\b'), 'Reminders', 'remind'),

    # Checking reminders (catches "check my reminders", "show reminders", etc.)
    ((r'\b(check|show|list)\b.*\breminders\b', r'what are my reminders'), 'Reminders', 'check_reminders_nlp'),

    # --- Image Commands ---
    # Resize image
    ((r'\bresize\b', r'\bscale\b'), 'Image', 'resize'),

    # Convert image format
    ((r'\bconvert\b', r'\bchange to\b'), 'Image', 'convert'),

    # --- Fun Commands ---
    # 8-Ball
    ((r'8\s?-?ball',), 'Fun', 'eight_ball'),
]

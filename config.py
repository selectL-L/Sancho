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
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# --- Core Paths ---
APP_PATH = get_application_path()
ENV_PATH = os.path.join(APP_PATH, 'info.env')
LOG_PATH = os.path.join(APP_PATH, 'sancho.log')
DB_PATH = os.path.join(APP_PATH, 'reminders.db')
COGS_PATH = os.path.join(APP_PATH, 'cogs')

# --- Bot Configuration ---
BOT_PREFIX = ".sancho "

# --- Logging Configuration ---
LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 5

# --- NLP Command Registry ---
# To add a new command, add a tuple to this list.
# Format: ( (keywords), 'CogClassName', 'method_name' )
NLP_COMMANDS: list[tuple[tuple[str, ...], str, str]] = [
    # Dice rolling
    ((r'\broll\b', r'\bdice\b'), 'DiceRoller', 'roll'),
    
    # Deleting reminders (catches "delete/remove reminder 1", etc.)
    # This should be checked BEFORE setting reminders, to avoid conflict on the word "remind"
    ((r'\b(delete|remove)\b.*\breminder',), 'Reminders', 'delete_reminders_nlp'),

    # Setting reminders
    ((r'\bremind\b', r'\breminder\b', r'\bremember\b'), 'Reminders', 'remind'),

    # Checking reminders (catches "check my reminders", "show reminders", etc.)
    ((r'\b(check|show|list)\b.*\breminders\b', r'what are my reminders'), 'Reminders', 'check_reminders_nlp'),
]

"""
config.py

This module centralizes all configuration settings for the Sancho bot.
It handles path definitions, loading environment variables (like the bot token),
and defining static configurations such as the NLP command registry.
"""
import os
import sys
import logging
from dotenv import load_dotenv

# --- Pathing ---

def get_application_path() -> str:
    """
    Determines the base path for the application. This is crucial for ensuring
    that file paths work correctly whether the application is running from source
    or as a bundled executable (e.g., via PyInstaller).
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running as a bundled executable
        return os.path.dirname(sys.executable)
    # Running as a script from source
    return os.path.dirname(os.path.abspath(__file__))

def discover_cogs(cogs_path: str) -> list[str]:
    """
    Scans the `cogs` directory and returns a list of all valid cog modules
    (e.g., 'cogs.math', 'cogs.reminders'). This allows for dynamic loading
    of cogs without having to manually list them.
    """
    cogs = []
    for filename in os.listdir(cogs_path):
        # Ensure the file is a Python file and not a special file like __init__.py
        if filename.endswith('.py') and not filename.startswith('__'):
            cogs.append(f'cogs.{filename[:-3]}')
    return cogs

# --- Core Paths ---
# Define all essential paths based on the application's root directory.
APP_PATH = get_application_path()
ASSETS_PATH = os.path.join(APP_PATH, 'assets')
ENV_PATH = os.path.join(APP_PATH, 'info.env')
LOG_PATH = os.path.join(APP_PATH, 'sancho.log')
DB_PATH = os.path.join(ASSETS_PATH, 'sanchobase.db')
COGS_PATH = os.path.join(APP_PATH, 'cogs')
STARTUP_GIF_PATH = os.path.join(ASSETS_PATH, 'startup.gif')
SHUTDOWN_GIF_PATH = os.path.join(ASSETS_PATH, 'shutdown.gif')

# --- Bot Configuration ---

def check_and_create_env_file():
    """
    Checks for the existence of the `info.env` file. If it doesn't exist,
    it creates a template file and exits the application with instructions
    for the user. This ensures the bot isn't run without a configuration file.
    """
    if not os.path.exists(ENV_PATH):
        logging.warning(f"'{os.path.basename(ENV_PATH)}' not found. Creating a new one.")
        with open(ENV_PATH, 'w') as f:
            f.write("DISCORD_TOKEN=\n")
            f.write("OWNER_ID=\n")
        # This message is critical for the user to see on the first run.
        print(f"'{os.path.basename(ENV_PATH)}' was not found.")
        print(f"A new one has been created at: {ENV_PATH}")
        print("\nPlease open this file and add your bot's DISCORD_TOKEN.")
        print("The OWNER_ID is optional but recommended.")
        sys.exit("Exiting: Bot token not configured.")

# Check for and/or create the .env file before trying to load from it.
check_and_create_env_file()

# Load the environment variables from the .env file.
load_dotenv(dotenv_path=ENV_PATH)

# --- Environment Variables ---
TOKEN = os.getenv('DISCORD_TOKEN')
raw_owner_id = os.getenv('OWNER_ID')
raw_startup_channel_id = os.getenv('STARTUP_CHANNEL_ID')
raw_shutdown_channel_id = os.getenv('SHUTDOWN_CHANNEL_ID')
OWNER_ID = int(raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else None
STARTUP_CHANNEL_ID = int(raw_startup_channel_id) if raw_startup_channel_id and raw_startup_channel_id.isdigit() else None
SHUTDOWN_CHANNEL_ID = int(raw_shutdown_channel_id) if raw_shutdown_channel_id and raw_shutdown_channel_id.isdigit() else None
BOT_PREFIX = [".sancho ", ".s "]

# --- Cog Loading ---
# Dynamically discover all cogs to be loaded at runtime.
# This list is also used by the build script to ensure all cogs are included.
try:
    COGS_TO_LOAD = discover_cogs(COGS_PATH)
except FileNotFoundError:
    # This fallback is for when the script is run in a context where the `cogs`
    # directory isn't present (like in a bundled executable). The build script
    # freezes the necessary modules, so this list acts as a placeholder.
    COGS_TO_LOAD = [
        'cogs.math',
        'cogs.reminders',
        'cogs.image',
        'cogs.fun',
        'cogs.skills'
    ]

# --- Logging Configuration ---
# These are default values that can be used by the logging setup function.
LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 5

# --- NLP Command Registry ---
# This is the central registry for all NLP-based commands. The `on_message`
# event in `main.py` iterates through this list to find a matching command.
# The order is important: more specific patterns should come before general ones.
#
# Format: ( (tuple_of_regex_keywords), 'CogClassName', 'method_to_call' )
NLP_COMMANDS: list[tuple[tuple[str, ...], str, str]] = [

    # --- Math Commands ---
    # Limbus Company coin flip
    ((r'\blimbus\b', r'\bcoin\s.*flip\b'), 'Math', 'limbus_roll_nlp'),
    # Dice rolling (should be checked before basic calculation)
    ((r'\broll\b', r'\bdice\b', r'd\d'), 'Math', 'roll'),
    # Basic calculation
    ((r'\bcalculate\b', r'\bcalc\b', r'\bcompute\b', r'\bevaluate\b'), 'Math', 'calculate'),
    
    # --- Skill Commands ---
    # Specific "delete" command that must come before the general "skill" command.
    ((r'\b(delete|remove)\s.*skill\b',), 'Skills', 'delete_skill_nlp'),
    # Specific "list" command.
    ((r'\b(list|show|check)\s.*skill(s)?\b', r'\bskill\s.*(list|show|check)\b', r'\bskilllist\b'), 'Skills', 'list_skills_nlp'),
    # Specific "save" command.
    ((r'\bsave\s.*skill\b', r'\bskill\s.*save\b', r'create.*skill'), 'Skills', 'save_skill_nlp'),
    # General "use skill" command, should be last in this group.
    ((r'\bskill\b',), 'Skills', 'use_skill_nlp'),

    # --- Reminder Commands ---
    # Deleting reminders (catches "delete/remove reminder 1", etc.)
    # This should be checked BEFORE setting reminders, to avoid conflict on the word "remind"
    ((r'\b(delete|remove)\b.*\breminder',), 'Reminders', 'delete_reminders_nlp'),
    # Setting reminders
    ((r'\bremind\b', r'\breminder\b', r'\bremember\b', r'set\s+a\s+reminder', r'set\s.*reminder'), 'Reminders', 'remind'),
    # Checking reminders (catches "check my reminders", "show reminders", etc.)
    ((r'\b(check|show|list)\b.*\breminders\b', r'what are my reminders', r'^\s*reminders\s*$'), 'Reminders', 'check_reminders_nlp'),
    # Setting user timezone
    ((r'\b(set|change)\s.*timezone\b', r'\b(set|change)\s.*tz\b', r'\btz\b'), 'Reminders', 'set_timezone_nlp'),

    # --- Image Commands ---
    # Resize image
    ((r'\bresize\b', r'\bscale\b'), 'Image', 'resize'),

    # Convert image format
    ((r'\bconvert\b', r'\bchange to\b'), 'Image', 'convert'),

    # --- Fun Commands ---
    # 8-Ball
    ((r'8\s?-?ball',), 'Fun', 'eight_ball'),
    # BOD
    ((r'\bbod\b',), 'Fun', 'bod'),
    # Sanitize
    ((r'\bsanitize\b', r'\bsanitise\b'), 'Fun', 'sanitize'),
]

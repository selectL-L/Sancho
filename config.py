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
from utils.extensions import discover_cogs

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

# --- Core Paths ---
# Define all essential paths based on the application's root directory.
APP_PATH = get_application_path()
ASSETS_PATH = os.path.join(APP_PATH, 'assets')
ENV_PATH = os.path.join(APP_PATH, 'info.env')
LOG_PATH = os.path.join(APP_PATH, 'sancho.log')
DB_PATH = os.path.join(ASSETS_PATH, 'sanchobase.db')
COGS_PATH = os.path.join(APP_PATH, 'cogs')

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
            f.write("# Discord Token for bot start up.\n")
            f.write("DISCORD_TOKEN=\n\n")
            f.write("# Bot Prefixes, ensure they're seperated with commas.\n")
            f.write("BOT_PREFIX=\n\n")
            f.write("# (Optional) Owner ID for owner specific commands.\n")
            f.write("OWNER_ID=\n\n")
            f.write("# (Optional) Channel ID for system messages.\n")
            f.write("SYSTEM_CHANNEL_ID=\n\n")
            f.write("# (Optional) Enable developer mode (bot only responds to OWNER_ID). Can be True or False.\n")
            f.write("DEV_MODE=False\n")
        # This message is critical for the user to see on the first run.
        print(f"'{os.path.basename(ENV_PATH)}' was not found.")
        print(f"A new one has been created at: {ENV_PATH}")
        print("\nPlease open this file and add your bot's DISCORD_TOKEN and BOT_PREFIX.")
        print("The OWNER_ID is optional but recommended.")
        sys.exit("Exiting: Bot token and prefix not configured.")

# Check for and/or create the .env file before trying to load from it.
check_and_create_env_file()

# Load the environment variables from the .env file.
load_dotenv(dotenv_path=ENV_PATH)

# --- Environment Variables ---
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_PREFIX_RAW = os.getenv('BOT_PREFIX')

if not TOKEN or not BOT_PREFIX_RAW:
    print("DISCORD_TOKEN and BOT_PREFIX must be set in info.env.")
    sys.exit("Exiting: Missing required configuration.")

# Sort prefixes by length descending to ensure longer prefixes are matched first
# (e.g., '.mayors' before '.m') and add a trailing space to act as a delimiter.
BOT_PREFIX = sorted([p.strip() + ' ' for p in BOT_PREFIX_RAW.split(',')], key=len, reverse=True)

raw_owner_id = os.getenv('OWNER_ID')
raw_system_channel_id = os.getenv('SYSTEM_CHANNEL_ID')
OWNER_ID = int(raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else None
SYSTEM_CHANNEL_ID = int(raw_system_channel_id) if raw_system_channel_id and raw_system_channel_id.isdigit() else None

raw_dev_mode = os.getenv('DEV_MODE', 'False')
DEV_MODE = raw_dev_mode.lower() in ('true', '1', 't')

# --- Logging Configuration ---
# These are default values that can be used by the logging setup function.
LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 5

# --- NLP Command Registry ---
# This is the central registry for all NLP-based commands. It is structured
# as a list of "groups" (each group is a list of commands).
#
# 1.  **Intra-Group Priority**: Within each group, the commands are checked in
#     the order they are defined. The first one that matches becomes the "group winner".
#     This means more specific commands should always be placed before more general ones
#     (e.g., 'delete skill' before 'skill').
# 2.  **Inter-Group Priority**: After finding a winner from each group that has a match,
#     the bot compares the position of the matched keywords in the user's query.
#     The group winner whose keyword appeared earliest in the query is the final command executed.
#
# Format: [
#   [ ( (keywords), 'Cog', 'method'), ... ],  # Group 1
#   [ ( (keywords), 'Cog', 'method'), ... ],  # Group 2
# ]
NLP_COMMANDS: list[list[tuple[tuple[str, ...], str, str]]] = [
    # --- Math Group ---
    [
        # Limbus Company coin flip
        ((r'\blimbus\b', r'\bcoin\s.*flip\b'), 'Math', 'limbus_roll_nlp'),
        # Dice rolling (should be checked before basic calculation)
        ((r'\broll\b', r'\bdice\b'), 'Math', 'roll'),
        # Basic calculation
        ((r'\bcalculate\b', r'\bcalc\b', r'\bcompute\b', r'\bevaluate\b'), 'Math', 'calculate'),
    ],
    # --- Skills Group ---
    [
        # Management commands are checked first for specific verb-noun phrases.
        ((r'\b(delete|remove)\s.*skill(s)?\b',), 'Skills', 'delete_skill_nlp'),
        ((r'\b(edit|change|update)\s.*skill(s)?\b',), 'Skills', 'edit_skill_nlp'),
        ((r'\b(list|check|show)\s.*skill(s)?\b', r'^\s*skills\s*$'), 'Skills', 'list_skills_nlp'),
        ((r'\b(save|create|make)\s.*skill\b',), 'Skills', 'save_skill_nlp'),
        
        # Commands for casting or using skills.
        ((r'\bcast\b', r'\bskill\b', r'\buse\b'), 'Skills', 'use_skill_nlp'),
    ],
    # --- Reminders Group --- (note: unlike other groups, this one ENFORCES matching at the front to prevent polluting the query)
    [
        # Deleting reminders (catches "delete/remove reminder 1", etc.)
        # This should be checked BEFORE setting reminders, to avoid conflict on the word "remind"
        ((r'^\s*(delete|remove)\b.*\breminder',), 'Reminders', 'delete_reminders_nlp'),
        # Setting reminders
        ((r'^\s*(remind|reminder|remember|set\s+a\s+reminder|set\s.*reminder)\b',), 'Reminders', 'remind'),
        # Checking reminders (catches "check my reminders", "show reminders", etc.)
        ((r'^\s*(check|show|list)\b.*\breminders\b', r'what are my reminders', r'^\s*reminders\s*$'), 'Reminders', 'check_reminders_nlp'),
        # Setting user timezone
        ((r'^\s*(set|change)\s.*timezone\b', r'^\s*(set|change)\s.*tz\b', r'^\s*timezone\b', r'^\s*tz\b'), 'Reminders', 'set_timezone_nlp'),
    ],
    # --- Image Group ---
    [
        # Resize image
        ((r'\bresize\b', r'\bscale\b'), 'ImageCog', 'resize'),
        # Convert image format
        ((r'\bconvert\b', r'\bchange to\b'), 'ImageCog', 'convert'),
    ],
    # --- Fun Group ---
    [
        # 8-Ball
        ((r'8\s?-?ball',), 'Fun', 'eight_ball'),
        # BOD Leaderboard (must be checked before the general 'bod' command)
        ((r'\bbod\s.*(leaderboard|lb|scores|ranks)\b',), 'Fun', 'bod_leaderboard'),
        # BOD
        ((r'\bbod\b',), 'Fun', 'bod'),
        # Sanitize
        ((r'\bsanitize\b', r'\bsanitise\b'), 'Fun', 'sanitize'),
        # Issues
        ((r'\bissues\b', r'\bissue\b'), 'Fun', 'issues'),
    ]
]

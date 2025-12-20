"""config.py

This module centralizes all configuration settings for the bot.
It handles path definitions, loading environment variables (like the bot token),
and defining static configurations such as the NLP command registry.
"""

import logging
import os
import sys
from typing import List, Tuple

from dotenv import dotenv_values, load_dotenv

# Pathing


def get_application_path() -> str:
    """Determines the base path for the application.

    This is crucial for ensuring that file paths work correctly whether the
    application is running from source or as a bundled executable (e.g., via
    PyInstaller).

    Returns:
        str: The absolute path to the application's root directory.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running as a bundled executable
        return os.path.dirname(sys.executable)
    # Running as a script from source
    return os.path.dirname(os.path.abspath(__file__))


def get_internal_path() -> str:
    """Determines the internal path for bundled resources (like cogs).

    When frozen, this points to the temporary directory where PyInstaller
    extracts the bundled files. When running from source, it's the same
    as the application path.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS  # type: ignore
    return os.path.dirname(os.path.abspath(__file__))


# Core Paths
# Define all essential paths based on the application's root directory.
APP_PATH = get_application_path()
INTERNAL_PATH = get_internal_path()

ASSETS_PATH = os.path.join(APP_PATH, 'assets')
ENV_PATH = os.path.join(APP_PATH, 'info.env')


# Bot Configuration


def check_and_create_env_file() -> None:
    """Checks for the existence of the `info.env` file.

    If it doesn't exist, it creates a template file. If it exists, it checks
    for missing fields and updates the file if necessary by recreating it with
    preserved data.

    Raises:
        SystemExit: If the file cannot be created or updated.
    """
    required_fields = {
        "DISCORD_TOKEN": "",
        "BOT_PREFIX": "",
        "OWNER_ID": "",
        "SYSTEM_CHANNEL_ID": "",
        "DEV_MODE": "False",
        "DEV_GUILD": "",
        "BOT_NAME": "NoName",
        "CONTROL_PORT": "",
        "AMBIENCE_ENABLED": "True"
    }

    field_comments = {
        "DISCORD_TOKEN": "# Discord Token for bot start up.",
        "BOT_PREFIX": "# Bot Prefixes, ensure they're seperated with commas.",
        "OWNER_ID": "# (Optional) Owner ID for owner specific commands.",
        "SYSTEM_CHANNEL_ID": "# (Optional) Channel ID for system messages.",
        "DEV_MODE": "# (Optional) Enable developer mode (bot only responds to OWNER_ID). Can be True or False.",
        "DEV_GUILD": "# (Optional) Guild ID for testing app commands when DEV_MODE is True.",
        "BOT_NAME": "# The name the bot calls itself in user-facing strings.",
        "CONTROL_PORT": "# (Optional) TCP port for remote control commands (e.g., 9999). Binds to localhost only.",
        "AMBIENCE_ENABLED": "# (Optional) Enable ambience user-facing strings. True unless explicitly set to False."
    }

    if not os.path.exists(ENV_PATH):
        logging.warning(
            f"'{os.path.basename(ENV_PATH)}' not found. Creating a new one.")
        try:
            with open(ENV_PATH, 'w') as f:
                for key, default_val in required_fields.items():
                    f.write(f"{field_comments[key]}\n")
                    f.write(f"{key}={default_val}\n\n")
        except Exception as e:
            logging.critical(f"Failed to create {ENV_PATH}: {e}")
            sys.exit(f"Exiting: Failed to create {ENV_PATH}.")

        # This message is critical for the user to see on the first run.
        print(f"'{os.path.basename(ENV_PATH)}' was not found.")
        print(f"A new one has been created at: {ENV_PATH}")
        print("\nPlease open this file and add your bot's DISCORD_TOKEN and BOT_PREFIX.")
        print("The OWNER_ID is optional but recommended.")
        sys.exit("Exiting: Bot token and prefix not configured.")

    else:
        # Check for missing fields
        current_values = dotenv_values(ENV_PATH)
        missing_keys = [
            key for key in required_fields if key not in current_values]

        if missing_keys:
            logging.info(
                f"Updating {os.path.basename(ENV_PATH)} with missing keys: {missing_keys}")
            print(
                f"Updating {os.path.basename(ENV_PATH)} with new configuration fields...")

            # Prepare new content preserving existing values
            new_content = []
            for key in required_fields:
                value = current_values.get(key, required_fields[key])
                new_content.append(f"{field_comments[key]}\n")
                new_content.append(f"{key}={value}\n\n")

            try:
                os.remove(ENV_PATH)
                with open(ENV_PATH, 'w') as f:
                    f.writelines(new_content)
                print(f"Successfully updated {os.path.basename(ENV_PATH)}.")
            except Exception as e:
                logging.critical(
                    f"Failed to update {ENV_PATH}. Data preserved: {current_values}")
                print(f"CRITICAL ERROR: Failed to update {ENV_PATH}.")
                print(
                    "Your existing data has been logged. Please check the logs directory.")
                print(f"Error: {e}")
                sys.exit("Exiting: Failed to update configuration file.")


# Check for and/or create the .env file before trying to load from it.
check_and_create_env_file()

# Load the environment variables from the .env file.
load_dotenv(dotenv_path=ENV_PATH)

# Environment Variables
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_PREFIX_RAW = os.getenv('BOT_PREFIX')
BOT_NAME = os.getenv('BOT_NAME')

# Dependent Paths
# These paths depend on the BOT_NAME environment variable.
LOGS_DIR = os.path.join(APP_PATH, 'logs')
LOG_RETENTION_COUNT = 10  # Keep 10 completed logs + current
RESOURCE_TRACK_INTERVAL = 15  # Minutes between resource usage snapshots

# Database Path Discovery
# We scan for an existing .db file to use, regardless of its name.
# This strictly enforces a "Single Database" rule.
found_dbs = [f for f in os.listdir(ASSETS_PATH) if f.endswith('.db')]

if len(found_dbs) == 0:
    # No DB found (Fresh Install), create one with the bot's name.
    DB_PATH = os.path.join(ASSETS_PATH, f'{BOT_NAME}.db')
elif len(found_dbs) == 1:
    # Single DB found, use it.
    DB_PATH = os.path.join(ASSETS_PATH, found_dbs[0])
else:
    # Multiple DBs found, ambiguous state.
    print(
        f"CRITICAL ERROR: Multiple database files found in {ASSETS_PATH}: {found_dbs}")
    print("Please ensure only ONE .db file exists to prevent data fragmentation.")
    sys.exit("Exiting: Multiple databases found.")

COGS_PATH = os.path.join(INTERNAL_PATH, 'cogs')

if not TOKEN or not BOT_PREFIX_RAW or not BOT_NAME:
    print("DISCORD_TOKEN, BOT_PREFIX, and BOT_NAME must be set in info.env.")
    sys.exit("Exiting: Missing or invalid required configuration.")

if BOT_NAME == "NoName":
    print("WARNING: BOT_NAME is set to the default 'NoName'.")
    print("Please update 'info.env' with your bot's actual name.")

# Sort prefixes by length descending to ensure longer prefixes are matched first
# (e.g., '.mayors' before '.m') and add a trailing space to act as a delimiter.
BOT_PREFIX = sorted(
    [p.strip() + ' ' for p in BOT_PREFIX_RAW.split(',')], key=len, reverse=True)

raw_owner_id = os.getenv('OWNER_ID')
raw_system_channel_id = os.getenv('SYSTEM_CHANNEL_ID')
OWNER_ID = int(
    raw_owner_id) if raw_owner_id and raw_owner_id.isdigit() else None
SYSTEM_CHANNEL_ID = int(
    raw_system_channel_id) if raw_system_channel_id and raw_system_channel_id.isdigit() else None

raw_dev_mode = os.getenv('DEV_MODE', 'False')
DEV_MODE = raw_dev_mode.lower() in ('true', '1', 't')

raw_dev_guild = os.getenv('DEV_GUILD')
DEV_GUILD = int(
    raw_dev_guild) if raw_dev_guild and raw_dev_guild.isdigit() else None

raw_control_port = os.getenv('CONTROL_PORT')
CONTROL_PORT = int(
    raw_control_port) if raw_control_port and raw_control_port.isdigit() else None

# Ambience Configuration
# When disabled, moods/activities still cycle internally but user-facing strings
# (greetings, interrupts, etc.) are silenced. Music playlist selection is NOT affected.
raw_ambience_enabled = os.getenv('AMBIENCE_ENABLED', 'True')
AMBIENCE_ENABLED = raw_ambience_enabled.lower() in ('true', '1', 't')

# Music Cog Configuration
MUSIC_CACHE_PATH = os.path.join(APP_PATH, 'cache', 'music')

# Logging Configuration
# These are default values that can be used by the logging setup function.
LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 5

# NLP Command Registry
# This is the central registry for all NLP-based commands. It is structured
# as a list of "groups" (each group is a list of commands).
#
# Intra-Group Priority: Within each group, the commands are checked in
# the order they are defined. The first one that matches becomes the "group winner".
# This means more specific commands should always be placed before more general ones.
#
# Inter-Group Priority: After finding a winner from each group that has a match,
# the bot compares the position of the matched keywords in the user's query.
# The group winner whose keyword appeared earliest in the query is the final command executed.
#
# Format: [
#   [ ( (keywords), 'Cog', 'method'), ... ],  # Group 1
#   [ ( (keywords), 'Cog', 'method'), ... ],  # Group 2
# ]
NLP_COMMANDS: List[List[Tuple[Tuple[str, ...], str, str]]] = [
    # Math Group
    [
        # Limbus Company coin flip
        ((r'\blimbus\b', r'\bcoin\s.*flip\b'), 'Math', 'limbus_roll_nlp'),
        # Dice rolling (should be checked before basic calculation)
        ((r'\broll\b', r'\bdice\b'), 'Math', 'roll'),
        # Basic calculation
        ((r'\bcalculate\b', r'\bcalc\b', r'\bcompute\b',
         r'\bevaluate\b'), 'Math', 'calculate'),
    ],
    # Skills Group
    [
        # Management commands are checked first for specific verb-noun phrases.
        ((r'\b(delete|remove)\s.*skill(s)?\b',), 'Skills', 'delete_skill_nlp'),
        ((r'\b(edit|change|update)\s.*skill(s)?\b',), 'Skills', 'edit_skill_nlp'),
        ((r'\b(list|check|show)\s.*skill(s)?\b',
         r'^\s*skills\s*$'), 'Skills', 'list_skills_nlp'),
        ((r'\b(save|create|make)\s.*skill\b',), 'Skills', 'save_skill_nlp'),

        # Commands for casting or using skills.
        ((r'\bcast\b', r'\bskill\b', r'\buse\b'), 'Skills', 'use_skill_nlp'),
    ],
    # Reminders Group (note: unlike other groups, this one ENFORCES matching at the front to prevent polluting the query)
    [
        # Deleting reminders (catches "delete/remove reminder 1", etc.)
        # This should be checked BEFORE setting reminders, to avoid conflict on the word "remind"
        ((r'^\s*(delete|remove)\b.*\breminder',),
         'Reminders', 'delete_reminders_nlp'),
        # Editing reminders
        ((r'^\s*(edit|change|update)\b.*\breminder',),
         'Reminders', 'edit_reminder_nlp'),
        # Checking reminders (catches "check my reminders", "show reminders", etc.)
        ((r'^\s*(check|show|list)\b.*\breminders\b', r'what are my reminders',
         r'^\s*reminders\s*$'), 'Reminders', 'check_reminders_nlp'),
        # Setting user timezone
        ((r'^\s*(set|change)\s.*timezone\b', r'^\s*(set|change)\s.*tz\b',
         r'^\s*timezone\b', r'^\s*tz\b'), 'Reminders', 'set_timezone_nlp'),
        # Reminder Settings
        ((r'^\s*reminder\s+settings\b', r'^\s*reminders\s+settings\b'),
         'Reminders', 'reminder_settings_nlp'),
        # Setting reminders
        ((r'^\s*(remind|reminder|remember|set\s+a\s+reminder|set\s.*reminder)\b',),
         'Reminders', 'remind'),
    ],
    # Image Group
    [
        # Resize image
        ((r'\bresize\b', r'\bscale\b'), 'ImageCog', 'resize'),
        # Convert image format
        ((r'\bconvert\b', r'\bchange to\b'), 'ImageCog', 'convert'),
    ],
    # Fun Group
    [
        # 8-Ball
        ((r'8\s?-?ball',), 'Fun', 'eight_ball'),
        # BOD Leaderboard (must be checked before the general 'bod' command)
        ((r'\bbod\s.*(leaderboard|lb|scores|ranks)\b',), 'Fun', 'bod_leaderboard'),
        # BOD
        ((r'\bbod\b',), 'Fun', 'bod'),
        # Sanitize
        ((r'\bsanitize\b', r'\bsanitise\b'), 'Fun', 'sanitize'),
        # Pear Wiggler
        ((r'\bpear\s?wiggler\b',), 'Fun', 'pear_wiggler'),
        # Issues
        ((r'\bissues\b', r'\bissue\b'), 'Fun', 'issues'),
    ],
    # Music Group
    [
        # Lyrics search (check before general music commands)
        ((r'\blyrics?\b', r'\bfind\s*lyrics\b',
         r'\bsearch\s*lyrics\b'), 'Music', 'lyrics_nlp'),
        # Listen along / play music (most common entry point)
        ((r'\blisten\s*along\b', r'\bplay\s*music\b',
         r'\bjoin\s*(vc|voice|channel)?\b'), 'Music', 'listen_along_nlp'),
        # Pause playback
        ((r'\bpause\b',), 'Music', 'pause_nlp'),
        # Resume playback - 'play' only when it's the whole command (no song name after)
        ((r'\bresume\b', r'\bunpause\b', r'\bcontinue\b',
         r'\bplay\s*$'), 'Music', 'resume_nlp'),
        # Play/queue a specific song - 'play' or 'queue' followed by something (URL or search query)
        ((r'\bplay\s+\S+', r'\bqueue\s+\S+'), 'Music', 'play_nlp'),
        # Skip current track
        ((r'\bskip\b', r'\bnext\b'), 'Music', 'skip_nlp'),
        # Now playing / current song
        ((r'\bnow\s*playing\b', r'\bnp\b', r'\bcurrent\s*(song|track)\b',
         r"\bwhat('?s| is)\s*(playing|this)\b"), 'Music', 'now_playing_nlp'),
        # Queue / playlist - show queue (only when no song after)
        ((r'\bqueue\s*$', r'\bplaylist\b', r'\bup\s*next\b'), 'Music', 'queue_nlp'),
        # Shuffle toggle
        ((r'\bshuffle\b',), 'Music', 'shuffle_nlp'),
        # Jump to track
        ((r'\bjump\b', r'\bgoto\b', r'\bgo to\b'), 'Music', 'jump_nlp'),
        # Loop toggle
        ((r'\bloop\b', r'\brepeat\b'), 'Music', 'loop_nlp'),
        # Remove track from playlist
        ((r'\bremove\b', r'\bdelete\b'), 'Music', 'remove_nlp'),
        # Move track in playlist
        ((r'\bmove\b',), 'Music', 'move_nlp'),
        # Clear queue (keep current track only)
        ((r'\bclear\s*(queue|playlist)\b', r'\bempty\s*(queue|playlist)\b'), 'Music', 'clear_queue_nlp'),
        # Leave / disconnect
        ((r'\bleave\b', r'\bdisconnect\b', r'\bstop\s*music\b'), 'Music', 'leave_nlp'),
    ]
]

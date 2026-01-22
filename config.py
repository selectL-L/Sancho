"""config.py

Configuration hub for the bot.
Handles the NLP registry, path definitions, and environment variable loading.
See the info.env file for user-configurable settings;
each field there has a descriptive comment explaining its purpose. The field_comments
dict below is the canonical source for those descriptions.

Structure:
    1. Path Resolution     - PyInstaller-safe paths (APP_PATH, ASSETS_PATH, etc.)
    2. Environment Setup   - info.env creation/migration and loading
    3. Runtime Constants   - Derived values from env vars (DB_PATH, BOT_PREFIX, etc.)
    4. NLP Command Registry - Natural language routing configuration

"""

import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values, load_dotenv


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
        return sys._MEIPASS  # type: ignore[attr-defined,return-value]
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
    # Fields are ordered by section for proper file generation.
    # Default values: empty string = no default (user must fill or feature disabled).
    required_fields: Dict[str, str] = {
        # BOT IDENTITY
        "DISCORD_TOKEN": "",
        "BOT_PREFIX": "",
        "BOT_NAME": "NoName",
        "OWNER_ID": "",
        "AMBIENCE_ENABLED": "True",
        "DEFAULT_VISIBILITY": "online",
        "THEME_COLOR": "",
        # SYSTEM COMMUNICATION
        "SYSTEM_CHANNEL_ID": "",
        "CONTROL_PORT": "",
        # WEB SERVER
        "WEB_ENABLED": "False",
        "WEB_PORT": "8000",
        "WEB_HOST": "0.0.0.0",
        "WEB_SESSION_SECRET": "",
        "OAUTH_CLIENT_ID": "",
        "OAUTH_CLIENT_SECRET": "",
        "OAUTH_REDIRECT_URI": "",
        "WEB_MOCK_DATA": "False",
        # DEBUGGING
        "DEV_MODE": "False",
        "DEV_GUILD": "",
        # LOGGING
        "LOG_RETENTION_COUNT": "10",
        "LOG_MAX_MB": "5",
        "RESOURCE_TRACK_INTERVAL": "15",
        # MUSIC - YouTube Authentication
        "POT_PROVIDER_PORT": "",
        # MUSIC - Residential Proxy
        "RESIDENTIAL_PROXY_USER": "",
        "RESIDENTIAL_PROXY_PASSWORD": "",
        "RESIDENTIAL_PROXY_HOST": "",
        "RESIDENTIAL_PROXY_PORT": "",
    }

    # Section headers are written before their first field.
    section_headers: Dict[str, str] = {
        "DISCORD_TOKEN": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  BOT IDENTITY\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
        ),
        "SYSTEM_CHANNEL_ID": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  SYSTEM COMMUNICATION\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
        ),
        "WEB_ENABLED": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  WEB SERVER\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
        ),
        "DEV_MODE": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  DEBUGGING\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
        ),
        "LOG_RETENTION_COUNT": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  LOGGING\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
        ),
        "POT_PROVIDER_PORT": (
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "#  MUSIC\n"
            "# ═══════════════════════════════════════════════════════════════════════════════\n"
            "\n"
            "# --- YouTube Authentication ---\n"
        ),
        "RESIDENTIAL_PROXY_USER": (
            "\n"
            "# --- Residential Proxy ---\n"
        ),
    }

    # Field comments serve as documentation for BOTH the env file AND developers reading this code.
    field_comments: Dict[str, str] = {
        # BOT IDENTITY
        "DISCORD_TOKEN":
            "# (Required) Your bot's Discord token from the Developer Portal.",
        "BOT_PREFIX":
            "# (Required) Command prefixes, separated by pipes ( | ).",
        "BOT_NAME":
            "# (Required) The name the bot uses in user-facing messages.\n"
            "# WARNING: 'NoName' is a placeholder. Please set a real name.",
        "OWNER_ID":
            "# (Optional) Discord user IDs for bot owners, separated by pipes ( | ).\n"
            "# Example: 123456789|987654321. Enables owner-only commands.\n"
            "# If empty, owner commands are disabled.",
        "AMBIENCE_ENABLED":
            "# (Optional) Enable personality-driven responses (greetings, mood-based replies).\n"
            "# Default: True. Set to False to disable without removing ambience.toml.",
        "DEFAULT_VISIBILITY":
            "# (Optional) Initial Discord presence status on startup.\n"
            "# Options: online, idle, dnd, invisible. Default: online.\n"
            "# Useful for dev bots to start invisible and avoid online/offline spam.",
        "THEME_COLOR":
            "# (Optional) Hex color for the web UI theme (e.g., #9333ea for purple).\n"
            "# If empty, the web UI defaults to green.",
        # SYSTEM COMMUNICATION
        "SYSTEM_CHANNEL_ID":
            "# (Optional) Channel ID for bot status messages (startup, errors).\n"
            "# If empty, system messages are not sent to Discord.",
        "CONTROL_PORT":
            "# (Optional) TCP port for remote control commands (exit, restart, reload).\n"
            "# Binds to localhost only. If empty, TCP control is disabled.",
        # WEB SERVER
        "WEB_ENABLED":
            "# (Optional) Enable the availability scheduler web server.\n"
            "# Default: False. Set to True to start the web UI.",
        "WEB_PORT":
            "# (Optional) Port for the web server.\n"
            "# Default: 8000.",
        "WEB_HOST":
            "# (Optional) Host to bind the web server.\n"
            "# Default: 0.0.0.0 (all interfaces). Use 127.0.0.1 for local only.",
        "WEB_SESSION_SECRET":
            "# (Required if WEB_ENABLED) Random secret for signing session cookies.\n"
            "# Generate with: python -c \"import secrets; print(secrets.token_hex(32))\"",
        "OAUTH_CLIENT_ID":
            "# (Required if WEB_ENABLED) Discord application client ID.\n"
            "# Found in Discord Developer Portal > Your App > OAuth2.",
        "OAUTH_CLIENT_SECRET":
            "# (Required if WEB_ENABLED) Discord application client secret.\n"
            "# Found in Discord Developer Portal > Your App > OAuth2.",
        "OAUTH_REDIRECT_URI":
            "# (Required if WEB_ENABLED) OAuth callback URL.\n"
            "# Must match exactly in Discord Developer Portal.\n"
            "# Example: http://localhost:8000/auth/callback",
        "WEB_MOCK_DATA":
            "# (Optional) Return mock/fake data from web APIs for UI testing.\n"
            "# Separate from DEV_MODE so you can have debug logging without mock data.\n"
            "# Default: False. Only set True when testing the web UI without real data.",
        # DEBUGGING
        "DEV_MODE":
            "# (Optional) Developer mode restricts the bot to OWNER_ID only.\n"
            "# Default: False. Set to True during development/testing.",
        "DEV_GUILD":
            "# (Optional) Guild ID for instant slash command sync during development.\n"
            "# If empty, slash commands sync globally (can take up to an hour).",
        # LOGGING
        "LOG_RETENTION_COUNT":
            "# (Optional) Number of completed log files to keep before cleanup.\n"
            "# Default: 10. Current session log is not counted.",
        "LOG_MAX_MB":
            "# (Optional) Maximum size per log file in megabytes before rotation.\n"
            "# Default: 5. Increase for verbose debugging sessions.",
        "RESOURCE_TRACK_INTERVAL":
            "# (Optional) Minutes between resource usage snapshots (CPU, memory).\n"
            "# Default: 15. Lower values = more granular data, slightly more overhead.",
        # MUSIC - YouTube Authentication
        "POT_PROVIDER_PORT":
            "# (Optional) Port for the Proof-of-Origin token provider server.\n"
            "# This is external code (not ours). If installed, set to 4416.\n"
            "# If empty, PO token authentication is disabled.",
        # MUSIC - Residential Proxy
        "RESIDENTIAL_PROXY_USER":
            "# (Optional) Decodo/Smartproxy username for residential proxy fallback.\n"
            "# ⚠️  ALL FOUR proxy fields must be set, or the feature is disabled.\n"
            "# ⚠️  This feature costs real money (~$0.01-0.02 per song). Use wisely.",
        "RESIDENTIAL_PROXY_PASSWORD":
            "# Residential proxy password (from your Decodo dashboard).",
        "RESIDENTIAL_PROXY_HOST":
            "# Proxy hostname.",
        "RESIDENTIAL_PROXY_PORT":
            "# Proxy port.",
    }

    def write_env_file(values: Dict[str, str]) -> None:
        """Write the env file with proper sections and comments."""
        with open(ENV_PATH, 'w', encoding='utf-8') as f:
            for key in required_fields:
                # Write section header if this field starts a new section
                if key in section_headers:
                    f.write(f"\n{section_headers[key]}\n")
                # Write field comment and value
                f.write(f"{field_comments[key]}\n")
                f.write(f"{key}={values.get(key, required_fields[key])}\n\n")

    if not os.path.exists(ENV_PATH):
        logging.warning(
            f"'{os.path.basename(ENV_PATH)}' not found. Creating a new one.")
        try:
            write_env_file(required_fields)
        except Exception as e:
            logging.critical(f"Failed to create {ENV_PATH}: {e}")
            sys.exit(f"Exiting: Failed to create {ENV_PATH}.")

        # This message is critical for the user to see on the first run.
        print(f"'{os.path.basename(ENV_PATH)}' was not found.")
        print(f"A new one has been created at: {ENV_PATH}")
        print("\nPlease open this file and add your bot's DISCORD_TOKEN, BOT_PREFIX, and BOT_NAME.")
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

            # Merge existing values with defaults for missing keys
            # Use `or` to coalesce None values from dotenv_values to defaults
            merged_values: Dict[str, str] = {
                key: current_values.get(key) or required_fields[key]
                for key in required_fields
            }

            try:
                os.remove(ENV_PATH)
                write_env_file(merged_values)
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

# =============================================================================
# ENVIRONMENT VARIABLES - Core Identity
# =============================================================================
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_PREFIX_RAW = os.getenv('BOT_PREFIX')
BOT_NAME = os.getenv('BOT_NAME')

if not TOKEN or not BOT_PREFIX_RAW or not BOT_NAME:
    print("DISCORD_TOKEN, BOT_PREFIX, and BOT_NAME must be set in info.env.")
    sys.exit("Exiting: Missing or invalid required configuration.")

if BOT_NAME == "NoName":
    print("WARNING: BOT_NAME is set to the default 'NoName'.")
    print("Please update 'info.env' with your bot's actual name.")

# Sort prefixes by length descending to ensure longer prefixes are matched first
# (e.g., '.mayors' before '.m') and add a trailing space to act as a delimiter.
BOT_PREFIX = sorted(
    [p.strip() + ' ' for p in BOT_PREFIX_RAW.split('|')], key=len, reverse=True)

raw_owner_ids = os.getenv('OWNER_ID', '')
OWNER_IDS: set[int] = {
    int(id_str.strip())
    for id_str in raw_owner_ids.split('|')
    if id_str.strip().isdigit()
}

raw_ambience_enabled = os.getenv('AMBIENCE_ENABLED', 'True')
AMBIENCE_ENABLED = raw_ambience_enabled.lower() in ('true', '1', 't')

# Default visibility on startup. Valid: online, idle, dnd, invisible
raw_default_visibility = os.getenv('DEFAULT_VISIBILITY', 'online').lower()
DEFAULT_VISIBILITY = raw_default_visibility if raw_default_visibility in ('online', 'idle', 'dnd', 'invisible') else 'online'

THEME_COLOR = os.getenv('THEME_COLOR', '')  # Empty = web UI defaults to green

# =============================================================================
# ENVIRONMENT VARIABLES - System Communication
# =============================================================================
raw_system_channel_id = os.getenv('SYSTEM_CHANNEL_ID')
SYSTEM_CHANNEL_ID: Optional[int] = int(raw_system_channel_id) if raw_system_channel_id and raw_system_channel_id.isdigit() else None

raw_control_port = os.getenv('CONTROL_PORT')
CONTROL_PORT: Optional[int] = int(raw_control_port) if raw_control_port and raw_control_port.isdigit() else None

# =============================================================================
# ENVIRONMENT VARIABLES - Web Server
# =============================================================================
# Availability scheduler web UI with Discord OAuth authentication.
# Requires: WEB_ENABLED=True + all OAuth fields configured.
raw_web_enabled = os.getenv('WEB_ENABLED', 'False')
WEB_ENABLED = raw_web_enabled.lower() in ('true', '1', 't')

raw_web_port = os.getenv('WEB_PORT', '8000')
WEB_PORT = int(raw_web_port) if raw_web_port.isdigit() else 8000

WEB_HOST = os.getenv('WEB_HOST', '0.0.0.0')
WEB_SESSION_SECRET = os.getenv('WEB_SESSION_SECRET', '')
OAUTH_CLIENT_ID = os.getenv('OAUTH_CLIENT_ID', '')
OAUTH_CLIENT_SECRET = os.getenv('OAUTH_CLIENT_SECRET', '')
OAUTH_REDIRECT_URI = os.getenv('OAUTH_REDIRECT_URI', '')

# Validate web configuration - key fields required if enabled
_web_required = [WEB_SESSION_SECRET, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_REDIRECT_URI]
if WEB_ENABLED and not all(_web_required):
    print("WARNING: WEB_ENABLED is True but OAuth configuration is incomplete.")
    print("Required: WEB_SESSION_SECRET, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_REDIRECT_URI")
    print("Web server is DISABLED.")
    WEB_ENABLED = False

# =============================================================================
# ENVIRONMENT VARIABLES - Debugging
# =============================================================================
raw_dev_mode = os.getenv('DEV_MODE', 'False')
DEV_MODE = raw_dev_mode.lower() in ('true', '1', 't')

raw_web_mock = os.getenv('WEB_MOCK_DATA', 'False')
WEB_MOCK_DATA = raw_web_mock.lower() in ('true', '1', 't')

raw_dev_guild = os.getenv('DEV_GUILD')
DEV_GUILD: Optional[int] = int(raw_dev_guild) if raw_dev_guild and raw_dev_guild.isdigit() else None

# =============================================================================
# ENVIRONMENT VARIABLES - Logging
# =============================================================================
raw_log_retention = os.getenv('LOG_RETENTION_COUNT', '10')
LOG_RETENTION_COUNT = int(raw_log_retention) if raw_log_retention.isdigit() else 10

raw_log_max_mb = os.getenv('LOG_MAX_MB', '5')
LOG_MAX_MB = int(raw_log_max_mb) if raw_log_max_mb.isdigit() else 5
LOG_MAX_BYTES = LOG_MAX_MB * 1024 * 1024  # Convert to bytes for logging module

raw_resource_interval = os.getenv('RESOURCE_TRACK_INTERVAL', '15')
RESOURCE_TRACK_INTERVAL = int(raw_resource_interval) if raw_resource_interval.isdigit() else 15

LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'

# =============================================================================
# ENVIRONMENT VARIABLES - Music (YouTube Authentication)
# =============================================================================
# PO Token Provider - HTTP server that generates proof-of-origin tokens for YouTube.
# This helps bypass 403 errors on datacenter IPs. The server is started/stopped with the bot.
# One-time setup: clone repo to <venv>/utils/pot_provider, run npm install && npx tsc
# Uses sys.prefix to find the venv directory (keeps JS code away from Python code).
POT_PROVIDER_PATH = os.path.join(sys.prefix, 'utils', 'pot_provider', 'server', 'build', 'main.js')

raw_pot_port = os.getenv('POT_PROVIDER_PORT')
POT_PROVIDER_PORT: Optional[int] = int(raw_pot_port) if raw_pot_port and raw_pot_port.isdigit() else None

# Cookie/token file paths (manual fallback authentication)
YOUTUBE_COOKIE_PATH = os.path.join(APP_PATH, 'youtube_cookies.txt')
YOUTUBE_PO_TOKEN_PATH = os.path.join(APP_PATH, 'youtube_po_token.txt')

# =============================================================================
# ENVIRONMENT VARIABLES - Music (Residential Proxy)
# =============================================================================
# Used as fallback when direct YouTube streaming fails with 403 errors.
# YouTube embeds the requester's IP in audio URLs, so datacenter IPs often get blocked.
# Residential proxies provide real ISP IPs that bypass these blocks.
# Pricing: ~$4/GB (Decodo PAYG), ~$0.012-0.02 per song.
RESIDENTIAL_PROXY_USER = os.getenv('RESIDENTIAL_PROXY_USER', '')
RESIDENTIAL_PROXY_PASSWORD = os.getenv('RESIDENTIAL_PROXY_PASSWORD', '')
RESIDENTIAL_PROXY_HOST = os.getenv('RESIDENTIAL_PROXY_HOST', '')
raw_proxy_port = os.getenv('RESIDENTIAL_PROXY_PORT', '')
RESIDENTIAL_PROXY_PORT: Optional[int] = int(raw_proxy_port) if raw_proxy_port and raw_proxy_port.isdigit() else None
RESIDENTIAL_PROXY_COST_PER_GB = 4.00  # USD, for cost tracking

# Validate proxy configuration - ALL fields required or feature is disabled
_proxy_fields = [RESIDENTIAL_PROXY_USER, RESIDENTIAL_PROXY_PASSWORD, RESIDENTIAL_PROXY_HOST, RESIDENTIAL_PROXY_PORT]
_proxy_filled = [bool(f) for f in _proxy_fields]
if any(_proxy_filled) and not all(_proxy_filled):
    print("WARNING: Residential proxy configuration is incomplete.")
    print("All four fields (USER, PASSWORD, HOST, PORT) must be set for the feature to work.")
    print("Residential proxy fallback is DISABLED.")
RESIDENTIAL_PROXY_ENABLED = all(_proxy_filled)

# =============================================================================
# DERIVED PATHS
# =============================================================================
LOGS_DIR = os.path.join(APP_PATH, 'logs')
COGS_PATH = os.path.join(INTERNAL_PATH, 'cogs')
MUSIC_CACHE_PATH = os.path.join(APP_PATH, 'cache', 'music')
YTDLP_CACHE_PATH = os.path.join(MUSIC_CACHE_PATH, 'ytdlp')  # yt-dlp's cache (OAuth tokens, etc.)

# =============================================================================
# DATABASE PATH DISCOVERY
# =============================================================================
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

# =============================================================================
# NLP COMMAND REGISTRY
# =============================================================================
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
#
# IMPORTANT: The Fun cog dynamically appends its own simple commands to the END
# of this registry at load time (see cogs/fun.py -> FUN_NLP_ENTRIES). These are
# hyper-specific patterns (exact phrases, niche triggers) that should only match
# after all other groups have been checked. Do not add Fun commands here unless
# they are complex commands that require priority ordering within the Fun group
# itself (like BOD). Simple Fun commands go in fun.py.
#
# Note: *Never* use black formatting, it makes this section unreadable.
NLP_COMMANDS: List[List[Tuple[Tuple[str, ...], str, str]]] = [
    # Math Group
    [
        # Limbus Company coin flip
        ((r'\blimbus\b', r'\bcoin\s.*flip\b'), 'Math', 'limbus_roll_nlp'),
        # Dice rolling (should be checked before basic calculation)
        ((r'\broll\b', r'\bdice\b'), 'Math', 'roll'),
        # Basic calculation
        ((r'\bcalculate\b', r'\bcalc\b', r'\bcompute\b', r'\bevaluate\b', r'\bsolve\b', r"what('?s| is)\s+\d", r'\bhow much is\b'), 'Math', 'calculate'),
    ],
    # Skills Group
    [
        # Managment commands should be checked first to avoid conflicting with *casting* skills
        # Delete a saved skill
        ((r'\b(delete|remove)\s.*skill(s)?\b',), 'Skills', 'delete_skill_nlp'),
        # Edit an existing skill
        ((r'\b(edit|change|update)\s.*skill(s)?\b',), 'Skills', 'edit_skill_nlp'),
        # List all saved skills
        ((r'\b(list|check|show|see)\s.*skill(s)?\b', r'\bmy skills\b', r'\bwhat\b.*\bskills\b', r'^\s*skills\s*$'), 'Skills', 'list_skills_nlp'),
        # Save a new skill
        ((r'\b(save|create|make|add)\s.*skill\b',), 'Skills', 'save_skill_nlp'),
        # Cast or use a skill (anchored to start to prevent broad matching)
        ((r'^\s*cast\b', r'^\s*use\s+\w', r'^\s*skill\s+\w'), 'Skills', 'use_skill_nlp'),
    ],
    # Reminders Group (note: unlike other groups, this one ENFORCES matching at the front to prevent polluting the query)
    [
        # Deleting reminders (catches "delete/remove/cancel reminder 1", etc.)
        # This should be checked BEFORE setting reminders, to avoid a conflict on the word "remind"
        ((r'^\s*(delete|remove|cancel)\b.*\breminder',), 'Reminders', 'delete_reminders_nlp'),
        # Editing reminders
        ((r'^\s*(edit|change|update)\b.*\breminder',), 'Reminders', 'edit_reminder_nlp'),
        # Checking reminders (catches "check my reminders", "show reminders", etc.)
        ((r'^\s*(check|show|list|see|view)\b.*\breminders?\b', r'\bmy reminders\b', r'\bwhat are my reminders\b', r'^\s*reminders\s*$'), 'Reminders', 'check_reminders_nlp'),
        # Setting user timezone
        ((r'^\s*(set|change)\s.*timezone\b', r'^\s*(set|change)\s.*tz\b', r'^\s*timezone\b', r'^\s*tz\b'), 'Reminders', 'set_timezone_nlp'),
        # Reminder Settings
        ((r'^\s*reminder\s+settings\b', r'^\s*reminders\s+settings\b'), 'Reminders', 'reminder_settings_nlp'),
        # Setting reminders
        ((r'^\s*(remind|reminder|remember|set\s+a\s+reminder|set\s.*reminder)\b',), 'Reminders', 'remind'),
    ],
    # Image Group
    [
        # Profile picture / avatar
        ((r'\bpfp\b', r'\bavatar\b', r'\bprofile\s*pic(ture)?\b', r'\b(show|get)\s.*(pfp|avatar)\b', r"what('?s| is)\s+(their|his|her|my)\s+(pfp|avatar)\b"), 'ImageCog', 'pfp'),
        # Banner
        ((r'\bbanner\b', r'\bprofile\s*banner\b', r"what('?s| is)\s+(their|his|her|my)\s+banner\b"), 'ImageCog', 'banner'),
        # Resize image
        ((r'\bresize\b', r'\bscale\b'), 'ImageCog', 'resize'),
        # Convert image format
        ((r'\bconvert\b', r'\bchange to\b'), 'ImageCog', 'convert'),
    ],
    # Fun Group - Complex commands only (BOD fate system)
    # Simple Fun commands (including yujin_quotes) are registered dynamically by the Fun cog
    [
        # BOD Leaderboard (must be checked before the general 'bod' command)
        ((r'\bbod\s.*(leaderboard|lb|scores|ranks)\b',), 'Fun', 'bod_leaderboard'),
        # BOD
        ((r'\bbod\b',), 'Fun', 'bod'),
    ],
    # Music Group
    [
        # Lyrics search (check before general music commands)
        ((r'\blyrics?\b', r'\bfind\s*lyrics\b', r'\bsearch\s*lyrics\b'), 'Music', 'lyrics_nlp'),
        # Listen along / play music (most common entry point)
        # 'join' alone is strong enough; come/get/hop just need vc/voice/channel somewhere after
        ((r'\blisten\s*along\b', r'\bplay\s*music\b', r'\bjoin\b', r'\b(come|get|hop)\b.*\b(vc|voice|channel)\b'), 'Music', 'listen_along_nlp'),
        # Pause playback
        ((r'\bpause\b',), 'Music', 'pause_nlp'),
        # Resume playback - 'play' only when it's the whole command (no song name after)
        ((r'\bresume\b', r'\bunpause\b', r'\bcontinue\b', r'\bplay\s*$'), 'Music', 'resume_nlp'),
        # Play/queue a specific song - 'play' or 'queue' followed by something (URL or search query)
        ((r'\bplay\s+\S+', r'\bqueue\s+\S+'), 'Music', 'play_nlp'),
        # Skip current track
        ((r'\bskip\b', r'\bnext\b'), 'Music', 'skip_nlp'),
        # Now playing / current song
        ((r'\bnow\s*playing\b', r'\bnp\b', r'\bcurrent\s*(song|track)\b', r"\bwhat('?s| is)\s*(playing|this)\b"), 'Music', 'now_playing_nlp'),
        # Clear queue (keep current track only) - must be before queue_nlp to avoid "clear queue" matching queue first
        ((r'\bclear\s*(queue|playlist)?\b', r'\bempty\s*(queue|playlist)\b'), 'Music', 'clear_queue_nlp'),
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
        # Leave / disconnect
        ((r'\bleave\b', r'\bdisconnect\b', r'\bstop\s*music\b'), 'Music', 'leave_nlp'),
    ],
    # Schedule Group - Weekly availability scheduler
    # Patterns TBD - handlers exist in cog but patterns need priority tuning
    [
        # Who's available at a time
        ((), 'Schedule', 'who_available_nlp'),
        # View someone's schedule
        ((), 'Schedule', 'view_schedule_nlp'),
        # Find overlapping availability
        ((), 'Schedule', 'find_overlap_nlp'),
        # Edit availability (returns web link)
        ((), 'Schedule', 'edit_availability_nlp'),
    ],
]

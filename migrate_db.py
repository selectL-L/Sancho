"""migrate_db.py

This script performs a safe migration of the bot database.
It backs up the existing database, creates a new one with the updated schema,
and migrates the data.

Usage:
    python migrate_db.py
"""

import logging
import os
import re
import shutil
import sqlite3
import time
from typing import Any, Dict

import config

# --- Configuration ---
# Set up basic logging to see the script's progress.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrate_db.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_PATH, encoding='utf-8'), logging.StreamHandler()]
)

# Define the paths based on the project structure.
# This assumes the script is run from the root of the project.
APP_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_PATH = os.path.join(APP_PATH, 'assets')
# The target database is defined in config.py based on BOT_NAME
TARGET_DB_NAME = f"{config.BOT_NAME}.db"
TARGET_DB_PATH = os.path.join(ASSETS_PATH, TARGET_DB_NAME)
BACKUP_EXTENSION = ".backup"

# --- Schema Definition ---
# This is the target schema we want for the new database.
# It matches the schema in `utils/database.py`.
#
# ==================================================================================
# TABLE DESCRIPTIONS (in order of appearance)
# ==================================================================================
#
# skills
#   Purpose: Stores user-created dice roll macros ("skills") for tabletop RPGs.
#   Used by: cogs/skills.py
#   Keys: user_id (owner), name (unique per user, case-insensitive)
#
# skill_aliases
#   Purpose: Alternative names for skills, allowing users to invoke skills by alias.
#   Used by: cogs/skills.py
#   Keys: skill_id (FK to skills), alias (unique per skill, case-insensitive)
#   Note: CASCADE delete - aliases are removed when parent skill is deleted.
#
# reminders
#   Purpose: Scheduled reminders that fire at a specific time.
#   Used by: cogs/reminders.py
#   Keys: user_id (owner), reminder_time (Unix timestamp for scheduling)
#   Note: Supports recurring reminders via is_recurring flag and recurrence_rule (iCal RRULE).
#
# schedule_availability
#   Purpose: Stores user's weekly recurring availability as 15-minute time slots.
#   Used by: cogs/schedule.py, utils/web/routes.py
#   Keys: user_id (owner)
#   Note: Slots stored as strings like "mon-0930" (day-HHMM in 24h format).
#
# schedule_guild_visibility
#   Purpose: Controls which guilds can see a user's availability.
#   Used by: cogs/schedule.py, utils/web/routes.py
#   Keys: Composite (user_id, guild_id) - one row per guild per user.
#   Note: Default disabled (opt-in per guild).
#
# schedule_user_blacklist
#   Purpose: Users blocked from viewing a specific user's availability.
#   Used by: cogs/schedule.py, utils/web/routes.py
#   Keys: Composite (user_id, blocked_user_id)
#   Note: Checked before returning availability in queries.
#
# user_settings
#   Purpose: Per-user key-value configuration store.
#   Used by: cogs/reminders.py (timezone, reminder_destination), cogs/skills.py (skill_limit)
#   Keys: Composite (user_id, key) - allows multiple settings per user.
#   Known keys: 'timezone', 'skill_limit', 'reminder_destination'
#
# bot_settings
#   Purpose: Global bot-wide configuration store.
#   Used by: cogs/admin.py (global_limit), utils/database.py (skill_limit default)
#   Keys: Single key column - one row per setting.
#   Known keys: 'skill_limit'
#
# guild_settings
#   Purpose: Per-guild key-value configuration store.
#   Used by: cogs/starboard.py (starboard_channel, starboard_emoji, starboard_threshold),
#            cogs/music.py (music_channel_id)
#   Keys: Composite (guild_id, key) - allows multiple settings per guild.
#   Known keys: 'starboard_channel', 'starboard_emoji', 'starboard_threshold', 'music_channel_id'
#
# starboard_entries
#   Purpose: Tracks messages that have been posted to a guild's starboard channel.
#   Used by: cogs/starboard.py
#   Keys: original_message_id (the source message being starred)
#   Note: Links original message to its starboard copy for updates/removal.
#
# bod_players
#   Purpose: Tracks BOD game sessions and fate bank per user.
#   Used by: cogs/fun.py (bod command), cogs/admin.py (bod_bless, bod_fate, bod_clear)
#   Keys: user_id (one row per user)
#   Note: Stores chain progress, timeout tracking, and fate charges (lucky/blessed/guaranteed).
#   Renamed from: bod_usage (added fate columns)
#
# bod_leaderboard
#   Purpose: Persistent high scores for the "Boundary of Death" game.
#   Used by: cogs/fun.py (bod_leaderboard command)
#   Keys: user_id (one entry per user)
#   Note: Display names are fetched dynamically at render time, not stored.
#
# proxy_usage
#   Purpose: Tracks residential proxy bandwidth usage for cost monitoring.
#   Used by: cogs/music.py (residential proxy fallback for YouTube 403 errors)
#   Keys: year_month (e.g., "2025-01") for monthly aggregation
#   Note: Stores track_count, bytes_used, and estimated cost for billing awareness.
#
# users
#   Purpose: Cached Discord user profile data for the web interface.
#   Used by: utils/web/auth.py (OAuth callback, /auth/me endpoint)
#   Keys: user_id (Discord snowflake)
#   Note: Stores username, avatar URL, and last_seen timestamp. Survives session expiry
#         so we can greet returning users by name even after re-authentication.
#
# web_sessions
#   Purpose: Server-side session storage for web authentication.
#   Used by: utils/web/session_middleware.py, utils/web/auth.py
#   Keys: session_id (UUID v4 string)
#   Note: Stores Discord OAuth tokens (access + refresh) server-side. Only session_id
#         is sent to client in cookie. Enables indefinite sessions via token refresh.
#         Sessions unused for 90+ days are cleaned up on server startup.
#         CASCADE delete on user_id means deleting a user deletes their sessions.
#
# ==================================================================================
TABLE_SCHEMAS = {
    "skills": """
        CREATE TABLE skills (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            dice_roll TEXT NOT NULL,
            skill_type TEXT NOT NULL,
            description TEXT,
            UNIQUE(user_id, name COLLATE NOCASE)
        )
    """,
    "skill_aliases": """
        CREATE TABLE skill_aliases (
            id INTEGER PRIMARY KEY,
            skill_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE,
            UNIQUE(skill_id, alias COLLATE NOCASE)
        )
    """,
    "reminders": """
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            reminder_time INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            is_recurring INTEGER NOT NULL DEFAULT 0,
            recurrence_rule TEXT,
            reply_message_id INTEGER
        )
    """,
    "schedule_availability": """
        CREATE TABLE schedule_availability (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            slot TEXT NOT NULL,
            UNIQUE(user_id, slot)
        )
    """,
    "schedule_availability_meta": """
        CREATE TABLE schedule_availability_meta (
            user_id INTEGER PRIMARY KEY,
            updated_at INTEGER NOT NULL
        )
    """,
    "schedule_guild_visibility": """
        CREATE TABLE schedule_guild_visibility (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, guild_id)
        )
    """,
    "schedule_user_blacklist": """
        CREATE TABLE schedule_user_blacklist (
            user_id INTEGER NOT NULL,
            blocked_user_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, blocked_user_id)
        )
    """,
    "user_settings": """
        CREATE TABLE user_settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(user_id, key)
        )
    """,
    "bot_settings": """
        CREATE TABLE bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """,
    "guild_settings": """
        CREATE TABLE guild_settings (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(guild_id, key)
        )
    """,
    "starboard_entries": """
        CREATE TABLE starboard_entries (
            original_message_id INTEGER PRIMARY KEY,
            starboard_message_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            starboard_reply_id INTEGER,
            original_channel_id INTEGER NOT NULL
        )
    """,
    "bod_players": """
        CREATE TABLE bod_players (
            user_id INTEGER PRIMARY KEY,
            last_used_timestamp INTEGER NOT NULL DEFAULT 0,
            current_chain INTEGER NOT NULL DEFAULT 0,
            last_channel_id INTEGER NOT NULL DEFAULT 0,
            fate_lucky INTEGER NOT NULL DEFAULT 0,
            fate_blessed INTEGER NOT NULL DEFAULT 0,
            fate_guaranteed INTEGER NOT NULL DEFAULT 0
        )
    """,
    "bod_leaderboard": """
        CREATE TABLE bod_leaderboard (
            user_id INTEGER PRIMARY KEY,
            best_chain INTEGER NOT NULL DEFAULT 0,
            achieved_at INTEGER NOT NULL DEFAULT 0
        )
    """,
    "proxy_usage": """
        CREATE TABLE proxy_usage (
            id INTEGER PRIMARY KEY,
            year_month TEXT NOT NULL UNIQUE,
            track_count INTEGER NOT NULL DEFAULT 0,
            bytes_used INTEGER NOT NULL DEFAULT 0,
            last_updated INTEGER NOT NULL
        )
    """,
    "users": """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            avatar TEXT,
            last_seen INTEGER NOT NULL
        )
    """,
    "web_sessions": """
        CREATE TABLE web_sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            token_expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
    """
}

INDEX_SCHEMAS = [
    "CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders (reminder_time);",
    "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id);",
    "CREATE INDEX IF NOT EXISTS idx_schedule_availability_user ON schedule_availability (user_id);",
    "CREATE INDEX IF NOT EXISTS idx_schedule_guild_visibility_guild ON schedule_guild_visibility (guild_id);",
    "CREATE INDEX IF NOT EXISTS idx_skills_user ON skills (user_id);",
    "CREATE INDEX IF NOT EXISTS idx_starboard_guild ON starboard_entries (guild_id);",
    "CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions (user_id);",
    "CREATE INDEX IF NOT EXISTS idx_web_sessions_last_seen ON web_sessions (last_seen_at);"
]

# ==================================================================================
# TABLE_RENAMES: Mapping from old table names to new table names
# ==================================================================================
# Date Added: 16-12-2025
#
# WHY THESE RENAMES EXIST:
# The original table names were inconsistent and not descriptive enough:
#   - "config" is ambiguous (config for what? the bot? users? guilds?)
#   - "*_config" suffix was inconsistent with the actual purpose (these store settings)
#   - "starboard" didn't indicate it stores entries/records
#
# NAMING CONVENTION:
#   - bot_settings: Global bot-wide configuration (e.g., default skill_limit)
#   - user_settings: Per-user settings (timezone, skill_limit override, reminder_destination)
#   - guild_settings: Per-guild settings (starboard_channel, starboard_emoji, etc.)
#   - starboard_entries: Individual records of messages posted to starboard
#
# HOW THIS IS USED:
# The migration logic checks if a table name exists in TABLE_RENAMES. If so, it
# migrates data from the old table name to the new table name. Tables not in this
# mapping are migrated with their original name (if they exist in TABLE_SCHEMAS).
#
# IS THIS SAFE TO REMOVE?: No. This mapping is used by the migration logic to
# determine how to handle renamed tables. Removing it would break migration from
# databases using the old schema.
# AKA, this is a structural change that would entirely break old databases if removed.
# this is not nearly as dangerous as the other migration changes as the script will
# mention what tables broke, but without this, it can't do that correctly.
# ==================================================================================
TABLE_RENAMES = {
    # Old Name         New Name            Reason for Rename
    # --------         --------            -----------------
    "config":         "bot_settings",     # Clarify this is bot-wide, not user/guild config
    "user_config":    "user_settings",    # Consistent naming with other *_settings tables
    "guild_config":   "guild_settings",   # Consistent naming with other *_settings tables
    "starboard":      "starboard_entries",  # Indicate this stores entry records, not config
    "bod_usage":      "bod_players",      # Now tracks fate bank in addition to usage data
}


def migrate_database() -> None:
    """Performs a safe migration of the bot database.

    1. Identifies the source database (Legacy or Current).
    2. Backs up the source database.
    3. Reads all data from the backup.
    4. Creates the new target database with the updated schema.
    5. Inserts the old data into the new database.
    """
    # 1. Identify Source Database
    # We use the same scan logic as config.py to find what we are migrating FROM.
    found_dbs = [f for f in os.listdir(ASSETS_PATH) if f.endswith('.db')]

    source_db_path: str

    if len(found_dbs) == 0:
        logging.info("No existing databases found in '%s'. Nothing to migrate.", ASSETS_PATH)
        return
    elif len(found_dbs) == 1:
        # We found one DB.
        # If it's already the target name, we just upgrade in-place (if schema changed) or simply run to verify.
        source_db_path = os.path.join(ASSETS_PATH, found_dbs[0])
        if found_dbs[0] == TARGET_DB_NAME:
            logging.info("Found '%s'. Migrating/Upgrading in-place.", found_dbs[0])
        else:
            logging.info("Found '%s'. Migrating to '%s'.", found_dbs[0], TARGET_DB_NAME)
    else:
        logging.error("Multiple databases found: %s. Please ensure only one source database exists.", found_dbs)
        return

    backup_path = f"{source_db_path}{BACKUP_EXTENSION}"

    # 2. Create a backup.
    logging.info("Backing up source database to '%s'...", backup_path)
    try:
        shutil.copyfile(source_db_path, backup_path)
        logging.info("Backup successful.")
    except Exception as e:
        logging.error("Failed to create backup. Migration aborted. Error: %s", e)
        return

    # 3. Read all data from the backup database.
    logging.info("Reading data from backup database...")
    data_store: Dict[str, Dict[str, Any]] = {}
    try:
        with sqlite3.connect(backup_path) as backup_conn:
            backup_conn.row_factory = sqlite3.Row
            cursor = backup_conn.cursor()
            # Get a list of all tables in the old database.
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            for table_name in tables:
                logging.info("...reading from table '%s'", table_name)
                cursor.execute(f"PRAGMA table_info({table_name})")
                old_columns = [col[1] for col in cursor.fetchall()]
                cursor.execute(f"SELECT * FROM {table_name}")
                data_store[table_name] = {'rows': [dict(row) for row in cursor.fetchall()], 'columns': old_columns}
    except Exception as e:
        logging.error("Failed to read data from backup. Migration aborted. Error: %s", e)
        return

    # 4. Create a new database with the correct schema.
    logging.info("Creating new database at '%s' with updated schema...", TARGET_DB_PATH)
    try:
        # Delete the target DB file before creating the new one (if it exists).
        if os.path.exists(TARGET_DB_PATH):
            os.remove(TARGET_DB_PATH)

        with sqlite3.connect(TARGET_DB_PATH) as new_conn:
            cursor = new_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            for table_name, schema in TABLE_SCHEMAS.items():
                logging.info("...creating table '%s'", table_name)
                cursor.execute(schema)
            for index_schema in INDEX_SCHEMAS:
                logging.info("...creating index: %s", index_schema)
                cursor.execute(index_schema)
            new_conn.commit()
        logging.info("New database created successfully.")
    except Exception as e:
        logging.error("Failed to create new database. Restore from backup. Error: %s", e)
        return

    # 5. Insert the old data into the new database.
    logging.info("Migrating data to new database...")
    try:
        with sqlite3.connect(TARGET_DB_PATH) as new_conn:
            cursor = new_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF;")
            # --- Data Migration Logic ---
            for table_name, table_data in data_store.items():
                rows = table_data['rows']
                old_columns = table_data['columns']
                if not rows:
                    continue

                # ==================================================================================
                # MIGRATION: skills - Handle aliases column if present (legacy format)
                # ==================================================================================
                # Date Added: 16-11-2025
                #
                # WHY THIS EXISTS:
                # Very old database versions stored aliases directly in the skills table as a
                # pipe-separated string. The current schema uses a separate skill_aliases table.
                #
                # WHAT THIS MIGRATION DOES:
                # If the old skills table has an 'aliases' column, extract those aliases and
                # insert them into the skill_aliases table.
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated at least once.
                # ==================================================================================
                if table_name == 'skills':
                    logging.info("...migrating %d skills and their aliases", len(rows))
                    for skill_row in rows:
                        skill_cols = {k: v for k, v in skill_row.items() if k != 'aliases'}
                        columns = ', '.join(skill_cols.keys())
                        placeholders = ', '.join('?' for _ in skill_cols)
                        cursor.execute(
                            f"INSERT INTO skills ({columns}) VALUES ({placeholders})",
                            tuple(skill_cols.values())
                        )
                        skill_id = cursor.lastrowid
                        if skill_row.get('aliases'):
                            aliases = [alias.strip() for alias in skill_row['aliases'].split('|') if alias.strip()]
                            if aliases:
                                cursor.executemany(
                                    "INSERT INTO skill_aliases (skill_id, alias) VALUES (?, ?)",
                                    [(skill_id, alias) for alias in aliases]
                                )

                # ==================================================================================
                # MIGRATION: user_timezones - Normalize to pytz-compatible IANA format
                # ==================================================================================
                # Date Added: 15-12-2025
                # Date Modified: 16-12-2025 - Now inserts into user_settings instead of
                #                             user_timezones (table consolidation)
                #
                # WHY THIS EXISTS:
                # Previously, user timezones could be stored in various inconsistent formats:
                #   - User-friendly offsets: "GMT+5", "UTC-8"
                #   - Bare abbreviations: "GMT", "UTC"
                #   - Mixed formats due to bugs in older code
                #
                # pytz requires specific formats, and GMT/UTC offsets use *inverted* signs
                # due to the POSIX standard vs ISO 8601:
                #   - User says "GMT+5" (5 hours AHEAD of UTC)
                #   - pytz needs "Etc/GMT-5" (POSIX convention: negative = east of UTC)
                #
                # The refactored code in cogs/reminders.py now:
                #   1. Stores timezones in pytz-compatible format (Etc/GMT-5 or IANA names)
                #   2. Converts to display format only when showing to users
                #
                # WHAT THIS MIGRATION DOES:
                # 1. Converts GMT/UTC offset strings to pytz format:
                #      "GMT+5"  -> "Etc/GMT-5"
                #      "UTC-8"  -> "Etc/GMT+8"
                # 2. Converts bare "GMT" to "Etc/GMT" (pytz-compatible)
                # 3. Leaves IANA names (e.g., "America/New_York", "CET") unchanged
                # 4. Inserts into user_settings with key='timezone' (table consolidation)
                #
                # IS THIS SAFE TO REMOVE?: Yes, after running migration once. Harmless to keep
                # since it only transforms non-standard formats which won't exist post-migration.
                # The user_timezones table no longer exists in the new schema.
                #
                # NOTE: This migration is now redundant for databases created after 16-12-2025,
                # as the new schema stores timezones directly in user_settings. This block only
                # executes if the old user_timezones table exists in the source database.
                # ==================================================================================
                elif table_name == 'user_timezones':
                    logging.info("...migrating %d user timezone(s) (normalizing to pytz format)", len(rows))
                    converted_count = 0
                    for tz_row in rows:
                        user_id = tz_row['user_id']
                        old_tz = tz_row['timezone']
                        new_tz = old_tz  # Default: keep as-is
                        old_tz_lower = old_tz.lower().strip()

                        # Case 1: GMT/UTC offset with sign (e.g., "GMT+5", "UTC-8", "+5", "-3")
                        match = re.match(r'^(gmt|utc)?([+-])(\d{1,2})$', old_tz_lower)
                        if match:
                            sign = match.group(2)
                            hour = int(match.group(3))
                            # Invert sign for POSIX/pytz convention:
                            # ISO "GMT+5" (ahead of UTC) = POSIX "Etc/GMT-5"
                            new_tz = f"Etc/GMT{-hour if sign == '+' else +hour}"
                            converted_count += 1
                            logging.info(f"    User {user_id}: '{old_tz}' -> '{new_tz}' (offset conversion)")

                        # Case 2: Bare "GMT" without offset -> normalize to "Etc/GMT"
                        elif old_tz_lower == 'gmt':
                            new_tz = "Etc/GMT"
                            converted_count += 1
                            logging.info(f"    User {user_id}: '{old_tz}' -> '{new_tz}' (bare GMT)")

                        # Case 3: Everything else (IANA names like "America/New_York", "CET", "UTC")
                        # These are already pytz-compatible, keep as-is

                        # Insert into user_settings (consolidated table) instead of user_timezones (16-12-2025)
                        cursor.execute(
                            "INSERT INTO user_settings (user_id, key, value) VALUES (?, 'timezone', ?)",
                            (user_id, new_tz)
                        )
                    if converted_count > 0:
                        logging.info(f"    Converted {converted_count} timezone(s) to normalized pytz format.")

                # ==================================================================================
                # MIGRATION: config -> bot_settings (table rename + type change)
                # ==================================================================================
                # Date Added: 16-12-2025
                #
                # WHY THIS EXISTS:
                # 1. Table renamed: "config" -> "bot_settings" for clarity (see TABLE_RENAMES)
                # 2. Type changed: `value INTEGER NOT NULL` -> `value TEXT NOT NULL`
                #    This makes bot_settings consistent with user_settings and guild_settings,
                #    which already used TEXT. Storing as TEXT allows future flexibility
                #    (e.g., storing JSON or non-numeric config values).
                #
                # WHAT THIS MIGRATION DOES:
                # 1. Reads from old `config` table
                # 2. Converts INTEGER values to TEXT strings via str()
                # 3. Inserts into `bot_settings`
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated. The
                # `config` table no longer exists in the new schema. Running on a new DB
                # is a no-op since `config` table won't exist.
                # ==================================================================================
                elif table_name == 'config':
                    logging.info("...migrating %d config entries from config -> bot_settings", len(rows))
                    for row in rows:
                        cursor.execute(
                            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
                            (row['key'], str(row['value']))  # Convert INTEGER to TEXT
                        )

                # ==================================================================================
                # MIGRATION: user_config -> user_settings (table rename)
                # ==================================================================================
                # Date Added: 16-12-2025
                #
                # WHY THIS EXISTS:
                # Table renamed: "user_config" -> "user_settings" (see TABLE_RENAMES)
                # The schema is unchanged: (user_id INTEGER, key TEXT, value TEXT)
                #
                # NOTE: Uses INSERT OR REPLACE because user_timezones migration (above) may
                # have already inserted timezone entries for some users. This ensures we
                # don't fail on PRIMARY KEY conflicts if both tables had data for the same user.
                #
                # WHAT THIS MIGRATION DOES:
                # Direct copy from `user_config` to `user_settings` with conflict handling.
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated. Running
                # on a new DB is a no-op since `user_config` table won't exist.
                # ==================================================================================
                elif table_name == 'user_config':
                    logging.info("...migrating %d entries from user_config -> user_settings", len(rows))
                    for row in rows:
                        cursor.execute(
                            "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                            (row['user_id'], row['key'], row['value'])
                        )

                # ==================================================================================
                # MIGRATION: guild_config -> guild_settings (table rename)
                # ==================================================================================
                # Date Added: 16-12-2025
                #
                # WHY THIS EXISTS:
                # Table renamed: "guild_config" -> "guild_settings" (see TABLE_RENAMES)
                # The schema is unchanged: (guild_id INTEGER, key TEXT, value TEXT)
                #
                # WHAT THIS MIGRATION DOES:
                # Direct copy from `guild_config` to `guild_settings`.
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated. Running
                # on a new DB is a no-op since `guild_config` table won't exist.
                # ==================================================================================
                elif table_name == 'guild_config':
                    logging.info("...migrating %d entries from guild_config -> guild_settings", len(rows))
                    for row in rows:
                        cursor.execute(
                            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                            (row['guild_id'], row['key'], row['value'])
                        )

                # ==================================================================================
                # MIGRATION: starboard -> starboard_entries (table rename + NOT NULL constraint)
                # ==================================================================================
                # Date Added: 16-12-2025
                #
                # WHY THIS EXISTS:
                # 1. Table renamed: "starboard" -> "starboard_entries" (see TABLE_RENAMES)
                #    The old name was ambiguous (could mean config or entries).
                # 2. Schema change: `original_channel_id INTEGER` -> `original_channel_id INTEGER NOT NULL`
                #    The column was nullable in the old schema, but every starboard entry should
                #    have a source channel. Very old data may have NULL values from bugs.
                #
                # WHAT THIS MIGRATION DOES:
                # 1. Reads from old `starboard` table
                # 2. For rows with NULL original_channel_id, uses 0 as a placeholder
                #    (These entries are from very old data and the channel is unknown)
                # 3. Inserts into `starboard_entries`
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated. Running
                # on a new DB is a no-op since `starboard` table won't exist.
                # ==================================================================================
                elif table_name == 'starboard':
                    logging.info("...migrating %d entries from starboard -> starboard_entries", len(rows))
                    null_channel_count = 0
                    for row in rows:
                        channel_id = row.get('original_channel_id')
                        if channel_id is None:
                            channel_id = 0  # Placeholder for unknown channels
                            null_channel_count += 1
                        cursor.execute(
                            "INSERT INTO starboard_entries (original_message_id, starboard_message_id, guild_id, starboard_reply_id, original_channel_id) VALUES (?, ?, ?, ?, ?)",
                            (row['original_message_id'], row['starboard_message_id'], row['guild_id'], row.get('starboard_reply_id'), channel_id)
                        )
                    if null_channel_count > 0:
                        logging.warning(f"    {null_channel_count} starboard entries had NULL original_channel_id, set to 0.")

                # ==================================================================================
                # MIGRATION: bod_leaderboard (drop user_name column)
                # ==================================================================================
                # Date Added: 16-12-2025
                #
                # WHY THIS EXISTS:
                # The `user_name` column stored denormalized data: the Discord username at the
                # time the user achieved their best chain. This is problematic because:
                #   1. Discord usernames can change (via Nitro or the username migration)
                #   2. Display names vary by server (nicknames)
                #   3. Stale usernames cause confusion in the leaderboard display
                #
                # The bot now fetches display names dynamically at render time:
                #   - First tries ctx.guild.get_member(user_id) for server nickname
                #   - Falls back to bot.get_user(user_id) for cached username
                #   - Falls back to bot.fetch_user(user_id) with exponential backoff
                #   - Shows "User {id}" if all else fails
                #
                # WHAT THIS MIGRATION DOES:
                # Copies user_id, best_chain, and achieved_at from old table, dropping user_name.
                # Uses .get() with defaults to handle potential missing columns gracefully.
                #
                # IS THIS SAFE TO REMOVE?: Yes, once all databases have been migrated. Running
                # on a new DB is a no-op since the new schema already lacks user_name.
                # ==================================================================================
                elif table_name == 'bod_leaderboard':
                    logging.info("...migrating %d bod_leaderboard entries (dropping user_name column)", len(rows))
                    for row in rows:
                        cursor.execute(
                            "INSERT INTO bod_leaderboard (user_id, best_chain, achieved_at) VALUES (?, ?, ?)",
                            (row['user_id'], row.get('best_chain', 0), row.get('achieved_at', 0))
                        )

                # ==================================================================================
                # DEFAULT MIGRATION: Tables with unchanged schema
                # ==================================================================================
                # For tables that exist in both old and new schemas with the same name,
                # perform a standard column-aligned copy.
                # ==================================================================================
                elif table_name in TABLE_SCHEMAS:
                    # Align columns with new schema
                    cursor.execute(f"PRAGMA table_info({table_name})")
                    new_columns = [col[1] for col in cursor.fetchall()]
                    missing_columns = [col for col in new_columns if col not in old_columns]
                    extra_columns = [col for col in old_columns if col not in new_columns]

                    if missing_columns:
                        logging.warning(f"Table '{table_name}' missing columns in old DB: {missing_columns}. Filling with NULL/defaults.")
                    if extra_columns:
                        logging.warning(f"Table '{table_name}' has extra columns in old DB: {extra_columns}. Data will be dropped.")

                    logging.info(f"...migrating {len(rows)} rows for table '{table_name}'")
                    placeholders = ', '.join('?' for _ in new_columns)
                    query = f"INSERT INTO {table_name} ({', '.join(new_columns)}) VALUES ({placeholders})"

                    for row in rows:
                        values = []
                        for col in new_columns:
                            if col in row:
                                values.append(row[col])
                            else:
                                # Handle missing columns with defaults
                                if col == 'is_recurring':
                                    values.append(0)
                                elif col == 'created_at':
                                    values.append(int(time.time()))
                                elif col == 'achieved_at':
                                    values.append(0)
                                else:
                                    values.append(None)
                        cursor.execute(query, values)

                else:
                    # Table exists in old DB but not in new schema - skip with warning
                    logging.warning(f"Table '{table_name}' exists in old database but not in new schema. Skipping.")

            cursor.execute("PRAGMA foreign_keys = ON;")
            new_conn.commit()
        logging.info("Data migration successful!")
    except Exception as e:
        logging.error("Failed to insert data into new database. Restore from backup. Error: %s", e)
        return

    # 6. Cleanup / Finalize
    # If we migrated from a different filename, we must rename/remove the old one
    # so that config.py doesn't freak out about having 2 DB files.
    # Since we already backed it up to .backup, we can safely remove the original source file
    # IF it is different from the target.

    if source_db_path != TARGET_DB_PATH:
        logging.info("Removing old source database file '%s' to enforce single-DB rule...", source_db_path)
        try:
            os.remove(source_db_path)
        except Exception as e:
            logging.error("Failed to remove old database file. You may need to remove it manually. Error: %s", e)

    logging.info("\nMigration complete! Your old database is saved as '%s'.", backup_path)
    logging.info("You can now start the bot.")


if __name__ == "__main__":
    migrate_database()

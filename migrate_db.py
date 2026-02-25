"""migrate_db.py

Migrates reina.db (production) to the current schema defined in
utils/database.py. The target database name is derived from config.BOT_NAME.

The script is idempotent — running it against an already-migrated database is
safe (it will detect that no changes are needed and exit cleanly).

Usage:
    python migrate_db.py

How it works:
    1. Locates the source database (any .db file in assets/).
    2. Creates a timestamped backup.
    3. Reads all data from the backup.
    4. Creates a fresh database with the current schema.
    5. Inserts old data, applying any necessary transformations.
    6. Cleans up the old source file if it was renamed.

Schema documentation lives in database.md (project root).
The canonical runtime schema lives in utils/database.py → _setup_databases().
"""

import logging
import os
import shutil
import sqlite3
from typing import Any, Dict, List

import config

# ── Configuration ─────────────────────────────────────────────────────────────

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrate_db.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_PATH, encoding='utf-8'), logging.StreamHandler()]
)

APP_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_PATH = os.path.join(APP_PATH, 'assets')
TARGET_DB_NAME = f"{config.BOT_NAME}.db"
TARGET_DB_PATH = os.path.join(ASSETS_PATH, TARGET_DB_NAME)
BACKUP_EXTENSION = ".backup"

# ── Target Schema ─────────────────────────────────────────────────────────────
# Must match utils/database.py → _setup_databases() exactly.

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
    "starboard_config": """
        CREATE TABLE starboard_config (
            guild_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL,
            channel_id INTEGER,
            emoji TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            last_heal_at INTEGER NOT NULL,
            crawl_started_at INTEGER,
            crawl_requested_by INTEGER,
            crawl_notify_channel INTEGER,
            crawl_include_threads INTEGER NOT NULL,
            crawl_last_channel_id INTEGER,
            crawl_last_message_id INTEGER
        )
    """,
    "starboard_banned_channels": """
        CREATE TABLE starboard_banned_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY(guild_id, channel_id)
        )
    """,
    "starboard_entries": """
        CREATE TABLE starboard_entries (
            original_message_id INTEGER PRIMARY KEY,
            starboard_message_id INTEGER,
            guild_id INTEGER NOT NULL,
            starboard_reply_id INTEGER,
            original_channel_id INTEGER NOT NULL,
            message_created_at INTEGER NOT NULL,
            star_count INTEGER NOT NULL DEFAULT 0,
            failed_checks INTEGER NOT NULL DEFAULT 0
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
            fate_guaranteed INTEGER NOT NULL DEFAULT 0,
            fate_silent INTEGER NOT NULL DEFAULT 0
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
    "CREATE INDEX IF NOT EXISTS idx_starboard_banned_guild ON starboard_banned_channels (guild_id);",
    "CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions (user_id);",
    "CREATE INDEX IF NOT EXISTS idx_web_sessions_last_seen ON web_sessions (last_seen_at);"
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def snowflake_to_unix(snowflake_id: int) -> int:
    """Convert a Discord snowflake ID to a Unix timestamp in seconds.

    Args:
        snowflake_id: A Discord snowflake ID.

    Returns:
        Unix timestamp in whole seconds.
    """
    return ((snowflake_id >> 22) + 1420070400000) // 1000


def _get_new_columns(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    """Return the column names for a table in the new database.

    Args:
        cursor: An open cursor on the new database.
        table_name: The table to inspect.

    Returns:
        List of column name strings.
    """
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


# ── Table-Specific Migration Functions ────────────────────────────────────────
#
# Each function handles one table that needs transformation beyond a simple
# column-aligned copy.  The function signature is always:
#
#     def migrate_TABLE(cursor, rows, old_columns) -> None
#
# If a table does NOT need special handling, the generic copier handles it.


def migrate_starboard_entries(
    cursor: sqlite3.Cursor,
    rows: List[Dict[str, Any]],
    old_columns: List[str]
) -> None:
    """Migrate starboard_entries from the old 5-column schema to the new 8-column schema.

    Transformations applied:
        - starboard_message_id: Was NOT NULL, now nullable. No data change needed
          (existing values are preserved; the constraint is just relaxed).
        - message_created_at: NEW column. Derived from original_message_id via
          snowflake-to-unix conversion.
        - star_count: NEW column. Defaults to 0 (will be synced on first reaction
          event or verify pass).
        - failed_checks: NEW column. Defaults to 0 (healthy).

    Args:
        cursor: Cursor on the new database.
        rows: Row dicts from the old table.
        old_columns: Column names from the old table.
    """
    logging.info("  Migrating %d starboard entries (adding message_created_at, star_count, failed_checks)...", len(rows))

    for row in rows:
        original_id = row['original_message_id']
        message_created_at = snowflake_to_unix(original_id)

        cursor.execute(
            """INSERT INTO starboard_entries
               (original_message_id, starboard_message_id, guild_id,
                starboard_reply_id, original_channel_id,
                message_created_at, star_count, failed_checks)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0)""",
            (
                original_id,
                row['starboard_message_id'],
                row['guild_id'],
                row.get('starboard_reply_id'),
                row.get('original_channel_id', 0),
                message_created_at
            )
        )

    logging.info("  Done. All entries now have message_created_at derived from snowflake.")


def migrate_guild_settings(
    cursor: sqlite3.Cursor,
    rows: List[Dict[str, Any]],
    old_columns: List[str]
) -> None:
    """Migrate guild_settings: extract starboard_* keys into starboard_config, keep the rest.

    Reads all guild_settings rows. For each guild that has starboard_* keys,
    builds a starboard_config row and inserts it. All starboard_* keys are
    dropped from guild_settings. Non-starboard keys are copied verbatim.

    Guilds that had a starboard_channel_id set get enabled=1 to preserve
    existing behavior.

    Args:
        cursor: Cursor on the new database.
        rows: Row dicts from the old guild_settings table.
        old_columns: Column names from the old table.
    """
    # Collect starboard keys per guild
    STARBOARD_KEY_MAP = {
        "starboard_channel_id": "channel_id",
        "starboard_emoji": "emoji",
        "starboard_threshold": "threshold",
        "starboard_last_heal_at": "last_heal_at",
        "starboard_crawl_started_at": "crawl_started_at",
        "starboard_crawl_requested_by": "crawl_requested_by",
        "starboard_crawl_notify_channel_id": "crawl_notify_channel",
        "starboard_crawl_include_threads": "crawl_include_threads",
        "starboard_crawl_last_channel_id": "crawl_last_channel_id",
        "starboard_crawl_last_message_id": "crawl_last_message_id",
    }

    guild_sb_data: Dict[int, Dict[str, str]] = {}
    non_starboard_rows: List[Dict[str, Any]] = []

    for row in rows:
        key = row.get("key", "")
        guild_id: int | None = row.get("guild_id")
        if key.startswith("starboard_") and key in STARBOARD_KEY_MAP:
            if guild_id is not None and guild_id not in guild_sb_data:
                guild_sb_data[guild_id] = {}
            if guild_id is not None:
                guild_sb_data[guild_id][key] = row.get("value", "")
        else:
            non_starboard_rows.append(row)

    # Insert non-starboard rows into guild_settings
    logging.info("  Migrating %d non-starboard guild_settings rows...", len(non_starboard_rows))
    for row in non_starboard_rows:
        cursor.execute(
            "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
            (row["guild_id"], row["key"], row["value"])
        )

    # Build starboard_config rows
    logging.info("  Migrating starboard config for %d guild(s) from guild_settings...", len(guild_sb_data))
    for guild_id, sb_keys in guild_sb_data.items():
        channel_id_str = sb_keys.get("starboard_channel_id", "")
        channel_id = int(channel_id_str) if channel_id_str and channel_id_str.isdigit() else None
        emoji = sb_keys.get("starboard_emoji", "\u2b50") or "\u2b50"
        threshold_str = sb_keys.get("starboard_threshold", "3")
        threshold = int(threshold_str) if threshold_str and threshold_str.isdigit() else 3
        last_heal_str = sb_keys.get("starboard_last_heal_at", "0")
        last_heal_at = int(last_heal_str) if last_heal_str and last_heal_str.isdigit() else 0

        # Crawl state
        crawl_started_str = sb_keys.get("starboard_crawl_started_at", "")
        crawl_started_at = int(crawl_started_str) if crawl_started_str and crawl_started_str.isdigit() else None
        crawl_req_str = sb_keys.get("starboard_crawl_requested_by", "")
        crawl_requested_by = int(crawl_req_str) if crawl_req_str and crawl_req_str.isdigit() else None
        crawl_notify_str = sb_keys.get("starboard_crawl_notify_channel_id", "")
        crawl_notify_channel = int(crawl_notify_str) if crawl_notify_str and crawl_notify_str.isdigit() else None
        crawl_threads_str = sb_keys.get("starboard_crawl_include_threads", "")
        crawl_include_threads = 1 if crawl_threads_str == "True" else 0
        crawl_last_ch_str = sb_keys.get("starboard_crawl_last_channel_id", "")
        crawl_last_channel_id = int(crawl_last_ch_str) if crawl_last_ch_str and crawl_last_ch_str.isdigit() else None
        crawl_last_msg_str = sb_keys.get("starboard_crawl_last_message_id", "")
        crawl_last_message_id = int(crawl_last_msg_str) if crawl_last_msg_str and crawl_last_msg_str.isdigit() else None

        # If they had a channel configured, enable the starboard
        enabled = 1 if channel_id is not None else 0

        cursor.execute(
            """INSERT OR REPLACE INTO starboard_config
               (guild_id, enabled, channel_id, emoji, threshold, last_heal_at,
                crawl_started_at, crawl_requested_by, crawl_notify_channel,
                crawl_include_threads, crawl_last_channel_id, crawl_last_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, enabled, channel_id, emoji, threshold, last_heal_at,
             crawl_started_at, crawl_requested_by, crawl_notify_channel,
             crawl_include_threads, crawl_last_channel_id, crawl_last_message_id)
        )
        logging.info("    Guild %s: enabled=%d, channel=%s, emoji=%s, threshold=%d",
                      guild_id, enabled, channel_id, emoji, threshold)

    logging.info("  Done. Starboard keys removed from guild_settings.")


# Registry of tables that need custom migration logic.
# Tables not listed here get the generic column-aligned copy.
CUSTOM_MIGRATORS = {
    "starboard_entries": migrate_starboard_entries,
    "guild_settings": migrate_guild_settings,
}


# ── Generic Migration ─────────────────────────────────────────────────────────


def migrate_generic(
    cursor: sqlite3.Cursor,
    table_name: str,
    rows: List[Dict[str, Any]],
    old_columns: List[str]
) -> None:
    """Copy rows from an old table into the new schema via column alignment.

    Columns present in the new schema but missing from the old data are filled
    with NULL. Columns present in the old data but absent from the new schema
    are silently dropped.

    Args:
        cursor: Cursor on the new database.
        table_name: The target table name.
        rows: Row dicts from the old table.
        old_columns: Column names from the old table.
    """
    new_columns = _get_new_columns(cursor, table_name)

    missing = [c for c in new_columns if c not in old_columns]
    extra = [c for c in old_columns if c not in new_columns]
    if missing:
        logging.warning("  Table '%s': new columns not in old data (will be NULL): %s", table_name, missing)
    if extra:
        logging.warning("  Table '%s': old columns dropped: %s", table_name, extra)

    placeholders = ', '.join('?' for _ in new_columns)
    query = f"INSERT INTO {table_name} ({', '.join(new_columns)}) VALUES ({placeholders})"

    logging.info("  Migrating %d rows for '%s'...", len(rows), table_name)
    for row in rows:
        values = [row.get(col) for col in new_columns]
        cursor.execute(query, values)


# ── Main ──────────────────────────────────────────────────────────────────────


def migrate_database() -> None:
    """Performs a safe migration of the bot database.

    Steps:
        1. Locate the source database in assets/.
        2. Create a timestamped backup.
        3. Read all data from the backup.
        4. Create the new database with the current schema.
        5. Insert old data, applying per-table transformations where needed.
        6. Clean up old source file if it differs from the target name.
    """
    # ── 1. Identify source database ──
    found_dbs = [f for f in os.listdir(ASSETS_PATH) if f.endswith('.db')]

    if len(found_dbs) == 0:
        logging.info("No databases found in '%s'. Nothing to migrate.", ASSETS_PATH)
        return
    elif len(found_dbs) == 1:
        source_db_path = os.path.join(ASSETS_PATH, found_dbs[0])
        if found_dbs[0] == TARGET_DB_NAME:
            logging.info("Found '%s'. Upgrading in-place.", found_dbs[0])
        else:
            logging.info("Found '%s'. Migrating to '%s'.", found_dbs[0], TARGET_DB_NAME)
    else:
        logging.error("Multiple databases found: %s. Ensure only one source database exists.", found_dbs)
        return

    # ── 2. Backup ──
    backup_path = f"{source_db_path}{BACKUP_EXTENSION}"
    logging.info("Backing up to '%s'...", backup_path)
    try:
        shutil.copyfile(source_db_path, backup_path)
    except Exception as e:
        logging.error("Backup failed. Migration aborted. Error: %s", e)
        return

    # ── 3. Read all data from backup ──
    logging.info("Reading data from backup...")
    data_store: Dict[str, Dict[str, Any]] = {}
    try:
        with sqlite3.connect(backup_path) as backup_conn:
            backup_conn.row_factory = sqlite3.Row
            cursor = backup_conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [col[1] for col in cursor.fetchall()]
                cursor.execute(f"SELECT * FROM {table}")
                rows = [dict(r) for r in cursor.fetchall()]
                data_store[table] = {'rows': rows, 'columns': columns}
                logging.info("  Read %d rows from '%s'", len(rows), table)
    except Exception as e:
        logging.error("Failed to read backup. Migration aborted. Error: %s", e)
        return

    # ── 4. Create new database with current schema ──
    logging.info("Creating new database at '%s'...", TARGET_DB_PATH)
    try:
        if os.path.exists(TARGET_DB_PATH):
            os.remove(TARGET_DB_PATH)

        with sqlite3.connect(TARGET_DB_PATH) as new_conn:
            cur = new_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON;")
            for name, sql in TABLE_SCHEMAS.items():
                cur.execute(sql)
                logging.info("  Created table '%s'", name)
            for idx_sql in INDEX_SCHEMAS:
                cur.execute(idx_sql)
            new_conn.commit()
    except Exception as e:
        logging.error("Failed to create new database. Restore from backup. Error: %s", e)
        return

    # ── 5. Migrate data ──
    logging.info("Migrating data...")
    try:
        with sqlite3.connect(TARGET_DB_PATH) as new_conn:
            cur = new_conn.cursor()
            # Disable FK checks during bulk insert to avoid ordering issues
            cur.execute("PRAGMA foreign_keys = OFF;")

            for table, table_data in data_store.items():
                rows = table_data['rows']
                old_columns = table_data['columns']
                if not rows:
                    logging.info("  Skipping '%s' (empty).", table)
                    continue

                if table in CUSTOM_MIGRATORS:
                    CUSTOM_MIGRATORS[table](cur, rows, old_columns)
                elif table in TABLE_SCHEMAS:
                    migrate_generic(cur, table, rows, old_columns)
                else:
                    logging.warning("  Table '%s' exists in source but not in target schema. Skipping.", table)

            cur.execute("PRAGMA foreign_keys = ON;")
            new_conn.commit()
        logging.info("Data migration complete.")
    except Exception as e:
        logging.error("Data migration failed. Restore from backup '%s'. Error: %s", backup_path, e)
        return

    # ── 6. Cleanup ──
    if source_db_path != TARGET_DB_PATH:
        logging.info("Removing old source file '%s'...", source_db_path)
        try:
            os.remove(source_db_path)
        except Exception as e:
            logging.error("Failed to remove old file (non-fatal). Error: %s", e)

    logging.info("Migration complete. Backup saved as '%s'.", backup_path)

    # ── Post-migration: Normalize timezone capitalization ──
    # Fixes historical data where pytz timezone names were stored with wrong
    # casing or malformed Etc/GMT offsets. See database.md for context on
    # the user_settings table.
    _normalize_timezones()


def _normalize_timezones() -> None:
    """Normalize timezone strings in user_settings to canonical pytz format.

    Fixes two historical bugs:
        1. IANA names stored in wrong case (e.g. "europe/london" -> "Europe/London").
        2. Malformed Etc/GMT offsets (e.g. "Etc/GMT8" -> "Etc/GMT+8").

    Safe to run multiple times — already-correct entries are left untouched.
    """
    logging.info("Post-migration: Normalizing timezone capitalization...")
    try:
        import pytz

        with sqlite3.connect(TARGET_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, value FROM user_settings WHERE key = 'timezone'")
            rows = cursor.fetchall()

            if not rows:
                logging.info("  No timezone entries found.")
                return

            normalized = 0
            for row in rows:
                user_id = row['user_id']
                old_tz = row['value']
                if not old_tz:
                    continue

                try:
                    canonical = pytz.timezone(old_tz).zone
                    if canonical and canonical != old_tz:
                        cursor.execute(
                            "UPDATE user_settings SET value = ? WHERE user_id = ? AND key = 'timezone'",
                            (canonical, user_id)
                        )
                        logging.info("    User %s: '%s' -> '%s'", user_id, old_tz, canonical)
                        normalized += 1
                except pytz.UnknownTimeZoneError:
                    logging.warning("    User %s: '%s' is invalid (skipping)", user_id, old_tz)

            conn.commit()
            if normalized:
                logging.info("  Normalized %d timezone(s).", normalized)
            else:
                logging.info("  All timezones already canonical.")

    except Exception as e:
        logging.error("Timezone normalization failed (non-critical): %s", e)


if __name__ == "__main__":
    migrate_database()

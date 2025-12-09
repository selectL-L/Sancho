"""migrate_db.py

This script performs a safe migration of the bot database.
It backs up the existing database, creates a new one with the updated schema,
and migrates the data.

Usage:
    python migrate_db.py
"""

import logging
import os
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
# It matches the schema in `utils/database.py`, including the `COLLATE NOCASE` fix.
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
    "user_timezones": """
        CREATE TABLE user_timezones (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL
        )
    """,
    "user_config": """
        CREATE TABLE user_config (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(user_id, key)
        )
    """,
    "config": """
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """,
    "guild_config": """
        CREATE TABLE guild_config (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(guild_id, key)
        )
    """,
    "starboard": """
        CREATE TABLE starboard (
            original_message_id INTEGER PRIMARY KEY,
            starboard_message_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            starboard_reply_id INTEGER,
            original_channel_id INTEGER
        )
    """,
    "bod_usage": """
        CREATE TABLE bod_usage (
            user_id INTEGER PRIMARY KEY,
            last_used_timestamp INTEGER NOT NULL DEFAULT 0,
            current_chain INTEGER NOT NULL DEFAULT 0,
            last_channel_id INTEGER NOT NULL DEFAULT 0
        )
    """,
    "bod_leaderboard": """
        CREATE TABLE bod_leaderboard (
            user_id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL,
            best_chain INTEGER NOT NULL DEFAULT 0,
            achieved_at INTEGER NOT NULL DEFAULT 0
        )
    """
}

INDEX_SCHEMAS = [
    "CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders (reminder_time);",
    "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id);"
]


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

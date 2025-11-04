"""
database.py

This module contains the DatabaseManager class, which handles all interactions
with the SQLite database for the bot. It abstracts away the SQL queries and
provides a clean, asynchronous interface for cogs to use.

Note: All list-like data stored as strings, such as skill aliases, are separated
by a pipe character (|).

Responsibilities:
- Establishing a connection to the database.
- Creating necessary tables on startup (`setup_databases`).
- Handling all CRUD (Create, Read, Update, Delete) operations. Period.
"""

import time
import aiosqlite
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class DatabaseManager:
    """
    Manages all database operations for Sancho, providing an async interface
    for interacting with the SQLite database.
    """

    def __init__(self, db_path: str):
        """
        Initializes the DatabaseManager.

        Args:
            db_path (str): The file path to the SQLite database.
        """
        self.db_path = db_path
        self.skill_limit = 8  # Default skill limit, loaded from DB on startup.

    @classmethod
    async def create(cls, db_path: str) -> "DatabaseManager":
        """
        Creates and initializes a new DatabaseManager instance.

        This factory method handles the asynchronous setup, including creating
        tables and loading initial configuration from the database.

        Args:
            db_path (str): The file path to the SQLite database.

        Returns:
            DatabaseManager: A fully initialized DatabaseManager instance.
        """
        manager = cls(db_path)
        await manager._setup_databases()
        await manager._load_skill_limit()
        return manager

    async def _setup_databases(self) -> None:
        """
        Ensures all necessary tables exist in the database. This is called once
        on bot startup. It creates tables for skills, reminders, user timezones,
        and configurations if they don't already exist.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Stores user-created skills with their dice rolls and aliases.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    aliases TEXT,
                    dice_roll TEXT NOT NULL,
                    skill_type TEXT NOT NULL,
                    UNIQUE(user_id, name)
                )
            ''')
            # Stores reminders for users, including recurring ones.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    reminder_time INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    is_recurring INTEGER NOT NULL DEFAULT 0,
                    recurrence_rule TEXT
                )''')
            # Stores the preferred timezone for each user.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_timezones (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL
                )''')
            # Stores user-specific configurations, like a custom skill limit.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(user_id, key)
                )''')
            # Stores global bot configurations.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )''')

            # Stores guild-specific configurations for features like the starboard.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(guild_id, key)
                )''')

            # Stores messages that have been posted to the starboard.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS starboard (
                    original_message_id INTEGER PRIMARY KEY,
                    starboard_message_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    starboard_reply_id INTEGER
                )
            ''')
            
            # --- Schema Migrations ---
            # Add starboard_reply_id column if it doesn't exist (for older DBs)
            try:
                await db.execute("ALTER TABLE starboard ADD COLUMN starboard_reply_id INTEGER")
                logger.info("Migrated starboard table: Added 'starboard_reply_id' column.")
            except aiosqlite.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise # Re-raise if it's not the expected error
            
            # Set a default global skill limit if one isn't already in the database.
            cursor = await db.execute("SELECT value FROM config WHERE key = 'skill_limit'")
            if await cursor.fetchone() is None:
                await db.execute("INSERT INTO config (key, value) VALUES ('skill_limit', ?)", (self.skill_limit,))
            
            await db.commit()
            logger.info("All database tables initialized.")

    async def _load_skill_limit(self) -> None:
        """Loads the global skill limit from the database into the instance."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM config WHERE key = 'skill_limit'")
            row = await cursor.fetchone()
            if row:
                self.skill_limit = row[0]
                logger.info(f"Loaded skill limit from database: {self.skill_limit}")

    async def set_skill_limit(self, limit: int) -> None:
        """Sets the global skill limit in the database and updates the instance."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('skill_limit', ?)", (limit,))
            await db.commit()
        self.skill_limit = limit
        logger.info(f"Global skill limit set to {limit}.")

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        """Sets a configuration value for a specific guild."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)",
                (guild_id, key, value)
            )
            await db.commit()
        logger.info(f"Guild config for {guild_id} set: {key} = {value}")

    async def get_guild_config(self, guild_id: int, key: str) -> Optional[str]:
        """Gets a configuration value for a specific guild."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
                (guild_id, key)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def add_starboard_entry(self, original_message_id: int, starboard_message_id: int, guild_id: int, starboard_reply_id: Optional[int] = None) -> None:
        """Saves a new starboard entry to the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO starboard (original_message_id, starboard_message_id, guild_id, starboard_reply_id) VALUES (?, ?, ?, ?)",
                (original_message_id, starboard_message_id, guild_id, starboard_reply_id)
            )
            await db.commit()

    async def get_starboard_entry(self, original_message_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a starboard entry by the original message's ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM starboard WHERE original_message_id = ?", (original_message_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def remove_starboard_entry(self, original_message_id: int) -> None:
        """Removes a starboard entry from the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM starboard WHERE original_message_id = ?", (original_message_id,))
            await db.commit()

    async def set_user_skill_limit(self, user_id: int, limit: int) -> None:
        """Sets a skill limit override for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_config (user_id, key, value) VALUES (?, 'skill_limit', ?)",
                (user_id, str(limit))
            )
            await db.commit()
        logger.info(f"Skill limit for user {user_id} set to {limit}.")

    async def get_user_skill_limit(self, user_id: int) -> int:
        """
        Gets a user's skill limit, checking for a user-specific override
        before falling back to the global limit.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM user_config WHERE user_id = ? AND key = 'skill_limit'",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row and row[0].isdigit():
                return int(row[0])
        return self.skill_limit

    async def count_user_skills(self, user_id: int) -> int:
        """Counts the total number of skills a user has created."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM skills WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def save_skill(self, user_id: int, name: str, aliases: List[str], dice_roll: str, skill_type: str) -> None:
        """
        Saves a new skill or updates an existing one for a user (upsert).
        Uses `ON CONFLICT` to handle uniqueness for (user_id, name).
        """
        aliases_str = "|".join(aliases).lower()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO skills (user_id, name, aliases, dice_roll, skill_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET
                aliases=excluded.aliases,
                dice_roll=excluded.dice_roll,
                skill_type=excluded.skill_type
                """,
                (user_id, name.lower(), aliases_str, dice_roll, skill_type.lower())
            )
            await db.commit()

    async def get_skill(self, user_id: int, skill_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a skill by its name or one of its aliases for a specific user.
        The search is case-insensitive and matches against the name column or within
        the pipe-separated aliases string.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            skill_name_lower = skill_name.lower()
            # This query checks the name and also if the skill_name is present within the pipe-separated aliases string.
            # `INSTR` checks for a substring, and pipes are added to ensure whole-word matching.
            cursor = await db.execute(
                """
                SELECT * FROM skills
                WHERE user_id = ? AND (name = ? OR INSTR('|' || aliases || '|', '|' || ? || '|'))
                """,
                (user_id, skill_name_lower, skill_name_lower)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_skills(self, user_id: int) -> List[Dict[str, Any]]:
        """Retrieves all skills for a specific user, ordered by name."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM skills WHERE user_id = ? ORDER BY name ASC", (user_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_skills(self) -> List[Dict[str, Any]]:
        """Retrieves all skills for all users, ordered by user_id."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM skills ORDER BY user_id, name ASC")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_skill(self, user_id: int, skill_id: int) -> int:
        """Deletes a skill by its unique ID for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM skills WHERE id = ? AND user_id = ?", (skill_id, user_id))
            await db.commit()
            return cursor.rowcount

    async def add_reminder(
        self, user_id: int, channel_id: int, reminder_time: int, message: str,
        created_at: int, is_recurring: bool = False, recurrence_rule: Optional[str] = None
    ) -> Optional[int]:
        """Adds a reminder to the database and returns the new reminder's ID."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO reminders
                (user_id, channel_id, reminder_time, message, created_at, is_recurring, recurrence_rule)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, channel_id, reminder_time, message, created_at, 1 if is_recurring else 0, recurrence_rule)
            )
            await db.commit()
            return cursor.lastrowid

    async def update_reminder_time(self, reminder_id: int, new_time: int) -> None:
        """Updates the trigger time (`reminder_time`) for a specific reminder."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE reminders SET reminder_time = ? WHERE id = ?",
                (new_time, reminder_id)
            )
            await db.commit()

    async def get_due_reminders(self, current_time: int) -> List[Dict[str, Any]]:
        """Fetches all reminders that are due to be sent (time is in the past)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE reminder_time <= ?", (current_time,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_reminders(self) -> List[Dict[str, Any]]:
        """Retrieves all reminders for all users, ordered by user_id."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders ORDER BY user_id, reminder_time ASC")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminders(self, reminder_ids: List[int]) -> None:
        """Deletes one or more reminders from the database by their IDs."""
        if not reminder_ids:
            return
        async with aiosqlite.connect(self.db_path) as db:
            # Use a parameterized query to safely delete multiple IDs.
            await db.execute(f"DELETE FROM reminders WHERE id IN ({','.join('?' for _ in reminder_ids)})", reminder_ids)
            await db.commit()

    async def get_user_reminders(self, user_id: int) -> List[Dict[str, Any]]:
        """Fetches all reminders for a specific user, ordered by due time."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_reminder_by_id(self, reminder_id: int) -> Optional[Dict[str, Any]]:
        """Fetches a single reminder by its unique ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_timezone(self, user_id: int) -> Optional[str]:
        """Fetches a user's saved timezone string (e.g., 'America/New_York')."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT timezone FROM user_timezones WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_user_timezone(self, user_id: int, timezone: str) -> None:
        """Saves or updates a user's timezone."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO user_timezones (user_id, timezone) VALUES (?, ?)", (user_id, timezone))
            await db.commit()
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

    async def ping(self) -> float:
        """
        Performs a quick, simple query to the database to measure latency.

        Returns:
            float: The latency in milliseconds.
        """
        start_time = time.monotonic()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("SELECT 1")
        end_time = time.monotonic()
        return (end_time - start_time) * 1000

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
            await db.execute("PRAGMA foreign_keys = ON;")
            # Stores user-created skills with their dice rolls and aliases.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    dice_roll TEXT NOT NULL,
                    skill_type TEXT NOT NULL,
                    UNIQUE(user_id, name COLLATE NOCASE)
                )
            ''')
            # Stores aliases for skills, linked by skill_id.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS skill_aliases (
                    id INTEGER PRIMARY KEY,
                    skill_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE,
                    UNIQUE(skill_id, alias COLLATE NOCASE)
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
            # Add indexes for performance on the reminders table.
            await db.execute('CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders (reminder_time);')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id);')

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

            # Stores usage and chain data for the 'bod' command.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS bod_usage (
                    user_id INTEGER PRIMARY KEY,
                    last_used_timestamp INTEGER NOT NULL DEFAULT 0,
                    current_chain INTEGER NOT NULL DEFAULT 0,
                    last_channel_id INTEGER NOT NULL DEFAULT 0
                )
            ''')

            # Stores the global leaderboard for the 'bod' command.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS bod_leaderboard (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    best_chain INTEGER NOT NULL DEFAULT 0
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

            # Add last_channel_id to bod_usage if it doesn't exist
            try:
                await db.execute("ALTER TABLE bod_usage ADD COLUMN last_channel_id INTEGER NOT NULL DEFAULT 0")
                logger.info("Migrated bod_usage table: Added 'last_channel_id' column.")
            except aiosqlite.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
            
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

    async def get_bod_usage(self, user_id: int) -> Dict[str, Any]:
        """
        Retrieves the last usage time, current chain, and last channel for a user's 'bod' command.
        If the user is not in the table, it returns default values.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT last_used_timestamp, current_chain, last_channel_id FROM bod_usage WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {'last_used_timestamp': 0, 'current_chain': 0, 'last_channel_id': 0}

    async def update_bod_usage(self, user_id: int, last_used_timestamp: int, current_chain: int, channel_id: Optional[int] = None) -> None:
        """
        Updates or inserts a user's 'bod' command usage data.
        If channel_id is not provided, it remains unchanged.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if channel_id is not None:
                await db.execute(
                    "INSERT OR REPLACE INTO bod_usage (user_id, last_used_timestamp, current_chain, last_channel_id) VALUES (?, ?, ?, ?)",
                    (user_id, last_used_timestamp, current_chain, channel_id)
                )
            else:
                # This logic ensures we don't overwrite last_channel_id with 0 if it's not passed.
                await db.execute(
                    "INSERT INTO bod_usage (user_id, last_used_timestamp, current_chain, last_channel_id) VALUES (?, ?, ?, (SELECT last_channel_id FROM bod_usage WHERE user_id = ?))"
                    "ON CONFLICT(user_id) DO UPDATE SET last_used_timestamp = excluded.last_used_timestamp, current_chain = excluded.current_chain",
                    (user_id, last_used_timestamp, current_chain, user_id)
                )
            await db.commit()

    async def get_all_active_bod_chains(self) -> List[Dict[str, Any]]:
        """Retrieves all users who are currently in an active 'bod' chain."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT user_id, last_channel_id, current_chain FROM bod_usage WHERE current_chain > 0")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_bod_leaderboard(self) -> List[Dict[str, Any]]:
        """Retrieves the entire 'bod' leaderboard, ordered by best chain."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT user_name, best_chain FROM bod_leaderboard ORDER BY best_chain DESC")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_user_bod_best(self, user_id: int) -> int:
        """Retrieves a single user's best chain from the leaderboard."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT best_chain FROM bod_leaderboard WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def update_bod_leaderboard(self, user_id: int, user_name: str, chain_length: int) -> None:
        """Updates the 'bod' leaderboard with a user's new best score."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bod_leaderboard (user_id, user_name, best_chain) VALUES (?, ?, ?)",
                (user_id, user_name, chain_length)
            )
            await db.commit()
            logger.info(f"New BOD leaderboard score for {user_name}: {chain_length}.")

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
        Saves a new skill and its aliases to the database.
        This is a transactional operation to ensure data integrity.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            async with db.execute("BEGIN") as cursor:
                try:
                    # Insert the main skill
                    await cursor.execute(
                        "INSERT INTO skills (user_id, name, dice_roll, skill_type) VALUES (?, ?, ?, ?)",
                        (user_id, name, dice_roll, skill_type.lower())
                    )
                    skill_id = cursor.lastrowid

                    # Insert all aliases
                    if aliases and skill_id:
                        await cursor.executemany(
                            "INSERT INTO skill_aliases (skill_id, alias) VALUES (?, ?)",
                            [(skill_id, alias) for alias in aliases]
                        )
                except aiosqlite.Error as e:
                    await db.rollback()
                    logger.error(f"Failed to save skill '{name}': {e}")
                    raise
            await db.commit()

    async def get_skill(self, user_id: int, skill_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a skill by its name or one of its aliases for a specific user.
        It joins the skills and skill_aliases tables to perform the search.
        """
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type,
                   GROUP_CONCAT(sa.alias, '|') as aliases
            FROM skills s
            LEFT JOIN skill_aliases sa ON s.id = sa.skill_id
            WHERE s.user_id = ?
              AND (s.name = ? COLLATE NOCASE OR s.id IN (
                SELECT skill_id FROM skill_aliases WHERE alias = ? COLLATE NOCASE
              ))
            GROUP BY s.id
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, (user_id, skill_name, skill_name))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_skills(self, user_id: int) -> List[Dict[str, Any]]:
        """Retrieves all skills for a specific user, including their aliases."""
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type,
                   GROUP_CONCAT(sa.alias, '|') as aliases
            FROM skills s
            LEFT JOIN skill_aliases sa ON s.id = sa.skill_id
            WHERE s.user_id = ?
            GROUP BY s.id
            ORDER BY s.name ASC
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, (user_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_skills(self) -> List[Dict[str, Any]]:
        """Retrieves all skills for all users, including their aliases."""
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type,
                   GROUP_CONCAT(sa.alias, '|') as aliases
            FROM skills s
            LEFT JOIN skill_aliases sa ON s.id = sa.skill_id
            GROUP BY s.id
            ORDER BY s.user_id, s.name ASC
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_skill(self, user_id: int, skill_id: int) -> int:
        """
        Deletes a skill by its unique ID for a specific user.
        The `ON DELETE CASCADE` constraint will automatically delete its aliases.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            cursor = await db.execute("DELETE FROM skills WHERE id = ? AND user_id = ?", (skill_id, user_id))
            await db.commit()
            return cursor.rowcount

    async def update_skill(self, skill_id: int, user_id: int, updates: Dict[str, Any]) -> int:
        """
        Updates specific fields of a skill for a user.
        If aliases are updated, it replaces all existing aliases for the skill.
        """
        if not updates:
            return 0

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            rows_affected = 0
            async with db.execute("BEGIN") as cursor:
                try:
                    # Handle alias updates separately
                    if 'aliases' in updates:
                        new_aliases = updates.pop('aliases')
                        # Delete old aliases
                        await cursor.execute("DELETE FROM skill_aliases WHERE skill_id = ?", (skill_id,))
                        # Insert new ones
                        if new_aliases:
                            await cursor.executemany(
                                "INSERT INTO skill_aliases (skill_id, alias) VALUES (?, ?)",
                                [(skill_id, alias) for alias in new_aliases]
                            )

                    # Handle other field updates
                    if updates:
                        set_clause = ", ".join(f"{key} = ?" for key in updates.keys())
                        params = list(updates.values())
                        params.extend([skill_id, user_id])
                        query = f"UPDATE skills SET {set_clause} WHERE id = ? AND user_id = ?"
                        await cursor.execute(query, params)
                    
                    rows_affected = cursor.rowcount

                except aiosqlite.Error as e:
                    await db.rollback()
                    logger.error(f"Failed to update skill {skill_id}: {e}")
                    raise
            await db.commit()
            return rows_affected


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
# NOTE: All list-like data stored as strings in this database,
# such as skill aliases, should be separated by a pipe character (|).

import aiosqlite
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all database operations for Sancho."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.skill_limit = 8  # Default skill limit per user

    async def setup_databases(self) -> None:
        """Ensures all necessary tables exist in the database."""
        async with aiosqlite.connect(self.db_path) as db:
            # Skills Table
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
            # Reminders Table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    reminder_time INTEGER NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL
                )''')
            # User Timezones Table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_timezones (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL
                )''')
            # Skill Limit Config Table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )''')
            
            # Set default skill limit if not present
            cursor = await db.execute("SELECT value FROM config WHERE key = 'skill_limit'")
            if await cursor.fetchone() is None:
                await db.execute("INSERT INTO config (key, value) VALUES ('skill_limit', ?)", (self.skill_limit,))
            
            await db.commit()
            logger.info("All database tables initialized.")

    async def load_skill_limit(self) -> None:
        """Loads the skill limit from the database."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM config WHERE key = 'skill_limit'")
            row = await cursor.fetchone()
            if row:
                self.skill_limit = row[0]
                logger.info(f"Loaded skill limit from database: {self.skill_limit}")

    async def set_skill_limit(self, limit: int) -> None:
        """Sets the global skill limit."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('skill_limit', ?)", (limit,))
            await db.commit()
        self.skill_limit = limit
        logger.info(f"Global skill limit set to {limit}.")

    async def count_user_skills(self, user_id: int) -> int:
        """Counts the number of skills a user has."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM skills WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def save_skill(self, user_id: int, name: str, aliases: List[str], dice_roll: str, skill_type: str) -> None:
        """Saves a new skill or updates an existing one for a user."""
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
        """Retrieves a skill by its name or one of its aliases for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            skill_name_lower = skill_name.lower()
            # This query checks the name and also if the skill_name is present within the pipe-separated aliases string.
            # The INSTR function is a good way to check for substrings in SQLite.
            # We add pipes around both the aliases column and the search term to ensure we match whole words.
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
        """Retrieves all skills for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM skills WHERE user_id = ? ORDER BY name ASC", (user_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_skill(self, user_id: int, skill_id: int) -> int:
        """Deletes a skill by its ID for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM skills WHERE id = ? AND user_id = ?", (skill_id, user_id))
            await db.commit()
            return cursor.rowcount

    async def add_reminder(self, user_id: int, channel_id: int, reminder_time: int, message: str, created_at: int) -> None:
        """Adds a reminder to the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO reminders (user_id, channel_id, reminder_time, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, channel_id, reminder_time, message, created_at)
            )
            await db.commit()

    async def get_due_reminders(self, current_time: int) -> List[Dict[str, Any]]:
        """Fetches reminders that are due."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE reminder_time <= ?", (current_time,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminders(self, reminder_ids: List[int]) -> None:
        """Deletes reminders by their IDs."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"DELETE FROM reminders WHERE id IN ({','.join('?' for _ in reminder_ids)})", reminder_ids)
            await db.commit()

    async def get_user_reminders(self, user_id: int) -> List[Dict[str, Any]]:
        """Fetches all reminders for a specific user."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, reminder_time, message FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_user_timezone(self, user_id: int) -> Optional[str]:
        """Fetches a user's timezone."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT timezone FROM user_timezones WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_user_timezone(self, user_id: int, timezone: str) -> None:
        """Sets a user's timezone."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO user_timezones (user_id, timezone) VALUES (?, ?)", (user_id, timezone))
            await db.commit()
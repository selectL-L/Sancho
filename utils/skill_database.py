import aiosqlite
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class SkillDatabase:
    """Handles all database operations for user-defined skills."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def setup_database(self) -> None:
        """Ensures the skills table exists in the database."""
        async with aiosqlite.connect(self.db_path) as db:
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
            await db.commit()
            logger.info("Skills database table initialized.")

    async def save_skill(self, user_id: int, name: str, aliases: List[str], dice_roll: str, skill_type: str) -> None:
        """Saves a new skill or updates an existing one for a user."""
        aliases_str = ",".join(aliases).lower()
        async with aiosqlite.connect(self.db_path) as db:
            # Use INSERT OR REPLACE to handle both new skills and updates.
            # We need to select the existing skill ID if we want to replace it without changing the ID.
            # For simplicity, we'll just replace the whole row if the name matches.
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
            # Search by name OR by alias. The aliases are stored as a comma-separated string.
            cursor = await db.execute(
                """
                SELECT * FROM skills
                WHERE user_id = ? AND (name = ? OR INSTR(',' || aliases || ',', ',' || ? || ','))
                """,
                (user_id, skill_name.lower(), skill_name.lower())
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

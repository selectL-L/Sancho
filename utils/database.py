"""utils/database.py

This module contains the DatabaseManager class, which handles all interactions
with the SQLite database for the bot. It abstracts away the SQL queries and
provides a clean, asynchronous interface for cogs to use.

Note: All list-like data stored as strings, such as skill aliases, are separated
by a pipe character (|).

Responsibilities:
- Establishing a connection to the database.
- Creating necessary tables on startup (`setup_databases`).
- Handling all CRUD (Create, Read, Update, Delete) operations.

Methods are organized alphabetically by the cog that primarily uses them.
Generic/shared methods appear in the "Core / Shared" section. Each method
includes a "Used By:" line documenting which cog(s) call it.
"""

import logging
import shutil
import time
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages all database operations for the bot.

    Provides an async interface for interacting with the SQLite database.

    Methods are organized into sections alphabetically by cog name:
    - Core / Shared: Fundamental methods used by multiple cogs (includes Admin utilities)
    - Fun Cog: BOD command game mechanics
    - Help Cog: (Placeholder - no DB operations yet)
    - Image Cog: (Placeholder - no DB operations yet)
    - Math Cog: (Placeholder - no DB operations yet)
    - Reminders Cog: Reminder scheduling and user timezone management
    - Skills Cog: User skill management
    - Starboard Cog: Starboard tracking and guild configuration

    Note: The Admin cog uses methods from multiple sections (Core, Skills, Reminders)
    for administrative operations. Look for "Used By: cogs/admin.py" in docstrings.
    """

    def __init__(self, db_path: str):
        """Initializes the DatabaseManager.

        Args:
            db_path (str): The file path to the SQLite database.
        """
        self.db_path = db_path
        self.skill_limit = 8  # Default skill limit, loaded from DB on startup.

    # ==========================================================================
    # CORE / SHARED METHODS
    # These methods are used by multiple cogs or are fundamental to the system.
    # ==========================================================================

    @classmethod
    async def create(cls, db_path: str) -> "DatabaseManager":
        """Creates and initializes a new DatabaseManager instance.

        This factory method handles the asynchronous setup, including creating
        tables and loading initial configuration from the database.

        Used By: utils/lifecycle.py (bot startup)

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
        """Ensures all necessary tables exist in the database.

        Creates missing tables automatically.
        Checks for schema mismatches in existing tables and warns if found.

        Used By: DatabaseManager.create (internal)
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")

            # Define Table Schemas (Creation SQL)
            # Note: Table name descriptions are in migrate_db.py for easier reference.
            table_schemas = {
                "skills": '''CREATE TABLE IF NOT EXISTS skills (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        dice_roll TEXT NOT NULL,
                        skill_type TEXT NOT NULL,
                        description TEXT,
                        UNIQUE(user_id, name COLLATE NOCASE)
                    )''',
                "skill_aliases": '''CREATE TABLE IF NOT EXISTS skill_aliases (
                        id INTEGER PRIMARY KEY,
                        skill_id INTEGER NOT NULL,
                        alias TEXT NOT NULL,
                        FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE,
                        UNIQUE(skill_id, alias COLLATE NOCASE)
                    )''',
                "reminders": '''CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        reminder_time INTEGER NOT NULL,
                        message TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        is_recurring INTEGER NOT NULL DEFAULT 0,
                        recurrence_rule TEXT,
                        reply_message_id INTEGER
                    )''',
                "schedule_availability": '''CREATE TABLE IF NOT EXISTS schedule_availability (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        slot TEXT NOT NULL,
                        UNIQUE(user_id, slot)
                    )''',
                "schedule_availability_meta": '''CREATE TABLE IF NOT EXISTS schedule_availability_meta (
                        user_id INTEGER PRIMARY KEY,
                        updated_at INTEGER NOT NULL
                    )''',
                "schedule_guild_visibility": '''CREATE TABLE IF NOT EXISTS schedule_guild_visibility (
                        user_id INTEGER NOT NULL,
                        guild_id INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, guild_id)
                    )''',
                "schedule_user_blacklist": '''CREATE TABLE IF NOT EXISTS schedule_user_blacklist (
                        user_id INTEGER NOT NULL,
                        blocked_user_id INTEGER NOT NULL,
                        PRIMARY KEY (user_id, blocked_user_id)
                    )''',
                "user_settings": '''CREATE TABLE IF NOT EXISTS user_settings (
                        user_id INTEGER NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY(user_id, key)
                    )''',
                "bot_settings": '''CREATE TABLE IF NOT EXISTS bot_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )''',
                "guild_settings": '''CREATE TABLE IF NOT EXISTS guild_settings (
                        guild_id INTEGER NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY(guild_id, key)
                    )''',
                "starboard_entries": '''CREATE TABLE IF NOT EXISTS starboard_entries (
                        original_message_id INTEGER PRIMARY KEY,
                        starboard_message_id INTEGER NOT NULL,
                        guild_id INTEGER NOT NULL,
                        starboard_reply_id INTEGER,
                        original_channel_id INTEGER NOT NULL
                    )''',
                "bod_players": '''CREATE TABLE IF NOT EXISTS bod_players (
                        user_id INTEGER PRIMARY KEY,
                        last_used_timestamp INTEGER NOT NULL DEFAULT 0,
                        current_chain INTEGER NOT NULL DEFAULT 0,
                        last_channel_id INTEGER NOT NULL DEFAULT 0,
                        fate_lucky INTEGER NOT NULL DEFAULT 0,
                        fate_blessed INTEGER NOT NULL DEFAULT 0,
                        fate_guaranteed INTEGER NOT NULL DEFAULT 0
                    )''',
                "bod_leaderboard": '''CREATE TABLE IF NOT EXISTS bod_leaderboard (
                        user_id INTEGER PRIMARY KEY,
                        best_chain INTEGER NOT NULL DEFAULT 0,
                        achieved_at INTEGER NOT NULL DEFAULT 0
                    )''',
                "proxy_usage": '''CREATE TABLE IF NOT EXISTS proxy_usage (
                        id INTEGER PRIMARY KEY,
                        year_month TEXT NOT NULL UNIQUE,
                        track_count INTEGER NOT NULL DEFAULT 0,
                        bytes_used INTEGER NOT NULL DEFAULT 0,
                        last_updated INTEGER NOT NULL
                    )''',
                "users": '''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL,
                        avatar TEXT,
                        last_seen INTEGER NOT NULL
                    )''',
                "web_sessions": '''CREATE TABLE IF NOT EXISTS web_sessions (
                        session_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        access_token TEXT NOT NULL,
                        refresh_token TEXT NOT NULL,
                        token_expires_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        last_seen_at INTEGER NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    )'''
            }

            # Get existing tables
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table';")
            existing_tables = {row[0] async for row in cursor}

            # Create missing tables
            for table, sql in table_schemas.items():
                if table not in existing_tables:
                    await db.execute(sql)
                    logger.info(f"Created missing table: {table}")
                    existing_tables.add(table)

            # Create Indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders(reminder_time)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_schedule_availability_user ON schedule_availability(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_schedule_guild_visibility_guild ON schedule_guild_visibility(guild_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_skills_user ON skills(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_starboard_guild ON starboard_entries(guild_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_last_seen ON web_sessions(last_seen_at)")

            await db.commit()

            # Check for schema mismatches (Columns)
            expected_schema = {
                "skills": {"id", "user_id", "name", "dice_roll", "skill_type", "description"},
                "skill_aliases": {"id", "skill_id", "alias"},
                "reminders": {"id", "user_id", "channel_id", "reminder_time", "message", "created_at", "is_recurring", "recurrence_rule", "reply_message_id"},
                "schedule_availability": {"id", "user_id", "slot"},
                "schedule_availability_meta": {"user_id", "updated_at"},
                "schedule_guild_visibility": {"user_id", "guild_id", "enabled"},
                "schedule_user_blacklist": {"user_id", "blocked_user_id"},
                "user_settings": {"user_id", "key", "value"},
                "bot_settings": {"key", "value"},
                "guild_settings": {"guild_id", "key", "value"},
                "starboard_entries": {"original_message_id", "starboard_message_id", "guild_id", "starboard_reply_id", "original_channel_id"},
                "bod_players": {"user_id", "last_used_timestamp", "current_chain", "last_channel_id", "fate_lucky", "fate_blessed", "fate_guaranteed"},
                "bod_leaderboard": {"user_id", "best_chain", "achieved_at"},
                "proxy_usage": {"id", "year_month", "track_count", "bytes_used", "last_updated"},
                "users": {"user_id", "username", "avatar", "last_seen"},
                "web_sessions": {"session_id", "user_id", "access_token", "refresh_token", "token_expires_at", "created_at", "last_seen_at"}
            }

            schema_issues = []

            # Check for column mismatches in expected tables
            for table, expected_columns in expected_schema.items():
                if table in existing_tables:
                    cursor = await db.execute(f"PRAGMA table_info({table});")
                    columns = {row[1] async for row in cursor}
                    if columns != expected_columns:
                        missing_cols = expected_columns - columns
                        extra_cols = columns - expected_columns
                        issue_parts = []
                        if missing_cols:
                            issue_parts.append(f"missing: {missing_cols}")
                        if extra_cols:
                            issue_parts.append(f"extra: {extra_cols}")
                        schema_issues.append(f"Table '{table}' mismatch ({', '.join(issue_parts)})")

            # Check for orphaned tables (exist in DB but not in expected schema)
            # Exclude sqlite internal tables
            expected_table_names = set(expected_schema.keys())
            orphaned_tables = existing_tables - expected_table_names - {"sqlite_sequence"}
            if orphaned_tables:
                schema_issues.append(f"Orphaned tables found: {orphaned_tables}")

            if schema_issues:
                issue_summary = "; ".join(schema_issues)
                await self._warn_and_backup_db(issue_summary)
            else:
                logger.info("Database schema verified.")

    async def _warn_and_backup_db(self, issue: str) -> None:
        """Creates a backup of the database and logs a warning about schema issues.

        Used By: _setup_databases (internal)

        Args:
            issue (str): Description of the schema issue detected.
        """
        backup_path = self.db_path + ".backup"
        shutil.copyfile(self.db_path, backup_path)
        logger.warning(f"Database schema issue detected: {issue}. A backup has been created at {backup_path}. Please run migrate_db.py at your earliest convenience.")

    async def _load_skill_limit(self) -> None:
        """Loads the global skill limit from the database into the instance.

        Used By: DatabaseManager.create (internal, startup)
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM bot_settings WHERE key = 'skill_limit'")
            row = await cursor.fetchone()
            if row:
                self.skill_limit = int(row[0])
                logger.info(f"Loaded skill limit from database: {self.skill_limit}")

    async def set_skill_limit(self, limit: int) -> None:
        """Sets the global skill limit in the database and updates the instance.

        Used By: cogs/admin.py (global_limit command)

        Args:
            limit (int): The new skill limit.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('skill_limit', ?)", (str(limit),))
            await db.commit()
        self.skill_limit = limit
        logger.info(f"Global skill limit set to {limit}.")

    async def ping(self) -> float:
        """Performs a quick, simple query to the database to measure latency.

        Used By: cogs/admin.py (status command)

        Returns:
            float: The latency in milliseconds.
        """
        start_time = time.monotonic()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("SELECT 1")
        end_time = time.monotonic()
        return (end_time - start_time) * 1000

    async def db_fetchall(self, query: str, params: tuple = ()) -> List[aiosqlite.Row]:
        """Executes a raw SQL query and returns all results.

        This method is intended for administrative/export purposes only.
        Prefer using specific methods for normal operations.

        Used By: cogs/admin.py (dump_database callback for raw table exports)

        Args:
            query: The SQL query to execute.
            params: Optional parameters for the query.

        Returns:
            List of Row objects that can be converted to dicts.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                return list(await cursor.fetchall())

    async def set_user_config(self, user_id: int, key: str, value: str) -> None:
        """Sets a generic configuration value for a specific user.

        Used By: cogs/reminders.py (reminder_destination, timezone),
                 cogs/skills.py (via set_user_skill_limit)

        Args:
            user_id (int): The user's ID.
            key (str): The configuration key.
            value (str): The configuration value.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                (user_id, key, value)
            )
            await db.commit()
        logger.info(f"User setting for {user_id} set: {key} = {value}")

    async def get_user_config(self, user_id: int, key: str) -> Optional[str]:
        """Gets a generic configuration value for a specific user.

        Used By: cogs/reminders.py (reminder_destination, timezone),
                 cogs/skills.py (via get_user_skill_limit)

        Args:
            user_id (int): The user's ID.
            key (str): The configuration key.

        Returns:
            Optional[str]: The configuration value, or None if not found.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    # ==========================================================================
    # FUN COG METHODS
    # Methods for the 'bod' (Boundary of Death) game mechanics.
    # ==========================================================================

    async def get_bod_player(self, user_id: int) -> Dict[str, Any]:
        """Retrieves the BOD player data including fate bank.

        If the user is not in the table, it returns default values.

        Used By: cogs/fun.py (bod command, _handle_bod_session_timeout)

        Args:
            user_id (int): The user's ID.

        Returns:
            Dict[str, Any]: A dictionary containing player data with keys:
                            'last_used_timestamp', 'current_chain', 'last_channel_id',
                            'fate_lucky', 'fate_blessed', 'fate_guaranteed'.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT last_used_timestamp, current_chain, last_channel_id, fate_lucky, fate_blessed, fate_guaranteed "
                "FROM bod_players WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {'last_used_timestamp': 0, 'current_chain': 0, 'last_channel_id': 0,
                    'fate_lucky': 0, 'fate_blessed': 0, 'fate_guaranteed': 0}

    async def update_bod_player(self, user_id: int, last_used_timestamp: int, current_chain: int, channel_id: Optional[int] = None) -> None:
        """Updates or inserts a user's BOD player data.

        If channel_id is not provided, it remains unchanged.
        Fate columns are preserved during updates.

        Used By: cogs/fun.py (bod command, _handle_bod_session_timeout, _cleanup_bod_chains)

        Args:
            user_id (int): The user's ID.
            last_used_timestamp (int): The timestamp of the last usage.
            current_chain (int): The current chain length.
            channel_id (Optional[int]): The channel ID where the command was used.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if channel_id is not None:
                await db.execute(
                    "INSERT INTO bod_players (user_id, last_used_timestamp, current_chain, last_channel_id) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "last_used_timestamp = excluded.last_used_timestamp, "
                    "current_chain = excluded.current_chain, "
                    "last_channel_id = excluded.last_channel_id",
                    (user_id, last_used_timestamp, current_chain, channel_id)
                )
            else:
                # Ensure we don't overwrite last_channel_id with 0 if it's not passed.
                await db.execute(
                    "INSERT INTO bod_players (user_id, last_used_timestamp, current_chain, last_channel_id) "
                    "VALUES (?, ?, ?, (SELECT last_channel_id FROM bod_players WHERE user_id = ?)) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "last_used_timestamp = excluded.last_used_timestamp, current_chain = excluded.current_chain",
                    (user_id, last_used_timestamp, current_chain, user_id)
                )
            await db.commit()

    async def get_all_active_bod_chains(self) -> List[Dict[str, Any]]:
        """Retrieves all users who are currently in an active 'bod' chain.

        Used By: cogs/fun.py (_cleanup_bod_chains in cog_ready)

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing user chain data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT user_id, last_channel_id, current_chain FROM bod_players WHERE current_chain > 0")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_bod_leaderboard(self) -> List[Dict[str, Any]]:
        """Retrieves the entire 'bod' leaderboard, ordered by best chain.

        Used By: cogs/fun.py (bod_leaderboard command)

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing leaderboard data.
                Keys: user_id, best_chain, achieved_at
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, best_chain, achieved_at FROM bod_leaderboard ORDER BY best_chain DESC, achieved_at ASC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_user_bod_best(self, user_id: int) -> int:
        """Retrieves a single user's best chain from the leaderboard.

        Used By: cogs/fun.py (bod command, _handle_bod_session_timeout, _cleanup_bod_chains)

        Args:
            user_id (int): The user's ID.

        Returns:
            int: The user's best chain length.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT best_chain FROM bod_leaderboard WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def update_bod_leaderboard(self, user_id: int, chain_length: int, achieved_at: int = 0) -> None:
        """Updates the 'bod' leaderboard with a user's new best score.

        Used By: cogs/fun.py (bod command, _handle_bod_session_timeout, _cleanup_bod_chains)

        Args:
            user_id (int): The user's ID.
            chain_length (int): The new best chain length.
            achieved_at (int): The timestamp when the chain was achieved.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bod_leaderboard (user_id, best_chain, achieved_at) VALUES (?, ?, ?)",
                (user_id, chain_length, achieved_at)
            )
            await db.commit()
            logger.info(f"New BOD leaderboard score for user {user_id}: {chain_length} at {achieved_at}.")

    async def get_bod_fate(self, user_id: int) -> Dict[str, int]:
        """Get a user's fate bank counts.

        Used By: cogs/fun.py (bod command), cogs/admin.py (bod_fate command)

        Args:
            user_id (int): The Discord user ID.

        Returns:
            Dict with keys 'lucky', 'blessed', 'guaranteed' and their counts.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT fate_lucky, fate_blessed, fate_guaranteed FROM bod_players WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {'lucky': row[0], 'blessed': row[1], 'guaranteed': row[2]}
            return {'lucky': 0, 'blessed': 0, 'guaranteed': 0}

    async def add_bod_fate(self, user_id: int, tier: str, count: int = 1) -> None:
        """Add fate to a user's bank.

        Used By: cogs/fun.py (quote triggers), cogs/admin.py (bod_bless command)

        Args:
            user_id (int): The Discord user ID.
            tier (str): One of 'LUCKY', 'BLESSED', 'GUARANTEED'.
            count (int): Amount to add (default 1).
        """
        tier_lower = tier.lower()
        column = f"fate_{tier_lower}"
        if column not in ('fate_lucky', 'fate_blessed', 'fate_guaranteed'):
            raise ValueError(f"Invalid fate tier: {tier}")

        async with aiosqlite.connect(self.db_path) as db:
            # Ensure user row exists, then increment
            await db.execute(
                f"INSERT INTO bod_players (user_id, {column}) VALUES (?, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET {column} = {column} + ?",
                (user_id, count, count)
            )
            await db.commit()
            logger.debug(f"Added {count} {tier} fate to user {user_id}.")

    async def consume_bod_fate(self, user_id: int, tier: str) -> bool:
        """Consume one fate from a user's bank.

        Used By: cogs/fun.py (bod command)

        Args:
            user_id (int): The Discord user ID.
            tier (str): One of 'LUCKY', 'BLESSED', 'GUARANTEED'.

        Returns:
            True if fate was consumed, False if user had none of that tier.
        """
        tier_lower = tier.lower()
        column = f"fate_{tier_lower}"
        if column not in ('fate_lucky', 'fate_blessed', 'fate_guaranteed'):
            raise ValueError(f"Invalid fate tier: {tier}")

        async with aiosqlite.connect(self.db_path) as db:
            # Check current count
            cursor = await db.execute(
                f"SELECT {column} FROM bod_players WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row or row[0] <= 0:
                return False

            # Decrement
            await db.execute(
                f"UPDATE bod_players SET {column} = {column} - 1 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()
            logger.debug(f"Consumed 1 {tier} fate from user {user_id}.")
            return True

    async def clear_bod_fate(self, user_id: int, tier: Optional[str] = None) -> None:
        """Clear fate from a user's bank.

        Used By: cogs/admin.py (bod_clear command)

        Args:
            user_id (int): The Discord user ID.
            tier (Optional[str]): Specific tier to clear, or None for all tiers.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if tier is None:
                # Clear all fate
                await db.execute(
                    "UPDATE bod_players SET fate_lucky = 0, fate_blessed = 0, fate_guaranteed = 0 "
                    "WHERE user_id = ?",
                    (user_id,)
                )
            else:
                tier_lower = tier.lower()
                column = f"fate_{tier_lower}"
                if column not in ('fate_lucky', 'fate_blessed', 'fate_guaranteed'):
                    raise ValueError(f"Invalid fate tier: {tier}")
                await db.execute(
                    f"UPDATE bod_players SET {column} = 0 WHERE user_id = ?",
                    (user_id,)
                )
            await db.commit()
            logger.debug(f"Cleared {'all' if tier is None else tier} fate from user {user_id}.")

    # ==========================================================================
    # HELP COG METHODS
    # (No database operations required for this cog yet.)
    # ==========================================================================

    # ==========================================================================
    # IMAGE COG METHODS
    # (No database operations required for this cog yet.)
    # ==========================================================================

    # ==========================================================================
    # MATH COG METHODS
    # (No database operations required for this cog yet.)
    # ==========================================================================

    # ==========================================================================
    # MUSIC COG METHODS
    # Methods for proxy usage tracking (residential proxy fallback).
    # ==========================================================================

    async def increment_proxy_usage(self, bytes_downloaded: int) -> None:
        """Increment proxy usage for the current month.

        Adds to the running total of bytes used and increments track count.
        Creates a new row if this is the first usage this month.

        Used By: cogs/music.py (residential proxy fallback)

        Args:
            bytes_downloaded (int): Number of bytes downloaded via proxy.
        """
        year_month = time.strftime('%Y-%m')
        current_time = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            # Try to update existing row first
            cursor = await db.execute(
                """UPDATE proxy_usage
                   SET track_count = track_count + 1,
                       bytes_used = bytes_used + ?,
                       last_updated = ?
                   WHERE year_month = ?""",
                (bytes_downloaded, current_time, year_month)
            )
            if cursor.rowcount == 0:
                # No existing row, insert new one
                await db.execute(
                    """INSERT INTO proxy_usage (year_month, track_count, bytes_used, last_updated)
                       VALUES (?, 1, ?, ?)""",
                    (year_month, bytes_downloaded, current_time)
                )
            await db.commit()
            logger.debug(f"Proxy usage updated: +{bytes_downloaded} bytes for {year_month}")

    async def get_proxy_usage(self, year_month: Optional[str] = None) -> Dict[str, Any]:
        """Get proxy usage stats for a specific month.

        Used By: cogs/music.py (cost tracking), cogs/admin.py (usage reports)

        Args:
            year_month (str, optional): Month in 'YYYY-MM' format. Defaults to current month.

        Returns:
            Dict with keys: year_month, track_count, bytes_used, last_updated, estimated_cost_usd.
            Returns zeros if no usage recorded for that month.
        """
        if year_month is None:
            year_month = time.strftime('%Y-%m')

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM proxy_usage WHERE year_month = ?",
                (year_month,)
            )
            row = await cursor.fetchone()

            if row:
                result = dict(row)
                # Add estimated cost (Decodo: $4/GB)
                result['estimated_cost_usd'] = (result['bytes_used'] / (1024 ** 3)) * 4.0
                return result
            else:
                return {
                    'year_month': year_month,
                    'track_count': 0,
                    'bytes_used': 0,
                    'last_updated': 0,
                    'estimated_cost_usd': 0.0
                }

    async def get_all_proxy_usage(self) -> List[Dict[str, Any]]:
        """Get all proxy usage records, ordered by month descending.

        Used By: cogs/admin.py (monthly reports)

        Returns:
            List of usage records with estimated costs.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM proxy_usage ORDER BY year_month DESC"
            )
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                record = dict(row)
                record['estimated_cost_usd'] = (record['bytes_used'] / (1024 ** 3)) * 4.0
                results.append(record)
            return results

    # ==========================================================================
    # REMINDERS COG METHODS
    # Methods for reminder scheduling and user timezone management.
    # ==========================================================================

    async def add_reminder(
        self, user_id: int, channel_id: int, reminder_time: int, message: str,
        created_at: int, is_recurring: bool = False, recurrence_rule: Optional[str] = None,
        reply_message_id: Optional[int] = None
    ) -> Optional[int]:
        """Adds a reminder to the database and returns the new reminder's ID.

        Used By: cogs/reminders.py (remind_me_nlp, set_reminder slash command)

        Args:
            user_id (int): The user's ID.
            channel_id (int): The channel ID.
            reminder_time (int): The reminder time as Unix timestamp.
            message (str): The reminder message.
            created_at (int): The creation timestamp.
            is_recurring (bool): Whether the reminder is recurring.
            recurrence_rule (Optional[str]): The rrule recurrence rule string.
            reply_message_id (Optional[int]): The ID of the message to reply to.

        Returns:
            Optional[int]: The new reminder's ID.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO reminders
                (user_id, channel_id, reminder_time, message, created_at, is_recurring, recurrence_rule, reply_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, channel_id, reminder_time, message, created_at, 1 if is_recurring else 0, recurrence_rule, reply_message_id)
            )
            await db.commit()
            return cursor.lastrowid

    async def update_reminder(self, reminder_id: int, user_id: int, updates: Dict[str, Any]) -> int:
        """Updates specific fields of a reminder for a user.

        Used By: cogs/reminders.py (edit_reminder_nlp), cogs/admin.py (edit_entry)

        Args:
            reminder_id (int): The reminder's ID.
            user_id (int): The user's ID.
            updates (Dict[str, Any]): A dictionary of fields to update.

        Returns:
            int: The number of rows affected.
        """
        if not updates:
            return 0

        async with aiosqlite.connect(self.db_path) as db:
            set_clause = ", ".join(f"{key} = ?" for key in updates.keys())
            params = list(updates.values())
            params.extend([reminder_id, user_id])

            query = f"UPDATE reminders SET {set_clause} WHERE id = ? AND user_id = ?"
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount

    async def update_reminder_time(self, reminder_id: int, new_time: int) -> None:
        """Updates the trigger time (`reminder_time`) for a specific reminder.

        This is a specialized, efficient method for rescheduling recurring reminders
        without needing user_id validation.

        Used By: cogs/reminders.py (_handle_missed_recurring, _reschedule_recurring)

        Args:
            reminder_id (int): The reminder's ID.
            new_time (int): The new reminder time as Unix timestamp.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE reminders SET reminder_time = ? WHERE id = ?",
                (new_time, reminder_id)
            )
            await db.commit()

    async def get_due_reminders(self, current_time: int) -> List[Dict[str, Any]]:
        """Fetches all reminders that are due to be sent (time is in the past).

        Used By: cogs/reminders.py (_process_missed_reminders, _scheduler_loop)

        Args:
            current_time (int): The current timestamp.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing reminder data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE reminder_time <= ? ORDER BY reminder_time ASC", (current_time,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_all_reminders(self) -> List[Dict[str, Any]]:
        """Retrieves all reminders for all users, ordered by user_id.

        Used By: cogs/admin.py (report command dashboard)

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing reminder data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders ORDER BY user_id, reminder_time ASC")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_reminders(self, reminder_ids: List[int]) -> None:
        """Deletes one or more reminders from the database by their IDs.

        Used By: cogs/reminders.py (multiple: _handle_missed_reminder, _fire_reminder,
                                   _reschedule_recurring, delete_reminders_nlp),
                 cogs/admin.py (edit_entry)

        Args:
            reminder_ids (List[int]): A list of reminder IDs to delete.
        """
        if not reminder_ids:
            return
        async with aiosqlite.connect(self.db_path) as db:
            # Use a parameterized query to safely delete multiple IDs.
            await db.execute(f"DELETE FROM reminders WHERE id IN ({','.join('?' for _ in reminder_ids)})", reminder_ids)
            await db.commit()

    async def get_user_reminders(self, user_id: int) -> List[Dict[str, Any]]:
        """Fetches all reminders for a specific user, ordered by due time.

        Used By: cogs/reminders.py (list_reminders_nlp, delete_reminders_nlp, edit_reminder_nlp)

        Args:
            user_id (int): The user's ID.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing reminder data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_reminder_by_id(self, reminder_id: int) -> Optional[Dict[str, Any]]:
        """Fetches a single reminder by its unique ID.

        Used By: cogs/reminders.py (_fire_reminder), cogs/admin.py (edit_entry)

        Args:
            reminder_id (int): The reminder's ID.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing the reminder data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_timezone(self, user_id: int) -> Optional[str]:
        """Fetches a user's saved timezone string (e.g., 'America/New_York').

        This is a convenience wrapper around get_user_config for the 'timezone' key.

        Used By: cogs/reminders.py (_get_user_timezone helper)

        Args:
            user_id (int): The user's ID.

        Returns:
            Optional[str]: The timezone string, or None if not found.
        """
        return await self.get_user_config(user_id, 'timezone')

    async def set_user_timezone(self, user_id: int, timezone: str) -> None:
        """Saves or updates a user's timezone.

        This is a convenience wrapper around set_user_config for the 'timezone' key.

        Used By: cogs/reminders.py (set_timezone command)

        Args:
            user_id (int): The user's ID.
            timezone (str): The timezone string (pytz-compatible format).
        """
        await self.set_user_config(user_id, 'timezone', timezone)

    async def get_next_upcoming_reminder(self, current_time: int) -> Optional[Dict[str, Any]]:
        """Retrieves the single next reminder that is scheduled for the future.

        Used By: cogs/reminders.py (_scheduler_loop)

        Args:
            current_time (int): The current timestamp.

        Returns:
            Optional[Dict[str, Any]]: The next reminder, or None if no future reminders exist.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # We want the earliest reminder that is AFTER the current time.
            cursor = await db.execute(
                "SELECT * FROM reminders WHERE reminder_time > ? ORDER BY reminder_time ASC LIMIT 1",
                (current_time,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ==========================================================================
    # SCHEDULE COG METHODS
    # Methods for the weekly availability scheduling feature.
    # ==========================================================================

    async def schedule_get_availability(self, user_id: int) -> List[str]:
        """Retrieves all availability slots for a user.

        Used By: cogs/schedule.py, utils/web/routes.py

        Args:
            user_id: The user's Discord ID.

        Returns:
            List of slot strings in format "day-HHMM" (e.g., "mon-0930").
        """
        logger.debug(f"schedule_get_availability: querying for user_id={user_id} (type={type(user_id).__name__})")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT slot FROM schedule_availability WHERE user_id = ? ORDER BY slot",
                (user_id,)
            )
            rows = list(await cursor.fetchall())
            logger.debug(f"schedule_get_availability: found {len(rows)} slots for user_id={user_id}")
            return [row[0] for row in rows]

    async def schedule_get_availability_updated_at(self, user_id: int) -> Optional[int]:
        """Retrieves the last-modified timestamp for a user's availability.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.

        Returns:
            Unix timestamp of last modification, or None if never set.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT updated_at FROM schedule_availability_meta WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def schedule_set_availability(self, user_id: int, slots: List[str]) -> None:
        """Replaces all availability slots for a user.

        Deletes existing slots and inserts new ones in a single transaction.
        Also updates the last-modified timestamp in the meta table.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.
            slots: List of slot strings in format "day-HHMM" (e.g., "mon-0930").
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Clear existing slots
            await db.execute("DELETE FROM schedule_availability WHERE user_id = ?", (user_id,))
            # Insert new slots
            for slot in slots:
                await db.execute(
                    "INSERT INTO schedule_availability (user_id, slot) VALUES (?, ?)",
                    (user_id, slot)
                )
            # Update last-modified timestamp
            await db.execute(
                "INSERT OR REPLACE INTO schedule_availability_meta (user_id, updated_at) VALUES (?, ?)",
                (user_id, int(time.time()))
            )
            await db.commit()
        logger.info(f"Set {len(slots)} availability slots for user {user_id}")

    async def schedule_clear_availability(self, user_id: int) -> None:
        """Deletes all availability slots for a user.

        Also removes the last-modified timestamp from meta table.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM schedule_availability WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM schedule_availability_meta WHERE user_id = ?", (user_id,))
            await db.commit()
        logger.info(f"Cleared all availability slots for user {user_id}")

    async def schedule_get_guild_visibility(self, user_id: int) -> List[Dict[str, Any]]:
        """Retrieves guild visibility settings for a user.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.

        Returns:
            List of dicts with keys: 'guild_id', 'enabled'.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT guild_id, enabled FROM schedule_guild_visibility WHERE user_id = ?",
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def schedule_set_guild_visibility(self, user_id: int, guild_id: int, enabled: bool) -> None:
        """Sets visibility for a specific guild.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.
            guild_id: The guild's Discord ID.
            enabled: Whether the guild can see this user's availability.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO schedule_guild_visibility (user_id, guild_id, enabled) VALUES (?, ?, ?)",
                (user_id, guild_id, 1 if enabled else 0)
            )
            await db.commit()
        logger.info(f"Set guild {guild_id} visibility to {enabled} for user {user_id}")

    async def schedule_get_blacklist(self, user_id: int) -> List[int]:
        """Retrieves list of blocked user IDs for a user.

        Used By: utils/web/routes.py

        Args:
            user_id: The user's Discord ID.

        Returns:
            List of blocked user IDs.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT blocked_user_id FROM schedule_user_blacklist WHERE user_id = ?",
                (user_id,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def schedule_add_to_blacklist(self, user_id: int, blocked_user_id: int) -> None:
        """Adds a user to the blacklist.

        Used By: utils/web/routes.py

        Args:
            user_id: The user setting the block.
            blocked_user_id: The user being blocked.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO schedule_user_blacklist (user_id, blocked_user_id) VALUES (?, ?)",
                (user_id, blocked_user_id)
            )
            await db.commit()
        logger.info(f"User {user_id} blocked user {blocked_user_id} from viewing schedule")

    async def schedule_remove_from_blacklist(self, user_id: int, blocked_user_id: int) -> None:
        """Removes a user from the blacklist.

        Used By: utils/web/routes.py

        Args:
            user_id: The user who set the block.
            blocked_user_id: The user being unblocked.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM schedule_user_blacklist WHERE user_id = ? AND blocked_user_id = ?",
                (user_id, blocked_user_id)
            )
            await db.commit()
        logger.info(f"User {user_id} unblocked user {blocked_user_id}")

    async def schedule_can_view(self, requester_id: int, target_id: int, guild_id: int) -> bool:
        """Checks if requester can view target's availability in a guild.

        Checks: (1) target enabled visibility for guild, (2) requester not blacklisted.

        Used By: cogs/schedule.py (NLP query handlers)

        Args:
            requester_id: The user requesting to view.
            target_id: The user whose availability is being requested.
            guild_id: The guild context.

        Returns:
            True if allowed, False otherwise.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Check if target has enabled visibility for this guild
            cursor = await db.execute(
                "SELECT enabled FROM schedule_guild_visibility WHERE user_id = ? AND guild_id = ?",
                (target_id, guild_id)
            )
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False

            # Check if requester is blacklisted by target
            cursor = await db.execute(
                "SELECT 1 FROM schedule_user_blacklist WHERE user_id = ? AND blocked_user_id = ?",
                (target_id, requester_id)
            )
            if await cursor.fetchone():
                return False

            return True

    async def schedule_get_guild_availability(self, guild_id: int, requester_id: int) -> Dict[int, List[str]]:
        """Gets availability for all visible users in a guild.

        Filters by: guild visibility enabled AND requester not blacklisted.

        Used By: cogs/schedule.py (NLP query handlers)

        Args:
            guild_id: The guild to query.
            requester_id: The user making the request.

        Returns:
            Dict mapping user_id to list of slot strings.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Get all users who have enabled visibility for this guild
            # and haven't blacklisted the requester
            cursor = await db.execute("""
                SELECT DISTINCT gv.user_id
                FROM schedule_guild_visibility gv
                WHERE gv.guild_id = ? AND gv.enabled = 1
                AND gv.user_id NOT IN (
                    SELECT bl.user_id FROM schedule_user_blacklist bl
                    WHERE bl.blocked_user_id = ?
                )
            """, (guild_id, requester_id))

            visible_users = [row[0] for row in await cursor.fetchall()]

            result: Dict[int, List[str]] = {}
            for user_id in visible_users:
                cursor = await db.execute(
                    "SELECT slot FROM schedule_availability WHERE user_id = ? ORDER BY slot",
                    (user_id,)
                )
                slots = [row[0] for row in await cursor.fetchall()]
                result[user_id] = slots

            return result

    async def schedule_get_visible_users_in_guild(self, guild_id: int) -> List[int]:
        """Gets user IDs who have enabled visibility for a guild.

        Used By: utils/web/routes.py (viewable users endpoint)

        Args:
            guild_id: The guild to query.

        Returns:
            List of user IDs with visibility enabled for this guild.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT user_id FROM schedule_guild_visibility WHERE guild_id = ? AND enabled = 1",
                (guild_id,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def schedule_delete_all_user_data(self, user_id: int) -> None:
        """Deletes all schedule-related data for a user (GDPR/danger zone).

        Removes: availability slots, meta, guild visibility, blacklist entries (both directions).

        Used By: utils/web/routes.py (danger zone)

        Args:
            user_id: The user's Discord ID.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM schedule_availability WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM schedule_availability_meta WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM schedule_guild_visibility WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM schedule_user_blacklist WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM schedule_user_blacklist WHERE blocked_user_id = ?", (user_id,))
            await db.commit()
        logger.info(f"Deleted all schedule data for user {user_id}")

    # ==========================================================================
    # WEB SESSION METHODS
    # Methods for managing web sessions and OAuth tokens. PART OF SCHEDULE COGS METHODS.
    # ==========================================================================

    async def create_session(
        self,
        session_id: str,
        user_id: int,
        access_token: str,
        refresh_token: str,
        token_expires_at: int
    ) -> None:
        """Create a new web session.

        Used By: utils/web/auth.py (OAuth callback)

        Args:
            session_id: UUID v4 string for this session.
            user_id: The Discord user ID.
            access_token: Discord OAuth2 access token.
            refresh_token: Discord OAuth2 refresh token.
            token_expires_at: Unix timestamp when access_token expires.
        """
        import time
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO web_sessions (session_id, user_id, access_token, refresh_token, token_expires_at, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, access_token, refresh_token, token_expires_at, now, now)
            )
            await db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a web session by its ID.

        Used By: utils/web/session_middleware.py

        Args:
            session_id: The session UUID to look up.

        Returns:
            Dict with all session fields, or None if not found.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_id, user_id, access_token, refresh_token, token_expires_at, created_at, last_seen_at
                FROM web_sessions WHERE session_id = ?
                """,
                (session_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_session_tokens(
        self,
        session_id: str,
        access_token: str,
        refresh_token: str,
        token_expires_at: int
    ) -> None:
        """Update a session's OAuth tokens after refresh.

        Used By: utils/web/session_middleware.py (token refresh)

        Args:
            session_id: The session UUID to update.
            access_token: New Discord access token.
            refresh_token: New Discord refresh token.
            token_expires_at: New expiration timestamp.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE web_sessions
                SET access_token = ?, refresh_token = ?, token_expires_at = ?
                WHERE session_id = ?
                """,
                (access_token, refresh_token, token_expires_at, session_id)
            )
            await db.commit()

    async def update_session_last_seen(self, session_id: str) -> None:
        """Update a session's last_seen_at timestamp.

        Used By: utils/web/session_middleware.py (on every request)

        Args:
            session_id: The session UUID to update.
        """
        import time
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE web_sessions SET last_seen_at = ? WHERE session_id = ?",
                (int(time.time()), session_id)
            )
            await db.commit()

    async def delete_session(self, session_id: str) -> None:
        """Delete a single web session.

        Used By: utils/web/auth.py (logout, token refresh failure)

        Args:
            session_id: The session UUID to delete.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM web_sessions WHERE session_id = ?", (session_id,))
            await db.commit()

    async def delete_user_sessions(self, user_id: int) -> None:
        """Delete all web sessions for a user.

        Used By: utils/web/auth.py (logout with clear=true)

        Args:
            user_id: The Discord user ID whose sessions to delete.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
            await db.commit()

    async def cleanup_stale_sessions(self, max_age_days: int = 90) -> int:
        """Delete sessions that haven't been used in a long time.

        Used By: utils/lifecycle.py (bot startup), scheduled task

        Args:
            max_age_days: Sessions older than this many days are deleted.

        Returns:
            Number of sessions deleted.
        """
        import time
        cutoff = int(time.time()) - (max_age_days * 24 * 60 * 60)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM web_sessions WHERE last_seen_at < ?",
                (cutoff,)
            )
            await db.commit()
            return cursor.rowcount

    # ==========================================================================
    # SKILLS COG METHODS
    # Methods for user skill management (dice macros, etc.).
    # ==========================================================================

    async def set_user_skill_limit(self, user_id: int, limit: int) -> None:
        """Sets a skill limit override for a specific user.

        This is a convenience wrapper around set_user_config for the 'skill_limit' key.

        Used By: cogs/admin.py (user_limit command)

        Args:
            user_id (int): The user's ID.
            limit (int): The new skill limit.
        """
        await self.set_user_config(user_id, 'skill_limit', str(limit))

    async def get_user_skill_limit(self, user_id: int) -> int:
        """Gets a user's skill limit.

        Checks for a user-specific override before falling back to the global limit.
        This is a convenience wrapper around get_user_config for the 'skill_limit' key.

        Used By: cogs/skills.py (save_skill_nlp, list_skills_nlp)

        Args:
            user_id (int): The user's ID.

        Returns:
            int: The user's skill limit.
        """
        value = await self.get_user_config(user_id, 'skill_limit')
        if value and value.isdigit():
            return int(value)
        return self.skill_limit

    async def save_skill(self, user_id: int, name: str, aliases: List[str], dice_roll: str, skill_type: str, description: Optional[str] = None) -> None:
        """Saves a new skill and its aliases to the database.

        This is a transactional operation to ensure data integrity.

        Used By: cogs/skills.py (save_skill_nlp)

        Args:
            user_id (int): The user's ID.
            name (str): The skill name.
            aliases (List[str]): A list of aliases for the skill.
            dice_roll (str): The dice roll string.
            skill_type (str): The skill type.
            description (Optional[str]): The skill description.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            async with db.execute("BEGIN") as cursor:
                try:
                    # Insert the main skill
                    await cursor.execute(
                        "INSERT INTO skills (user_id, name, dice_roll, skill_type, description) VALUES (?, ?, ?, ?, ?)",
                        (user_id, name, dice_roll, skill_type.lower(), description)
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

    async def get_skill_by_id(self, skill_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a skill by its unique ID.

        Used By: cogs/admin.py (edit_entry command)

        Args:
            skill_id (int): The skill's ID.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing the skill data.
        """
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type, s.description,
                   GROUP_CONCAT(sa.alias, '|') as aliases
            FROM skills s
            LEFT JOIN skill_aliases sa ON s.id = sa.skill_id
            WHERE s.id = ?
            GROUP BY s.id
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, (skill_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_skills(self, user_id: int) -> List[Dict[str, Any]]:
        """Retrieves all skills for a specific user, including their aliases.

        Used By: cogs/skills.py (save_skill_nlp, use_skill_nlp, edit_skill_nlp,
                                list_skills_nlp, delete_skill_nlp)

        Args:
            user_id (int): The user's ID.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing skill data.
        """
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type, s.description,
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
        """Retrieves all skills for all users, including their aliases.

        Used By: cogs/admin.py (report command dashboard)

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing skill data.
        """
        query = """
            SELECT s.id, s.user_id, s.name, s.dice_roll, s.skill_type, s.description,
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
        """Deletes a skill by its unique ID for a specific user.

        The `ON DELETE CASCADE` constraint will automatically delete its aliases.

        Used By: cogs/skills.py (delete_skill_nlp), cogs/admin.py (edit_entry)

        Args:
            user_id (int): The user's ID.
            skill_id (int): The skill's ID.

        Returns:
            int: The number of rows deleted.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            cursor = await db.execute("DELETE FROM skills WHERE id = ? AND user_id = ?", (skill_id, user_id))
            await db.commit()
            return cursor.rowcount

    async def update_skill(self, skill_id: int, user_id: int, updates: Dict[str, Any]) -> int:
        """Updates specific fields of a skill for a user.

        If aliases are updated, it replaces all existing aliases for the skill.

        Used By: cogs/skills.py (edit_skill_nlp), cogs/admin.py (edit_entry)

        Args:
            skill_id (int): The skill's ID.
            user_id (int): The user's ID.
            updates (Dict[str, Any]): A dictionary of fields to update.

        Returns:
            int: The number of rows affected.
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

    # ==========================================================================
    # STARBOARD COG METHODS
    # Methods for starboard tracking and guild configuration.
    # ==========================================================================

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        """Sets a configuration value for a specific guild.

        Used By: cogs/starboard.py (set_channel, set_emoji, set_threshold commands)

        Args:
            guild_id (int): The guild's ID.
            key (str): The configuration key.
            value (str): The configuration value.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (guild_id, key, value)
            )
            await db.commit()
        logger.info(f"Guild setting for {guild_id} set: {key} = {value}")

    async def get_guild_config(self, guild_id: int, key: str) -> Optional[str]:
        """Gets a configuration value for a specific guild.

        Used By: cogs/starboard.py (get_starboard_config helper)

        Args:
            guild_id (int): The guild's ID.
            key (str): The configuration key.

        Returns:
            Optional[str]: The configuration value, or None if not found.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
                (guild_id, key)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def add_starboard_entry(
        self,
        original_message_id: int,
        starboard_message_id: int,
        guild_id: int,
        channel_id: int,
        starboard_reply_id: Optional[int] = None
    ) -> None:
        """Saves a new starboard entry to the database.

        Used By: cogs/starboard.py (_handle_star_event, _remake_impl, _migrate_starboard_channel)

        Args:
            original_message_id (int): The ID of the original message.
            starboard_message_id (int): The ID of the message in the starboard channel.
            guild_id (int): The guild's ID.
            channel_id (int): The ID of the original channel.
            starboard_reply_id (Optional[int]): The ID of the reply message in the starboard channel.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO starboard_entries (original_message_id, starboard_message_id, guild_id, original_channel_id, starboard_reply_id) VALUES (?, ?, ?, ?, ?)",
                (original_message_id, starboard_message_id, guild_id, channel_id, starboard_reply_id)
            )
            await db.commit()

    async def get_starboard_entry(self, original_message_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a starboard entry by the original message's ID.

        Used By: cogs/starboard.py (_handle_star_event, on_raw_reaction_remove)

        Args:
            original_message_id (int): The ID of the original message.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing the starboard entry data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM starboard_entries WHERE original_message_id = ?", (original_message_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_starboard_entries_for_guild(self, guild_id: int) -> List[Dict[str, Any]]:
        """Retrieves all starboard entries for a specific guild.

        Used By: cogs/starboard.py (remake_starboard, fix_starboard, _remake_impl)

        Args:
            guild_id (int): The guild's ID.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing starboard entry data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM starboard_entries WHERE guild_id = ?", (guild_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_starboard_for_guild(self, guild_id: int) -> None:
        """Deletes all starboard entries for a specific guild.

        Used By: cogs/starboard.py (_remake_impl)

        Args:
            guild_id (int): The guild's ID.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM starboard_entries WHERE guild_id = ?", (guild_id,))
            await db.commit()
            logger.info(f"Cleared all starboard entries for guild {guild_id}.")

    async def remove_starboard_entry(self, original_message_id: int) -> None:
        """Removes a starboard entry from the database.

        Used By: cogs/starboard.py (_handle_star_event, _fix_impl, on_raw_reaction_remove)

        Args:
            original_message_id (int): The ID of the original message.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM starboard_entries WHERE original_message_id = ?", (original_message_id,))
            await db.commit()

    async def update_starboard_entry(self, entry: dict) -> None:
        """Updates an existing starboard entry in the database.

        Expects all relevant keys in entry dict.

        Used By: cogs/starboard.py (_remake_impl, _migrate_starboard_channel, _fix_impl)

        Args:
            entry (dict): The dictionary containing starboard entry data.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE starboard_entries SET
                    starboard_message_id = ?,
                    guild_id = ?,
                    original_channel_id = ?,
                    starboard_reply_id = ?
                WHERE original_message_id = ?
                """,
                (
                    entry.get("starboard_message_id"),
                    entry.get("guild_id"),
                    entry.get("original_channel_id"),
                    entry.get("starboard_reply_id"),
                    entry["original_message_id"]
                )
            )
            await db.commit()

    # =========================================================================
    # Users Table Methods (Web Auth / User Cache)
    # =========================================================================

    async def upsert_user(self, user_id: int, username: str, avatar: str | None) -> None:
        """Insert or update a user's profile data.

        Used By: utils/web/auth.py (OAuth callback)

        Args:
            user_id: The Discord user ID.
            username: The user's display name.
            avatar: URL to the user's avatar, or None.
        """
        import time
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, username, avatar, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    avatar = excluded.avatar,
                    last_seen = excluded.last_seen
                """,
                (user_id, username, avatar, int(time.time()))
            )
            await db.commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        """Get a user's cached profile data.

        Used By: utils/web/auth.py (/auth/me endpoint)

        Args:
            user_id: The Discord user ID.

        Returns:
            Dict with user_id, username, avatar, last_seen or None if not found.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, username, avatar, last_seen FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Web Sessions Table Methods (Server-Side Session Storage)
    # =========================================================================

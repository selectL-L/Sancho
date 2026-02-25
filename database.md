# Database Documentation

This document describes every table in the bot's SQLite database, its purpose, which cog(s) use it, and any notable patterns. The canonical schema definition lives in `utils/database.py` → `_setup_databases()`.

For migration between database versions, see `migrate_db.py`.

---

## Conventions

- **Primary keys** are either `INTEGER PRIMARY KEY` (auto-increment rowid) or composite via `PRIMARY KEY(col_a, col_b)`.
- **Snowflake IDs** (Discord user/channel/message/guild IDs) are stored as `INTEGER`. SQLite's `INTEGER` is 64-bit, which is sufficient for Discord snowflakes.
- **Timestamps** are Unix timestamps in **seconds** (not milliseconds) unless otherwise noted.
- **Key-value tables** (`user_settings`, `bot_settings`, `guild_settings`) store all values as `TEXT`. The consuming code is responsible for parsing to the expected type.
- **Foreign keys** are enforced at connection time via `PRAGMA foreign_keys = ON`.
- **`COLLATE NOCASE`** is used on text fields that should be case-insensitive (skill names, aliases).

---

## Table Reference

### `skills`

Stores user-created dice roll macros ("skills") for tabletop RPGs. Each skill has a name, a dice notation string, a type label, and an optional description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY | Auto-increment row ID. |
| `user_id` | INTEGER | NOT NULL | Discord user ID of the skill owner. |
| `name` | TEXT | NOT NULL, UNIQUE(user_id, name COLLATE NOCASE) | Skill name, unique per user (case-insensitive). |
| `dice_roll` | TEXT | NOT NULL | The dice notation string (e.g. `2d20kh1 + 5`). |
| `skill_type` | TEXT | NOT NULL | Category label (e.g. "Attack", "Healing"). |
| `description` | TEXT | | Optional flavour text. |

**Used by:** `cogs/skills.py`
**Indexes:** `idx_skills_user` on `user_id`

---

### `skill_aliases`

Alternative names for skills. A user can invoke a skill by any of its aliases instead of the primary name. Aliases are removed automatically when the parent skill is deleted (CASCADE).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY | Auto-increment row ID. |
| `skill_id` | INTEGER | NOT NULL, FK → skills(id) ON DELETE CASCADE | Parent skill. |
| `alias` | TEXT | NOT NULL, UNIQUE(skill_id, alias COLLATE NOCASE) | The alias string, unique per skill. |

**Used by:** `cogs/skills.py`

---

### `reminders`

Scheduled reminders that fire at a specific time. Supports one-shot and recurring reminders.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY | Auto-increment row ID. |
| `user_id` | INTEGER | NOT NULL | Discord user ID of the reminder owner. |
| `channel_id` | INTEGER | NOT NULL | Channel to send the reminder in. |
| `reminder_time` | INTEGER | NOT NULL | Unix timestamp (seconds) when the reminder fires. |
| `message` | TEXT | NOT NULL | The reminder message text. |
| `created_at` | INTEGER | NOT NULL | Unix timestamp (seconds) when the reminder was created. |
| `is_recurring` | INTEGER | NOT NULL, DEFAULT 0 | Boolean flag: 0 = one-shot, 1 = recurring. |
| `recurrence_rule` | TEXT | | iCal RRULE string for recurring reminders (<https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html>). NULL for one-shot. |
| `reply_message_id` | INTEGER | | Message ID the reminder should reply to (if set). |

**Used by:** `cogs/reminders.py`
**Indexes:** `idx_reminders_time` on `reminder_time`, `idx_reminders_user` on `user_id`

---

### `schedule_availability`

Stores a user's weekly recurring availability as 15-minute time slots.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY | Auto-increment row ID. |
| `user_id` | INTEGER | NOT NULL | Discord user ID. |
| `slot` | TEXT | NOT NULL, UNIQUE(user_id, slot) | Slot string in `day-HHMM` format (e.g. `mon-0930`). 24-hour time. |

**Used by:** `cogs/schedule.py`, `utils/web/routes.py`
**Indexes:** `idx_schedule_availability_user` on `user_id`

---

### `schedule_availability_meta`

Tracks when a user last updated their availability (used for cache invalidation in the web UI).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | PRIMARY KEY | Discord user ID. |
| `updated_at` | INTEGER | NOT NULL | Unix timestamp of last availability change. |

**Used by:** `cogs/schedule.py`, `utils/web/routes.py`

---

### `schedule_guild_visibility`

Controls which guilds can see a user's availability. Opt-in: a row with `enabled=1` grants visibility.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | NOT NULL, PK | Discord user ID. |
| `guild_id` | INTEGER | NOT NULL, PK | Discord guild ID. |
| `enabled` | INTEGER | NOT NULL, DEFAULT 0 | 1 = visible in this guild, 0 = hidden. |

**Used by:** `cogs/schedule.py`, `utils/web/routes.py`
**Indexes:** `idx_schedule_guild_visibility_guild` on `guild_id`

---

### `schedule_user_blacklist`

Users blocked from viewing a specific user's availability. Checked before returning data in queries.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | NOT NULL, PK | The user whose schedule is protected. |
| `blocked_user_id` | INTEGER | NOT NULL, PK | The user who is blocked from viewing. |

**Used by:** `cogs/schedule.py`, `utils/web/routes.py`

---

### `user_settings`

Per-user key-value configuration store. All values are stored as TEXT strings.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | NOT NULL, PK | Discord user ID. |
| `key` | TEXT | NOT NULL, PK | Setting name. |
| `value` | TEXT | NOT NULL | Setting value (as string). |

**Known keys:**

| Key | Example Value | Used By |
|-----|---------------|---------|
| `timezone` | `America/New_York`, `Etc/GMT-5` | `cogs/reminders.py` |
| `skill_limit` | `12` | `cogs/skills.py` |
| `reminder_destination` | `dm` or `channel` | `cogs/reminders.py` |

**Used by:** `cogs/reminders.py`, `cogs/skills.py`

---

### `bot_settings`

Global bot-wide configuration. One row per setting.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `key` | TEXT | PRIMARY KEY | Setting name. |
| `value` | TEXT | NOT NULL | Setting value (as string). |

**Known keys:**

| Key | Example Value | Used By |
|-----|---------------|---------|
| `skill_limit` | `8` | `utils/database.py` (default), `cogs/admin.py` |

**Used by:** `cogs/admin.py`, `utils/database.py`

---

### `guild_settings`

Per-guild key-value configuration store.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `guild_id` | INTEGER | NOT NULL, PK | Discord guild ID. |
| `key` | TEXT | NOT NULL, PK | Setting name. |
| `value` | TEXT | NOT NULL | Setting value (as string). |

**Known keys:**

| Key | Example Value | Used By |
|-----|---------------|---------|
| `music_channel_id` | `123456789012345678` | `cogs/music.py` |

**Used by:** `cogs/music.py`

> **Migration note:** Starboard keys (`starboard_channel_id`, `starboard_emoji`, `starboard_threshold`, `starboard_last_heal_at`, `starboard_crawl_*`) were moved to the dedicated `starboard_config` table. The migration in `migrate_db.py` (`migrate_guild_settings`) handles this automatically and deletes the old keys.

---

### `starboard_config`

Dedicated per-guild starboard configuration. One row per guild. Created via UPSERT — the row is inserted on first write and updated on subsequent writes. Replaces the old `guild_settings` key-value approach for starboard settings.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `guild_id` | INTEGER | PRIMARY KEY | Discord guild ID. |
| `enabled` | INTEGER | NOT NULL, DEFAULT 0 | Whether the starboard is active (1 = enabled, 0 = disabled). |
| `channel_id` | INTEGER | | The starboard channel ID. NULL if unset. |
| `emoji` | TEXT | NOT NULL, DEFAULT '⭐' | The reaction emoji that triggers starboard posts. |
| `threshold` | INTEGER | NOT NULL, DEFAULT 5 | Minimum reaction count to qualify for the starboard. |
| `last_heal_at` | INTEGER | | Unix timestamp of the last self-heal run. NULL if never run. |
| `crawl_started_at` | INTEGER | | Unix timestamp when the current deep crawl started. NULL if no crawl in progress. |
| `crawl_requested_by` | INTEGER | | Discord user ID of the person who requested the crawl. NULL if no crawl. |
| `crawl_notify_channel` | INTEGER | | Channel ID to send crawl completion notifications. NULL if no crawl. |
| `crawl_include_threads` | INTEGER | NOT NULL, DEFAULT 0 | Whether the current crawl should scan threads (1 = yes). |
| `crawl_last_channel_id` | INTEGER | | Last channel ID processed by the crawl (resume checkpoint). |
| `crawl_last_message_id` | INTEGER | | Last message ID processed by the crawl (resume checkpoint). |

**Used by:** `cogs/starboard.py`
**Dataclass:** `StarboardConfig` in `cogs/starboard.py` wraps this row for type-safe access.

---

### `starboard_banned_channels`

Junction table listing channels excluded from starboard tracking per guild. Reactions in banned channels are ignored by the starboard reaction handlers.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `guild_id` | INTEGER | NOT NULL, PK | Discord guild ID. |
| `channel_id` | INTEGER | NOT NULL, PK | The banned channel ID. |

**Used by:** `cogs/starboard.py`
**Indexes:** `idx_starboard_banned_guild` on `guild_id`

---

### `starboard_entries`

Tracks messages that have been posted (or are pending posting) to a guild's starboard channel. Each row links an original message to its starboard copy.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `original_message_id` | INTEGER | PRIMARY KEY | The source message being starred. |
| `starboard_message_id` | INTEGER | | The message ID in the starboard channel. **Nullable** — NULL means the entry is known to qualify but hasn't been posted yet (crawl result, or mid-remake). |
| `guild_id` | INTEGER | NOT NULL | The guild this entry belongs to. |
| `starboard_reply_id` | INTEGER | | If the original was a reply, the reply-context message ID in the starboard channel. |
| `original_channel_id` | INTEGER | NOT NULL | The channel the original message lives in. |
| `message_created_at` | INTEGER | NOT NULL | Unix timestamp (seconds) derived from the original message's snowflake. Used for chronological ordering. |
| `star_count` | INTEGER | NOT NULL, DEFAULT 0 | Last known star reaction count. Updated on every reaction event and during verify. |
| `failed_checks` | INTEGER | NOT NULL, DEFAULT 0 | Consecutive verification failures. 0 = healthy. 1 = flagged. 2+ = tombstoned. Reset to 0 on successful check. |

**Used by:** `cogs/starboard.py`
**Indexes:** `idx_starboard_guild` on `guild_id`

---

### `bod_players`

Tracks BOD (Boundary of Death) game state per user — chain progress, cooldown timing, and fate charges.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | PRIMARY KEY | Discord user ID. |
| `last_used_timestamp` | INTEGER | NOT NULL, DEFAULT 0 | Unix timestamp of last BOD roll (cooldown). |
| `current_chain` | INTEGER | NOT NULL, DEFAULT 0 | Current chain length (resets on loss). |
| `last_channel_id` | INTEGER | NOT NULL, DEFAULT 0 | Channel of the last roll (for chain context). |
| `fate_lucky` | INTEGER | NOT NULL, DEFAULT 0 | Lucky fate charges remaining. |
| `fate_blessed` | INTEGER | NOT NULL, DEFAULT 0 | Blessed fate charges remaining. |
| `fate_guaranteed` | INTEGER | NOT NULL, DEFAULT 0 | Guaranteed fate charges remaining. |
| `fate_silent` | INTEGER | NOT NULL, DEFAULT 0 | Silent fate charges remaining. |

**Used by:** `cogs/fun.py` (bod command), `cogs/admin.py` (bod_bless, bod_fate, bod_clear)

---

### `bod_leaderboard`

Persistent high scores for the BOD game. Display names are fetched dynamically at render time, not stored.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | PRIMARY KEY | Discord user ID. |
| `best_chain` | INTEGER | NOT NULL, DEFAULT 0 | All-time best chain length. |
| `achieved_at` | INTEGER | NOT NULL, DEFAULT 0 | Unix timestamp when the record was set. |

**Used by:** `cogs/fun.py` (bod_leaderboard command)

---

### `proxy_usage`

Tracks residential proxy bandwidth usage for cost monitoring. One row per month.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY | Auto-increment row ID. |
| `year_month` | TEXT | NOT NULL, UNIQUE | Month identifier (e.g. `2025-01`). |
| `track_count` | INTEGER | NOT NULL, DEFAULT 0 | Number of tracks fetched via proxy this month. |
| `bytes_used` | INTEGER | NOT NULL, DEFAULT 0 | Total bytes downloaded through the proxy. |
| `last_updated` | INTEGER | NOT NULL | Unix timestamp of last update. |

**Used by:** `cogs/music.py` (residential proxy fallback)

---

### `users`

Cached Discord user profile data for the web interface. Survives session expiry so we can greet returning users by name after re-authentication.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `user_id` | INTEGER | PRIMARY KEY | Discord user snowflake ID. |
| `username` | TEXT | NOT NULL | Discord username. |
| `avatar` | TEXT | | Avatar URL (nullable). |
| `last_seen` | INTEGER | NOT NULL | Unix timestamp of last interaction. |

**Used by:** `utils/web/auth.py` (OAuth callback, `/auth/me` endpoint)

---

### `web_sessions`

Server-side session storage for web authentication. Only the `session_id` is sent to the client in a cookie; tokens stay server-side.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `session_id` | TEXT | PRIMARY KEY | UUID v4 string. |
| `user_id` | INTEGER | NOT NULL, FK → users(user_id) ON DELETE CASCADE | Owning user. Deleting a user cascades to their sessions. |
| `access_token` | TEXT | NOT NULL | Discord OAuth2 access token. |
| `refresh_token` | TEXT | NOT NULL | Discord OAuth2 refresh token. |
| `token_expires_at` | INTEGER | NOT NULL | Unix timestamp when the access token expires. |
| `created_at` | INTEGER | NOT NULL | Unix timestamp when the session was created. |
| `last_seen_at` | INTEGER | NOT NULL | Unix timestamp of last activity. Sessions unused for 90+ days are cleaned on startup. |

**Used by:** `utils/web/session_middleware.py`, `utils/web/auth.py`
**Indexes:** `idx_web_sessions_user` on `user_id`, `idx_web_sessions_last_seen` on `last_seen_at`

---

## Index Summary

| Index Name | Table | Column(s) | Purpose |
|------------|-------|-----------|---------|
| `idx_reminders_time` | `reminders` | `reminder_time` | Fast lookup for the next reminder to fire. |
| `idx_reminders_user` | `reminders` | `user_id` | List reminders for a specific user. |
| `idx_schedule_availability_user` | `schedule_availability` | `user_id` | Fetch all slots for a user. |
| `idx_schedule_guild_visibility_guild` | `schedule_guild_visibility` | `guild_id` | Find all users visible in a guild. |
| `idx_skills_user` | `skills` | `user_id` | List all skills for a user. |
| `idx_starboard_guild` | `starboard_entries` | `guild_id` | Fetch all starboard entries for a guild. |
| `idx_web_sessions_user` | `web_sessions` | `user_id` | Find sessions by user. |
| `idx_web_sessions_last_seen` | `web_sessions` | `last_seen_at` | Efficient cleanup of stale sessions. |

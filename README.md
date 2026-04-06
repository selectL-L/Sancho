# Shiori (V0.17.0) - Doomsday appraoches.

**Format**: Major.Minor.Bugfix

**A Discord bot built around natural language instead of command syntax.**

Most Discord bots force you to memorize rigid command structures — `/play`, `!roll 2d20`. Shiori is built on the premise that interaction should feel natural. Ask her to "roll me a d20" and she understands. Ask her to "please roll a d20 for me?" and she understands that too. The entire bot is designed around this: regex-based intent matching as the primary interface, with slash commands and prefix commands as secondary citizens that route through the same system.

## Features

### 🎲 Math & Dice
Dice engine with its own lexer/parser, and a math evaluator that walks the AST directly (no `eval()`).
*   **Dice Notation**: `roll 2d20kh1 + 5` — roll two d20s, keep the highest, add 5. Supports exploding dice, advantage/disadvantage, and value clamping.
*   **Calculator**: Trig, logarithms, and physical constants (`pi`, `e`, speed of light, Avogadro's number).
*   **Limbus Coin Flips**: Simulator for Limbus Company's SP-weighted coin probability.

### ⚔️ Skills
Save dice formulas as named skills so you don't have to type them out every time.
*   **Save**: `save skill` walks you through setup — name, aliases, formula (validated so people can't roll 999d999), type, and description.
*   **Cast**: `cast Fireball +2` runs the saved formula with modifiers appended.
*   **Manage**: `show my skills`, `delete Fireball`, `edit Fireball`.

### ⏰ Reminders
Set reminders with natural language. She tries figures out the time from however you phrase it.
*   **Natural Phrasing**: `remind me in 2 hours to check the laundry` or `remind me next tuesday to visit my nan`.
*   **Recurring**: `remind me every weekday at 9am to standup` — daily, weekly, monthly, custom intervals.
*   **Missed Recovery**: If the bot was down when a reminder was due, it gets delivered on startup.
*   **Timezone Aware**: `set timezone` so she uses *your* local time, not the server's.

### 🖼️ File Tools
Reply to a file with `convert` or `resize`. Handles images, audio, video, animated formats, and cross-category conversions like video-to-GIF.
*   **Convert**: Shows a settings panel for quality, bitrate, resolution, and codec options before converting.
*   **Resize**: Reply with `resize` to scale images.
*   **Avatar/Banner**: Fetch any user's avatar or banner at full resolution.
*   **Formats**: Static images, animated images (GIF/WebP/APNG), audio, and video. PIL for images, FFmpeg for everything else.

### ⭐ Starboard
Messages with enough stars get reposted to a highlight channel. Unlike most starboards, this one maintains itself.
*   **Self-Repair**: Star counts, channel drift, and failure flags are fixed inline on every reaction event. A background audit runs every 12 hours against live Discord state.
*   **Tombstoning**: If the original message gets deleted, the starboard entry is edited in-place rather than removed — it keeps its position in the timeline.
*   **Remake**: Recreate the entire starboard in starred order from the database.
*   **Deep Crawl**: Scan a server's history for missed starboard-worthy content. Progress persists to database, so crawls survive restarts.

### 📅 Availability Schedule
Weekly availability tracker with a web UI inspired by [Timeful](https://en.wikipedia.org/wiki/Timeful). Set your free times on a heatmap grid, check others' through Discord.
*   **Web UI**: Heatmap grid with drag-to-paint editing, glow on popular slots, desktop and mobile layouts. Authenticated via Discord OAuth2.
*   **NLP Queries**: `when is @user free`, `who's free Saturday afternoon` — most people just ask in chat rather than visiting the site.
*   **Privacy**: Per-guild visibility and user blocking. Your schedule in one server doesn't leak to another.
*   **Cross-Timezone**: Slot comparisons convert between timezones automatically.

### 🎵 Music
The bot idles by "listening" to music — cycling through playlists in its Discord status. Ask her to join voice and she plays what she's been listening to.
*   **Listen Along**: Joins your voice channel and plays from the current mood's playlist, with loudness normalization across tracks.
*   **Search**: YouTube and YouTube Music. Japanese, Chinese, and Korean titles get transliterated for cross-language matching.
*   **Player Controls**: Skip, queue, shuffle, loop, jump, now-playing with a Components V2 player (thumbnail, progress bar, controls).
*   **Global Session**: A current restriction is the bot can only be in one voice channel at a time across all servers.
*   **Lyrics**: Multi-provider (Genius, LRCLIB) with artist filtering and pagination.

### 🎰 Fun & Misc
*   **Magic 8-Ball**: Ask a question, get a hopefully accurate answer.
*   **BOD (Boundary of Death)**: It's yujin from library of ruina guys. She's real. She *can* hurt you!
*   **Extensible**: Adding a new simple command is a single dataclass declaration. It auto-wires into NLP.

## What Architecture Makes Shiori Different

### NLP-First Interaction

Shiori doesn't bolt natural language onto a command framework — the command framework bolts onto natural language. The primary dispatcher is a regex pattern registry (`config.NLP_COMMANDS`) that maps intent patterns to cog methods. Priority is resolved in two stages: first match within a group wins, then the group whose pattern matched earliest in the user's input wins across groups.

Slash commands exist, but the `/nlp` command literally forwards its argument through the same dispatcher. Standard prefix commands are checked first and used for edge cases where regex matching would be too ambiguous, but the expectation is that most users interact through natural language.

Cogs can register additional patterns at runtime via `bot.register_nlp_group()` without touching the central config — the Fun cog does this to auto-export its declarative command registry.

### The Interaction Shim

The challenge with NLP-first design is that handlers need to work from prefix messages *and* slash commands without two code paths that drift. Shiori solves this with a `ContextLike` protocol and an `InteractionContextAdapter` that wraps `discord.Interaction` to look like `commands.Context`.

Handlers accept `ctx: ContextLike` and call `ctx.send()` — whether the original trigger was a prefix message, NLP match, or slash command is invisible to the handler. For slash-specific limitations (no `message.reference`, no `message.attachments`), a `_StubMessage` proxy raises `SlashUnsupportedError` with a user-friendly message suggesting the prefix alternative.

This means every NLP handler is written once and works from all three invocation methods with zero adaptation.

### Music Metadata Resolution

When a user pastes a YouTube URL, it could be a fan cover, a dance routine from a mix playlist, a slowed+reverb edit, or a legitimate music video. The music system's job is to figure out *what song this actually is* and find the canonical Audio Track Version (ATV) in YouTube Music's catalog.

The pipeline:

1. **Catalog mismatch detection** — YTM's structured metadata (`videoDetails`) is compared against the raw YouTube title (`microformat`). If YTM claims the video is a "Slowed + Reverb" version but the raw title is just "Dior," the system detects the mismatch and falls back to the raw title for search.

2. **CJK artist extraction from video tags** — Tags are scored 0.0–1.0 for likelihood of being an artist name. Agency names (ホロライブ, nijisanji), format markers (MV, lyrics), and song title duplicates are penalized. VocaloidP patterns (みきとP) are boosted. High-confidence CJK names generate additional search queries paired with the song title.

3. **Multi-query cross-script search** — All queries hit YTM and YouTube in parallel. Results from Japanese, Chinese, and Korean titles are transliterated to romanized forms (pykakasi, pypinyin, korean-romanizer) for cross-script comparison.

4. **Tiered garbage filtering** — Results are evaluated with word overlap expanded across scripts and character-level similarity. Strong overlap passes immediately; partial overlap needs similarity confirmation; no overlap at all requires very high similarity. Cross-script results get a lower threshold because transliteration is inherently lossy.

5. **Sliding-scale star scoring** — Each ATV candidate is scored against the original. Title matching uses containment, word overlap, and similarity across all script representations. Artist confidence uses text similarity as a base with an ID-match bonus. Higher artist confidence lowers the required title threshold — and a perfect title match auto-passes regardless of artist. The top candidate gets a star recommendation in the selection UI.

### Ambient Personality Engine

The bot maintains internal mood and activity state that colors every interaction. Four moods (relaxing, productive, social, out and about) each contain activities (studying, drinking boba, gaming, etc.) with hand-crafted personality responses.

This isn't cosmetic. The mood determines which music playlists cycle in the bot's status. Activities carry per-cog response overrides — interrupting the bot while it's "studying" gets a different tone than while it's "drinking boba." The help command's greeting changes. The ambience data lives in `ambience.toml` (gitignored, hot-reloaded on file change) while the mood/activity structure lives in Python.

The music system coordinates via a pub/sub handshake: ambience decides to switch playlists, publishes the change, the music cog picks it up on its next cycle and confirms. The result is a bot that feels like a person who happens to be listening to music, not a music bot with a status message.

### Self-Healing Starboard

Most starboard implementations post a message when a threshold is hit and never look back. Shiori's starboard actively maintains data integrity through two paths.

The **hot path** fires on every reaction event and inline-fixes star counts, channel drift, and failure flags with zero additional API calls beyond what the reaction handler already needs. The **cold path** runs a full verification audit every 12 hours, checking each database entry against live Discord state. Entries follow a lifecycle: healthy → flagged (first failure) → tombstoned (second failure, edited in-place to preserve position) → or recovered if the original reappears.

The remake engine can destructively recreate the entire starboard in starred order from the database. The crawl engine scans server history with resumable checkpoints persisted to the database — a crawl interrupted by a restart picks up exactly where it left off.

### Proactive Ambient Music Caching

Rather than fetching audio URLs on demand for ambient cycling, the bot proactively downloads every track from every configured playlist on startup and on a 24-hour cycle, building a local library of M4A files with embedded metadata and cover art.

The in-memory index uses an MVCC-style concurrency protocol: reads take deep-copy snapshots, writes go through a synchronous-only mutation function under lock, and serialization copies again before handing to a writer thread. No method holds a direct reference to the index across an await point.

When playlists change, dropped tracks aren't deleted — they're moved to an orphan directory with a 90-day TTL. If the track reappears (playlist curator re-added it), it's un-orphaned. Audio fetching uses a multi-level cascade with context-aware strategies — speculative prefetch stays conservative, while live user playback is willing to try harder.

### Full-Stack Availability Platform

The schedule system is a complete web application running in-process with the bot — FastAPI on Uvicorn sharing the event loop, no IPC needed. Users authenticate via Discord OAuth2 (server-side sessions, no tokens in cookies), set their weekly availability on a Timeful-inspired heatmap grid, and control per-guild visibility with opt-in toggles and bidirectional user blocking.

The web UI has dedicated desktop and mobile layouts with shared JS modules. The heatmap renders 672 cells (7 days × 96 quarter-hour slots) with intensity-based coloring and a canvas glow layer highlighting popular times. Editing uses Bresenham line interpolation for drag-painting to prevent skipped cells during fast mouse movement.

But most users never open the web UI to *check* availability — they type `who's free Saturday afternoon` in Discord and get an instant answer. The NLP handlers parse time expressions, convert between users' timezones, compute slot intersections for group queries, and format results as readable time ranges.

## Getting Started

### Pre-built Binaries (No Python Required)

If you don't need to modify the code, you can grab a pre-built executable instead of setting up Python:

1.  Go to the **Actions** tab in this repository.
2.  Click on the latest successful workflow run.
3.  Scroll down to the **Artifacts** section at the bottom of the page.
4.  Download the **Shiori** zip for your platform.

Extract it, place the `assets/` folder and `info.env` alongside the executable. FFmpeg is bundled. Skip to step 3 under Installation to configure `info.env`.

**If you want to run from source or modify the code, continue below.**

### Requirements

*   **Python 3.14+**
*   **FFmpeg** accessible globally via PATH (pre-built executables bundle this)
*   **[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)** — PO token server for YouTube authentication

### Installation

1.  **Clone the repository**
    ```bash
    git clone https://github.com/selectL-L/Shiori.git
    cd Shiori
    ```

2.  **Install dependencies**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Run once to generate config**
    ```bash
    python main.py
    ```
    The bot detects the missing configuration, generates an `info.env` template with documented fields, and exits.

4.  **Configure `info.env`**
    Open the generated file and set at minimum:
    ```ini
    DISCORD_TOKEN=your_token_here
    BOT_PREFIX=your_prefix_here
    OWNER_ID=your_discord_id_here (TECHNICALLY optional, but not really.)
    ```
    The file is self-documenting — each field has inline comments explaining its purpose.

5.  **Set up ambience**
    ```bash
    cp assets/ambience.toml.example assets/ambience.toml
    ```
    Edit `ambience.toml` with your own playlists and personality content. The bot requires this file to run. The structure:

    | Section | Purpose |
    |---------|---------|
    | `[config]` | Timing settings (mood cycle frequency, music weight) |
    | `[interests.*]` | Personality data (books, games, snacks, etc.) used for flavor text |
    | `[playlists]` | YouTube playlist URLs organized by mood (cozy, energetic, sleepy, etc.) |
    | `[playlists.descriptions]` | Short descriptions for each playlist mood |

    This file hot-reloads on changes — no restart needed.

6.  **Launch**
    ```bash
    python main.py
    ```

*For building standalone executables, see [BUILD.md](BUILD.md). For database schema documentation, see [database.md](database.md).*

## Developer Guide

### Key Deviations from Standard discord.py

**Three-phase cog lifecycle.** Standard discord.py has `cog_load()` and `cog_unload()`. Shiori adds `cog_ready()`, called once after the bot is connected and ready. Background tasks, network calls, and recovery operations go in `cog_ready()`, **not** `cog_load()`. The reconnect guard (`_has_initialized`) prevents duplicate calls on Discord reconnects.

**Dual schema locations.** Database schema exists in two places — `utils/database.py` (runtime) and `migrate_db.py` (migrations). You **must** update both when making schema changes. See [database.md](database.md) for table documentation.

**Path system.** Never use `__file__` or relative paths. Use `config.APP_PATH` (runtime files), `config.ASSETS_PATH` (static assets), or `config.INTERNAL_PATH` (bundled code). This ensures PyInstaller compatibility.

**Soft restart.** The `restart` control command purges all `utils.*`, `cogs.*`, and `config` modules from `sys.modules` and re-imports them — code changes take effect without process restart. The console reader thread and log file persist across restarts.

### Adding NLP Commands

Register patterns in `config.NLP_COMMANDS` (static) or call `bot.register_nlp_group()` (dynamic):

```python
# In config.py — ((regex_patterns), 'CogName', 'method_name')
((r'\b8\s?-?ball\b',), 'Fun', 'eight_ball'),
```

Handler signature:
```python
async def eight_ball(self, ctx: ContextLike, query: str) -> None:
    await ctx.send("Result here")
```

`ctx.send()` works transparently for prefix, NLP, and slash invocations. If your handler needs message-specific context (`ctx.message.reference`, `ctx.message.attachments`), it only works from prefix/NLP — slash will raise `SlashUnsupportedError` with a user-facing suggestion.

### UI Patterns

All reusable UI components live in `utils/views.py`. Use these instead of building ad-hoc views:

| Function | Purpose |
|----------|---------|
| `get_selection()` | Generic selection — users pick via button *or* by typing |
| `launch_modal()` | Launches a modal from text commands (sends a button bridge first) |
| `show_now_playing()` | Components V2 music player with thumbnail and controls |
| `show_track_failed()` | Skip/Remove buttons on track failure |
| `show_dashboard()` | Multi-category paginated view with export |
| `show_status()` | Multi-page bot health dashboard |
| `show_conversion()` | File conversion settings with per-format dropdowns |

Views handle their own cleanup (disable buttons on timeout, update on selection). Cogs don't manage view lifecycle beyond the initial send.

### Runtime Control

The bot accepts `exit`, `restart`, `reload`, and `status` commands via console input (local development) or TCP socket (headless/production).

| Command | Effect |
|---------|--------|
| `reload` | Hot-reload all cogs without disconnecting |
| `restart` | Full soft restart (module purge + re-import) |
| `exit` | Graceful shutdown with state save |
| `status` | Returns running state (TCP only) |

**Console** — just type into the terminal running `python main.py`.

**TCP** — set `CONTROL_PORT` in `info.env` (binds to `127.0.0.1` only for safety):

```bash
# Linux
echo "reload" | nc localhost 9999

# Windows (PowerShell)
"reload" | ncat localhost 9999
```

Commands return `OK: <message>` on success or `ERROR: <message>` on failure.

### Code Quality

The project use **Ruff** for linting and **pylance** for type-checking (configured in `pyproject.toml` and `pyrightconfig.json` — bugs, security, and async rules, never style enforcement, though we try to adher to pep8). Google-style docstrings and type hints are expected on all functions. All cogs inherit from `utils.base_cog.BaseCog` which provides `self.logger`. Async only in cogs — no `requests`, no `time.sleep()` discord heartbeats MUST be sent, so no event can be blocking.

Match existing formatting patterns in each file rather than imposing a style. Discord.py has dynamic attributes that cause false positives — use `# noqa` when you're certain code is correct.

### DEV_MODE

Set `DEV_MODE=True` in `info.env` to enable debug-level logging and restrict slash command sync to `DEV_GUILD` (so you're not waiting for global sync during development). This is the main toggle for local development.

### Web UI Development

Set `DEV_MODE=True` and `WEB_MOCK_DATA=True` in `info.env` to develop the web UI without a live Discord connection. Mock data provides 15 fake users with varied availability patterns. You'll need to create `utils/web/mock_data.py` yourself (gitignored) — check imports in `utils/web/routes.py` for the expected function signatures.

The web frontend lives in `assets/web/` with shared modules in `web-core/` and shared styles in `web-css/`. Desktop and mobile are separate HTML files with device detection at the routing layer. When rendering user-controlled content, use `escapeHtml()` from `web-core/utils.js`.

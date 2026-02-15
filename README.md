# Shiori (V0.13.02 - The "HOLY IT'S FINALLY HERE!" Update)

**Format**: Major.Minor.Bugfix

**A Discord bot designed to be more reasonable.**

Shiori is built on the simple premise interacting with her should feel natural. Rather than forcing you to memorize rigid command structures like most other bots, or using slash commands, Shiori attempts to interpret your *intent*.

If you ask her to "roll me a d20", she understands. If you ask her to "please roll a d20 for me?", she understands that too... hopefully.

## Features

### 🎲 Math & Dice
There's a robust dice engine capable of handling complex notations and complex arithemtic questions.
*   **Standard Rolling**: `roll 2d20kh1 + 5` (Roll two d20s, keep the highest, add 5).
*   **Limbus Coin Flips**: A dedicated binary outcome generator for Limbus Company rolls.
*   **Calculator**: Evaluate mathematical expressions directly in chat.

### ⚔️ Skills Database
The Skills system acts as a macro manager, allowing you to save complex dice notations (with descriptions!) as named "Skills".
*   **Save**: Simply type `save skill` to enter an interactive setup wizard.
*   **Cast**: Use `cast Fireball` to execute the saved macro instantly.
*   **Manage**: List, edit, or delete your skills with natural language commands like `show my skills` or `delete Fireball`.

### ⏰ Reminders
Set reminders using natural language without worrying about strict syntax.
*   **Natural Phrasing**: `remind me in 2 hours to check the laundry` or `remind me next tuesday to visit my nan`.
*   **Timezone Aware**: Use `set timezone` to ensure Shiori knows *your* "8 PM", not the server's.
*(Though it is very important to understand that reminders changes very often, it's the most complex function)*

### 🖼️ Image Tools
Useful utilities for modifying images without opening Photoshop.
*   **Resize**: Reply to an image with `resize` to scale it.
*   **Convert**: Reply with `convert` to change formats (e.g., PNG to JPG).

### ⭐ Starboard
She can even automatically aggregates the best content in your server.
*   **Thresholds**: Messages with enough specific reactions (e.g., 5 ⭐) are reposted to a designated starboard channel.
*   **Smart Context**: The repost includes a link to the original message and preserves the context of the conversation.

### 🎰 Misc & Fun
*   **Magic 8-Ball**: Ask a question, get an answer.
*   **BOD (Boundary of Death)**: A probability game for the risk-takers.
*   **Sanitize**: A utility to post the YouTube sanitization guide (don't ask).

### 🎵 Music (Ambient Presence)
The bot appears to "listen" to music via its Discord status, cycling through a configured YouTube playlist.
*   **Listen Along**: Ask the bot to join your voice channel and it will play the music it's "listening to".
*   **Player Controls**: Skip tracks, view the queue, toggle shuffle, see what's playing.
*   **Global Session**: The bot can only be in one voice channel at a time across all servers.
*   **Idle Timeout**: If no one joins within 5 minutes, the bot returns to idle mode.

## The Architecture: A "Hybrid" System

Shiori is distinct from standard `discord.py` bots because of her **Hybrid Command Dispatcher**.

### 1. The Brain: NLP Dispatcher
Traditional bots wait for a specific string (e.g., `!ping`). Shiori understands normal language. (Mostly)
*   **Interceptor**: The bot intercepts messages before being sent to the usual command dispatcher.
*   **Analysis**: It scans the content against a registry of regex patterns defined in `config.py`.
*   **Intent**: If a pattern matches (e.g., `r'\broll\b'`), it routes the message to the appropriate handler, regardless of surrounding "fluff" words.

### 2. The Backbone: Modular Configuration
Because regex patterns can get complex, the bot centralizes them in `config.py`. This allows developers to tweak the "vocabulary" of the bot without diving into deep logic code.
*   **`config.NLP_COMMANDS`**: The central registry where patterns are mapped to Cog functions.
*   **Priority System**: The dispatcher intelligently resolves conflicts if a sentence matches multiple commands.

## Pre-built Binaries (No Python Required)

If you don't have Python installed or prefer a standalone executable, you can download the latest build from our GitHub Actions:

1.  Go to the **Actions** tab in this repository.
2.  Click on the latest workflow run (usually named "Build Application").
3.  Scroll down to the **Artifacts** section at the bottom.
4.  Download the **Shiori** zip file for your platform (Windows/Linux/macOS).

*Note: You will still need to download the `assests` folder and configure `info.env` in the folder where you extract the executable.*

**If you want to be able to modify the code and run it yourself, you can continue reading, otherwise this is all you need to know.**

## Getting Started

*If you're here for information on ***creating*** standalone executables, please refer to [BUILD.md](BUILD.md) instead.*

### Prerequisites
*   Python 3.11+
*   A Discord Bot Token
*   A Name (Not a requirement, but highly recommended)
*   **FFmpeg** (required for music playback - see below)

#### FFmpeg Installation

The music cog requires FFmpeg to be installed and available in your system PATH:

*   **Windows**: `winget install ffmpeg` or download from [ffmpeg.org](https://ffmpeg.org/download.html)
*   **Linux**: `sudo apt install ffmpeg` (Debian/Ubuntu) or `sudo dnf install ffmpeg` (Fedora)
*   **macOS**: `brew install ffmpeg`

*Note: Pre-built executables may include FFmpeg bundled, so end-users don't need to install it separately.*

#### Ambience System (Optional)

The bot has an optional "ambience" system that gives it personality - it cycles through moods, activities, and can play music from your playlists. To set this up:

1.  Copy `assets/ambience.toml.example` to `assets/ambience.toml`
2.  Customize the interests and playlists with your own preferences
3.  The file hot-reloads on changes - no restart needed!

**What's in `ambience.toml`:**

| Section | Purpose |
|---------|---------|
| `[config]` | Timing settings (mood cycle frequency, music weight) |
| `[interests.*]` | Personality data (favorite books, games, snacks, etc.) used for flavor text |
| `[playlists]` | YouTube playlist URLs organized by mood (cozy, energetic, sleepy, etc.) |
| `[playlists.descriptions]` | Short descriptions for each playlist mood |

The mood/activity *structure* (what moods exist, what activities belong to each) is defined in `utils/ambience.py`. The TOML file only contains the *content* - your personal preferences and playlists.

To disable ambience output while keeping internal mood cycling, set `AMBIENCE_ENABLED=False` in `info.env`.

### Installation

1.  **Clone the repository**
    ```bash
    git clone https://github.com/selectL-L/Shiori.git
    cd Shiori
    ```

2.  **Run once to generate config**
    Run the bot.
    ```bash
    python main.py
    ```
    It will detect the missing configuration, then generate a `info.env` template, and exit.

3.  **Configure `info.env`**
    Open the newly created `info.env` file and set your credentials:
    ```ini
    DISCORD_TOKEN=your_token_here
    BOT_PREFIX=your_prefix_here
    BOT_NAME=your_bot_name_here (you CAN leave it as NoName, but again, we recommend setting a name)
    OWNER_ID=your_id_here
    ```

4.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Launch**
    ```bash
    python main.py
    ```

## Developer Guide

We welcome contributions! Please follow these guidelines to keep the project healthy.

### DEV_MODE

Setting `DEV_MODE=True` in `info.env` enables debug logging and restricts slash command sync to `DEV_GUILD`. 

**Web UI testing:** DEV_MODE routes use mock data instead of real Discord data. You must create `utils/web/mock_data.py` yourself—this file is gitignored. Check the imports in `utils/web/routes.py` for the expected function signatures.

### Runtime Control

Shiori can be controlled at runtime without using Discord commands. This is **by design** and can be used for server administration, automated scripts, or when you simply prefer managing the bot externally.

#### Available Commands

| Command | Description |
|---------|-------------|
| `reload` | Hot-reload all cogs without restarting the process |
| `restart` | Full restart without manually re-running `python main.py` |
| `exit` | Graceful shutdown (saves state, closes DB connections) |
| `status` | Returns the bot's current running state (TCP only) |

#### Method 1: Console (Local Development)

When running `python main.py` directly in a terminal, you can type commands into the console:

```
reload
```

This is ideal for local development where you have direct terminal access.

#### Method 2: TCP Socket (Remote / Headless)

For production deployments (e.g., systemd services, Docker containers), the console isn't accessible. Instead, enable the TCP control socket by setting `CONTROL_PORT` in your `info.env`:

```ini
CONTROL_PORT=9999
```

The server binds to `127.0.0.1` only—it cannot be accessed from outside the machine.

**Sending commands:**

```bash
# Linux (using netcat)
echo "reload" | nc localhost 9999
echo "restart" | nc localhost 9999
echo "status" | nc localhost 9999

# Windows (PowerShell)
"reload" | ncat localhost 9999
```

**Response format:** Commands return `OK: <message>` on success or `ERROR: <message>` on failure, making it easy to script around.

### Critical Differences

> [!WARNING]
> **Database Schema Management**
> Shiori maintains **two** definitions of the database schema. You must update **BOTH** when making changes:
> 1.  `utils/database.py`: Used for runtime validation and fresh installs.
> 2.  `migrate_db.py`: Used for migrating existing data to a new schema.

### UI Patterns
*   **Hybrid Selection**: Use `utils.views.get_selection`. It allows users to pick an option via Button *or* by typing the answer.
*   **Modals**: Use `utils.views.launch_modal`. This wrapper allows text-based commands to "launch" modals (by sending a button first), unifying the behavior with slash commands.

### Code Style
*   **Docstrings**: **Google Style** is mandatory for all functions/classes.
*   **Type Hinting**: Required for all arguments and returns.
*   **Base Class**: All new Cogs must inherit from `utils.base_cog.BaseCog`.
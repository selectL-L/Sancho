# Sancho (V0.72 - The Name Update)

**A Discord bot designed to be understood by humans.**

Sancho is built on a simple premise: interacting with a bot should feel natural. Rather than forcing you to memorize rigid command structures like a terminal operator, Sancho attempts to interpret your *intent*.

If you ask her to "roll me a d20", she understands. If you ask her to "please roll a d20 for me?", she understands that too.

## Features

### 🎲 Math & Dice
Sancho supports a robust dice engine capable of handling complex notations.
*   **Standard Rolling**: `roll 2d20kh1 + 5` (Roll two d20s, keep the highest, add 5).
*   **Limbus Coin Flips**: A dedicated binary outcome generator for Limbus Company rolls.
*   **Calculator**: Evaluate mathematical expressions directly in chat.

### ⚔️ Skills Database
The Skills system acts as a macro manager, allowing you to save complex dice notations or text as named "Skills".
*   **Save**: Simply type `save skill` to enter an interactive setup wizard.
*   **Cast**: Use `cast Fireball` to execute the saved macro instantly.
*   **Manage**: List, edit, or delete your skills with natural language commands like `show my skills` or `delete Fireball`.

### ⏰ Reminders
Set reminders using natural language without worrying about strict syntax.
*   **Natural Phrasing**: `remind me in 2 hours to check the laundry` or `remind me next tuesday to deploy`.
*   **Timezone Aware**: Use `set timezone` to ensure Sancho knows *your* "8 PM", not the server's.

### 🖼️ Image Tools
Useful utilities for modifying images without opening Photoshop.
*   **Resize**: Reply to an image with `resize` to scale it.
*   **Convert**: Reply with `convert` to change formats (e.g., PNG to JPG).

### ⭐ Starboard
Sancho automatically aggregates the best content in your server.
*   **Thresholds**: Messages with enough specific reactions (e.g., 5 ⭐) are reposted to a designated starboard channel.
*   **Smart Context**: The repost includes a link to the original message and preserves the context of the conversation.

### 🎰 Misc & Fun
*   **Magic 8-Ball**: Ask a question, get an answer.
*   **BOD (Boundary of Death)**: A probability game for the risk-takers.
*   **Sanitize**: A utility to post the YouTube sanitization guide (don't ask).

## The Architecture: A "Hybrid" System

Sancho is distinct from standard `discord.py` bots because of her **Hybrid Command Dispatcher**.

### 1. The Brain: NLP Dispatcher
Traditional bots wait for a specific string (e.g., `!ping`). Sancho understands normal language. (Mostly)
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
4.  Download the **Sancho** zip file for your platform (Windows/Linux/macOS).

*Note: You will still need to download the `assests` folder and configure `info.env` in the folder where you extract the executable.*

**If you want to be able to modify the code and run it yourself, you can continue reading, otherwise this is all you need to know.**

## Getting Started

### Prerequisites
*   Python 3.11+
*   A Discord Bot Token
*   A Name (Not a requirement, but highly recommended)

### Installation

1.  **Clone the repository**
    ```bash
    git clone https://github.com/selectL-L/Sancho.git
    cd Sancho
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

We welcome contributions! Please follow these guidelines to keep Sancho healthy.
*If you're here for information on creating standalone executables, please refer to [BUILD.md](BUILD.md).*

### Helpful Console Commands
Sancho has a console listener and can accept commands from the console while running, this is not a very expansive list, but they're useful to know.

*   **Hot Reload**: Type `reload` in the running bot's console to reload all cogs without restarting the process.
*   **Graceful Exit**: Type `exit` in the console to shut down cleanly (saves state, closes DB).

### Critical Differences

> [!WARNING]
> **Database Schema Management**
> Sancho maintains **two** definitions of the database schema. You must update **BOTH** when making changes:
> 1.  `utils/database.py`: Used for runtime validation and fresh installs.
> 2.  `migrate_db.py`: Used for migrating existing data to a new schema.

### UI Patterns
*   **Hybrid Selection**: Use `utils.views.get_selection`. It allows users to pick an option via Button *or* by typing the answer.
*   **Modals**: Use `utils.views.launch_modal`. This wrapper allows text-based commands to "launch" modals (by sending a button first), unifying the behavior with slash commands.

### Code Style
*   **Docstrings**: **Google Style** is mandatory for all functions/classes.
*   **Type Hinting**: Required for all arguments and returns.
*   **Base Class**: All new Cogs must inherit from `utils.base_cog.BaseCog`.
# Sancho (v0.7 - The Name Update)

Sancho is a local Discord bot built on a simple premise, that interacting with her SHOULD feel natural.

That is to mean rather than forcing you to type rigid commands like a terminal operator, Sancho uses a custom Natural Language Processing (NLP) dispatcher to interpret your requests. It attempts to understand what you want, even if you phrase it with extreme verbosity.

## Input Processing

You are not required to memorize specific command structures (Unless you are an admin), though it would be more efficient if you did.
As an example here are some cases Sancho can currently understand, with how easily it is for her to understand you in brackets besides each.
- **Standard:** `.sancho roll 1d20` (Preferred.)
- **Verbose:** `.sancho roll me a d20` (Acceptable.)
- **Excessive:** `.sancho can you please roll a d20 for me?` (Functionally identical, if verbose.)

## Modules

### 🎲 Math & Dice
*Arithmetic services.*

- **Dice Rolling:** Supports standard notation (`2d6+5`), keep/drop (`4d6kh3`), and other common patterns. It generates random numbers.
- **Limbus Company Coin Flips:** A simple binary outcome generator.
- **Calculator:** Evaluates basic mathematical expressions. `.sancho calculate 5 + 5`.

### ⚔️ Skills Database
*Macro management.*

Allows users to save complex dice notations as "skills" to avoid repetitive typing.
- **Save:** `.sancho save skill Fireball`
- **Use:** `.sancho cast Fireball`
- **Manage:** CRUD operations for user-defined skills.
- **Limits:** Configurable caps to prevent database bloat.

### ⏰ Reminders
*Externalized memory.*

Set reminders using natural language.
- **Set:** `.sancho remind me in 2 hours to check the logs`
- **Timezones:** Handles local time conversion. `.sancho set timezone EST`
- **Manage:** Review and delete pending alerts.

### 🖼️ Image Tools
*Basic media manipulation.*

- **Resize:** `.sancho resize` (reply/attach an image).
- **Convert:** `.sancho convert` (reply/attach an image). Changes file formats.

### ⭐ Starboard
*Message aggregation.*

Automatically reposts messages that meet a specific reaction threshold.
- **Context Awareness:** Preserves reply chains for context.
- **Configuration:** Adjustable channel targets, emojis, and thresholds.
- **Maintenance:** Tools for database synchronization and reloading.

### 🎰 Misc
*Various time sinks, sorry I mean fun commands.*
(please note that the dispatcher for commands like pear wiggler and sanitize is exceptionally flexible
included commands are examples and not requirements.)

- **BOD (Boundary of Death):** A probability game sponsored by Yujin.
    - *Cooldown:* 12 hours.
    - *Session:* 20 minutes.
    - *Leaderboard:* Tracks statistical outliers. (I know who you are)
- **Magic 8-Ball:** Returns a randomized string from a pre-defined list.
- **Pear Wiggler:** Posts a gif of the "pear wiggler".
- **Sanitize:** Posts an image of the youtube sanitization guide.

## Administration
*System operations.*

- **Status:** Displays latency, uptime, and resource consumption.
- **Reports:** Generates data dumps on user skills and reminders.
- **Limits:** Global and per-user configuration.

## Installation

If you want to run your own instance:

1.  **Clone.**
2.  **Configure:** Create `info.env` with `DISCORD_TOKEN` and `BOT_PREFIX`.
3.  **Install:** `pip install -r requirements.txt`
4.  **Execute:** `python main.py`

## Building

### Automated Builds
We have set up a GitHub Actions workflow to automatically build the bot for **Windows**, **Linux** (Ubuntu), and **macOS** whenever the `Master` branch is updated.
1.  Go to the **Actions** tab in your GitHub repository.
2.  Click on the latest workflow run.
3.  Scroll down to the **Artifacts** section.
4.  Download the executable for your desired platform.

### Manual Builds
If you want to build the executable yourself (e.g., for a different platform or with custom changes), please refer to [BUILD.md](BUILD.md) for detailed instructions on using our `build.py` script.

## Contributing

The [Issues](https://github.com/selectL-L/Sancho/issues) page is available for bug reports. Feature requests will be reviewed, eventually.
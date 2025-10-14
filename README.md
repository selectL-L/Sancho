# AI-Sancho

Your friendly neighborhood Discord bot, ready to roll dice and remember your stuff.

This is a small, personal bot built to handle commands in a more natural way. Instead of rigid command structures, you can just talk to it.

## What's it do?

Sancho is currently equipped with a couple of handy features:

*   **A pretty slick Dice Roller:** You can throw complex dice notations at it, and it'll figure it out. We're talking things like `.sancho roll (2d6+5)*3` or `.sancho roll 4d20kh3 with advantage`.
*   **Natural Language Reminders:** Set reminders just by talking to the bot. For example: `.sancho remind me in 2 hours to check on my laundry`. It also supports custom timezones so you don't have to do mental math.

## How to Use It

Just start a message with `.sancho` and tell it what you want.

### Dice Rolling

*   `.sancho roll 2d6 + 5`
*   `.sancho roll 1d20 with advantage`
*   `.sancho roll 8d6kh5` (Rolls 8 six-sided dice and keeps the highest 5)

### Reminders

*   `.sancho remind me in 30 minutes to take a break`
*   `.sancho remind me on Friday at 8pm to watch the new movie`
*   `.sancho timezone EST` (Sets your personal timezone for all reminders)
*   `.sancho checkreminders` (See what you've asked it to remember)
*   `.sancho delrem 1` (Deletes your first reminder from the list)

## How to Get It Running

1.  **Clone the repo.**
2.  **Create an `info.env` file** in the main directory. It needs to contain one thing:
    ```
    DISCORD_TOKEN=YourSuperSecretBotTokenHere
    ```
3.  **Install the good stuff:**
    ```
    pip install -r requirements.txt
    ```
4.  **Run it!**
    ```
    python main.py
    ```

## Want to build it yourself?

If you want to bundle this up into a neat little executable, check out the [**Build Guide**](BUILD.md).

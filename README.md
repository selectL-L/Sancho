# Sancho (Now offically on V0.1!)

A local discord bot, mostly designed for my own small community, but does have flexibility to be used in your servers, or even forked for your own use!

The major design philosophy is to handle commands in a more natural way. Instead of rigid command structures, you can just talk to it. (Though some rigid commands exist for admin stuff!)

## What's it do?

Sancho is currently equipped with a couple of handy features (more to come!):

*   **A Dice Roller:** You can use complex dice notations with it. Stuff like `.sancho roll (2d6+5)*3` or `.sancho roll 4d20kh3 with advantage` should simply just work.
*   **Natural Language Reminders:** Set reminders just by talking to the bot (still doesn't work super well (yet)). For example: `.sancho remind me in 2 hours to check on my laundry`. It also supports custom timezones so you don't have to do any conversions.
*   **Image Conversion:** You can convert and resize images just by asking `.sancho convert` or `.sancho resize` and either attaching your image, or replying to an image.
*   **Skill database:** You can save dice notation as skills that can be called at any time, helps with roleplay and/or repetitive work.
*   **General fun commands:** You can call the bot for certain more basic fun commands like 8ball or BOD (from hit game library of ruina)!

## How to Use It

Just start a message with `.sancho` or `.s` (by default, changeable in config.py!) and speak to it.

(List of commands removed in lieu of figuring out a better way to do so.)

## How to Get It Running

1.  **Clone the repo.**
2.  **Create an `info.env` file** in the main directory. You only have to fill in the token:
    ```
    DISCORD_TOKEN=YourSuperSecretBotTokenHere
    ```
3.  **Install the good stuff:**
    ```
    pip install -r requirements.txt
    ```
4.  **Run it! (or don't, we don't judge):**
    ```
    python main.py
    ```

## Want to build it yourself?

If you want to bundle this up into a neat little executable (THAT MAY OR MAY NOT WORK), check out the [**Build Guide**](BUILD.md).

"""Ambience system for bot personality and idle activities.

Architecture:
    TOML (ambience.toml) = Static personality data (interests, playlists)
    Python (this file) = Structure (moods, activities) + Runtime state

Hierarchy:
    MOOD (king) - relaxing, productive, social, out_and_about
        └── ACTIVITY (subset) - drinking_boba, studying, etc.
              ├── status (Discord status text)
              ├── background_music (can music play alongside?)
              ├── cog overrides (activity-specific responses)
              └── context_key (for dynamic content from TOML)

Cascade for cog queries:
    activity config → mood config → global default

Music is special:
    - is_music=True activities: Status AND presence show music
    - background_music=True: Status shows activity, presence shows track
    - Otherwise: No music presence
"""
from __future__ import annotations

import random
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import config

# ════════════════════════════════════════════════════════════════════════════════
# TOML CONFIG LOADING
# ════════════════════════════════════════════════════════════════════════════════

_TOML_PATH = Path(config.ASSETS_PATH) / "ambience.toml"
_toml_cache: Optional[dict[str, Any]] = None
_toml_mtime: float = 0.0


def _load_toml() -> dict[str, Any]:
    """Load ambience.toml with caching and hot-reload."""
    global _toml_cache, _toml_mtime

    if not _TOML_PATH.exists():
        return {}

    try:
        current_mtime = _TOML_PATH.stat().st_mtime
    except OSError:
        return _toml_cache or {}

    if _toml_cache is not None and current_mtime == _toml_mtime:
        return _toml_cache

    try:
        with open(_TOML_PATH, "rb") as f:
            _toml_cache = tomllib.load(f)
            _toml_mtime = current_mtime
    except Exception:
        _toml_cache = _toml_cache or {}

    return _toml_cache


def get_interest(category: str, key: str) -> Optional[str]:
    """Get a random value from interests.

    Args:
        category: Interest category (e.g., 'reading', 'gaming')
        key: Key within category (e.g., 'books', 'cozy_games')

    Returns:
        Random item from the list, or None.
    """
    toml = _load_toml()
    items = toml.get("interests", {}).get(category, {}).get(key, [])
    return random.choice(items) if items else None


def get_all_interests(category: str, key: str) -> list[str]:
    """Get all values from an interest category."""
    toml = _load_toml()
    return toml.get("interests", {}).get(category, {}).get(key, [])


def get_config(key: str, default: Any = None) -> Any:
    """Get a config value."""
    toml = _load_toml()
    return toml.get("config", {}).get(key, default)


def get_playlist(music_mood: str) -> Optional[str]:
    """Get a random playlist URL for a music mood."""
    toml = _load_toml()
    playlists = toml.get("playlists", {}).get(music_mood, [])
    return random.choice(playlists) if playlists else None


def get_all_playlists(music_mood: str) -> list[str]:
    """Get all playlist URLs for a music mood."""
    toml = _load_toml()
    return toml.get("playlists", {}).get(music_mood, [])


def get_playlist_description(music_mood: str) -> Optional[str]:
    """Get the description for a music mood."""
    toml = _load_toml()
    return toml.get("playlists", {}).get("descriptions", {}).get(music_mood)


# ════════════════════════════════════════════════════════════════════════════════
# ACTIVITY & MOOD DEFINITIONS
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class Activity:
    """Definition of an idle activity."""
    id: str
    status: str  # Discord status text
    background_music: bool = False  # Can music play alongside?
    is_music: bool = False  # Is this JUST music? (status & presence align)
    # (category, key) for interests
    context_key: Optional[tuple[str, str]] = None
    cog: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Mood:
    """Definition of a mood containing activities."""
    id: str
    activities: dict[str, Activity]
    default_cog: dict[str, dict[str, Any]] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────────
# MOOD: RELAXING - Cozy, self-care, chill vibes
# ────────────────────────────────────────────────────────────────────────────────

_RELAXING = Mood(
    id="relaxing",
    activities={
        "listening_music": Activity(
            id="listening_music",
            status="listening to music~ 🎵",
            is_music=True,
            cog={
                "reminders": {"interrupt": ""},
            }
        ),
        "drinking_boba": Activity(
            id="drinking_boba",
            status="boba time 🧋",
            background_music=True,
            context_key=("comfort", "boba_flavors"),
            cog={
                "reminders": {"interrupt": "*puts down boba* "},
                "help": {
                    "greeting": ["*sips boba* Yeah?", "Hmm?"],
                    "intro": ["I'm enjoying some boba rn", "I'm in the middle of a boba break"],
                    "availability": ["but I'm happy to help!", "what's up?"],
                },
            }
        ),
        "blanket_burrito": Activity(
            id="blanket_burrito",
            status="maximum cozy 🌯",
            background_music=True,
            cog={
                "reminders": {"interrupt": "*peeks out of blankets* "},
                "help": {
                    "greeting": ["*emerges* Yeah?"],
                    "intro": ["I'm wrapped up in blankets 🌯", "I'm in maximum cozy mode"],
                    "availability": ["but I'll peek out to help~", "cozy but here for you!"],
                },
            }
        ),
        "petting_cat": Activity(
            id="petting_cat",
            status="petting a cat 🐱",
            background_music=True,
            context_key=("pets", "cat_names"),
            cog={
                "reminders": {"interrupt": "*cat protests* "},
            }
        ),
        "napping": Activity(
            id="napping",
            status="napping 😴",
            background_music=False,
            cog={
                "reminders": {"interrupt": "*yawns* "},
                "help": {
                    "greeting": ["*yawns* Mm? 😴", "*stretches* Oh, hey~"],
                    "intro": ["I was just napping 😴", "I'm a bit sleepy 😴"],
                    "availability": ["but I'm awake now, what's up?", "*yawns* how can I help?"],
                },
            }
        ),
        "doing_skincare": Activity(
            id="doing_skincare",
            status="skincare time ✨",
            background_music=True,
            cog={}
        ),
        "candle_vibes": Activity(
            id="candle_vibes",
            status="cozy candle vibes 🕯️",
            background_music=True,
            context_key=("comfort", "candle_scents"),
            cog={}
        ),
        "watching_rain": Activity(
            id="watching_rain",
            status="watching the rain 🌧️",
            background_music=True,
            cog={
                "help": {
                    "greeting": ["*rain sounds* Hey~", "Perfect weather~ What's up?"],
                    "intro": ["I'm watching the rain 🌧️", "I'm enjoying the rain"],
                    "availability": ["perfect weather to help out~", "what can I do for you?"],
                },
            }
        ),
        "stargazing": Activity(
            id="stargazing",
            status="stargazing ✨",
            background_music=True,
            cog={}
        ),
        "snacking": Activity(
            id="snacking",
            status="snack break 🍿",
            background_music=True,
            context_key=("comfort", "snacks"),
            cog={
                "reminders": {"interrupt": "*puts down snacks* "},
            }
        ),
    },
    default_cog={
        "reminders": {
            "interrupt": "",
            "flavor": [" ✨", " ~", ""],
        },
        "help": {
            "greeting": ["Hey~ ✨", "Hmm? What's up?", "Hi hi~"],
            "intro": ["I'm just relaxing", "I'm taking it easy"],
            "availability": ["but I'm here if you need me~", "what's on your mind?"],
        },
        "fun": {
            "sass_level": 0.2,
        },
    }
)

# ────────────────────────────────────────────────────────────────────────────────
# MOOD: PRODUCTIVE - Studying, creating, focused
# ────────────────────────────────────────────────────────────────────────────────

_PRODUCTIVE = Mood(
    id="productive",
    activities={
        "listening_music": Activity(
            id="listening_music",
            status="focus music 🎵",
            is_music=True,
            cog={}
        ),
        "studying": Activity(
            id="studying",
            status="studying 📚",
            background_music=True,
            cog={
                "reminders": {"interrupt": "*looks up from notes* "},
                "help": {
                    "greeting": ["Study break! What's up?", "*closes textbook* Yeah?"],
                    "intro": ["I'm busy studying 📚", "I'm in the middle of studying"],
                    "availability": ["but if you need me I'll still be around!", "what do you need?"],
                },
            }
        ),
        "researching": Activity(
            id="researching",
            status="down a rabbit hole 🔍",
            background_music=True,
            context_key=("learning", "hyperfixations"),
            cog={
                "reminders": {"interrupt": "*closes 50 tabs* "},
                "help": {
                    "greeting": ["*emerges from research* Oh!", "Did you know- wait, what's up?"],
                    "intro": ["I'm deep in a research rabbit hole 🔍", "I'm researching something fascinating"],
                    "availability": ["but I can surface to help!", "did you know- wait, what do you need?"],
                },
            }
        ),
        "learning_new": Activity(
            id="learning_new",
            status="learning something new ✨",
            background_music=True,
            context_key=("learning", "topics"),
            cog={}
        ),
        "drawing": Activity(
            id="drawing",
            status="drawing 🎨",
            background_music=True,
            context_key=("creative", "art_subjects"),
            cog={
                "reminders": {"interrupt": "*sets pen down* "},
            }
        ),
        "writing": Activity(
            id="writing",
            status="writing ✍️",
            background_music=True,
            context_key=("creative", "writing_projects"),
            cog={
                "reminders": {"interrupt": "*saves draft* "},
            }
        ),
        "editing_photos": Activity(
            id="editing_photos",
            status="editing photos 📷",
            background_music=True,
            cog={}
        ),
        "cooking": Activity(
            id="cooking",
            status="cooking 🍳",
            background_music=True,
            cog={
                "reminders": {"interrupt": "*sets timer* "},
            }
        ),
        "baking": Activity(
            id="baking",
            status="baking 🧁",
            background_music=True,
            cog={
                "reminders": {"interrupt": "*flour everywhere but* "},
            }
        ),
    },
    default_cog={
        "reminders": {
            "interrupt": "",
            "flavor": [" 📝", " ✨", ""],
        },
        "help": {
            "greeting": ["Taking a break! What's up?", "Yeah?", "Perfect timing~"],
            "intro": ["I'm being productive", "I'm in the zone"],
            "availability": ["but I can take a break to help!", "but, perfect timing for a break~"],
        },
        "fun": {
            "sass_level": 0.1,  # Less sassy when focused
        },
    }
)

# ────────────────────────────────────────────────────────────────────────────────
# MOOD: SOCIAL - Consuming content, watching, reading, gaming
# ────────────────────────────────────────────────────────────────────────────────

_SOCIAL = Mood(
    id="social",
    activities={
        "listening_music": Activity(
            id="listening_music",
            status="vibing to music 🎵",
            is_music=True,
            cog={}
        ),
        "watching_anime": Activity(
            id="watching_anime",
            status="watching anime 📺",
            background_music=False,
            context_key=("watching", "anime"),
            cog={
                "reminders": {"interrupt": "*pauses anime* "},
                "help": {
                    "greeting": ["*pauses* What's up?"],
                    "intro": ["I'm watching anime 📺", "I'm in the middle of a good episode"],
                    "availability": ["but I can pause for you!", "good timing, it was a cliffhanger anyway~"],
                },
            }
        ),
        "watching_movies": Activity(
            id="watching_movies",
            status="movie time 🎬",
            background_music=False,
            cog={
                "reminders": {"interrupt": "*pauses movie* "},
            }
        ),
        "watching_youtube": Activity(
            id="watching_youtube",
            status="youtube rabbit hole 📱",
            background_music=False,
            context_key=("watching", "youtube_topics"),
            cog={
                "help": {
                    "greeting": ["How did I end up here... anyway!", "*closes 47 tabs* Yeah?"],
                    "intro": ["I'm in a YouTube rabbit hole 📱", "I'm watching videos"],
                    "availability": ["how did I end up here... anyway, what's up?", "but I can close some tabs~"],
                },
            }
        ),
        "watching_shorts": Activity(
            id="watching_shorts",
            status="scrolling shorts 📱",
            background_music=False,
            cog={
                "reminders": {"interrupt": "*finally puts phone down* "},
            }
        ),
        "watching_streams": Activity(
            id="watching_streams",
            status="watching streams 📺",
            background_music=False,
            cog={
                "reminders": {"interrupt": "*lurking in chat but* "},
            }
        ),
        "reading_book": Activity(
            id="reading_book",
            status="reading 📚",
            background_music=True,
            context_key=("reading", "books"),
            cog={
                "reminders": {"interrupt": "*bookmarks page* "},
                "help": {
                    "greeting": ["*sets book down* Hey!"],
                    "intro": ["I'm reading 📚", "I'm in the middle of a good book"],
                    "availability": ["but this is a good stopping point!", "it was getting good but I'm here~"],
                },
            }
        ),
        "reading_manga": Activity(
            id="reading_manga",
            status="reading manga 📖",
            background_music=True,
            context_key=("reading", "manga"),
            cog={
                "reminders": {"interrupt": "*bookmarks chapter* "},
            }
        ),
        "reading_webnovel": Activity(
            id="reading_webnovel",
            status="reading webnovel 📱",
            background_music=True,
            context_key=("reading", "webnovels"),
            cog={
                "help": {
                    "greeting": ["*bookmarks* Yeah?"],
                    "intro": ["I'm reading a webnovel 📱", "I'm deep into a webnovel"],
                    "availability": ["chapter 847 can wait~", "but I can bookmark this!"],
                },
            }
        ),
        "browsing_reddit": Activity(
            id="browsing_reddit",
            status="doom scrolling",
            background_music=True,
            cog={
                "reminders": {"interrupt": "*closes app* "},
            }
        ),
        "browsing_twitter": Activity(
            id="browsing_twitter",
            status="on twitter",
            background_music=True,
            cog={}
        ),
        "listening_podcast": Activity(
            id="listening_podcast",
            status="podcast time 🎙️",
            background_music=False,
            context_key=("learning", "podcast_topics"),
            cog={
                "reminders": {"interrupt": "*pauses podcast* "},
            }
        ),
        "playing_cozy_game": Activity(
            id="playing_cozy_game",
            status="playing cozy games 🌱",
            background_music=True,
            context_key=("gaming", "cozy_games"),
            cog={
                "reminders": {"interrupt": "*pauses game* "},
                "help": {
                    "greeting": ["*saves game* Yeah?"],
                    "intro": ["I'm playing cozy games 🌱", "I was just tending to my farm"],
                    "availability": ["but the crops can wait~", "*saves game* what's up?"],
                },
            }
        ),
        "playing_rhythm_game": Activity(
            id="playing_rhythm_game",
            status="rhythm game time 🎵",
            background_music=False,
            context_key=("gaming", "rhythm_games"),
            cog={
                "reminders": {"interrupt": "*misses note* ...anyway, "},
            }
        ),
        "playing_gacha": Activity(
            id="playing_gacha",
            status="gacha despair 😭",
            background_music=True,
            context_key=("gaming", "gacha_games"),
            cog={
                "help": {
                    "greeting": ["*closes gacha in despair* Yeah?"],
                    "intro": ["I'm playing gacha 😭", "I'm testing my luck"],
                    "availability": ["no luck today anyway... what's up?", "but I could use the distraction~"],
                },
            }
        ),
        "playing_puzzle_game": Activity(
            id="playing_puzzle_game",
            status="puzzle time 🧩",
            background_music=True,
            context_key=("gaming", "puzzle_games"),
            cog={}
        ),
        "video_calling": Activity(
            id="video_calling",
            status="calling friends 📱",
            background_music=False,
            cog={
                "reminders": {"interrupt": "*waves at camera* one sec— "},
            }
        ),
    },
    default_cog={
        "reminders": {
            "interrupt": "",
            "flavor": [" 📱", " ~", ""],
        },
        "help": {
            "greeting": ["What's up?", "Hey!", "Yeah?"],
            "intro": ["I'm just hanging out", "I'm chilling"],
            "availability": ["what's on your mind?", "how can I help?"],
        },
        "fun": {
            "sass_level": 0.3,
        },
    }
)

# ────────────────────────────────────────────────────────────────────────────────
# MOOD: OUT AND ABOUT - Café, park, exploring
# ────────────────────────────────────────────────────────────────────────────────

_OUT_AND_ABOUT = Mood(
    id="out_and_about",
    activities={
        "listening_music": Activity(
            id="listening_music",
            status="music on the go 🎵",
            is_music=True,
            cog={}
        ),
        "at_cafe": Activity(
            id="at_cafe",
            status="at a café ☕",
            background_music=True,
            cog={
                "help": {
                    "greeting": ["*ambient café noises* Hey~", "Café vibes! What's up?"],
                    "intro": ["I'm at a café ☕", "I'm enjoying some coffee"],
                    "availability": ["perfect place to help out~", "what can I get you?"],
                },
            }
        ),
        "at_park": Activity(
            id="at_park",
            status="at the park 🌳",
            background_music=True,
            cog={}
        ),
        "at_bookstore": Activity(
            id="at_bookstore",
            status="lost in a bookstore 📚",
            background_music=False,
            cog={
                "help": {
                    "greeting": ["The good kind of lost~"],
                    "intro": ["I'm lost in a bookstore 📚", "I'm browsing books"],
                    "availability": ["so many books... but what do you need?", "the good kind of lost, what's up?"],
                },
            }
        ),
        "window_shopping": Activity(
            id="window_shopping",
            status="window shopping 🛒",
            background_music=True,
            cog={
                "help": {
                    "greeting": ["Ohhh, it's so shiny!~"],
                    "intro": ["I'm window shopping 🛒", "I'm just browsing"],
                    "availability": ["shame I'm not buying anything!... anyway!", "just looking, what's up?"],
                },
            }
        ),
        "exploring": Activity(
            id="exploring",
            status="exploring ✨",
            background_music=True,
            cog={}
        ),
        "thrifting": Activity(
            id="thrifting",
            status="treasure hunting 🛍️",
            background_music=True,
            cog={
                "help": {
                    "greeting": ["Thrift store adventures~"],
                    "intro": ["I'm treasure hunting 🛍️", "I'm thrifting"],
                    "availability": ["finding good stuff! what's up?", "thrift store adventures~ need something?"],
                },
            }
        ),
        "cloud_watching": Activity(
            id="cloud_watching",
            status="cloud watching ☁️",
            background_music=True,
            cog={
                "help": {
                    "greeting": ["That ones so big!~"],
                    "intro": ["I'm cloud watching ☁️", "I'm watching the clouds"],
                    "availability": ["that one looks a little like you~ what do you need?", "peaceful day, what's up?"],
                },
            }
        ),
        "watching_sunset": Activity(
            id="watching_sunset",
            status="golden hour 🌅",
            background_music=True,
            cog={}
        ),
    },
    default_cog={
        "reminders": {
            "interrupt": "",
            "flavor": [" ✨", " ~", ""],
        },
        "help": {
            "greeting": ["Hey~", "What's up?", "Oh hey!"],
            "intro": ["I'm out and about", "I'm exploring"],
            "availability": ["but still here to help!", "what can I do for you?"],
        },
        "fun": {
            "sass_level": 0.25,
        },
    }
)

# ────────────────────────────────────────────────────────────────────────────────
# SPECIAL: DAYDREAMING - Can happen in any mood
# ────────────────────────────────────────────────────────────────────────────────

_DAYDREAMING = Activity(
    id="daydreaming",
    status="daydreaming 💭",
    background_music=True,
    cog={
        "reminders": {"interrupt": "*snaps back* "},
        "help": {
            "greeting": ["Huh? Oh!", "*snaps out of it* Hey!"],
            "intro": ["I was daydreaming 💭", "I was lost in thought"],
            "availability": ["oops, what's up?", "oh! how can I help?"],
        },
    }
)

# ════════════════════════════════════════════════════════════════════════════════
# REGISTRY
# ════════════════════════════════════════════════════════════════════════════════

MOODS: dict[str, Mood] = {
    "relaxing": _RELAXING,
    "productive": _PRODUCTIVE,
    "social": _SOCIAL,
    "out_and_about": _OUT_AND_ABOUT,
}

# Music moods (for playlist selection) - separate from activity moods
MUSIC_MOODS: list[str] = ["cozy", "energetic",
                          "sleepy", "creative", "nostalgic"]

# Global defaults (fallback when mood doesn't define something)
_GLOBAL_DEFAULTS: dict[str, dict[str, Any]] = {
    "reminders": {
        "interrupt": "",
        "flavor": [" ✨", " ~", ""],
    },
    "help": {
        "greeting": ["Hey!", "What's up?", "Hi~"],
        "intro": ["I'm here", "I'm around"],
        "availability": ["What can I help with?", "Need something?", "How can I help?"],
    },
    "fun": {
        "sass_level": 0.2,
    },
}

# ════════════════════════════════════════════════════════════════════════════════
# RUNTIME STATE
# ════════════════════════════════════════════════════════════════════════════════


@dataclass
class MusicState:
    """Current music playback state (separate from activity)."""
    is_playing: bool = False
    current_mood: Optional[str] = None  # Music mood (cozy, energetic, etc.)
    current_playlist: Optional[str] = None
    playlist_description: Optional[str] = None
    last_mood_change: float = 0.0


_current_mood: Optional[str] = None
_current_activity: Optional[Activity] = None
_music_state: MusicState = MusicState()
_playlist_callbacks: list[Callable[[Optional[str], Optional[str]], None]] = []


def initialize() -> None:
    """Initialize ambience state on bot startup. Call this in on_ready."""
    global _current_mood, _current_activity

    # Fresh start - pick a random mood and activity
    _current_mood = random.choice(list(MOODS.keys()))
    mood = MOODS[_current_mood]
    _current_activity = random.choice(list(mood.activities.values()))


def get_current_mood() -> Optional[Mood]:
    """Get the current Mood object."""
    if _current_mood is None:
        return None
    return MOODS.get(_current_mood)


def get_current_mood_id() -> Optional[str]:
    """Get the current mood ID string."""
    return _current_mood


def get_current_activity() -> Optional[Activity]:
    """Get the current Activity object."""
    return _current_activity


def get_current_activity_id() -> Optional[str]:
    """Get the current activity ID string."""
    return _current_activity.id if _current_activity else None


# ════════════════════════════════════════════════════════════════════════════════
# STATE CHANGES
# ════════════════════════════════════════════════════════════════════════════════

def set_mood(mood_id: str) -> bool:
    """Change the current mood.

    Args:
        mood_id: Mood to switch to.

    Returns:
        True if successful, False if mood doesn't exist.
    """
    global _current_mood, _current_activity

    if mood_id not in MOODS:
        return False

    _current_mood = mood_id
    mood = MOODS[mood_id]
    _current_activity = random.choice(list(mood.activities.values()))
    return True


def set_activity(activity_id: str) -> bool:
    """Change the current activity within the current mood.

    Args:
        activity_id: Activity to switch to.

    Returns:
        True if successful, False if activity doesn't exist in current mood.
    """
    global _current_activity

    # Special case: daydreaming can happen in any mood
    if activity_id == "daydreaming":
        _current_activity = _DAYDREAMING
        return True

    if _current_mood is None:
        return False

    mood = MOODS[_current_mood]
    if activity_id not in mood.activities:
        return False

    _current_activity = mood.activities[activity_id]
    return True


def cycle_activity() -> Optional[Activity]:
    """Cycle to a random different activity in the current mood.

    Returns:
        The new activity, or None if failed.
    """
    global _current_activity

    if _current_mood is None:
        return None

    mood = MOODS[_current_mood]
    activities = list(mood.activities.values())

    # Small chance to daydream instead
    if random.random() < 0.05:
        _current_activity = _DAYDREAMING
        return _current_activity

    # Try to pick something different
    if len(activities) > 1 and _current_activity:
        activities = [a for a in activities if a.id != _current_activity.id]

    _current_activity = random.choice(activities)
    return _current_activity


def maybe_cycle() -> bool:
    """Maybe cycle mood/activity based on time and randomness.

    Should be called periodically (e.g., by music cog's presence loop).

    Returns:
        True if something changed.
    """
    global _current_mood, _current_activity, _music_state

    cycle_minutes = get_config("cycle_minutes", 60)
    cycle_seconds = float(cycle_minutes) * 60

    time_since_change = time.time() - _music_state.last_mood_change
    if time_since_change < cycle_seconds:
        return False

    # Random chance to even consider changing
    if random.random() > 0.5:
        _music_state.last_mood_change = time.time()
        return False

    # Decide what to change
    roll = random.random()
    music_weight = get_config("music_weight", 0.4)

    if roll < 0.2:
        # 20% chance: Change mood entirely
        old_mood = _current_mood
        _current_mood = random.choice(list(MOODS.keys()))
        if _current_mood != old_mood:
            mood = MOODS[_current_mood]
            _current_activity = random.choice(list(mood.activities.values()))
            _music_state.last_mood_change = time.time()
            return True

    elif roll < 0.2 + music_weight * 0.5:
        # Chance: Switch to music activity
        if _current_mood:
            mood = MOODS[_current_mood]
            if "listening_music" in mood.activities:
                _current_activity = mood.activities["listening_music"]
                _music_state.last_mood_change = time.time()
                return True

    else:
        # Otherwise: Just cycle activity within mood
        old_activity = _current_activity
        cycle_activity()
        if _current_activity != old_activity:
            _music_state.last_mood_change = time.time()
            return True

    return False


# ════════════════════════════════════════════════════════════════════════════════
# STATUS & PRESENCE
# ════════════════════════════════════════════════════════════════════════════════

def get_status() -> str:
    """Get the current Discord status text.

    Returns:
        Status string based on current activity.
    """
    if _current_activity is None:
        return "chilling~"
    return _current_activity.status


def get_context_value() -> Optional[str]:
    """Get dynamic context for current activity from TOML interests.

    E.g., if activity is reading_book with context_key=('reading', 'books'),
    returns a random book title.

    Returns:
        Context string or None.
    """
    if _current_activity is None or _current_activity.context_key is None:
        return None

    category, key = _current_activity.context_key
    return get_interest(category, key)


def allows_background_music() -> bool:
    """Check if current activity allows background music.

    Returns:
        True if music can play alongside current activity.
    """
    if _current_activity is None:
        return True
    return _current_activity.background_music or _current_activity.is_music


def is_music_activity() -> bool:
    """Check if current activity IS music (not background).

    Returns:
        True if music is the main activity.
    """
    if _current_activity is None:
        return False
    return _current_activity.is_music


# ════════════════════════════════════════════════════════════════════════════════
# COG QUERY INTERFACE - The cascade!
# ════════════════════════════════════════════════════════════════════════════════

def get_cog_value(cog_name: str, key: str, default: Any = None) -> Any:
    """Get a cog configuration value with cascade.

    Checks in order:
    1. Current activity's cog override
    2. Current mood's default_cog
    3. Global defaults
    4. Provided default

    Args:
        cog_name: Name of the cog (e.g., 'reminders', 'help')
        key: Configuration key (e.g., 'interrupt', 'greeting')
        default: Fallback if nothing found

    Returns:
        The configuration value.
    """
    # 1. Activity level
    if _current_activity:
        if cog_name in _current_activity.cog:
            if key in _current_activity.cog[cog_name]:
                return _current_activity.cog[cog_name][key]

    # 2. Mood level
    mood = get_current_mood()
    if mood:
        if cog_name in mood.default_cog:
            if key in mood.default_cog[cog_name]:
                return mood.default_cog[cog_name][key]

    # 3. Global defaults
    if cog_name in _GLOBAL_DEFAULTS:
        if key in _GLOBAL_DEFAULTS[cog_name]:
            return _GLOBAL_DEFAULTS[cog_name][key]

    return default


def get_cog_list(cog_name: str, key: str) -> list[str]:
    """Get a cog list value, returns random item if list.

    Convenience wrapper for lists like 'greeting' or 'flavor'.
    """
    value = get_cog_value(cog_name, key, [])
    if isinstance(value, list):
        return value
    return [value] if value else []


def get_cog_random(cog_name: str, key: str, default: str = "") -> str:
    """Get a random item from a cog list value.

    Args:
        cog_name: Name of the cog
        key: Configuration key
        default: Fallback if nothing found

    Returns:
        Random string from the list.
    """
    items = get_cog_list(cog_name, key)
    return random.choice(items) if items else default


# ════════════════════════════════════════════════════════════════════════════════
# MUSIC STATE MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════

def subscribe_playlist_change(callback: Callable[[Optional[str], Optional[str]], None]) -> None:
    """Subscribe to playlist changes.

    Args:
        callback: Function(playlist_url, description) called on changes.
    """
    if callback not in _playlist_callbacks:
        _playlist_callbacks.append(callback)


def unsubscribe_playlist_change(callback: Callable[[Optional[str], Optional[str]], None]) -> None:
    """Unsubscribe from playlist changes."""
    if callback in _playlist_callbacks:
        _playlist_callbacks.remove(callback)


def _notify_playlist_change(playlist_url: Optional[str], description: Optional[str]) -> None:
    """Notify all subscribers of a playlist change."""
    for callback in _playlist_callbacks:
        try:
            callback(playlist_url, description)
        except Exception:
            pass


def get_music_state() -> MusicState:
    """Get current music state."""
    return _music_state


def is_music_playing() -> bool:
    """Check if music is currently playing."""
    return _music_state.is_playing


def get_current_playlist() -> Optional[str]:
    """Get current playlist URL if playing."""
    return _music_state.current_playlist if _music_state.is_playing else None


def get_current_music_mood() -> Optional[str]:
    """Get current music mood if playing."""
    return _music_state.current_mood if _music_state.is_playing else None


def get_available_music_moods() -> list[str]:
    """Get list of music moods that have playlists configured."""
    return [mood for mood in MUSIC_MOODS if get_playlist(mood)]


def start_music(music_mood: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Start playing music.

    Args:
        music_mood: Optional specific mood. If None, picks randomly.

    Returns:
        Tuple of (playlist_url, description), or (None, None) if failed.
    """
    global _music_state

    available = get_available_music_moods()
    if not available:
        return None, None

    # Pick mood
    if music_mood and music_mood in available:
        selected = music_mood
    else:
        selected = random.choice(available)

    playlist = get_playlist(selected)
    if not playlist:
        return None, None

    description = get_playlist_description(selected)

    _music_state.is_playing = True
    _music_state.current_mood = selected
    _music_state.current_playlist = playlist
    _music_state.playlist_description = description
    _music_state.last_mood_change = time.time()

    _notify_playlist_change(playlist, description)
    return playlist, description


def stop_music() -> None:
    """Stop playing music."""
    global _music_state

    was_playing = _music_state.is_playing

    _music_state.is_playing = False
    _music_state.current_mood = None
    _music_state.current_playlist = None
    _music_state.playlist_description = None

    if was_playing:
        _notify_playlist_change(None, None)


def switch_music_mood(music_mood: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Switch to a different music mood.

    Args:
        music_mood: Optional specific mood. If None, picks different random.

    Returns:
        Tuple of (playlist_url, description).
    """
    global _music_state

    available = get_available_music_moods()
    if not available:
        return None, None

    # Pick different mood
    if music_mood and music_mood in available:
        selected = music_mood
    elif _music_state.current_mood and len(available) > 1:
        others = [m for m in available if m != _music_state.current_mood]
        selected = random.choice(others)
    else:
        selected = random.choice(available)

    playlist = get_playlist(selected)
    if not playlist:
        return None, None

    description = get_playlist_description(selected)

    _music_state.current_mood = selected
    _music_state.current_playlist = playlist
    _music_state.playlist_description = description
    _music_state.is_playing = True
    _music_state.last_mood_change = time.time()

    _notify_playlist_change(playlist, description)
    return playlist, description


def ensure_music_for_user() -> tuple[Optional[str], Optional[str]]:
    """Ensure music is playing (for listen-along requests).

    Returns:
        Tuple of (playlist_url, description).
    """
    if _music_state.is_playing and _music_state.current_playlist:
        return _music_state.current_playlist, _music_state.playlist_description
    return start_music()


# ════════════════════════════════════════════════════════════════════════════════
# COG-SPECIFIC HELPERS
# ════════════════════════════════════════════════════════════════════════════════
# Each cog can import just what it needs from here.
# These provide nicer APIs than raw get_cog_value() calls.


class RemindersAmbience:
    """Ambience helpers for the Reminders cog."""

    @staticmethod
    def get_interrupt() -> str:
        """Get interrupt text for when user sets a reminder."""
        return get_cog_value("reminders", "interrupt", "")

    @staticmethod
    def get_flavor() -> str:
        """Get flavor text to append to reminder delivery."""
        return get_cog_random("reminders", "flavor", "")


class HelpAmbience:
    """Ambience helpers for the Help cog."""

    @staticmethod
    def get_greeting() -> str:
        """Get a mood-appropriate greeting."""
        return get_cog_random("help", "greeting", "Hey!")

    @staticmethod
    def get_intro() -> str:
        """Get activity-appropriate intro text.

        Returns:
            A string like "I'm busy studying 📚" or "I'm just relaxing".
        """
        return get_cog_random("help", "intro", "I'm here")

    @staticmethod
    def get_availability() -> str:
        """Get a friendly availability comment.

        Returns:
            A string like "but if you need me I'll still be around!".
        """
        return get_cog_random("help", "availability", "What can I help with?")

    @staticmethod
    def get_activity_description() -> str:
        """Get the full activity description for help embed.

        Combines intro and availability into a friendly sentence.

        Returns:
            A string like "I'm busy studying 📚, but if you need me I'll still be around!"
        """
        intro = HelpAmbience.get_intro()
        availability = HelpAmbience.get_availability()
        return f"{intro}, {availability}"


class FunAmbience:
    """Ambience helpers for the Fun cog."""

    @staticmethod
    def get_sass_level() -> float:
        """Get current sass level (0.0 - 1.0)."""
        return get_cog_value("fun", "sass_level", 0.2)


class MusicAmbience:
    """Ambience helpers for the Music cog (special handling!)."""

    @staticmethod
    def should_show_track_presence() -> bool:
        """Should the presence show the current track?

        True if music is playing AND (activity is music OR allows background).
        """
        if not _music_state.is_playing:
            return False
        return allows_background_music()

    @staticmethod
    def get_status_for_presence() -> str:
        """Get what to show in Discord status.

        If is_music activity: returns music status
        Otherwise: returns activity status
        """
        if _current_activity and _current_activity.is_music:
            return _current_activity.status
        return get_status()

    @staticmethod
    def get_listen_along_response() -> str:
        """Get a response for when user asks to listen along.

        If the current activity doesn't allow background music, adds flavor text
        about "putting things down" to play music instead.
        """
        activity = get_current_activity()
        activity_id = get_current_activity_id()

        # If activity allows background music or IS music, simple response
        if activity is None or activity.background_music or activity.is_music:
            options = [
                "Come vibe with me! 🎵",
                "Perfect timing, this playlist is *chef's kiss* 🎧",
                "Ooh yes, let's listen together~ 🎵",
                "Music time? Let's go! 🎵",
            ]
            return random.choice(options)

        # Activity doesn't allow background music - she's "putting it down"
        put_down_responses: dict[str, list[str]] = {
            "napping": [
                "*yawns* Music sounds better than sleep~ 🎵",
                "*stretches* Okay okay, I'm up~ 🎵",
            ],
            "watching_anime": [
                "Pausing this for music? Good choice~ 🎵",
                "*pauses at cliffhanger* Fine, I wanted a break anyway~ 🎵",
            ],
            "watching_movies": [
                "*pauses movie* Music break! 🎵",
                "The movie can wait~ 🎵",
            ],
            "watching_youtube": [
                "*closes 47 tabs* Yeah, music is better anyway~ 🎵",
                "Escaping the algorithm for music! 🎵",
            ],
            "watching_shorts": [
                "*finally puts phone down* Music time! 🎵",
                "Thank you for rescuing me from shorts~ 🎵",
            ],
            "watching_streams": [
                "*leaves lurk mode* Music time! 🎵",
                "They won't miss me~ 🎵",
            ],
            "listening_podcast": [
                "*pauses podcast* Okay, music instead~ 🎵",
                "The podcast will still be there~ 🎵",
            ],
            "playing_rhythm_game": [
                "*misses note* ...you know what, let's just listen instead~ 🎵",
                "Real music > rhythm game music anyway~ 🎵",
            ],
            "video_calling": [
                "*waves at camera* brb, music time! 🎵",
                "One sec friends, important music business~ 🎵",
            ],
            "at_bookstore": [
                "*puts book back on shelf* Okay leaving now~ 🎵",
                "The books will still be here tomorrow~ 🎵",
            ],
        }

        # Check for specific response
        if activity_id in put_down_responses:
            return random.choice(put_down_responses[activity_id])

        # Generic fallback for any other non-background activity
        generic_responses = [
            f"*puts down the {activity.status.replace(' ', ', ').split(',')[0].lower()}* Music time! 🎵",
            "Let me switch gears for music~ 🎵",
            "Okay, dropping what I'm doing for this~ 🎵",
        ]
        return random.choice(generic_responses)

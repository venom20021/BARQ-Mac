"""
Dynamic greeting engine for BARQ.

Generates time-aware, context-aware spoken greetings with maximum
variety — time-of-day, weekend, seasonal, and holiday greetings.

Priority order when choosing:
  1. Holiday greeting (specific calendar dates)
  2. Weekend greeting (Saturday/Sunday with day name)
  3. Seasonal greeting (spring/summer/fall/winter)
  4. Time-of-day greeting (morning/afternoon/evening/night)

User's name is optionally included. Follow-up question is randomly
selected from a large pool.
"""

import random
from datetime import date, datetime
from typing import Any, Optional


# ── Date helpers ─────────────────────────────────────────────────────

def _today() -> date:
    return date.today()


def _get_day_name() -> str:
    """Return the current day name (e.g. 'Monday', 'Saturday')."""
    return _today().strftime("%A")


def _is_weekend() -> bool:
    """Check if today is Saturday or Sunday."""
    return _today().weekday() >= 5  # 5=Saturday, 6=Sunday


def _get_season() -> str:
    """Determine the meteorological season in the Northern Hemisphere.

    Returns:
        "spring" | "summer" | "fall" | "winter"
    """
    month = _today().month
    if 3 <= month <= 5:
        return "spring"
    elif 6 <= month <= 8:
        return "summer"
    elif 9 <= month <= 11:
        return "fall"
    else:
        return "winter"


def _get_holiday() -> Optional[str]:
    """Check if today is a well-known holiday.

    Returns:
        Holiday greeting key (e.g. "christmas", "new_year") or None.
    """
    today = _today()
    m, d = today.month, today.day

    # Fixed-date holidays
    holidays = {
        (1,  1):  "new_year",
        (2, 14):  "valentines",
        (3, 17):  "st_patricks",
        (4,  1):  "april_fools",
        (5,  4):  "star_wars",
        (7,  4):  "independence_us",
        (10, 31): "halloween",
        (12, 24): "christmas_eve",
        (12, 25): "christmas",
        (12, 26): "boxing_day",
        (12, 31): "new_year_eve",
    }
    key = holidays.get((m, d))
    if key:
        return key

    # Diwali: approximate window (late Oct to mid-Nov)
    if (m == 10 and d >= 20) or (m == 11 and d <= 15):
        return "diwali"

    return None


# ── Time-of-day greeting pools ───────────────────────────────────────
# 8–10 variants each for real variety; duplicates help weight common ones

_MORNING_GREETINGS = [
    "Good morning",
    "Good morning",
    "Morning",
    "Rise and shine",
    "Top of the morning",
    "Beautiful morning",
    "Bright and early",
    "Another beautiful day",
    "Good morning sunshine",
    "Lovely morning",
]

_AFTERNOON_GREETINGS = [
    "Good afternoon",
    "Good afternoon",
    "Afternoon",
    "Happy afternoon",
    "Lovely afternoon",
    "Beautiful afternoon",
    "Good afternoon sunshine",
    "Hope you're having a good day",
]

_EVENING_GREETINGS = [
    "Good evening",
    "Good evening",
    "Evening",
    "Happy evening",
    "Lovely evening",
    "Beautiful evening",
    "Good evening",
]

_NIGHT_GREETINGS = [
    "Working late",
    "Burning the midnight oil",
    "Late night",
    "Hello night owl",
    "Hello",
    "Hey there",
    "Still going strong",
    "Night owl alert",
]

# ── Weekend greeting pools ───────────────────────────────────────────
# These are only used when _is_weekend() is True

_SATURDAY_GREETINGS = [
    "Happy Saturday",
    "Happy Saturday",
    "Lovely Saturday",
    "Beautiful Saturday",
    "Weekend vibes",
    "Saturday morning",
    "Happy weekend",
]

_SUNDAY_GREETINGS = [
    "Happy Sunday",
    "Happy Sunday",
    "Lazy Sunday",
    "Lovely Sunday",
    "Peaceful Sunday",
    "Sunday morning",
    "Happy weekend",
]

# ── Seasonal greeting pools ──────────────────────────────────────────
# These are mixed in as occasional variants during each season

_SPRING_GREETINGS = [
    "Happy spring",
    "Beautiful spring day",
    "Spring is in the air",
    "Lovely spring morning",
    "Fresh spring morning",
    "Spring has sprung",
    "Beautiful spring",
    "Lovely spring day",
]

_SUMMER_GREETINGS = [
    "Hot summer day",
    "Beautiful summer day",
    "Summer is here",
    "Lovely summer morning",
    "Warm summer day",
    "Bright summer day",
    "Perfect summer weather",
    "Beautiful summer",
]

_FALL_GREETINGS = [
    "Happy fall",
    "Crisp autumn morning",
    "Beautiful fall day",
    "Lovely autumn evening",
    "Cozy fall morning",
    "Golden autumn day",
    "Beautiful autumn",
    "Lovely fall day",
]

_WINTER_GREETINGS = [
    "Cozy winter day",
    "Cold winter morning",
    "Winter is here",
    "Beautiful winter day",
    "Snowy winter morning",
    "Warm winter inside",
    "Crisp winter air",
    "Beautiful winter",
]

# ── Holiday greeting pools ───────────────────────────────────────────

_HOLIDAY_GREETINGS: dict[str, list[str]] = {
    "new_year": [
        "Happy New Year",
        "Happy New Year",
        "Welcome to the new year",
    ],
    "valentines": [
        "Happy Valentine's Day",
        "Happy Valentine's",
    ],
    "st_patricks": [
        "Happy St Patrick's Day",
        "Top o' the morning",
    ],
    "april_fools": [
        "April Fools",
        "Happy April Fools Day",
    ],
    "star_wars": [
        "May the Fourth be with you",
        "Happy Star Wars Day",
    ],
    "independence_us": [
        "Happy Fourth of July",
        "Happy Independence Day",
    ],
    "halloween": [
        "Happy Halloween",
        "Boo",
        "Happy Halloween",
    ],
    "christmas_eve": [
        "Merry Christmas Eve",
        "Happy Christmas Eve",
    ],
    "christmas": [
        "Merry Christmas",
        "Merry Christmas",
        "Happy Christmas",
        "Merry Christmas",
    ],
    "boxing_day": [
        "Happy Boxing Day",
        "Happy Boxing Day",
    ],
    "diwali": [
        "Happy Diwali",
        "Happy Diwali",
        "Shubh Diwali",
    ],
    "new_year_eve": [
        "Happy New Year's Eve",
        "Happy New Year's Eve",
    ],
}

# ── Follow-up phrases (appended after greeting + name) ───────────────
# 14 variants for real variety
# Each tuple is (phrase, punctuation): "?" for questions, "." for statements

_FOLLOWUPS: list[tuple[str, str]] = [
    ("How can I help you",       "?"),
    ("How can I help you",       "?"),
    ("What can I do for you",    "?"),
    ("How can I assist you",     "?"),
    ("What's on your mind",      "?"),
    ("How may I help you",       "?"),
    ("What do you need",         "?"),
    ("I'm listening",            "."),  # statement, not a question
    ("Tell me what you need",    "?"),
    ("I'm all ears",             "."),  # statement, not a question
    ("How can I be useful",      "?"),
    ("What's up",                "?"),
    ("What's going on",          "?"),
    ("Ready when you are",       "."),  # statement, not a question
]


# ── Core helpers ─────────────────────────────────────────────────────

def _pick_random(pool: list[Any]) -> Any:
    """Pick a random item from a list."""
    return random.choice(pool)


def get_time_of_day() -> str:
    """Determine time-of-day segment: morning, afternoon, evening, night."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 22:
        return "evening"
    else:
        return "night"


def get_time_greeting() -> str:
    """Return a greeting appropriate to the current time, day, and season.

    Priority:
      1. Holiday greeting (today is a holiday)
      2. Weekend greeting (Saturday or Sunday)
      3. Seasonal greeting with ~30% chance
      4. Time-of-day greeting (default)

    Returns:
        A greeting phrase like "Good morning", "Happy Saturday",
        "Merry Christmas", "Beautiful fall day", etc.
    """
    # 1. Check for holiday
    holiday = _get_holiday()
    if holiday and holiday in _HOLIDAY_GREETINGS:
        return _pick_random(_HOLIDAY_GREETINGS[holiday])

    # 2. Check for weekend
    if _is_weekend():
        day_name = _get_day_name()
        if day_name == "Saturday":
            return _pick_random(_SATURDAY_GREETINGS)
        else:
            return _pick_random(_SUNDAY_GREETINGS)

    # 3. Seasonal greeting — 30% chance for variety
    if random.random() < 0.3:
        season = _get_season()
        season_pools = {
            "spring": _SPRING_GREETINGS,
            "summer": _SUMMER_GREETINGS,
            "fall":   _FALL_GREETINGS,
            "winter": _WINTER_GREETINGS,
        }
        return _pick_random(season_pools[season])

    # 4. Default: time-of-day
    pools = {
        "morning":   _MORNING_GREETINGS,
        "afternoon": _AFTERNOON_GREETINGS,
        "evening":   _EVENING_GREETINGS,
        "night":     _NIGHT_GREETINGS,
    }
    return _pick_random(pools[get_time_of_day()])


def get_followup() -> tuple[str, str]:
    """Return a random follow-up phrase with its punctuation.

    Returns:
        A tuple of (phrase, punctuation), e.g. ("How can I help you", "?")
        or ("I'm listening", ".").
    """
    return _pick_random(_FOLLOWUPS)


def build_greeting(
    user_name: Optional[str] = None,
    assistant_name: Optional[str] = None,
    include_followup: bool = True,
    time_aware: bool = True,
    context_phrase: Optional[str] = None,
) -> str:
    """Build a complete dynamic spoken greeting.

    Mimics the Mark-L approach where the greeting is assembled from
    time/day/season context, user name, optional context (weather/news),
    and follow-up components.

    When ``context_phrase`` is provided, it's inserted between the
    name greeting and the follow-up, like:
        "Good morning, Sai. Looks like rain in Lucknow. How can I help you?"

    Args:
        user_name: The user's preferred name (e.g., "Sai").
        assistant_name: The assistant's name (e.g., "BARQ").
        include_followup: Whether to append "How can I help you?" etc.
        time_aware: Whether to use contextual greetings (holiday,
                    weekend, seasonal, time-of-day).
        context_phrase: Optional weather/news context to insert between
                        the name greeting and the follow-up phrase.
                        E.g. "Looks like rain in Lucknow".

    Returns:
        A complete greeting string for TTS, e.g.:
        - "Good morning, Sai. How can I help you?"
        - "Happy Saturday, Sai. What's on your mind?"
        - "Merry Christmas, Sai. How can I assist you?"
        - "Beautiful fall day, Sai. I'm all ears."
        - "Good morning, Sai. Looks like rain in Lucknow. How can I help you?"
    """
    if time_aware:
        greeting = get_time_greeting()
    else:
        greeting = _pick_random([
            "Hello", "Hey there", "Hi", "Welcome back",
        ])

    followup_text = ""
    followup_punct = "?"  # default
    if include_followup:
        followup_text, followup_punct = get_followup()

    # Insert context phrase between greeting+name and followup
    context_part = f" {context_phrase}." if context_phrase else ""

    # Build with proper punctuation
    if user_name:
        name = user_name.strip(".!,?")
        if followup_text:
            result = f"{greeting}, {name}.{context_part} {followup_text}{followup_punct}"
        else:
            result = f"{greeting}, {name}.{context_part}"
    else:
        if followup_text:
            result = f"{greeting},{context_part} {followup_text}{followup_punct}"
        else:
            result = f"{greeting}.{context_part}"

    return result


def build_wake_greeting(
    user_name: Optional[str] = None,
    assistant_name: Optional[str] = None,
    context_phrase: Optional[str] = None,
) -> str:
    """Short greeting for wake-word activation.

    This is the greeting spoken right after the wake chime, before
    the user says their request.

    Examples:
        "Good morning, Sai. How can I help you?"
        "Happy Saturday. What can I do for you?"
        "Merry Christmas, Sai. How can I assist you?"
        "Good morning, Sai. Looks like rain in Lucknow. How can I help you?"

    Args:
        user_name: The user's preferred name.
        assistant_name: The assistant's name (unused, reserved).
        context_phrase: Optional weather/news context to insert between
                        the name greeting and the follow-up.

    Returns:
        A concise greeting string for TTS.
    """
    return build_greeting(
        user_name=user_name,
        include_followup=True,
        time_aware=True,
        context_phrase=context_phrase,
    )


# ── Self-test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Dynamic Greeting Demo ===\n")
    print(f"Date:      {_today()}")
    print(f"Day:       {_get_day_name()}")
    print(f"Weekend:   {_is_weekend()}")
    print(f"Season:    {_get_season()}")
    print(f"Time:      {get_time_of_day()}")
    holiday = _get_holiday()
    print(f"Holiday:   {holiday or 'None'}")
    print()

    print("--- With name (Sai) ---")
    for _ in range(10):
        print(f'  "{build_wake_greeting(user_name="Sai")}"')

    print()
    print("--- Without name ---")
    for _ in range(8):
        print(f'  "{build_wake_greeting()}"')

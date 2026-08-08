"""
Fast context fetcher for the spoken TTS greeting.

Fetches current weather and top news headline in parallel, then formats
a short context phrase that can be inserted into the spoken greeting.

Examples:
  "Looks like rain in Lucknow today — umbrella needed."
  "It's 35°C in London — stay hydrated."
  "It's snowing in Toronto — drive safe."
  "Bitcoin hit $68K overnight."  (if news API key is configured)

The fetch is fast (< 1 second) and non-blocking — if anything fails or
times out, an empty string is returned and the greeting proceeds normally.
"""

import asyncio
from typing import Any, Optional


async def _fetch_weather(city: str) -> str:
    """Fetch current weather for a city using Open-Meteo (free, no API key).

    Returns:
        A weather context phrase like "Looks like rain in {city}"
        or "It's {temp}°C in {city}"
        or empty string if fetch fails or nothing noteworthy.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            # 1. Geocode
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            )
            if geo_resp.status_code != 200:
                return ""
            geo_data = geo_resp.json()
            results = geo_data.get("results", [])
            if not results:
                return ""

            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            city_name = results[0].get("name", city)

            # 2. Fetch current weather
            weather_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code,precipitation",
                    "timezone": "auto",
                },
            )
            if weather_resp.status_code != 200:
                return ""

            w = weather_resp.json()
            current = w.get("current", {})
            temp = current.get("temperature_2m")
            weather_code = current.get("weather_code", 0)

            if temp is None:
                return ""

            # WMO weather codes (same as web_media/routes.py)
            # Code 0-3: clear/cloudy, 40-49: fog, 50-69: drizzle/rain,
            # 70-79: snow, 80-99: showers/thunderstorms
            if 50 <= weather_code <= 69:
                return f"Looks like rain in {city_name}"
            elif 70 <= weather_code <= 79:
                return f"It's snowing in {city_name}"
            elif 80 <= weather_code <= 99:
                return f"Thunderstorms in {city_name}"
            elif temp >= 38:
                return f"It's {temp:.0f}°C in {city_name} — stay hydrated"
            elif temp <= 0:
                return f"It's {temp:.0f}°C in {city_name} — bundle up"
            elif temp >= 30:
                return f"It's {temp:.0f}°C in {city_name} — warm out there"

            return ""  # Normal weather, nothing noteworthy

    except (httpx.TimeoutException, httpx.ConnectError):
        return ""  # Non-blocking: just skip on timeout
    except Exception:
        return ""


async def _fetch_headline() -> str:
    """Fetch a single top news headline using NewsAPI (if key is configured).

    Returns:
        A brief headline phrase like "Bitcoin hits $68K"
        or empty string if no key configured or fetch fails.
    """
    try:
        import os
        import httpx

        api_key = os.getenv("NEWS_API_KEY", "")
        if not api_key:
            return ""

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "language": "en",
                    "pageSize": 1,
                    "apiKey": api_key,
                },
            )
            if resp.status_code != 200:
                return ""

            articles = resp.json().get("articles", [])
            if not articles:
                return ""

            title = articles[0].get("title", "")
            if not title:
                return ""

            # Clean up trailing punctuation and truncate to 60 chars
            title = title.rstrip(".!?:; ")
            if len(title) > 60:
                title = title[:57] + "..."

            return title

    except (httpx.TimeoutException, httpx.ConnectError):
        return ""
    except Exception:
        return ""


async def fetch_greeting_context(
    city: Optional[str] = None,
    include_news: bool = True,
) -> str:
    """Fetch weather + news context for the spoken TTS greeting.

    Both fetches run in parallel and are non-blocking (timeout-safe).
    If both succeed, they're combined into a single sentence.
    If one fails, the other is still used.

    Args:
        city: City name for weather lookup. Defaults to "my city".
        include_news: Whether to try fetching a news headline.

    Returns:
        A short context phrase like:
        - "Looks like rain in Lucknow" (weather only)
        - "Bitcoin hits $68K" (news only)
        - "Looks like rain in London. Also, Bitcoin hits $68K." (both)
        - "" (nothing noteworthy, fall back to normal greeting)
    """
    tasks: list[Any] = []

    if city:
        tasks.append(_fetch_weather(city))
    else:
        tasks.append(_fetch_weather("my city"))

    if include_news:
        tasks.append(_fetch_headline())
    else:
        tasks.append(asyncio.sleep(0))  # dummy — returns None

    # Run all fetches in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    weather_phrase = results[0] if isinstance(results[0], str) else ""
    headline = results[1] if len(results) > 1 and isinstance(results[1], str) else ""

    # Build combined context
    parts: list[str] = []
    if weather_phrase:
        parts.append(weather_phrase)
    if headline:
        parts.append(headline)

    if not parts:
        return ""

    if len(parts) == 1:
        return parts[0]

    return f"{parts[0]}. Also, {parts[1]}."

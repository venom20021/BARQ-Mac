"""Regression tests for the greeting-context cache.

The wake greeting used to await fetch_greeting_context() inline, before
speak_text() and before start_conversation() — so every wake paid ~1.65s of
sequential open-meteo round trips before the audio pipeline came up (measured).

The fetch is now (a) started before agent.connect() so it overlaps the
websocket handshake, and (b) cached, so repeat wakes are free. These tests pin
the cache contract without touching the network.

Deliberately dependency-free (no pytest import; sync wrappers around
asyncio.run) so it runs in the pytest-less macOS venv directly and is still
collectable by pytest in CI.
"""

import asyncio

import voice.greeting_context as gc


def _run(coro):
    return asyncio.run(coro)


def _patch_fetch(calls: list):
    """Replace the network fetch with a counter, returning a phrase."""

    async def _fake(city, include_news):
        calls.append(city)
        return f"context for {city}"

    gc._fetch_greeting_context_uncached = _fake


def test_repeat_call_is_served_from_cache():
    calls: list = []
    _patch_fetch(calls)
    gc.clear_greeting_cache()

    async def scenario():
        first = await gc.fetch_greeting_context(city="London", include_news=True)
        second = await gc.fetch_greeting_context(city="London", include_news=True)
        return first, second

    first, second = _run(scenario())
    assert first == second == "context for London"
    assert len(calls) == 1, "second call must not hit the network"


def test_use_cache_false_bypasses():
    calls: list = []
    _patch_fetch(calls)
    gc.clear_greeting_cache()

    async def scenario():
        await gc.fetch_greeting_context(city="London")
        await gc.fetch_greeting_context(city="London", use_cache=False)

    _run(scenario())
    assert len(calls) == 2


def test_different_cities_do_not_collide():
    calls: list = []
    _patch_fetch(calls)
    gc.clear_greeting_cache()

    async def scenario():
        await gc.fetch_greeting_context(city="London")
        await gc.fetch_greeting_context(city="Lucknow")

    _run(scenario())
    assert calls == ["London", "Lucknow"]


def test_empty_result_is_cached_so_a_bad_city_cannot_stall_every_wake():
    calls: list = []

    async def _empty(city, include_news):
        calls.append(city)
        return ""

    gc._fetch_greeting_context_uncached = _empty
    gc.clear_greeting_cache()

    async def scenario():
        for _ in range(3):
            await gc.fetch_greeting_context(city="Nowhere")

    _run(scenario())
    assert len(calls) == 1, "a failing/empty lookup must be cached too"


def test_ttl_expiry_refetches():
    calls: list = []
    _patch_fetch(calls)
    gc.clear_greeting_cache()

    async def scenario():
        original_ttl = gc._CACHE_TTL_S
        try:
            gc._CACHE_TTL_S = 0.05
            await gc.fetch_greeting_context(city="London")
            await asyncio.sleep(0.1)
            await gc.fetch_greeting_context(city="London")
        finally:
            gc._CACHE_TTL_S = original_ttl

    _run(scenario())
    assert len(calls) == 2, "an expired entry must be refetched"


def test_clear_cache_forces_refetch():
    calls: list = []
    _patch_fetch(calls)
    gc.clear_greeting_cache()

    async def scenario():
        await gc.fetch_greeting_context(city="London")
        gc.clear_greeting_cache()
        await gc.fetch_greeting_context(city="London")

    _run(scenario())
    assert len(calls) == 2


if __name__ == "__main__":
    # Runnable without pytest (this venv has none): execute every test above.
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _t in _tests:
        _t()
        print(f"PASS  {_t.__name__}")
    print(f"\n{len(_tests)} tests passed")

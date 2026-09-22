"""Regression tests for the voice mute path (the "mute doesn't mute" bug).

The dashboard mute button POSTs /voice/stop, which calls
ConversationListener.stop_conversation().  The conversation loop runs on the
managed voice loop while the request runs on the main/uvicorn loop, so the
listener's ``_loop_task`` belongs to a DIFFERENT loop than the caller.

``await self._loop_task`` across loops raises RuntimeError.  That used to:

  * abort stop_conversation() before end_session / ws cancel / on_stop ran, and
  * propagate out of routes.stop_listening(), skipping the wake-detector stop,

and because the agent's microphone stream is only released by
``agent.stop()`` inside the loop task's ``finally``, an orphaned task meant the
OS microphone stayed hot indefinitely.

These tests pin the invariant: stopping a conversation must always release the
voice agent (and therefore the microphone), regardless of which loop owns the
loop task.

Deliberately dependency-free (no pytest import; sync wrappers around
asyncio.run) because the macOS venv ships no pytest — this file must be
runnable directly *and* collectable by pytest in CI.
"""

import asyncio
import threading

from voice.conversation_listener import ConversationListener


# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubConversation:
    def __init__(self):
        self.is_active = False
        self.turn_count = 0
        self.ended = 0

    def get_recent_history(self, n):
        return []

    def end_session(self):
        self.ended += 1


class _StubResponder:
    def __init__(self):
        self.conversation = _StubConversation()


class _StubAgent:
    """Stands in for a Voice Agent; stop() is the mic-release point."""

    def __init__(self):
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1


class _StubSTT:
    pass


async def _forever():
    while True:
        await asyncio.sleep(0.05)


async def _spawn(coro):
    """Create a Task on whichever loop is running — used on the foreign loop,
    because asyncio.ensure_future() with no running loop raises."""
    return asyncio.ensure_future(coro)


def _make_listener():
    return ConversationListener(stt=_StubSTT(), responder=_StubResponder(), on_stop=None)


class _ForeignLoop:
    """A second running event loop on a background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def spawn_task(self):
        """Create a real asyncio.Task owned by this loop."""
        fut = asyncio.run_coroutine_threadsafe(_spawn(_forever()), self.loop)
        return fut.result(timeout=5)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_stop_releases_agent_when_loop_task_owns_another_loop():
    """The reported bug: cross-loop task must not abort the mute."""
    foreign = _ForeignLoop()
    try:
        listener = _make_listener()
        agent = _StubAgent()

        listener._loop_task = foreign.spawn_task()
        listener._managed_loop = foreign.loop
        listener._agent = agent
        listener._conversation_active = True

        # Runs on THIS loop while _loop_task belongs to the foreign one.
        asyncio.run(listener.stop_conversation())

        assert agent.stop_calls == 1, "agent.stop() must be awaited — it releases the mic"
        assert listener._agent is None, "stale agent reference must be cleared"
        assert listener._loop_task is None, "loop task reference must be cleared"
        assert listener.responder.conversation.ended == 1, "session must be ended"
        assert listener.is_active is False
    finally:
        foreign.close()


def test_stop_awaits_loop_task_when_it_owns_the_running_loop():
    """Same-loop task is awaited and its cancellation absorbed."""

    async def scenario():
        listener = _make_listener()
        agent = _StubAgent()
        listener._loop_task = asyncio.ensure_future(_forever())
        listener._agent = agent
        listener._conversation_active = True
        await listener.stop_conversation()
        assert agent.stop_calls == 1
        assert listener._loop_task is None

    asyncio.run(scenario())


def test_stop_without_loop_task_or_agent_is_safe():
    """Idempotent: nothing to stop must not raise (double-mute from the UI)."""

    async def scenario():
        listener = _make_listener()
        listener._conversation_active = False
        await listener.stop_conversation()
        await listener.stop_conversation()

    asyncio.run(scenario())


def test_agent_stop_failure_does_not_break_the_rest_of_the_mute():
    """A failing agent.stop() must not skip end_session / ws teardown."""

    class _BrokenAgent(_StubAgent):
        async def stop(self):
            self.stop_calls += 1
            raise RuntimeError("audio backend already gone")

    async def scenario():
        listener = _make_listener()
        listener._agent = _BrokenAgent()
        listener._conversation_active = True
        await listener.stop_conversation()
        assert listener.responder.conversation.ended == 1

    asyncio.run(scenario())


if __name__ == "__main__":
    # Runnable without pytest (this venv has none): execute every test above.
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _t in _tests:
        _t()
        print(f"PASS  {_t.__name__}")
    print(f"\n{len(_tests)} tests passed")

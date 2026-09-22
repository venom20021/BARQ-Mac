"""
Tests for voice -> agent_chat_history persistence (voice/agent_history_sync.py).

Verifies that spoken commands are written into the ``agent_chat_history``
setting under the ``voice_commands`` key with the exact shape the brain
re-import reads (role='user', content, timestamp), with dedupe, other-key
preservation, and a merge-safe remote mirror.

Also proves the cross-loop marshaling fix (voice/loop_utils.py): a persist
call made from a foreign event loop is executed on the captured main loop
(``set_main_loop``), and the write actually lands.
"""

import asyncio
import contextlib
import json
import threading

import pytest

from database import settings_dao
from voice.agent_history_sync import (
    VOICE_COMMANDS_KEY,
    _DEFAULT_REMOTE_URL,
    _mirror_voice_commands,
    _remote_url,
    persist_voice_utterance,
    schedule_persist_voice_utterance,
)
from voice.loop_utils import set_main_loop


@pytest.fixture(autouse=True)
def _no_mirror(monkeypatch):
    """Prevent the fire-and-forget mirror from hitting the network in tests."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("voice.agent_history_sync._mirror_voice_commands", _noop)


async def _get_history() -> dict:
    raw = await settings_dao.get_setting("agent_chat_history")
    return json.loads(raw) if raw else {}


@pytest.mark.asyncio
async def test_persist_adds_user_message():
    """A spoken command is stored as a user message with a timestamp."""
    await persist_voice_utterance("open chrome")
    data = await _get_history()
    items = data[VOICE_COMMANDS_KEY]
    assert len(items) == 1
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "open chrome"
    assert isinstance(items[0]["timestamp"], (int, float))


@pytest.mark.asyncio
async def test_persist_appends_distinct_commands():
    """Different commands accumulate in order."""
    await persist_voice_utterance("scan jobs")
    await persist_voice_utterance("what is the weather")
    data = await _get_history()
    items = data[VOICE_COMMANDS_KEY]
    assert [m["content"] for m in items] == ["scan jobs", "what is the weather"]


@pytest.mark.asyncio
async def test_persist_dedupes_immediate_repeat():
    """The same utterance right after itself (agent re-transcription) is skipped."""
    await persist_voice_utterance("open chrome")
    await persist_voice_utterance("open chrome")
    data = await _get_history()
    assert len(data[VOICE_COMMANDS_KEY]) == 1


@pytest.mark.asyncio
async def test_persist_ignores_empty_text():
    """Blank utterances never create the key."""
    await persist_voice_utterance("   ")
    await persist_voice_utterance("")
    raw = await settings_dao.get_setting("agent_chat_history")
    assert raw is None


@pytest.mark.asyncio
async def test_persist_preserves_other_agent_keys():
    """Writing voice commands must not clobber other agents' history keys."""
    await settings_dao.set_setting(
        "agent_chat_history",
        json.dumps({"chat_page": [{"role": "user", "content": "hi", "timestamp": 1}]}),
        category="memory",
    )
    await persist_voice_utterance("scan jobs")
    data = await _get_history()
    assert "chat_page" in data
    assert len(data[VOICE_COMMANDS_KEY]) == 1


@pytest.mark.asyncio
async def test_mirror_merges_only_voice_key_into_remote(monkeypatch):
    """The mirror preserves remote keys and only injects the voice key."""
    remote_history = {"TestAgent": [{"role": "user", "content": "hello"}]}
    local_data = {
        VOICE_COMMANDS_KEY: [{"role": "user", "content": "scan jobs", "timestamp": 123.0}],
        "chat_page": [{"role": "user", "content": "hi", "timestamp": 1}],
    }
    posted = {}

    class FakeResponse:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data

        def json(self):
            return self._json

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, {"history": remote_history})

        async def post(self, url, json=None, **kwargs):
            posted["json"] = json

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())

    await _mirror_voice_commands(local_data)

    merged = posted["json"]["history"]
    # Remote keys preserved
    assert merged["TestAgent"] == remote_history["TestAgent"]
    # Voice key injected from local
    assert merged[VOICE_COMMANDS_KEY] == local_data[VOICE_COMMANDS_KEY]
    # Non-voice local keys are NOT pushed (avoids clobbering remote state)
    assert "chat_page" not in merged


def test_remote_url_env_override(monkeypatch):
    """BARQ_REMOTE_URL env var overrides the default remote URL."""
    monkeypatch.setenv("BARQ_REMOTE_URL", "http://example.test")
    assert _remote_url() == "http://example.test"


def test_remote_url_default():
    """Without env, the app's default remote URL is used.

    Asserts against the module constant rather than a literal so moving the
    backend (Oracle VM -> LAN host) can't silently rot this test.
    """
    import os
    old = os.environ.pop("BARQ_REMOTE_URL", None)
    try:
        assert _remote_url() == _DEFAULT_REMOTE_URL
    finally:
        if old is not None:
            os.environ["BARQ_REMOTE_URL"] = old


# ── Cross-loop marshaling (voice loop -> main loop) ───────────────────────


class _LoopRecorder:
    """Proxy over SettingsDAO that records which loop each call ran on.

    Lets the test prove the DB write executed on the main loop — not merely
    that it landed somewhere.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.called_on = []

    async def get_setting(self, *args, **kwargs):
        self.called_on.append(asyncio.get_running_loop())
        return await self._wrapped.get_setting(*args, **kwargs)

    async def set_setting(self, *args, **kwargs):
        self.called_on.append(asyncio.get_running_loop())
        return await self._wrapped.set_setting(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _start_foreign_loop():
    """Start a foreign event loop in a daemon thread (like the managed voice loop)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_forever, name="test-foreign-loop", daemon=True
    )
    thread.start()
    return loop, thread


def _stop_foreign_loop(loop, thread):
    try:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        if not loop.is_closed():
            loop.close()
    except RuntimeError:
        pass


@contextlib.asynccontextmanager
async def _foreign_loop_harness():
    """Run a test with the pytest loop as 'main' plus a foreign loop thread.

    Mirrors production topology: the backend's main loop — here the pytest
    loop, where the autouse ``setup_db`` fixture created the DB connection —
    is captured with ``set_main_loop``, while the voice pipeline calls persist
    from a SEPARATE managed loop running in another thread (the foreign
    loop).  Yields the foreign loop; restores ``set_main_loop(None)`` on exit.
    """
    main_loop = asyncio.get_running_loop()
    foreign_loop, thread = _start_foreign_loop()
    try:
        set_main_loop(main_loop)
        yield foreign_loop
    finally:
        set_main_loop(None)
        _stop_foreign_loop(foreign_loop, thread)


async def _persist_from_loop(
    text: str, caller_loops: list[asyncio.AbstractEventLoop]
) -> None:
    """Invoke persist_voice_utterance on the loop this coroutine runs on."""
    caller_loops.append(asyncio.get_running_loop())
    await persist_voice_utterance(text)


@pytest.mark.asyncio
async def test_persist_voice_utterance_marshals_onto_main_loop(monkeypatch):
    """A persist call from a foreign loop executes its DB write on the main loop."""
    async with _foreign_loop_harness() as foreign_loop:
        main_loop = asyncio.get_running_loop()  # pytest loop = backend main loop
        assert main_loop is not foreign_loop

        recorder = _LoopRecorder(settings_dao)
        monkeypatch.setattr("voice.agent_history_sync.settings_dao", recorder)

        # Call persist from the foreign loop (the managed voice loop in prod).
        # wrap_future keeps the pytest (main) loop free so it can run the
        # marshaled _persist_impl back onto itself without deadlocking.
        caller_loops: list[asyncio.AbstractEventLoop] = []
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                _persist_from_loop("cross-loop utterance", caller_loops), foreign_loop
            )
        )

        # Sanity: the persist really was invoked from a DIFFERENT loop than
        # the main loop — otherwise the marshaling assertion is vacuous.
        assert caller_loops == [foreign_loop]

        # 1) The DB calls ran on the MAIN loop — not the caller's foreign loop.
        assert recorder.called_on, "persist never reached the database"
        assert all(loop is main_loop for loop in recorder.called_on), (
            f"DB calls ran on foreign loops: {recorder.called_on}"
        )

        # 2) The write landed.
        raw = await settings_dao.get_setting("agent_chat_history")
        data = json.loads(raw) if raw else {}
        items = data[VOICE_COMMANDS_KEY]
        assert [m["content"] for m in items] == ["cross-loop utterance"]


@pytest.mark.asyncio
async def test_schedule_persist_fire_and_forget_lands_on_main_loop(monkeypatch):
    """The fire-and-forget scheduler (production call path) writes via the main loop."""
    async with _foreign_loop_harness() as foreign_loop:
        main_loop = asyncio.get_running_loop()
        assert main_loop is not foreign_loop

        recorder = _LoopRecorder(settings_dao)
        monkeypatch.setattr("voice.agent_history_sync.settings_dao", recorder)

        # Fire-and-forget from the foreign loop — the exact production path
        # (conversation_listener calls schedule_persist_voice_utterance).
        async def _fire():
            schedule_persist_voice_utterance("fire and forget")

        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(_fire(), foreign_loop)
        )

        # Fire-and-forget: poll until the write lands (or fail).
        landed = None
        for _ in range(100):
            await asyncio.sleep(0.05)
            raw = await settings_dao.get_setting("agent_chat_history")
            data = json.loads(raw) if raw else {}
            if data.get(VOICE_COMMANDS_KEY):
                landed = data
                break

        assert landed is not None, "scheduled persist never landed"
        items = landed[VOICE_COMMANDS_KEY]
        assert [m["content"] for m in items] == ["fire and forget"]
        assert all(loop is main_loop for loop in recorder.called_on), (
            f"DB calls ran on foreign loops: {recorder.called_on}"
        )

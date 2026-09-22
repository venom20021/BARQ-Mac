"""Regression tests for voice-cycle latency marks (Phase 0 baseline).

Why these exist: the voice path had NO instrumentation at all. The pre-existing
``ttfb`` event is only recorded on the Ollama text-chat path
(``ai/responder.py``, ``metadata={"model": "ollama"}``), so wake→first-audio —
the latency a user actually feels — was unmeasurable. These tests pin the
contract the ``/voice/latency`` endpoint reports on.

Runnable directly (``python tests/test_voice_latency_marks.py``) or via pytest.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice import evolution_logger as evo  # noqa: E402

# Redirect the singleton's storage to a temp dir so running these tests never
# appends to the real python/memory/evolution/*.json.
evo.get_evolution_logger()._dir = Path(tempfile.mkdtemp(prefix="barq-evo-test-"))


def _clear() -> None:
    logger = evo.get_evolution_logger()
    with logger._events_lock:
        logger._events.clear()


def _events(event_type: str) -> list[dict]:
    return evo.get_evolution_logger().query(event_type=event_type, limit=1000)


# ── Cycle marks ─────────────────────────────────────────────────────────


def test_mark_without_a_cycle_is_ignored():
    """A mark outside any cycle must be a no-op, never an exception.

    The voice path must not break because instrumentation was called early.
    """
    evo._cycle_start = 0.0
    _clear()
    assert evo.mark_voice_cycle("voice_first_audio") is None
    assert _events("voice_first_audio") == []


def test_mark_records_offset_from_cycle_start():
    _clear()
    evo.begin_voice_cycle()
    offset = evo.mark_voice_cycle("voice_connected")
    assert offset is not None and offset >= 0.0

    events = _events("voice_connected")
    assert len(events) == 1
    # The stored duration IS the offset — that is what makes pairwise deltas
    # between marks computable.
    assert events[0]["duration_ms"] == offset


def test_once_records_only_the_first_mark_in_a_cycle():
    """``once=True`` guards marks that fire per audio chunk."""
    _clear()
    evo.begin_voice_cycle()
    assert evo.mark_voice_cycle("voice_first_audio", once=True) is not None
    assert evo.mark_voice_cycle("voice_first_audio", once=True) is None
    assert len(_events("voice_first_audio")) == 1


def test_begin_cycle_resets_the_once_set():
    _clear()
    evo.begin_voice_cycle()
    evo.mark_voice_cycle("voice_first_audio", once=True)
    evo.begin_voice_cycle()
    assert evo.mark_voice_cycle("voice_first_audio", once=True) is not None
    assert len(_events("voice_first_audio")) == 2


def test_cycle_age_grows_and_is_none_without_a_cycle():
    evo._cycle_start = 0.0
    assert evo.voice_cycle_age_ms() is None
    evo.begin_voice_cycle()
    age = evo.voice_cycle_age_ms()
    assert age is not None and age >= 0.0


# ── Percentile reporting ────────────────────────────────────────────────


def test_percentile_edges_and_interpolation():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert evo._percentile(values, 50) == 30.0
    assert evo._percentile(values, 0) == 10.0
    assert evo._percentile(values, 100) == 50.0
    assert evo._percentile([], 50) == 0.0
    assert evo._percentile([7.0], 95) == 7.0


def test_latency_report_reports_tail_not_just_average():
    """p95 must exceed p50 — a single slow wake has to be visible.

    This is the whole reason ``get_latency_report`` exists rather than reusing
    ``get_summary``, which would report 400 ms average and hide the 1000 ms.
    """
    _clear()
    logger = evo.get_evolution_logger()
    for ms in (100.0, 200.0, 300.0, 400.0, 1000.0):
        logger.record("voice_first_audio", ms)

    entry = logger.get_latency_report(["voice_first_audio"])["event_types"]["voice_first_audio"]
    assert entry["count"] == 5
    assert entry["p50_ms"] == 300.0
    assert entry["min_ms"] == 100.0
    assert entry["max_ms"] == 1000.0
    assert entry["p95_ms"] > entry["p50_ms"]


def test_latency_report_filters_event_types():
    _clear()
    logger = evo.get_evolution_logger()
    logger.record("voice_first_audio", 250.0)
    logger.record("unrelated_event", 9999.0)

    report = logger.get_latency_report(["voice_first_audio"])["event_types"]
    assert set(report) == {"voice_first_audio"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

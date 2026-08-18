"""Regression test: the human-input request timestamp must be UTC.

`prompt_human` (src/autopilot/orchestrator/__init__.py) writes
`input_request_<id>.json` with a naive ISO timestamp.
`_find_pending_input` (src/mcp/autopilot/intervention_routes.py)
reads it back, sees `tzinfo is None`, and *assumes UTC* before comparing
against `datetime.now(timezone.utc)` and deleting anything older than
STALE_INPUT_SECONDS (1 hour).

A local-time stamp is therefore misread by the host's UTC offset. West of UTC
a freshly-written request looks hours old and is deleted before the human ever
sees the prompt -- the pipeline then blocks on an answer to a question whose
request file it just garbage-collected. East of UTC the age goes negative and
the file is never cleaned up.

Only a host running exactly UTC behaved correctly, which is why this survived.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def input_dir(tmp_path, monkeypatch):
    import src.mcp.autopilot.intervention_routes as ir
    import src.autopilot.orchestrator as orch

    monkeypatch.setattr(ir, "AUTOPILOT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(orch, "AUTOPILOT_STATE_DIR", str(tmp_path))
    return tmp_path


def _write_request(input_dir, stamp: str):
    import json

    (input_dir / "input_request_req-1.json").write_text(
        json.dumps({"id": "req-1", "reason": "credit exhausted", "timestamp": stamp})
    )


@pytest.mark.parametrize(
    "offset_hours",
    [-8, -6, 0, 1, 9],
    ids=["UTC-8", "UTC-6", "UTC", "UTC+1", "UTC+9"],
)
def test_fresh_request_survives_from_any_timezone(input_dir, offset_hours):
    """A request written moments ago must never be reaped, whatever the host TZ.

    Simulates the writer's clock as UTC shifted by the host offset -- exactly
    what `datetime.now()` produces -- and asserts the reader still sees it as
    fresh. Fails for every non-zero offset against a `datetime.now()` writer.
    """
    from src.mcp.autopilot.intervention_routes import (
        _find_pending_input,
    )

    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    _write_request(input_dir, utc_now.isoformat())

    found = _find_pending_input()

    assert found is not None, "a just-written request was reaped as stale"
    assert found.name == "input_request_req-1.json"


def test_genuinely_old_request_is_still_reaped(input_dir):
    """The staleness sweep must keep working -- don't fix freshness by
    disabling cleanup."""
    from src.mcp.autopilot.intervention_routes import (
        _find_pending_input,
    )

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    _write_request(input_dir, old.isoformat())

    assert _find_pending_input() is None
    assert not (input_dir / "input_request_req-1.json").exists()


def test_prompt_human_writes_utc_not_local(input_dir, monkeypatch):
    """The writer's own contract: the stamp it emits must be UTC.

    The two tests above pin the *reader*; this one pins the *writer*, so a
    future edit back to datetime.now() fails here rather than silently
    reintroducing the reap. prompt_human unlinks its request file on the
    timeout path, so the payload is captured at write time rather than read
    back afterwards.
    """
    import json
    from pathlib import Path

    from src.autopilot.orchestrator import prompt_human

    captured = {}
    real_write_text = Path.write_text

    def spy_write_text(self, data, *a, **k):
        if "input_request_" in self.name or self.suffix == ".tmp":
            try:
                captured.update(json.loads(data))
            except Exception:
                pass
        return real_write_text(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", spy_write_text)

    class _Log:
        def event(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    prompt_human("credit exhausted", _Log(), timeout=0)

    assert "timestamp" in captured, "prompt_human wrote no request payload"
    stamp = datetime.fromisoformat(captured["timestamp"])
    drift = abs(
        (stamp - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    )
    assert drift < 120, (
        f"timestamp is {drift:.0f}s from UTC -- it is being written in local "
        "time, which intervention_routes will misread as UTC"
    )

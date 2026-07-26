---
type: adversarial_review_result
feature_id: des-91c8-opencode-collector
blocker_count: 1
warning_count: 1
nit_count: 1
---

# Adversarial Review — OpenCode Cost Collector (des-91c8-opencode-collector)

**Reviewer:** Guardian (last-resort adversarial pass)
**Target:** `src/services/cost_collection_service.py` — `_discover_opencode_session()`,
`OpenCodeCollector`, and the `collect_task_cost()` opencode branch (+ `tests/test_cost_collection_service.py`)
**Prior passes:** `docs/architecture.md` (design), `docs/architectural_review/architectural_review_report.md`
(PASS, 0 blockers, 34/34 tests green)

The architecture design and architectural review both confirmed the implementation
matches the spec. This pass instead asks whether the spec itself, and the code that
faithfully implements it, actually work against real OpenCode data — not just against
the test fixtures written for it.

---

## B-1 (BLOCKER) — Time-window math uses local-time interpretation of a UTC value; on any host not set to the UTC timezone, `_discover_opencode_session()` silently fails to find any session

**Location:** `src/services/cost_collection_service.py:451-452`

```python
start_ms = int(agent_created_at.timestamp() * 1000)
end_ms = int(datetime.utcnow().timestamp() * 1000)
```

Both `agent_created_at` (from `Agent.created_at`, populated via `default=datetime.utcnow`
in `src/core/database.py`) and `datetime.utcnow()` are **naive** datetime objects whose
wall-clock reading represents UTC. Python's `datetime.timestamp()` on a naive object does
not know that — per the stdlib docs, "naive datetime instances are assumed to represent
local time," and it converts using the host's local timezone offset, not UTC.

Reproduced directly:

```
$ TZ="America/Los_Angeles" python3 -c "
from datetime import datetime
import time
u = datetime.utcnow()
print(u.timestamp() - time.time())
"
25199.999...   # ~7 hours — exactly the PDT UTC offset
```

The current execution host for this repo is `MDT` (`date` → `Sat Jul 25 23:25:17 MDT
2026`, `time.tzname` → `('MST', 'MDT')`), so this isn't a hypothetical "some future
deployment" concern — it reproduces on the machine this feature would actually run on
today. `start_ms` and `end_ms` both get the same ~6-7 hour offset added, so the query
window's *width* stays correct, but its *absolute position* is shifted away from real
UTC epoch-ms by the host's local offset.

OpenCode's own `session.time_created` values are genuine UTC epoch-ms (written by
OpenCode's own runtime, unaffected by this Python-side bug). The query is:

```sql
SELECT id, time_created FROM session
WHERE directory = ? AND time_created >= ? AND time_created <= ?
```

With `start_ms`/`end_ms` shifted by a non-zero offset from true UTC, a real session
created at the real current time will not fall inside `[start_ms, end_ms]` on any host
where local time isn't UTC — `_discover_opencode_session()` returns `None` every time,
indistinguishable from "OpenCode not installed" or "no session yet." `collect_task_cost`
then hits the pre-existing `if not session_file: ... return` guard and exits silently —
no exception, no cost entry, no checkpoint write, no log above `debug`. Every OpenCode
task's cost is silently dropped on any non-UTC host.

**Why the architecture design and both the architectural review and this feature's own
34 tests missed it:** the bug was baked into the architecture spec itself
(`docs/architecture.md:90`: `int(agent_created_at.timestamp() * 1000)`), so the
implementation is a faithful, "compliant" copy of a spec that was wrong from the start —
compliance review checks code-against-spec, not spec-against-reality. And the test
suite's own fixture helper reproduces the identical conversion on the expected side:

```python
# tests/test_cost_collection_service.py:443-444
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
```

Every test builds its fake `opencode.db` rows' `time_created` via `_ms(datetime.utcnow()
± timedelta(...))` — the *same* naive-datetime `.timestamp()` conversion the production
code applies to `agent_created_at`/`datetime.utcnow()`. Both sides of the comparison
apply the identical offset, so the tests are internally consistent and pass on any host,
timezone included — the offset cancels out in the test harness but never cancels out
against real OpenCode data, whose `time_created` is unaffected by Python's local-time
assumption.

**This is exactly the class of bug CLAUDE.md's `Always datetime.utcnow(), never bare
datetime.now()` invariant exists to catch** — a mixed/misinterpreted clock silently
breaking a staleness/window comparison — just via `.timestamp()`'s naive-datetime
assumption rather than a direct `.utcnow()`/`.now()` mix.

**The codebase already has the correct pattern for this**, unused here:
`src/mcp/autopilot_api.py:3993` does `ts = ts.replace(tzinfo=timezone.utc)` before
computing a timestamp delta on a naive-UTC value.

**Fix:** attach `timezone.utc` before calling `.timestamp()`:

```python
from datetime import timezone
...
start_ms = int(agent_created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
end_ms = int(datetime.utcnow().replace(tzinfo=timezone.utc).timestamp() * 1000)
```

And the test fixture helper needs the same fix (or the tests need to assert against a
real, TZ-independent expected value) or this will silently regress again the next time
someone "fixes" it back to match a re-broken spec.

---

## W-1 (WARNING) — No retry on transient `sqlite3.Error`; a locked/mid-write `opencode.db` at the moment of collection permanently drops that session's cost

**Location:** `OpenCodeCollector.collect()`, `src/services/cost_collection_service.py:299-311`

`collect_task_cost()` fires exactly once, synchronously, at task completion (by design —
architecture explicitly rules out polling/timers). If `sqlite3.connect(...)` or the
`SELECT` raises `sqlite3.Error` (e.g. `database is locked` while OpenCode's own process
is still flushing a write), the exception is caught, logged at `error`, and the function
returns `(entries, checkpoint)` with `checkpoint` unchanged — so a future call could
still succeed. That part is fine. But there is no caller that will ever make that future
call: `SessionCostCheckpoint` isn't advanced, but nothing re-invokes `collect_task_cost`
for a task that has already completed. The cost for that task is lost permanently, with
only an `error`-level log line as evidence — easy to miss in a busy log stream, and
nothing in the UI distinguishes "no cost" from "collection raced a lock and lost."

Lower confidence than B-1 (I did not find concrete evidence OpenCode's SQLite writes are
asynchronous relative to the CLI going idle), but the single-shot-no-retry design means
if it ever does race, the failure mode is silent and permanent rather than merely
delayed.

---

## N-1 (NIT) — `checkpoint_key` collision surface between OpenCode row IDs and Hephaestus session IDs

**Location:** `src/services/cost_collection_service.py:578`

`SessionCostCheckpoint.session_id` is now populated from two different ID spaces
depending on `cli_type`: Hephaestus's own tmux-derived session IDs for
`pi`/`claude_code`/`codex`, and OpenCode's own `session.id` (a string OpenCode
generates, format/uniqueness guarantees unknown to this codebase) for `opencode`. If
OpenCode's ID generator ever produced a string that collided with an existing
Hephaestus session ID (or vice versa), the two collectors would silently share a
checkpoint row and one would incorrectly short-circuit the other. Not a practical risk
today given the two ID formats are almost certainly incompatible (Hephaestus IDs are
tmux-session-name-derived, OpenCode's look ULID/ksuid-shaped), but it's an implicit
cross-namespace assumption worth a one-line comment if anyone revisits this table's key
design.

---

## Verdict

**1 BLOCKER.** `_discover_opencode_session()`'s time-window math is broken on any host
whose local timezone isn't UTC, including the one this code runs on today — it silently
finds zero sessions and drops 100% of OpenCode task costs with no error surfaced above
`debug`. This was invisible to both the architectural review and the full test suite
because the bug originates in the architecture spec itself and the test fixtures
reproduce the identical flawed conversion on the expected side, so the error cancels out
inside the test harness but not against real OpenCode data.

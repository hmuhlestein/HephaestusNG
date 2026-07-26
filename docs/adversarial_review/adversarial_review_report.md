---
type: adversarial_review_result
feature_id: des-91c8-opencode-collector
blocker_count: 0
warning_count: 0
nit_count: 0
---

# Adversarial Review — OpenCode Cost Collector (re-verification pass)

**Reviewer:** Guardian (adversarial re-review, verifying prior BLOCKER only)
**Target:** `src/services/cost_collection_service.py` (`_discover_opencode_session()`)
**Commit reviewed:** `af59ac8` ("phase(development): Fixed the adversarial_review
BLOCKER (B-1)...")

Per instructions, this pass verifies only whether the single BLOCKER that survived the
prior adversarial_review run is now fixed — not a from-scratch re-review. It is
confirmed fixed by diff inspection and by actually running the test suite (not just
trusting the commit message).

## B-1 — Naive-datetime `.timestamp()` misinterpreted as local time, silently breaking OpenCode session discovery on non-UTC hosts — FIXED

**Prior finding:** `_discover_opencode_session()` computed its time-window bounds via
`agent_created_at.timestamp()` and `datetime.utcnow().timestamp()`. Both values are
naive datetimes whose wall-clock reading is UTC, but Python's `.timestamp()` on a naive
object assumes *local* time. On any host not set to UTC (this dev host runs MDT), both
bounds silently shift by the host's UTC offset while OpenCode's own `session.time_created`
is genuine UTC epoch-ms — so the query window never overlaps a real session, and
`collect_task_cost` silently drops 100% of OpenCode costs with no error above `debug`.

**Fix verified (`src/services/cost_collection_service.py:451-457`):**

```python
start_ms = int(agent_created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
end_ms = int(datetime.utcnow().replace(tzinfo=timezone.utc).timestamp() * 1000)
```

`timezone` added to the `datetime` import. This is exactly the recommended fix —
attaching `tzinfo=timezone.utc` before calling `.timestamp()` so Python treats the naive
value as UTC instead of local time, matching the codebase's own existing pattern for
this exact problem elsewhere (`src/mcp/autopilot_api.py:3993`).

**Test fixture also fixed:** the prior finding noted the test suite's own `_ms()` helper
(`tests/test_cost_collection_service.py`) applied the identical flawed conversion to its
fixture data, which is why 34/34 tests passed despite the feature being broken against
real data — the offset cancelled out on both sides of the comparison inside the test
harness. `_ms()` now does `dt.replace(tzinfo=timezone.utc).timestamp()` too, matching the
production fix.

**New regression coverage is non-tautological:** `TestDiscoverOpencodeSession::
test_finds_session_using_real_utc_epoch_regardless_of_host_tz` computes its session's
`time_created` independently via `calendar.timegm()` (always UTC, ignores host TZ)
rather than reusing `_ms()`/`.timestamp()` under test — so it checks the result against
a real, TZ-independent UTC epoch value rather than merely checking both sides of a
comparison agree with each other (which is how the original bug hid). The test
self-skips only if the host is already UTC (`time.timezone == 0 and time.altzone == 0`).

**Verification performed, not just claimed:**
- `git show af59ac8 -- src/services/cost_collection_service.py` — diff matches the
  recommended fix exactly, no unrelated changes.
- Ran `pytest tests/test_cost_collection_service.py -q`: **35 passed**.
- Ran the new regression test individually and confirmed it was **not skipped** — this
  host's `time.timezone`/`time.altzone` are `25200`/`21600` (non-UTC, MDT), i.e. the
  exact condition required to reproduce B-1, and it passed against the fixed code.

## Previously-flagged non-blocking items

- **W-1** (no retry on transient `sqlite3.Error`; a locked `opencode.db` at the moment of
  collection permanently drops that session's cost) — left unaddressed, as expected: it
  was WARNING severity, not a blocker, and the commit message explicitly notes it was
  left per the review's own framing.
- **N-1** (checkpoint-key cross-namespace collision surface between OpenCode session IDs
  and Hephaestus session IDs) — left unaddressed, as expected: NIT severity.

Neither was re-raised as a BLOCKER; no new issues introduced by the fix itself (import
addition and two `.replace(tzinfo=...)` calls only — no behavioral change beyond
correcting the window math, no new code paths, no new failure modes).

## Verdict

**0 BLOCKER, 0 WARNING, 0 NIT.** The sole carried-forward BLOCKER (B-1) is fixed,
verified by direct diff review and by actually executing the test suite on this
non-UTC host — the same environment that reproduces the original bug. No new issues
found in the fix itself.

# Safe Restart Design

## Goal

`heph restart` (and `heph stop`) should stop the backend without silently
killing in-flight autopilot pipelines or interrupting agents mid-step. Today
it does both. Observed live, repeatedly, in one session: several `heph
restart` calls in a row (each deploying a small code fix) left an active
project's continuous pipeline loop dead afterward — no crash, no error, it
just never got another turn to call `pick_next_design`, while individual
orphaned *agents* did get resumed correctly. The project's own "is this
running" status still reported `true` (derived from a live workflow/agent,
not from the pipeline loop itself), so nothing surfaced the gap until a
human noticed unblocked work sitting untouched for hours.

This document is about making a restart **safe**: give in-flight agents a
chance to reach a safe checkpoint first, and guarantee the pipeline loop
actually comes back — not just individual agents.

## Current State: Three Gaps

### Gap 1 — `shutdown_event()` never touches the pipeline loop

`src/mcp/server.py:1020`'s `shutdown_event()` already has the right pattern
for background tasks — stop signal, bounded wait, cancel-as-fallback — and
applies it to the queue processor and phase-advancement sweep:

```python
server_state.shutdown_event.set()
if server_state.background_queue_processor_task:
    try:
        await asyncio.wait_for(server_state.background_queue_processor_task, timeout=5.0)
    except asyncio.TimeoutError:
        server_state.background_queue_processor_task.cancel()
```

It never does this for `AutopilotServiceRegistry` (`src/autopilot/service.py:402`).
Every running project's `AutopilotService._run_pipeline` task gets no stop
signal at all during shutdown — it's still alive when uvicorn's event loop
itself closes afterward, at which point asyncio hard-cancels it
(`_cancel_all_tasks`, visible in the traceback this produces: `File
"asyncio/runners.py", line 70, in close`). That's a `CancelledError` raised
at whatever `await` happens to be suspended, not a clean exit through the
loop's own `while True: ... if _should_stop(...): break` (`src/autopilot/
orchestrator.py:7779`) — nothing downstream of that point runs, including
`persistent_state.save(...)` calls that would otherwise happen right before
the loop's own graceful stop path.

### Gap 2 — the existing graceful-stop path is already undersized

`AutopilotService.stop()` (`service.py:255`) *does* attempt exactly the
right thing already — set `_stop_event`, wait for the task, fall back to
cancel:

```python
self._stop_event.set()
self._running = False
if self._task:
    try:
        await asyncio.wait_for(self._task, timeout=10.0)
    except asyncio.TimeoutError:
        self._task.cancel()
```

But `run_continuous_pipeline`'s loop only re-checks `_should_stop(project_id)`
(`orchestrator.py:7509`) between iterations — and two of its own paths block
for far longer than the 10s timeout without checking it at all:

```python
# orchestrator.py:7940 — an active workflow is still running elsewhere
time.sleep(POLL_INTERVAL)

# orchestrator.py:7959 — queue is empty, wait before rescanning
time.sleep(DESIGN_QUEUE_SCAN_INTERVAL)   # = 60 (line 104)
```

A stop request that arrives while the loop is inside either sleep is
invisible for up to 60 seconds. `stop()`'s 10s timeout will almost always
expire first and fall through to `self._task.cancel()` — meaning **even an
explicit, user-clicked Stop button today is not actually graceful most of
the time**, independent of the restart-specific gap in Gap 1.

### Gap 3 — agents get no signal at all

`heph stop`/`restart` sends SIGTERM only to the backend process
(`src/cli/commands/stop.py:73`). tmux sessions are independent OS processes
and survive it — the agent keeps running, but now against a backend that's
either dead or about to be. Nothing tells the agent a restart is coming, so
there's no chance for it to finish an atomic step (rather than being cut off
mid-file-write) or checkpoint its findings before the backend's tracking of
it goes away and comes back via `_resume_interrupted_workflows`
(`server.py`, `[RESUME] Auto-resuming...`) as a "you were restarted, run git
log / git status first" recovery.

## Design

### 3.1 — New pause path that doesn't clear persisted state

`AutopilotService.stop()` calls `self.clear_persisted_state()`
(`service.py:266`) — correct for an explicit user Stop (no auto-resume
wanted), wrong for a restart-triggered pause (auto-resume *is* wanted).
Reusing `stop()` for shutdown would silently disable the exact auto-resume
mechanism this whole design depends on.

Add a sibling method, `pause_for_restart()`: identical to `stop()` (same
`_stop_event`/`_running` handling, same bounded-wait-then-cancel fallback)
but skips `clear_persisted_state()`. The persisted "was running" marker
(`_running_state_key`, `service.py:28`) stays intact, so
`_resume_interrupted_workflows` picks it back up on the next startup exactly
as it does today for an abrupt kill — the difference is *how* the current
run winds down, not whether the next one starts.

### 3.2 — Wire it into `shutdown_event()`

```python
@app.on_event("shutdown")
async def shutdown_event():
    ...
    for service in get_registry().running():
        await _notify_agents_of_restart(service.project_id)
    await asyncio.gather(*(
        service.pause_for_restart()
        for service in get_registry().running()
    ), return_exceptions=True)
    ...
```

Notify before pausing, not after — an agent mid-step benefits most from
knowing *before* its session goes quiet, not after.

### 3.3 — Make the loop's stop-check actually responsive

Replace both blocking sleeps with short-interval polling so a stop request
is noticed within ~1s instead of up to 60s:

```python
def _interruptible_sleep(seconds: int, project_id: Optional[str]) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _should_stop(project_id):
            return
        time.sleep(1)
```

Swap in at both call sites (`orchestrator.py:7940`, `7959`). This also
fixes Gap 2 as a side effect — `stop()`'s existing 10s timeout becomes
realistic instead of routinely expiring.

### 3.4 — Checkpoint nudge to in-flight agents

`_notify_agents_of_restart(project_id)`: find every `working`-status phase
agent whose task belongs to this project (same query shape
`_audit_system_health` already uses, `monitor.py:2459`), and send one
best-effort text nudge via the existing `send_message_to_agent`:

> A backend restart is happening shortly. If you're mid-edit, finish this
> atomic step (don't start a new multi-file change). Call
> `hephaestus_save_memory` now with anything you don't want to lose — your
> session will resume automatically afterward.

This is explicitly **best-effort, not a guarantee**. tmux text injection
can't pause a model mid-generation or mid-tool-call — an agent that's
synchronously blocked on its own LLM call won't see the message until its
next turn. The value is for the common case (agent between turns, about to
start something new) and for encouraging the `save_memory`-as-you-go habit
the system prompt already asks for, not for hard atomicity guarantees. State
that plainly wherever this is documented so nobody builds on it as if it
were one.

### 3.5 — Safety net: detect a pipeline that didn't come back

The startup-time resume (`_resume_interrupted_workflows`) is one-shot — if
it fails silently for any reason (exactly what seems to have happened live:
several restarts in quick succession may have raced the persisted-marker
read/write), nothing else ever notices. Add a periodic self-heal check,
same shape as `_sync_stale_feature_statuses` (`orchestrator.py:2736`) and
run from the same background sweep (`background_phase_advancement_sweep`,
`server.py`):

```python
def _resync_pipeline_registry(logger) -> int:
    """A project whose persisted state says 'was running' but has no live
    entry in AutopilotServiceRegistry has fallen through the startup
    resume -- restart it here instead of leaving it silently idle."""
    resumed = 0
    for project_id, _state in AutopilotService.enumerate_persisted_states():
        if get_registry().get(project_id) is None:
            # project_path is part of the persisted state already
            ...
            resumed += 1
    return resumed
```

This closes the actual failure mode observed live, independent of whether
3.1–3.4 are also implemented — it's the cheapest, highest-value piece of
this design and could ship alone if the rest is deferred.

### 3.6 — Optional: `heph restart` gives the graceful path a head start

Not required (SIGTERM already reaches `shutdown_event()`), but `heph
stop`/`restart` (`src/cli/commands/stop.py:17`) could call a new `POST
/api/system/prepare-shutdown` first, with a short timeout (e.g. 3s,
best-effort — don't block the CLI on it), before sending SIGTERM. Marginal
benefit over 3.1–3.2 alone; listed for completeness, not prioritized.

## What This Does Not Do

- Does not make agent checkpointing atomic or guaranteed — see 3.4.
- Does not change `heph stop --force` / SIGKILL behavior — force is still
  force.
- Does not add a new "draining" state to the CLI's `heph status` output,
  though that would be a natural follow-up once 3.1–3.2 exist (today
  there's no way to tell "shutting down gracefully" apart from "starting
  up" from the outside).

## Testing Plan

Same git-stash-verify discipline as the rest of this codebase:

- `_interruptible_sleep`: unit test asserting it returns promptly once
  `_should_stop` flips, not after the full duration.
- `pause_for_restart`: unit test asserting persisted state survives it
  (unlike `stop()`), using the same mocking pattern
  `tests/test_budget_enforcement.py` already uses for `AutopilotService`.
- `_resync_pipeline_registry`: unit test with a persisted-but-not-in-registry
  project, asserting it gets restarted; a persisted-and-already-running one,
  asserting it's left alone (no duplicate start).
- Manual drill: start a real pipeline, `heph restart`, confirm via
  `logs/server.log` that `pause_for_restart` (not a `CancelledError`
  traceback) appears for the running project, and that the pipeline is
  processing new work again within one `DESIGN_QUEUE_SCAN_INTERVAL` of the
  restart completing.

## Open Questions

- Should the drain timeout (currently reusing `stop()`'s 10s) be longer
  specifically for the shutdown path, since there's no user impatiently
  waiting on a CLI command the way there is for an explicit Stop click?
- Should `_notify_agents_of_restart` skip agents that started a tool call
  very recently (likely mid-something) vs. one that's been idle for a
  while (likely between turns), to reduce noise? Probably not worth the
  complexity initially — a single extra message is cheap.

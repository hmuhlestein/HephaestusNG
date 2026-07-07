# Design: Self-review "gap check" loop before a phase task marks itself done

## Goal

Today, when an agent (e.g. the `development` phase) believes it's finished, it
calls `hephaestus_update_task_status(status="done")` once and the task is
accepted immediately (modulo the output-artifact hard floor — see below). All
gap-catching happens *downstream*, in later phases (`architectural_review`,
`adversarial_review`, `qa_validation`, ...), never before the agent's own
"done" claim.

This design adds an optional **self-review loop**: before a task is allowed
to actually complete, the same agent is sent a canned "look for gaps and
challenge your own assumptions" prompt and must explicitly respond. If it
reports more gaps, it keeps working and the loop repeats (up to a cap). Once
it reports no more gaps, the task completes normally. This is opt-in per
phase, not a global behavior change.

## Current State (verified against the real code)

**No self-review mechanism exists.** `config/workflows/autopilot/development.yaml`'s
`additional_notes` ends with a single "call `update_task_status` when done"
instruction and nothing else checks the agent's own work before accepting it.

**The closest existing analog is the validator loop**, which this design
deliberately mirrors:

- `Task.validation_enabled` (`src/core/database.py:207`, Boolean) and
  `Task.validation_iteration` (Integer) drive a loop where a **separate**
  validator agent reviews the work.
- `Phase.validation` (`src/core/database.py:438`, a JSON column) is read from
  each phase's own YAML `validation:` key at task-enrichment time
  (`src/mcp/server.py:1327-1335`): `if phase.validation.get("enabled", True): task.validation_enabled = True`.
  No current phase YAML actually sets `validation:`, so this path is dormant
  today but the plumbing exists and is the right template to copy.
- The hook point is `update_task_status` in `src/mcp/server.py`, specifically
  the `if request.status == "done" and task.validation_enabled:` branch at
  **line 2244**. When it fires: `task.status = "under_review"`,
  `task.validation_iteration += 1`, and
  `TaskCompletionService.spawn_validation(...)` (async, fire-and-forget)
  spins up a **separate** validator agent + its own `Task` row (see
  `src/validation/validator_agent.py:69` `spawn_validator_agent`).
- Feedback delivery back to the *original* (still-running) agent already has
  a working precedent: `send_feedback_to_agent(agent_id, feedback, iteration)`
  (`src/validation/validator_agent.py:352`) writes the feedback to a temp
  file and does `tmux send-keys -t agent_<id> "cat <file>" Enter` — i.e. it
  injects text into the agent's live tmux pane without creating a new agent
  or task. `AgentManager.send_message_to_agent` (`src/agents/manager.py:1716`)
  is the more general version of the same mechanism.
- `Agent.kept_alive_for_validation` (`src/core/database.py:146`) is set to
  `True` when validation spawns (`src/mcp/server.py:2259`) but is **never
  read anywhere else in the codebase** — confirmed via grep. It's a write-only
  field today, presumably meant to protect the agent from being reaped by
  monitor/stuck-agent logic (`src/monitoring/monitor.py:1447` `_handle_stuck_agent`,
  `_appears_stuck` at line 174) while it waits on a validator, but nothing
  currently consults it. **This design must not repeat that mistake** — see
  "Stuck-agent interaction" below.
- The output-artifact hard floor (`TaskCompletionService.verify_output_artifact`,
  `src/services/task_completion_service.py:67`) runs *before* the
  validation-enabled check, at `update_task_status`'s step 3b. It's a
  mechanical file-existence check, orthogonal to this design — the gap-check
  loop is about self-critique of already-produced output, not about whether
  the declared file exists.

## Design

### 1. Phase-level opt-in config

Add `Phase.gap_check` (JSON column, mirroring `Phase.validation`) with the
same read path as validation:

```python
# src/mcp/server.py, alongside the existing validation-inherit block (~line 1327)
if phase and phase.gap_check and phase.gap_check.get("enabled", False):
    task.gap_check_enabled = True
    task.gap_check_max_iterations = phase.gap_check.get("max_iterations", 3)
```

Note the default is `False` here (opposite of validation's `True` default) —
this is a new, more expensive behavior and should require an explicit opt-in
per phase YAML:

```yaml
# config/workflows/autopilot/development.yaml
gap_check:
  enabled: true
  max_iterations: 3
```

### 2. New `Task` columns (migration, following this repo's established pattern)

```python
gap_check_enabled = Column(Boolean, default=False, nullable=False)
gap_check_iteration = Column(Integer, default=0, nullable=False)
gap_check_max_iterations = Column(Integer, default=3, nullable=False)
```

Add `_migrate_gap_check_columns()` in `src/core/database.py`, registered in
`create_tables()`'s migration sequence — same shape as
`_migrate_task_retry_count_column()` /`_migrate_phase_retry_count_column()`
added earlier in this codebase's history.

### 3. Hook point: `update_task_status`, before the validation-enabled branch

In `src/mcp/server.py`, insert a new step between 3c (spec gate firing) and 4
(validation-enabled check):

```python
# 3d. Gap-check self-loop — before accepting "done", ask the same agent to
# self-review once more. Runs before the validation-enabled branch: an agent
# should exhaust its own self-review before a separate validator gets involved.
if (
    request.status == "done"
    and task.gap_check_enabled
    and task.gap_check_iteration < task.gap_check_max_iterations
):
    task.gap_check_iteration += 1
    task.status = "in_progress"  # NOT "done" yet — still the same agent, same task
    task.completion_notes = request.summary  # preserve what it claimed this round
    session.commit()

    prompt = GAP_CHECK_PROMPT_FIRST if task.gap_check_iteration == 1 else GAP_CHECK_PROMPT_REPEAT
    await server_state.agent_manager.send_message_to_agent(agent_id, prompt)

    return UpdateTaskStatusResponse(
        success=True,
        message=f"Gap-check round {task.gap_check_iteration}/{task.gap_check_max_iterations} sent to agent",
        termination_scheduled=False,
    )
```

`task.status` stays `"in_progress"` (not a new status value) — from the
orchestrator/UI's perspective this is indistinguishable from ordinary ongoing
work, which is correct: nothing downstream needs to know a self-review round
is happening.

### 4. The canned prompts

```python
GAP_CHECK_PROMPT_FIRST = """
Before marking this done, review your own work critically:
- What did you assume without verifying?
- What edge cases or error paths did you not handle?
- What would a skeptical reviewer flag?
- Did you actually run/verify what you claim works?

Fix anything real you find. If you find nothing, or once you've fixed
everything you found, call hephaestus_gap_check_response with
has_more_gaps=false. Otherwise call it with has_more_gaps=true and a summary
of what you're fixing, then continue working.
"""

GAP_CHECK_PROMPT_REPEAT = """
Gap-check round {iteration}/{max_iterations}. Look again, specifically for
anything you missed last round. If there is nothing left, call
hephaestus_gap_check_response with has_more_gaps=false — don't manufacture
findings just to keep looping.
"""
```

### 5. New MCP tool: `hephaestus_gap_check_response` — not a keyword scan

The original ask was "have the code look for the keyword DONE" in the
agent's raw output. **Recommend against this.** This codebase has been bitten
repeatedly (this session alone) by fragile terminal-output scraping —
`architectural_review.yaml` had to be explicitly told not to `cat` logs, and
the whole reason `give_validation_review` exists as a structured tool instead
of a free-text verdict is exactly this problem. A literal "DONE" is trivially
missed (agent writes "~~DONE~~ actually one more thing", or emits it
mid-paragraph, or a markdown-bolded `**DONE**` that a naive substring match
over- or under-matches). A dedicated tool call is unambiguous and typed:

```python
class GapCheckResponseRequest(BaseModel):
    task_id: str
    agent_id: str
    has_more_gaps: bool
    findings: Optional[str] = None  # what it found/fixed, for save_memory-style audit trail

@app.post("/api/hephaestus_gap_check_response")
async def gap_check_response(request: GapCheckResponseRequest):
    task = session.query(Task).filter_by(id=request.task_id).first()
    # ... authorize agent_id against task, same pattern as update_task_status ...

    if request.has_more_gaps:
        # Agent found something and is continuing — no state change needed,
        # task is already "in_progress". Just log it for the audit trail.
        logger.info(f"[GAP-CHECK] Task {task.id[:8]} iteration {task.gap_check_iteration}: agent found gaps: {request.findings}")
        return {"success": True, "message": "Continue working, then call update_task_status(done) again when ready."}

    # No more gaps — accept the "done" that was deferred in step 3d, but only
    # if the agent isn't lying: still runs verify_output_artifact et al. by
    # re-entering update_task_status's own completion path (or duplicating the
    # tail of it) rather than blindly trusting has_more_gaps=false.
    return await _complete_task_after_gap_check(task, agent_id, request.findings)
```

`_complete_task_after_gap_check` should reuse the *existing* tail of
`update_task_status` (steps 3b's output check, the validation-enabled branch,
commit-and-link-ticket, etc.) rather than duplicating it — refactor that tail
into a shared helper both call sites invoke, instead of copy-pasting it. This
also means a gap-checked task **still gets the output-artifact hard floor and
still goes into the validator loop afterward** if the phase has both enabled
— the two mechanisms compose (self-review first, then a separate validator),
they don't replace each other.

### 6. Loop exit / cap

Three ways the loop ends, all already covered above:
1. Agent calls `gap_check_response(has_more_gaps=false)` → completes.
2. `task.gap_check_iteration >= task.gap_check_max_iterations` → step 3d's
   condition stops firing, so the *next* `update_task_status(done)` call
   falls through to the normal completion path unconditionally (no infinite
   loop possible even if the agent keeps claiming gaps forever).
3. Agent never responds at all (dies, hangs) → same stuck-agent detection
   that already exists for any other "agent went quiet" scenario applies
   unchanged (see below) — no new timeout mechanism needed.

### 7. Stuck-agent interaction (the thing `kept_alive_for_validation` forgot to finish)

After step 3d sends the prompt, the agent's `Agent.status` is unchanged
(still `"working"`) and `Agent.last_activity` should be bumped by
`send_message_to_agent` the same way any other message injection does —
verify this call already touches `last_activity` (it should, being the same
code path `send_feedback_to_agent` uses, which apparently hasn't caused
false-positive stuck detection for the validator loop). If it doesn't
already, that's a prerequisite fix, not something new to this design.
**Do not introduce a new `kept_alive_for_gap_check` flag that nothing reads**
— either wire it into `_appears_stuck`/`_handle_stuck_agent`'s exclusion
logic for real, or don't add it at all and rely on `last_activity` freshness
like everything else does.

## Non-Goals

- **Not** replacing the validator loop — the two are independent and
  composable (self-review, then optionally a separate validator).
- **Not** a global default — `gap_check.enabled` is opt-in per phase YAML;
  `development` is the obvious first candidate, but this shouldn't be turned
  on everywhere without evaluating the cost/benefit per phase.
- **Not** keyword-scraping the agent's terminal output. See section 5.
- **Not** changing `Task.status` values or the CHECK constraint at
  `database.py:173` — deliberately reusing `"in_progress"` rather than adding
  a new status, to avoid touching every piece of code that enumerates task
  statuses (frontend `TASK_STATUS_CONFIG`, `_get_phase_statuses`, etc.).

## Open Questions

- **Same agent vs. fresh eyes**: a model re-reading its own reasoning is
  generally worse at catching its own blind spots than a fresh pair of eyes
  (this is *why* the validator loop uses a separate agent). Is a self-review
  loop worth the added latency/cost if `architectural_review`/
  `adversarial_review` already catch most of what this would? Recommend
  starting with `development` only and measuring whether it actually reduces
  findings in those later phases before expanding to more.
- **Cost control**: each gap-check round is a full LLM turn. `max_iterations:
  3` default caps the worst case, but should this also have a wall-clock
  budget (reuse the phase timeout mechanism) independent of iteration count?
- **Rubber-stamping**: nothing stops an agent from always replying
  `has_more_gaps=false` immediately to skip the loop. This is inherent to
  self-review (per the first open question) — no code-level fix, only prompt
  wording and, if it proves to be a real problem in practice, occasionally
  routing to a validator agent instead (already-existing mechanism) for a
  true second opinion.

## Rollout Order

1. Migration: add the three `Task` columns + `_migrate_gap_check_columns()`.
2. Refactor `update_task_status`'s completion tail into a shared helper
   (prerequisite for step 3, and a good idea regardless — reduces duplication
   risk between the direct-done path and the post-gap-check path).
3. Add step 3d to `update_task_status` + the new
   `hephaestus_gap_check_response` tool/endpoint.
4. Add the canned prompts as constants (or a `prompts/` template file,
   matching how other phase prompts are externalized).
5. Wire `Phase.gap_check` inheritance at task-enrichment time (mirrors the
   existing `validation_enabled` inheritance block).
6. Enable `gap_check: {enabled: true}` on `development.yaml` only.
7. Verify: unit tests for the new columns/hook + one integration test driving
   a fake agent through 2 gap-check rounds then completion, following this
   repo's existing pattern (`tests/test_orchestrator_helpers.py`,
   `tests/test_advance_phases.py` style — real sqlite DB, mocked agent/LLM
   calls).
8. Run a real smoke test with `development.yaml`'s gap-check enabled and
   check whether `architectural_review`/`adversarial_review` findings drop —
   this is the actual signal for whether to expand to other phases.

## Effort Estimate

Small-medium. The DB migration and hook-point wiring are mechanical (direct
copies of the validation-loop pattern). The refactor-the-completion-tail step
(#2 above) is the part most likely to take longer than it looks, since
`update_task_status` is a large handler with several side effects threaded
through it (learnings, ticket linking, spec-gate firing) that need to survive
being called from two entry points instead of one.

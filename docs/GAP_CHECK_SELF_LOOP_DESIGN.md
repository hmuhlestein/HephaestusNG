# Design: One-shot self-review before a phase task marks itself done

> **Supersedes my first draft of this doc.** The idea was already captured,
> in more specific and better-thought-out form, in
> `design_docs/autopilot_architecture_review.md` lines 989–1016 ("Near-term
> enhancement — one-shot intra-agent self-review at completion"). That
> version proposes a **single mandatory extra pass**, not an iterative loop
> with a configurable cap — which is simpler, cheaper, and sidesteps the
> "how does the agent signal it's done looping" question entirely, since
> there's no branching decision for the agent to make. This doc adopts that
> shape and adds the verification-against-current-code detail the original
> note didn't need at the time.

## Goal

When an agent (e.g. `development`) believes it's finished, it calls
`hephaestus_update_task_status(status="done")` once and the task completes
immediately (modulo the output-artifact hard floor). All gap-catching
happens downstream, in later phases, cold — never a warm-context second look
by the same agent right when it thinks it's done.

Add a **one-shot self-review**: the first time an eligible phase's agent
calls `status="done"`, don't complete the task. Send it a fixed checklist
prompt and tell it to look again, then call `done` a second time. The
*second* `done` call always completes normally — there is no third round, no
iteration count, no agent-reported "am I done reviewing" signal to trust or
distrust. One deterministic extra pass, then normal completion.

## Why one-shot, not an iterating loop

My first draft proposed `max_iterations` + a `has_more_gaps` boolean the
agent reports each round. The original note is right to reject that shape:

- It requires a new tool (`hephaestus_gap_check_response` or similar) whose
  entire job is to be trusted about whether the agent found something —
  which is exactly the kind of self-graded signal the *validator* loop
  exists to avoid trusting from the same agent that produced the work
  (`give_validation_review` is checked with `if not agent or agent.agent_type
  != "validator": raise 403` — the original agent is structurally barred
  from grading itself there). A self-reported `has_more_gaps=false` has the
  identical rubber-stamping risk, just one layer further in.
- One-shot means the loop-exit condition is trivial and can't be gamed: the
  *second* `done` call is unconditionally accepted (by this mechanism —
  the output-artifact floor and validator loop still apply normally). No
  cap-tuning, no "did it loop forever" risk to design around.
- It matches the framing in the original note precisely: *"cheap, higher-yield
  than a cold review agent rebuilding context"* — a pre-filter, not a
  substitute for the review phases or the spec gate. One warm-context pass
  is the whole value proposition; more passes hit diminishing returns fast
  and start to look like the thing the validator loop already does properly
  with a structurally-separate grader.

This also fully sidesteps the original "scan raw output for the keyword
DONE" question — there's no keyword to scan for either way. The signal is
the same structured `update_task_status` call the agent already makes,
observed for the second time.

## Current State (verified against the real code)

**No self-review mechanism exists today.**
`config/workflows/autopilot/development.yaml`'s `additional_notes` ends with
a single "call `update_task_status` when done" instruction.

**Column already exists but is claimed for something else.** The original
note says "reuse the existing `tasks.review_done` (or add
`self_review_done`)." Checked: `Task.review_done`
(`src/core/database.py:206`, Boolean) exists, but it is **already wired** to
a different meaning — it's set `True` in the *validator* pass-path
(`src/mcp/server.py:2674`, inside the `give_validation_review` handler, when
`request.validation_passed`). Reusing it here would conflate "a separate
validator approved this" with "the same agent looked at it twice."
**Recommend the explicit `self_review_done` alternative the note already
allows for**, not the reuse option.

**The validator loop is the closest existing analog** for the mechanics
(deferring completion, injecting a message into the still-running agent),
even though the shape (one-shot vs. iterating-with-a-grader) differs:

- Hook point: `update_task_status` in `src/mcp/server.py`, at the
  `if request.status == "done" and task.validation_enabled:` branch,
  currently at **line 2244**. This new one-shot check should run *before*
  that branch (self-review is the first gate; a separate validator, if the
  phase also has one, comes after).
- Message injection into a still-running agent has a working precedent:
  `send_feedback_to_agent(agent_id, feedback, iteration)`
  (`src/validation/validator_agent.py:352`) writes to a temp file and does
  `tmux send-keys -t agent_<id> "cat <file>" Enter`.
  `AgentManager.send_message_to_agent` (`src/agents/manager.py:1716`) is the
  general-purpose version of the same thing and is what this should call.
- The output-artifact hard floor
  (`TaskCompletionService.verify_output_artifact`,
  `src/services/task_completion_service.py:67`) runs before the
  validation-enabled branch today. This new check should run before *that*
  too — self-review happens first, then the mechanical floor, then the
  validator, in that order, each one a cheaper/faster gate than the next.
- `Agent.kept_alive_for_validation` (`src/core/database.py:146`) is set on
  validation-spawn (`server.py:2259`) but **never read anywhere else** —
  confirmed via grep, a write-only flag. Presumably meant to protect the
  agent from stuck-detection reaping while it waits, but nothing consults
  it. **This design must not repeat that**: either verify
  `send_message_to_agent` already keeps `Agent.last_activity` fresh (so
  existing stuck-detection just naturally doesn't fire — the validator loop
  apparently gets away with this today, which is why `kept_alive_for_validation`
  went unused: it wasn't needed), or, if it turns out to be needed, wire it
  into `_appears_stuck`/`_handle_stuck_agent`
  (`src/monitoring/monitor.py:174`, `:1447`) for real rather than adding
  another flag nothing reads.

## Design

### 1. New `Task` column

```python
self_review_done = Column(Boolean, default=False, nullable=False)
```

Migration following this repo's established pattern
(`_migrate_self_review_done_column()`, registered in `create_tables()`'s
migration sequence — same shape as `_migrate_task_retry_count_column()` /
`_migrate_phase_retry_count_column()`).

**Set the flag *before* sending the message, not after** — the original note
calls this out explicitly ("mandatory, set *before* the message, so a crash
can't re-trigger"). If the flag were set after successfully messaging the
agent, a crash between "send" and "commit" would replay the self-review
prompt indefinitely on every retry of the same `done` call. Setting it first
means the worst case is a review prompt that never got delivered — the
second `done` call still completes normally either way, so this fails safe.

### 2. Phase-level opt-in: `Phase.self_review` (or reuse `Phase.validation`'s shape)

Add a JSON column `Phase.self_review`, read at task-enrichment time
(mirrors `Phase.validation`'s read at `server.py:1327-1335`):

```python
if phase and phase.self_review and phase.self_review.get("enabled", False):
    task.self_review_enabled_for_this_task = True  # or just check phase config directly at completion time
```

Simpler alternative worth considering: skip a per-task column for
"enabled" entirely and just check `phase.self_review.get("enabled")` at
`update_task_status` time directly (one query already happens there for the
phase in the output-artifact check) — only `self_review_done` needs to live
on `Task`, since that's the part that must survive across the two `done`
calls. Decide during implementation based on which reads cleaner in
`update_task_status`'s existing structure.

```yaml
# config/workflows/autopilot/development.yaml, and the "fix" phases
self_review:
  enabled: true
```

**Scope, per the original note**: `development` and the "fix" phases
(`adversarial_review`, `security_review`, `doc_review` — phases that
*produce changes*, not just reports). Skip pure-reporting phases
(`product_requirements`, `qa_validation`, `product_validation`,
`forensics_analysis`) — a report has nothing to "fix" on a second pass in
the same sense code does.

### 3. Hook point: `update_task_status`, before the output-artifact floor

```python
# 3a (new, before 3b's output-artifact check). One-shot self-review: the
# first "done" from an eligible phase doesn't complete the task -- it sends
# a fixed checklist and requires a second "done" call.
if request.status == "done" and task.phase_id and not task.self_review_done:
    phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if phase and phase.self_review and phase.self_review.get("enabled", False):
        task.self_review_done = True  # set BEFORE messaging -- crash-safe
        task.completion_notes = request.summary  # preserve this round's claim
        session.commit()

        await server_state.agent_manager.send_message_to_agent(
            agent_id, SELF_REVIEW_CHECKLIST_PROMPT
        )

        return UpdateTaskStatusResponse(
            success=True,
            message="Self-review requested — re-check your work, then call update_task_status(done) again.",
            termination_scheduled=False,
        )
```

`task.status` is left untouched (still whatever it was — `"in_progress"` in
the normal case) — no new status value, no orchestrator/UI-visible state
change. The *second* `done` call arrives with `task.self_review_done` already
`True`, so this block is skipped and the task falls through to the existing
completion path (output-artifact floor, validation-enabled branch, etc.)
exactly as it does today. No refactor of `update_task_status`'s tail is
needed — unlike my first draft's design, there's only one entry point, called
twice.

### 4. The checklist prompt (content specified by the original note)

```python
SELF_REVIEW_CHECKLIST_PROMPT = """
Before this is actually done, re-check your own work:
- Re-read the design/requirements — is every requirement implemented?
- Edge cases and error handling — anything unhandled?
- Tests exist for new code, and they pass?
- Any TODOs, stubs, or dead code left behind?

Fix anything real you find, then call hephaestus_update_task_status
with status="done" again — record what you changed (if anything) in the
summary.
"""
```

No "find your own gaps" open-ended framing — the original note's checklist
is concrete and checkable, which also makes rubber-stamping ("looked, found
nothing") easier to spot-check later (nothing changed between the two
`completion_notes`, or no new commits between the two `done` timestamps).

### 5. Telemetry (per the original note)

Log when self-review fires and diff the working tree between the first and
second `done` (e.g. `git diff --stat` in the worktree between the two
timestamps, logged alongside the task id) — this is the actual signal for
whether one pass catches anything real often enough to be worth the extra
LLM turn, before considering a second round or expanding scope.

## Relationship to other mechanisms (per the original note — keep these distinct)

- **Not** the validator loop (`validation_enabled`/`give_validation_review`)
  — that's a structurally separate grader; this is the same agent, warm
  context, a cheap pre-filter that *reduces* how often work reaches the
  validator or the spec gate with something obviously wrong.
- **Not** the reviewer evaluation points (`architectural_review`,
  `adversarial_review`, etc. as pipeline phases) — those are cold-context
  critics at phase boundaries; self-review doesn't replace them, it just
  means they should see fewer easy misses.
- **Not** the spec gate (`_build_spec_phase_output` / `score_qa` at the QA
  boundary) — the independent hard floor stays exactly as-is regardless of
  whether self-review ran.
- **Not** the removed Tier-1 "nudge" block (`cc35043`, "remove nudge/auto-kill
  block that duplicated Guardian/Conductor") — that was a stuck-agent
  progress-timeout nudge ("no task progress for Ns, please continue"),
  unrelated to self-review; checked via `git show cc35043` to confirm it
  wasn't the same mechanism under an old name.

## Non-Goals

- Not a global default — opt-in per phase YAML, `development` first.
- Not a multi-round loop — see "Why one-shot" above. If one pass proves
  insufficient in practice, that's a reason to revisit scope/prompt content,
  not to add iteration.
- Not changing `Task.status` values or the CHECK constraint at
  `database.py:173`.
- Not reusing `Task.review_done` — see "Current State" above.

## Open Questions

- **Same agent vs. fresh eyes**: unchanged concern from my first draft — a
  model re-reading its own reasoning catches less than a fresh pair of eyes.
  The original note's counter is that warm context has real value the
  *review phases* don't get, so this isn't strictly worse, just different.
  Telemetry (above) is how to actually find out.
- **Sequencing dependency called out in the original note**: it says this
  should land *after* the spec-gate fix and a green "Run B" validation
  (§11.2 in the same doc) — "a self-review pass on a pipeline that can't
  reach the gate doesn't help." Given how much of this session's work has
  already gone into orchestrator/task-completion correctness, check whether
  that precondition is already satisfied before starting implementation,
  rather than assuming the original note's dated context still holds.
- **Rubber-stamping**: a self-review that always says "looked, found
  nothing" with no diff defeats the purpose. No code-level fix — telemetry
  makes it visible, and the checklist's concreteness (§4) makes a no-op pass
  easier to spot in the logged diff.

## Rollout Order

1. Migration: add `Task.self_review_done`.
2. Add the hook (§3) to `update_task_status`, before the output-artifact
   check — small, single entry point, no tail-refactor needed.
3. Add the checklist prompt constant.
4. Wire `Phase.self_review` config read (either as a new `Phase` JSON column
   mirroring `Phase.validation`, or a direct phase-config lookup at
   completion time — decide based on implementation, per §2).
5. Enable on `development.yaml` only.
6. Add the before/after diff telemetry (§5).
7. Tests: unit test for the hook (first `done` defers + messages, second
   `done` completes), following this repo's existing pattern (real sqlite
   DB, mocked `send_message_to_agent` — see `tests/test_orchestrator_helpers.py`
   style).
8. Real smoke test with `development.yaml`'s self-review on; check the
   telemetry diff and whether `architectural_review`/`adversarial_review`
   findings drop, before expanding to the other "fix" phases.

## Effort Estimate

Small. One column, one hook block with no branching complexity (the entire
"was this the second call" check is a single boolean), one prompt constant,
one YAML flag. Materially smaller than my first draft's design once the
iteration/tool-call machinery is dropped in favor of the one-shot shape.

# Prompt: Phase 2, §4.11 — Bulk state-mutation query project/design-scope audit

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.11 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Eleventh and final item in this session's Phase 2 sequence — §4.1 through §4.10 are done; read their findings docs for the established rigor and format before starting.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.11 (full text, short). The plan names three files as the audit surface (`orchestrator.py`, `autopilot_api.py`, `src/mcp/api.py`) — **all three are stale**, re-verify before trusting anything else in the plan text:

- `autopilot_api.py` and `src/mcp/api.py` no longer exist anywhere in the repo (both predate Phase 1's decomposition).
- `orchestrator.py` exists, but as `src/workflow_engine/orchestrator.py` — a different, unrelated file. The bulk-mutation logic the plan actually means lives in the decomposed `src/autopilot/orchestrator/` package (`engine_client.py`, `state.py`, `policy.py`, `phase_transitions.py`, `features.py`, `worktree_integration.py`). Don't confuse the two — this same naming trap was already flagged during §4.6.

This is explicitly an **audit item, not a single-function consolidation** — the plan's own words. Don't assume every match below is a live bug; some of this surface has already been hardened by prior fixes (see below). Your job is to tell the difference, file by file, not to rewrite everything defensively.

## Freshness check — confirmed as of this handoff, re-verify

**Three historical incidents, same defect class, escalating severity, three weeks apart:**

- `533de2a` (2026-07-13) — whole-project blocking: an unrelated project's paused workflow blocked *this* project's pipeline from starting. Fixed by introducing `_workflow_belongs_to_project(workflow, project_id, project_path)` — now at `src/autopilot/orchestrator/state.py:529` (moved here from the pre-decomposition flat `orchestrator.py` the plan text still references). **Use this as the template helper** — prefers the authoritative `Workflow.project_id` FK, falls back to `Path.is_relative_to()` containment. Explicitly **not** a raw `str.startswith()` prefix match: that form wrongly matched sibling directories (`project-a` matching `project-ab`).
- `f54811f` (2026-07-29) — narrower but still coarse: one in-progress design blocked every other design in the same project.
- `9cb947c` (2026-08-13, most severe) — `/queue/rerun`'s "stop everything" step had **no scoping at all**: `db.query(Agent).filter(Agent.status.in_([...]))` and `db.query(Workflow).filter(Workflow.status.in_([...]))`, unfiltered by project or design. Rerunning any one design terminated every active agent and paused every active workflow system-wide, across every other project and design. Root-caused from a live incident: a healthy `adversarial_review` agent with a complete, correct report was killed mid-review by an unrelated design's rerun.

**Checked directly this handoff — `src/mcp/autopilot/queue_routes.py` (where `9cb947c`'s fix now lives after decomposition) is correctly scoped, not a live bug:**
- `rerun_design` (`queue_routes.py:235`) derives `design_wf_ids` from `Workflow.design_id == design_for_scope.id` (line 363) and filters its stop-everything query to `Workflow.id.in_(design_wf_ids)` (line 390) — `9cb947c`'s fix, intact.
- `requeue_design` (`queue_routes.py:123`) carries an inline comment citing `9cb947c` directly and filters by `req_project_id` when present (line 158-159), plus a basename-equality check on the design document filename (line 171, itself citing `533de2a`'s prefix-match bug as the reason it isn't a substring match). Also scoped, not a live bug.

Don't re-litigate these two — they're evidence the audit will find a mix of already-fixed and still-broken sites, not a blanket rewrite target. Start your own review from the rest of the candidate list.

**Fresh grep this handoff, unfiltered, 24 candidate files** (`grep -rln "Workflow\.status\.in_\|Agent\.status\.in_\|Task\.status\.in_" src/ --include="*.py"`, excluding `queue_routes.py` since it's confirmed clean above):

```
src/agents/launch_pipeline.py
src/agents/manager.py
src/autopilot/orchestrator/engine_client.py
src/autopilot/orchestrator/features.py
src/autopilot/orchestrator/phase_transitions.py
src/autopilot/orchestrator/policy.py
src/autopilot/orchestrator/worktree_integration.py
src/core/status_derivation.py
src/core/worktree_manager.py
src/mcp/agents_api.py
src/mcp/autopilot/control_routes.py
src/mcp/autopilot/feature_routes.py
src/mcp/autopilot/project_routes.py
src/mcp/frontend/_shared.py
src/mcp/server.py
src/monitoring/auto_restart.py
src/monitoring/diagnostic_agent.py
src/monitoring/mechanical_recovery.py
src/monitoring/monitor.py
src/monitoring/orphan_reaper.py
src/phases/phase_manager.py
src/services/queue_service.py
src/services/task_blocking_service.py
src/workflow/termination_handler.py
```

This is a starting point, not a verified list — some of these will be read-only status queries (no scoping concern at all, e.g. dashboard aggregation code deliberately querying across all projects), some will already be correctly scoped, some will be genuinely single-agent/single-task lookups with no cross-project blast radius by construction. Triage before fixing.

**Check §4.2's completed work before assuming a site needs fixing from scratch.** §4.2 (this session, already done — `design_docs/phase2_termination_findings.md`) migrated every raw `agent.status = "terminated"` write to a shared `terminate_agent()` primitive (`engine_client.py`), including sites in `queue_routes.py`, `feature_routes.py`, `project_routes.py`, `launch_pipeline.py`, `server.py`, and others on the list above. **§4.2 fixed the termination-invariant axis (all three fields written together, task reset before agent flip) — it did not audit or fix the project/design-scoping axis** (whether the *query selecting which agents/workflows to act on* is correctly filtered). These are independent bugs that happen to touch the same call sites: a query can correctly terminate-with-all-three-fields an agent it never should have selected in the first place. Don't assume §4.2 closed this gap — verify each site's *selection query*, not its *write mechanics*, which is what §4.2 already covers.

## Target

For every bulk `Agent.status.in_(...)` / `Workflow.status.in_(...)` / `Task.status.in_(...)` query in the 24-file candidate list (as triaged — some will be out of scope, see above) that mutates state (not read-only reporting), confirm it carries explicit scope — `project_id`, `design_id`, or `workflow_id` — matching what the surrounding code's stated intent actually requires. Where a site is missing scope it should have, add it using `_workflow_belongs_to_project()` (`state.py:529`) as the template, extending it or calling it directly as appropriate to the site's own available data (a workflow row already in scope, vs. a project_id/design_id passed into the endpoint).

Where a site queries `Agent` directly (not via a `Workflow` join), determine the correct scoping path from the agent's own FK chain (`current_task_id` → `Task.workflow_id` → `Workflow.project_id`/`design_id`, matching the pattern already used in `queue_routes.py`'s confirmed-clean sites above) rather than inventing a new scoping mechanism per file.

## Verification

- For each site you fix, a regression test that fails against the pre-fix query (an unrelated project's/design's agent or workflow gets swept up) and passes after — matching `9cb947c`'s own regression-test pattern (cited in its commit message).
- For each site you audit and confirm already correctly scoped or correctly unscoped (deliberate cross-project read/report), a one-line note in findings — don't leave silent gaps in the audit trail where a reader can't tell "checked, fine" from "not checked."
- Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Use `git stash push --keep-index -- <file>` isolation per prior items in this session if a failure's origin (your change vs. the concurrent test-fixing session vs. pre-existing) is ambiguous — do not disturb files you don't own.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.10) — in particular, don't re-do §4.2's termination-invariant field migration; this item is about query *selection* scope, not write mechanics.
- Read-only/reporting queries that aggregate across projects by design (e.g. a dashboard or status-summary endpoint deliberately showing all projects) — flag these as reviewed-and-intentional, don't add scoping that would break their actual purpose.
- Phase 3 (confirmed live bugs) and Phase 4 (dead code deletion) — log anything found belonging to those instead of fixing it here.
- Rewriting `_workflow_belongs_to_project()` itself unless you find it has its own bug — this item extends/reuses it, it doesn't redesign it.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or this prompt's own freshness-check guesses (re-verify all of the above yourself, including the two sites this handoff claims are already clean). `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Findings doc (`design_docs/phase2_bulk_scoping_findings.md` or similar) — lead with the triage table (file → read-only/already-scoped/fixed) since that's the actual deliverable of an audit item, not just a diff. No commits — leave everything in the working tree for review.

# Phase 2, §4.11 — Bulk state-mutation query project/design-scope audit findings

## Triage table

Leading with this, not a diff, per this item's own nature — an audit's deliverable is the triage, and there's no code diff to lead with (see Result below).

| File | Sites checked | Verdict |
|---|---|---|
| `src/mcp/autopilot/queue_routes.py` | `requeue_design` (156, 190), `rerun_design` (390) | Already scoped — verified during prompt-doc freshness check, re-confirmed here. `rerun_design` anchors to `design_wf_ids` derived from `Workflow.design_id`; `requeue_design` anchors to `req_project_id` when given, plus basename-equality on the design filename (both citing `9cb947c`/`533de2a` inline). |
| `src/autopilot/orchestrator/engine_client.py` | `pause_project_workflows` (433, 463), `check_phase_sibling_active` (514) | Scoped — `Workflow.project_id == project_id` anchors the workflow query; the agent-termination query is filtered to `workflow_ids` derived from that same anchored set. Sibling check is `phase_id`-anchored. |
| `src/autopilot/orchestrator/features.py` | `_clean_stale_assigned_tasks` (370, 397) | Scoped to `Task.workflow_id == workflow_id` throughout — the function's own single argument. |
| `src/autopilot/orchestrator/phase_transitions.py` | retry/sibling-check sites (265, 323, 473, 552, 1322, 2716, 3221, 3311), active-agent check (2789) | Scoped — every site anchors to `phase_id` or `workflow_id` already in scope from the calling context. |
| `src/autopilot/orchestrator/policy.py` | stale-task cleanup (195) | Scoped to `Task.workflow_id == workflow_id`, the function's own argument. |
| `src/autopilot/orchestrator/worktree_integration.py` | abandoned-workflow candidate check (712) | Scoped to `Task.phase_id.in_(in_progress_phase_ids)`, itself derived from one specific candidate workflow in the enclosing loop. |
| `src/agents/launch_pipeline.py` | duplicate-agent guard (374) | Scoped to `Agent.current_task_id == task.id` — single task. |
| `src/agents/manager.py` | `get_project_context` (1302) | **Read-only, no mutation** — see "Found, out of scope" below; not a bulk-mutation site. |
| `src/core/status_derivation.py` | `derive_design_status` (236) | Scoped to `Workflow.design_id == design_id`, the function's own argument. |
| `src/core/worktree_manager.py` | active-worktree guard (1018) | **Deliberately global by design**, not a bug — this guard must check every project's active/paused workflows to avoid deleting a worktree genuinely in use elsewhere; scoping it to one project would reintroduce the exact race its own inline comment describes fixing. |
| `src/mcp/agents_api.py` | `get_task_progress` (740) | **Read-only status endpoint** — docstring states "all active tasks" is deliberate when no `task_id` given. Not a mutation. |
| `src/mcp/autopilot/control_routes.py` | `get_pipeline_status` (107, 121, 135, 141, 203), zombie-detect (370), cleanup/health report (638, 707) | All read-only reporting/diagnostics, or (370) explicitly `project_id`-anchored with an inline comment citing the exact scoping requirement. No mutation sites found unscoped. |
| `src/mcp/autopilot/feature_routes.py` | pause/resume/redo-guard sites (273, 351, 453, 522, 692) | Scoped to `feature.workflow_id`/`workflow_id` throughout, several with `phase_id` narrowing further. |
| `src/mcp/autopilot/project_routes.py` | design-delete agent termination (1398) | Scoped — `wf_ids` derived from the specific design `d.id` being deleted. |
| `src/mcp/frontend/_shared.py` | `stop_workflow` (2158), `reset_phase` (2262), plus read-only dashboard methods (177, 771, 810, 1374) | `stop_workflow`/`reset_phase` anchor to their own `workflow_id`/`phase_id` argument. The rest are `get_*` read-only dashboard aggregations. |
| `src/mcp/server.py` | resume-scan (653, 772), phase-advancement sweep (1844), workflow stop/cancel endpoints (4796, 4916) | Scoped — resume-scan takes explicit `workflow_id`/`project_id` params (or is a documented startup-wide scan); phase-advancement sweep filters to `AutopilotProject.is_active` project IDs, matching the `concurrent-active-projects` invariant; stop/cancel endpoints anchor to their own path-param `workflow_id`. |
| `src/monitoring/auto_restart.py` | stale-task check (64) | Scoped — per-agent iteration, `filter_by(id=agent.id)` on an agent already selected by the caller's own loop. |
| `src/monitoring/diagnostic_agent.py` | recent-agents query (432) | Scoped to `task_ids` derived from one workflow's own tasks; also read-only (diagnostic report). |
| `src/monitoring/mechanical_recovery.py` | stale-task checks (155, 356, 520, 1329, 1433, 1785), agent lookups (107, 726, 770, 883, 1625) | Scoped — every `Task.status.in_(...)` site is `.filter_by(assigned_agent_id=agent.id)` (or `id=task_id`), a single already-selected agent/task from the enclosing per-agent recovery loop, not a bulk cross-project select. Directly read all 7 sites in full context (not inferred from the grep pattern alone, unlike the first pass) — confirmed during gap-check. |
| `src/monitoring/monitor.py` | diagnostic sweep (668, 688) | **Read-only, log-output only** (`[DIAGNOSTIC]` prefix, no mutation) — not a bulk-mutation site. |
| `src/monitoring/orphan_reaper.py` | orphan detection (67, 85) | **Deliberately global by design** — an orphan has by definition lost a reliable association, so detection must scan every agent/workflow; the actual mutation (`terminate_agent`) is applied per-agent only after individually verifying that specific agent's own task's workflow is inactive, not as a group bulk-select. Not the `9cb947c` pattern. |
| `src/phases/phase_manager.py` | `initialize_workflow` (230), `get_active_agents_count` (2495, scoped), `load_active_executions` (2512) | `get_active_agents_count` scoped to `workflow_id`. `initialize_workflow`/`load_active_executions` implement a "SINGLE WORKFLOW POLICY" that's deliberately global — but see "Found, out of scope" below, this looks like dead code, a Phase 4 question, not a §4.11 scoping bug. |
| `src/services/queue_service.py` | `get_active_agent_count` (154), `get_active_agent_count_for_cli_model` (245) | **Read-only counts**, not mutations. `get_active_agent_count` takes an optional `project_id` with an honest docstring about global-when-omitted behavior; `get_active_agent_count_for_cli_model` is deliberately global (a cli/model resource is a system-wide concurrency budget, not per-project). See "Found, out of scope" below re: the first one's callers. |
| `src/services/task_blocking_service.py` | `sync_blocking_status` (285) | Deliberately global by design — each task's blocked/unblocked state is evaluated against its *own* ticket, independently; there is no group-selection blast radius since the mutation target is always the one task being individually evaluated. |
| `src/workflow/termination_handler.py` | `_terminate_workflow_agents` (133), `get_workflow_termination_status` (400) | Scoped to `Task.workflow_id == workflow_id`, the class's own argument. |

## Result: audited, zero live scoping bugs found

Every candidate site across the 25 files above was individually read in its surrounding call context (not just the matched grep line) and checked against the discriminator the three historical bugs share: **does the mutation's selection query carry an explicit `project_id`/`design_id`/`workflow_id`/`phase_id`/`task_id` anchor, or does it select purely by `.status.in_(...)` with nothing else narrowing which rows are affected?** Every genuine bulk state-mutation site found carries that anchor. No new instance of `9cb947c`'s "stop everything" pattern (an `Agent`/`Workflow` bulk query filtered by status alone) exists anywhere in the current codebase.

This is a real audit outcome, not a shortfall — the plan's own three fix commits (`533de2a`, `f54811f`, `9cb947c`) plus this session's §4.2 termination-primitive migration (which independently touched several of the same call sites for a different reason — write mechanics, not selection scope) had already closed this surface by the time this item started. The value of doing the audit is having verified that directly, file by file, rather than assumed it from the fix history.

## Found, out of scope (per this item's own prompt doc) — flagged, not fixed

- **`src/agents/manager.py:1291`, `AgentManager.get_project_context()`** — queries `Task`/`Agent` with **no project scoping anywhere in the method or the class** (`AgentManager` has no `self.project_id`), then formats the result as a `"## PROJECT STATUS"` string fed into task enrichment and dispatch prompts (`src/services/agent_dispatch_service.py:64`, `src/services/task_enrichment_service.py:102`). This is read-only — no `.status = ` mutation — so it's outside this item's stated target ("bulk state-mutation query"), but it's a real correctness/data-leak bug: an agent's dispatch prompt is told about every other project's active tasks and agents, mislabeled as its own project's status. Worth a Phase 3 entry.
- **`src/phases/phase_manager.py:224` (`initialize_workflow`) and `:2500` (`load_active_executions`)** — implement a literal "SINGLE WORKFLOW POLICY" (only one active/paused workflow system-wide is ever reused), which directly conflicts with the documented `concurrent-active-projects` invariant (multiple concurrent active projects, capped by `max_concurrent_projects`) that the rest of the codebase (e.g. `server.py`'s phase-advancement sweep, confirmed correct above) actually implements. Grepped for callers of both methods — **none found**, in `src/` or `tests/`. Reads as dead code from a pre-multi-project era rather than a live scoping bug. Flagged for Phase 4 (delete confirmed dead code), not fixed here — deleting it isn't this item's job, and CLAUDE.md's rule is to mention pre-existing dead code found, not remove it unasked.
- **`src/services/queue_service.py:139` (`get_active_agent_count`)** — its own docstring says omitting `project_id` "counts globally across every project (original behavior, kept for callers not yet updated)," explicitly flagging that some caller may still be getting a global count where a per-project one would be correct for a concurrency-gate decision. Didn't chase this further: it's a read count feeding a dispatch decision elsewhere, not itself a bulk-mutation query, so it's outside this item's target — but the docstring's own wording suggests an unaudited caller-side gap worth a Phase 3 look.
- **`src/monitoring/orphan_reaper.py:133`, `current_time = datetime.now()`** — a UTC-invariant violation (CLAUDE.md's `utc-only` rule: always `datetime.utcnow()`), used for tmux-session grace-period tracking. Unrelated to project/design scoping (the axis this item audits), not touched here.

## Verification

No code changes were made — the audit found nothing to fix. Ran the existing tests that already cover the primary scoping-history sites, to confirm the "already scoped, still passing" conclusion is a verified fact and not just a read of the code:

```
pytest tests/test_queue_requeue_scoping.py tests/test_termination_invariant_single_writer.py -q
```

4 passed (`test_queue_requeue_scoping.py` — confirms `requeue_design` doesn't pause/terminate across an unrelated project's same-named design, and doesn't match a design merely containing the requeued name as a substring, i.e. `533de2a`'s and `9cb947c`'s regressions stay fixed) plus the full termination-invariant single-writer suite, zero failures.

No new test file was written — there's no regression to characterize when no code changed, and `test_queue_requeue_scoping.py` already exercises the one call site (`requeue_design`) with the least self-evident scoping (basename equality, project_id-conditional filter) at the granularity this item cares about. If a future site is found to regress this audit's "already scoped" conclusion, add a case to that file or a similarly-scoped new one rather than re-running this full manual audit.

## Explicitly out of scope

- The four items in "Found, out of scope" above — logged for Phase 3/Phase 4, not acted on here.
- Anything already shipped (§4.1–§4.10).

No commits — left in the working tree for review. (No files were actually modified by this item — see "Result" above.)

## Gap-check addendum

Reread this item's own prompt doc (`design_docs/phase2_bulk_scoping_prompt.md`) end to end and checked every explicit instruction against what was actually delivered:

- **"lead with the triage table... since that's the actual deliverable of an audit item"** (Quality bar) — the first pass led with a narrative "Result" section instead, table second. Reordered: table now leads, narrative Result moved below it.
- **`mechanical_recovery.py`'s 7 flagged sites** were verified in the first pass by grep-pattern inference (`filter_by(id=agent.id)` visible in the matched-line-plus-a-few-lines grep output) rather than by directly reading each site's full surrounding context the way every other file in the triage table got. Went back and read all 7 in full — confirmed the inference was correct (each is a single already-selected agent/task from an enclosing per-agent recovery loop, not a bulk cross-project select), but this was a real gap in verification rigor, not just presentation, since the prompt's quality bar explicitly calls for "adversarial review against HEAD, not assumptions." Findings table row updated to note the direct re-read.
- Everything else checked clean on reread: the freshness-check facts (stale plan file names, the 24-file grep, `queue_routes.py`'s clean status) were independently re-verified during implementation, not just copied from the prompt; the FK-chain scoping guidance for direct-`Agent`-query sites was applied case by case rather than assumed; the explicitly-out-of-scope boundaries (§4.2's write-mechanics axis, Phase 3/4 material, `_workflow_belongs_to_project()` itself) were respected — the four "found, out of scope" items were logged, not fixed, matching the prompt's own instruction not to chase them here.

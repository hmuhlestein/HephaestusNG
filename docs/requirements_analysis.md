# Product Requirements Analysis: OpenCode Cost Collector

**Feature ID:** des-91c8-opencode-collector
**Feature Name:** OpenCode Cost Collector
**Status:** Requirements Extracted
**Date:** 2026-07-25
**Design Document:** `.hephaestus/design.md` ("OpenCode" subsections, lines 167-221 and 540-577; Implementation Phase 6, lines 695-705)
**Parent Feature:** Cost Tracking Design (DES-91c8) — Budget Enforcement and Cost Tracking UI slices already merged (`a71d84d` and ancestors)

---

## 0. Critical Finding: The Design's Core Premise for OpenCode Is Stale

The design document assumes OpenCode is invoked **one-shot** (`opencode run "$msg" --model X`, no `-i`), and builds its entire OpenCode collection strategy around that: capture `--format json` from stdout, or fall back to querying the OpenCode SQLite DB after the process exits, because "there's no session to wait for."

**That assumption no longer matches the shipped code.** `OpenCodeAgent.get_launch_command` (`src/interfaces/cli_interface.py:465-485`) now launches `opencode run -i --dangerously-skip-permissions --model {model} "$(cat {prompt_file})"` — the `-i` flag was added with an inline comment explaining that bare one-shot mode left the tmux pane at a dead shell prompt before Hephaestus's task message arrived ~25s later, "same bug class as the claude/pi launch." **OpenCode is now a persistent interactive session, architecturally identical in lifecycle to `pi` and Claude Code, not a one-shot process.** The design's "simpler than pi/Claude Code, no session to tail" framing is the opposite of current reality; the stdout-JSON-capture option is not viable at all (a `-i` session never exits after one exchange, so there is no single terminal JSON blob on stdout).

This changes the actual engineering problem from "capture one process's stdout" to "correlate a completed task to the right OpenCode session for cost totals" — much closer in shape to the pi/Claude Code collectors already built, except OpenCode never got the `--session-id`-equivalent wiring pi and Claude Code have.

## 1. Current State (verified against real code and a real OpenCode installation)

- ✅ `CostEntry`/`SessionCostCheckpoint` tables, `record_cost()` rollup helper (`src/core/cost_derivation.py:38`), and `collect_task_cost()` orchestration entry point (`src/services/cost_collection_service.py:404`) are already built and working end-to-end for `pi` and `claude_code`.
- ✅ `collect_task_cost()` already branches on `cli_type == "opencode"` at `cost_collection_service.py:482-485`, but the branch is a bare `pass` — no session file is ever discovered, so `collect_task_cost()` always returns early at line 490 for OpenCode tasks. **OpenCode cost is unconditionally zero today.**
- ⚠️ `OpenCodeCollector` (`cost_collection_service.py:263-323`) is a real, implemented class — but it deserializes `session_file` as a single JSON document (`json.load(f)`) with a top-level `cost`/`modelID`/`tokens` shape, i.e. it implements the *stdout-capture* design that `collect_task_cost()` never actually calls into (no code path ever sets `session_file` for `cli_type == "opencode"`). It is dead code today and, per Finding 0, built for a launch mode (`opencode run` one-shot) the agent no longer uses.
- ❌ `OpenCodeAgent.get_launch_command`/`get_session_args` pass no session identifier at all — the base `CLIAgentInterface.get_session_args` returns `""` and `OpenCodeAgent` doesn't override it (confirmed: no `session_id` reference anywhere in the `OpenCodeAgent` class). Unlike `pi` (`--session-id`, freely create-or-resume) and Claude Code (`--session-id <uuid5>`, landed in an earlier phase per the design doc), there is no deterministic, pre-known identifier this feature can use to look up "the session this task's agent created."
- ✅ **Verified directly against a real `~/.local/share/opencode/opencode.db` on this machine** (SQLite, `session` table): the `session` row itself carries **pre-aggregated** cost and token totals as real columns — `cost REAL`, `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write` — plus `directory TEXT` (the agent's cwd, stored as a literal path, no hashing/sanitization) and `time_created`/`time_updated` (epoch-ms integers). Sample real row: `directory='/Users/hmuhlestein/code/sotto', cost=0.0118441344, tokens_input=44012, tokens_output=6976, model='{"id":"xiaomi/mimo-v2.5","providerID":"openrouter",...}'`. This directly contradicts the design doc's claim that dollar cost requires either stdout capture or querying `message`/`part` rows for per-turn detail — **the `session` table alone has everything `CostEntry` needs, already summed, no per-turn tailing required.**
- ❌ No code anywhere in `src/` references OpenCode's SQLite DB, `opencode export`, or `opencode stats`.

## 2. Larger Project Context

This is the final data-source slice of the Cost Tracking feature family (design doc's Implementation Phases 1-7). Already merged: the `cost_entries`/`session_cost_checkpoints` schema, the `pi` collector, budget enforcement (`cost_limit_usd`, `_pause_project_workflows`, the `paused_by is not None` guard generalization), and the Cost Tracking UI (`ProjectCostSummary`, `FeatureCostBadge`, `DesignCostRow`, `BudgetPausedLabel`, wired into `PipelineStatusCard`/`DesignQueuePanel`). The design doc explicitly gates this phase ("OpenCode collector — gate on actual usage first") on checking whether `cli_type: opencode` is actually configured anywhere live before building — that check is performed in FR1 (§3 below), and **the design's own criterion for "stay deferred" is met**, which FR1 flags as a blocking decision for `scope_review` rather than resolving here. Codex remains an explicit stub/non-goal, out of scope here.

If `scope_review` authorizes proceeding despite the gate: because the rollup/budget/UI machinery already consumes `CostEntry` rows regardless of `source`, this feature's scope is: **produce correct `CostEntry` rows for OpenCode-driven tasks.** Nothing downstream needs to change.

## 3. Functional Requirements

**FR1 — The design's own build/defer gate, checked exactly as specified. [Gate condition for deferral is MET — flagged as a blocking decision for scope_review, not resolved here.]**

The design document (`.hephaestus/design.md:695-699`) states an explicit, unconditional gate for this entire phase of work: *"Before building anything, check `config/workflows/autopilot/workflow.yaml` and any `phase_cli_tool` overrides for whether `cli_type: opencode` is set on any live phase; if nothing in the current deployment uses it, this phase is dead weight and should stay deferred indefinitely rather than land speculatively."*

Checked exactly as specified: `config/workflows/autopilot/workflow.yaml` and every other file under `config/workflows/autopilot/` (`security_review.yaml`, `architectural_review.yaml`, `scope_review.yaml`, `development.yaml`, `adversarial_review.yaml`, `doc_review.yaml`, `product_requirements.yaml`, `architecture_design.yaml`, `qa_validation.yaml`, `product_validation.yaml`, `git_commit_push.yaml`, `forensics_analysis.yaml`) were grepped for `cli_type`/`phase_cli_tool`/`opencode` — **zero matches across all of them.** `hephaestus_config.yaml`'s own `agents.default_cli_tool` is `claude`; `simple_config.py:14`'s hardcoded fallback if that key is absent is `pi`. Neither is `opencode`. **By the design's own stated criterion, this is exactly the "nothing in the current deployment uses it" case it says should stay deferred.**

Additional context, stated but not used here to override the design's criterion: `OpenCodeAgent` is a maintained, non-stub class (`src/interfaces/cli_interface.py`, recently fixed for the `-i` hang bug), and `cli_type` is configurable per-project at runtime (`AutopilotProject.cli_tool`, `src/core/database.py:490`) even though no UI exposes that choice today (`ProjectSettingsModal.tsx` has no `cli_tool` field) — so "in use" is theoretically possible without appearing in any workflow YAML. That's a real fact about the codebase, but it doesn't satisfy the design's check, which asks specifically about workflow YAML / `phase_cli_tool` overrides, not about theoretical reachability.

**This is a direct conflict this requirements pass is not positioned to resolve unilaterally:** the design says defer; this feature was nonetheless explicitly commissioned as its own workflow ("OpenCode Cost Collector"). Whoever scoped this workflow may have information not visible in this repo (e.g. a deployment target where `cli_type: opencode` genuinely is configured) that justifies overriding the design's gate — but that's a scope decision, not a requirements-extraction one. **Flagged for `scope_review` (Phase 2) to explicitly rule on** before FR2-FR5 below are authorized to proceed. FR2-FR5 describe *how* to build the collector correctly, contingent on that gate being explicitly overridden by scope_review — they are not, themselves, a decision to build.

**FR2 — Correlate a completed task's agent to its OpenCode session row.**
No deterministic session ID exists for OpenCode (Finding 0). Acceptance criteria for the correlation mechanism:
- Match `session.directory` to the agent's cwd (via the same `_get_agent_cwd` helper already used by the `pi`/`claude_code` branches at `cost_collection_service.py:454`, `:459` — no path sanitization needed here since `session.directory` is stored as a literal path, unlike pi's/Claude Code's dash-mangled directory names).
- Narrow by a time window bounded by `Agent.created_at` (lower bound) and task-completion time (upper bound, i.e. "now" at `collect_task_cost()` call time) against `session.time_created`.
- Handle the case of zero matches (log and skip, matching the existing `pi`/`claude_code` "no session file found" debug-log-and-return pattern at line 490) and multiple matches (document the chosen tie-break, e.g. most recent `time_created` in-window) explicitly — don't leave this ambiguous, since a wrong pick silently attributes cost to the wrong task.
- Whether OpenCode's `-s`/`--session` flag can mint a *new* session under a caller-chosen ID (unresolved in the design doc, `-s` docs describe it as "continue," not "create") is worth one real test before committing to time-window matching as the permanent mechanism instead of a `pi`-style deterministic ID — if `-s` does support create-with-ID, that's a strictly better fix (same shape as the Claude Code UUID5 fix already landed) and should be preferred.

**FR3 — Replace the stdout-JSON `OpenCodeCollector` with a `session`-table query.**
Per §1, the existing `OpenCodeCollector.collect()` deserializes a `session_file` as a single JSON blob — that shape doesn't exist under `-i` mode. Rewrite it to query the OpenCode SQLite DB's `session` row (matched per FR2) and map its already-aggregated columns directly onto `CostEntry` fields: `cost` → `cost_usd`, `tokens_input/output/reasoning/cache_read/cache_write` → the matching `CostEntry` token columns, `model` (JSON string — parse `id`/`providerID`) → `CostEntry.model`. No new schema needed; `CostEntry.source="opencode"` already exists.

**FR4 — Wire the `collect_task_cost()` opencode branch to actually run.**
Replace the `pass` at `cost_collection_service.py:483-485` with real DB-path resolution (`~/.local/share/opencode/opencode.db`, matching the `Path.home() / ...` pattern the `pi`/`claude_code` branches already use) and the FR2 correlation query, then hand off to the rewritten collector from FR3.

**FR5 — Checkpointing / re-collection safety.**
Unlike `pi`/Claude Code transcript tailing, there is no natural "byte offset" — the `session` row's `cost` is a running total for that session's lifetime, not a per-turn delta. Since `OpenCodeAgent` never resumes a prior session (no session-ID flag passed at all, so every launch mints a fresh `session` row — confirmed by the absence of `get_session_args` override), each session row corresponds to exactly one agent launch, so `SessionCostCheckpoint`'s existing "already collected this session" guard (keyed by whatever ID FR2 settles on — see the open question in FR2 about whether a deterministic ID becomes available) is sufficient to prevent double-counting on collector re-runs, without needing partial/delta collection logic.

## 4. Non-Functional Requirements

- **No new tables/columns.** `CostEntry`, `SessionCostCheckpoint`, and the rollup chain are unchanged; this is purely a new data-source implementation plugging into existing sinks (matches the design doc: "No schema changes needed beyond what phase 1 already added").
- **Read-only access to `opencode.db`.** Never write to OpenCode's own database; open it read-only (e.g. SQLite URI `file:...?mode=ro`) to avoid any risk of corrupting a file another live OpenCode process may have open (the DB was observed with active `-wal`/`-shm` files, i.e. WAL mode, on this machine — concurrent read access is safe under WAL, writes are not this feature's business).
- **Path safety.** Match the existing `pi`/`claude_code` branches' pattern of resolving and verifying the target path stays under the expected base directory (`~/.local/share/opencode/`) before opening — same defensive pattern already applied to `_discover_session_file` (lines 360-383) and the Claude Code branch (lines 461-481).
- **Graceful absence.** If `~/.local/share/opencode/opencode.db` doesn't exist (OpenCode never run on this host) or the query returns nothing, skip silently (debug log), matching every other collector's existing behavior — never raise into `collect_task_cost()`'s caller.
- **No timer-based collection.** Same as every other collector: triggered once, from `collect_task_cost()` at task completion, not polled.

## 5. Component Dependencies

- `src/services/cost_collection_service.py` — `OpenCodeCollector.collect()` (rewrite), `collect_task_cost()`'s `opencode` branch (implement), possibly a new `_discover_opencode_session()` helper mirroring `_discover_session_file()`.
- `src/core/cost_derivation.py` — `record_cost()` consumed as-is, no changes expected.
- `src/core/database.py` — `Agent.created_at`, `Agent.cli_type`, `AgentWorktree`/`Workflow.working_directory` (via `_get_agent_cwd`, reused as-is) for FR2's cwd lookup.
- `src/interfaces/cli_interface.py` — `OpenCodeAgent` — only touched if FR2's investigation into `-s`/session-ID minting concludes a launch-command change is warranted; otherwise read-only reference.
- External: `~/.local/share/opencode/opencode.db` (OpenCode's own SQLite store, schema owned by the `opencode` CLI, not this codebase — a version bump of `opencode` could change this schema without warning; no migration/versioning hook exists for that today and none is proposed here beyond graceful-failure on unexpected shape).

## 6. Technology Constraints

- Python's stdlib `sqlite3` (already a transitive dependency via SQLAlchemy) is sufficient — no new package needed to read OpenCode's DB.
- Must not introduce a dependency on the `opencode` CLI binary being installed/on `PATH` at collection time (only at agent-launch time, which is a separate, pre-existing requirement) — direct DB reads mean collection works even if `opencode` itself isn't invoked again.
- Follows this codebase's existing multi-collector `ABC`/subclass pattern (`CostCollector`) — no new abstraction warranted for one additional source.

## 7. Integration Points

- `collect_task_cost()` is already called from task-completion handling (per the design doc, "on task completion... where the codebase already does end-of-task bookkeeping") — this feature adds a working implementation behind an existing, already-wired call path; no new call sites.
- Downstream consumers (`cost_derivation.py` rollups, budget enforcement, all Cost Tracking UI components) require no changes — they already treat `source="opencode"` as a valid, first-class value.

## Open Questions — Blocking Item for scope_review

0. **[BLOCKING] FR1's gate conflict must be explicitly ruled on before architecture starts.** The design document's own build/defer criterion (`design.md:695-699`) is unambiguous, and this pass's direct verification (grepping every file under `config/workflows/autopilot/` plus `hephaestus_config.yaml`'s `default_cli_tool`) confirms the deferral condition as the design defines it — zero live usage of `cli_type: opencode`. This feature was nonetheless commissioned as its own workflow. `scope_review` must explicitly decide: (a) proceed anyway, on the basis that this workflow's existence is itself the authorization to override the design's gate, or (b) treat the gate as still binding and return/close this feature without an architecture phase. This is not a decision the product_requirements phase should make on scope_review's behalf.

## Open Questions for Architecture Phase (contingent on scope_review authorizing FR2-FR5)

1. Does OpenCode's `-s <id>` flag support minting a **new** session under a caller-chosen ID (making time-window correlation unnecessary, mirroring the Claude Code UUID5 fix), or only resuming an existing one? One live test resolves this and should happen before locking in FR2's time-window design.
2. Tie-break policy when FR2's directory+time-window match returns multiple candidate sessions (e.g. two OpenCode tasks launched back-to-back in the same worktree within the same second).

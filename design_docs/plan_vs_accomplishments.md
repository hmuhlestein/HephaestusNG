# Plan vs. accomplishments — `AUTOPILOT_REFACTOR_PLAN.md`, 2026-08-15 → 08-19

A five-day span, 40 commits, five phases. This is a scorecard: what the plan
said would happen, section by section, against what the code and tests show
actually happened — not a re-audit (that's
[per_phase_correctness_review.md](per_phase_correctness_review.md)), but the
completion picture.

**Headline number:** the plan itself contains 9 `**Done**` markers, 5
`**Corrected**` markers, and 5 `**Verified**` markers — i.e. roughly a third
of the time a phase was marked complete, a later pass found the completion
claim itself needed fixing. That ratio is the plan's most important property:
it did not coast on its own status markers, and neither did this review.

---

## Phase 0 — Safety net: **done, exactly as scoped**

| Planned | Delivered |
|---|---|
| Route-count/path-set guardrails for every router the plan would split | All three exist (`server`, `frontend`, `autopilot` routers), pinned to a hardcoded pre-split baseline |
| Characterization tests for the three primitives Phase 1/2 would touch | Termination invariant, worktree-removal safety, task-creation-claim triad — all written before the corresponding consolidation |
| A held-out smoke script | `scripts/smoke_run_b.sh`, full 10-phase pipeline, re-run after every phase per §8 |

No corrections needed here on re-verification. The one property worth naming:
all three guardrails assert *no route dropped*, not *exact match* — a
deliberate choice (a strict-equality guardrail goes permanently red the first
time a route is legitimately added) that held up under the correctness
review.

## Phase 1 / 1b / 1c — Decomposition: **done, one god-object added mid-plan**

The plan named two files at the start (`orchestrator.py`, `autopilot_api.py`
via `backend_module_decomposition.md`), found four more once the pattern
proved out (§3.2, "Phase 1b": `api.py`, `create_agent_for_task`,
`MonitoringLoop`, `task_completion_service.py`), and then discovered the plan
itself had missed the largest file in the repository — `src/mcp/server.py`,
6,052 lines, added as "Phase 1c" on 2026-08-18, from a gap-audit finding, not
from the original plan.

| Split | Before | After |
|---|---|---|
| `orchestrator.py` | 10,246 lines, flat | `orchestrator/` package, 170 symbols across 8 modules |
| `autopilot_api.py` | 5,724 lines, flat | `mcp/autopilot/`, 63 routes across 6 `include_router` calls |
| `api.py` | 3,225 lines, flat | `mcp/frontend/` |
| `MonitoringLoop` | ~2,050-line class | 5 collaborators |
| `task_completion_service.py` | 1,125 lines | `task_completion/` package |
| `AgentManager` (went beyond §3.2's original scope — the whole class, not just `create_agent_for_task`) | 3,430 lines, 48 methods | `manager.py` (698 lines) + `launch_pipeline.py` (2,145) + `terminator.py` (360) + `output_capture.py` (748) |
| `server.py` (Phase 1c, not in the original plan) | 6,052 lines, flat | `mcp/server/`, 12 modules, 128 symbols |

Every split was re-verified this week with a systematic symbol-drop diff
against the pre-split commit, not trusted from the plan's own "Done" markers:
546 pre-split symbols total, two apparent drops, both confirmed as
intentional relocations (`terminate_agent_direct` became an alias, not a
`def`; `fire_spec_gate_if_ready` moved modules exactly as directed). **Zero
unaccounted symbol loss across all seven splits.**

**Where the plan corrected itself, and why it matters:**
- §3.1's "Exception 2" (`phase_transitions.py` — decompose-and-deduplicate
  together) was planned but **not executed as scoped**: the module shipped as
  a pure zero-behavior-change move, and the plan caught this itself on
  2026-08-16 by reading the shipped code rather than trusting the commit
  message. The claim triad it was supposed to consolidate became Phase 2
  §4.1's opening item instead.
- The `manager.py` split initially reported as complete (`87a221f`) had
  silently dropped 244 lines of five messaging/context methods — found by the
  gap audit, not by the split's own review, with 25 call sites that would
  have raised `AttributeError` in production. Restored.
- `output_capture.py`'s extraction was reported done but was never actually
  wired: `AgentManager.__init__` never constructed an `AgentOutputCapture`
  instance, and `manager.py` kept full duplicate inline copies of all 9
  transcript-capture methods — one of which had a bugfix (`chrome_re`
  filtering) the "extracted" copy never received. Fixed same week.

## Phase 2 — Consolidation (§4.1–§4.11): **10 of 11 items done, none of the "Done" markers taken at face value**

| Item | Planned | Delivered |
|---|---|---|
| §4.1 Task-creation-claim | One primitive replacing 5 hand-copies | `_clear_stale_task_creation_claim`/`reset_stale_executions_on_goto`, wired at 4+3 sites. **A fourth copy-family found this week, outside this item's scope** (phase-reopen semantics — deliberately not merged, differs materially per site) |
| §4.2 Agent-termination | One primitive, `kill_tmux: bool` flag | Two-collaborator split (better than planned); `kill_tmux` flag shipped dead and silently ignored, **removed this week** |
| §4.3 Dispatch reconciliation | Unify 3 dispatch paths | `check_phase_sibling_active` shared guard; found and confirmed-still-necessary 3 *other* duplicate-dispatch guards the consolidation didn't obsolete |
| §4.4 Worktree/merge primitives | One merge primitive, abort-and-preserve | `merge_shared_branch()`, confirmed the one true strategy |
| §4.5 tmux delivery | Route through `AgentMessenger` | Done |
| §4.6 SOLID-sourced (status derivation) | Wire 4 named reimplementations through `status_derivation.py` | 3 of 4 wired; the 4th (`run_design_aggregate`) **traced this week and confirmed a deliberate different-inputs duplication, not a live divergence** — left unwired on purpose |
| §4.7 Embedding unification | Share one provider instance, fix dimension mismatch | Fixed a real production bug in the process (RAG was returning empty results silently); an ordering bug in the "Done" fix itself (embedding_service was `None` at RAGSystem construction time) found and corrected the same week |
| §4.8 Pause-state primitive | One primitive, validated `reason` | `pause_workflow`/`resume_workflow` built; found the primitive's own cascade re-introduced the exact bug class it existed to prevent (paused a terminal Feature), fixed; **`reason` validation was still missing as of this week's review — added** |
| §4.9 Output/schema resolver | XL, `PhaseOutputResolver` class | Scope turned out narrower — 2 of 3 parts already existed as fallout from an earlier fix; consolidated the one real gap (path resolution) without inventing an unneeded class |
| §4.10 MCP tool-name registry | One declaration for 3 name surfaces | `MCPToolSpec`/`MCP_TOOL_REGISTRY`; found and fixed 2 more live "Unknown tool" bugs beyond the one named in the plan |
| §4.11 Bulk state-mutation scoping | Audit item | **Zero live scoping bugs found** — the three historical incidents had already been closed by earlier fixes; verified rather than assumed |

Two corrections landed this week that the plan itself hadn't caught:
`terminate_agent`'s dead `kill_tmux` parameter, and `pause_workflow`'s
unvalidated `reason` (which mattered because every consumer compares
`paused_by` against exact literals — an unvalidated typo would have silently
defeated every one of them at once).

## Phase 3 — Live bugs (Tiers 1–3): **done**

Marked complete in the plan (`88646e0`); this week's review spot-checked the
classes most likely to regress rather than re-deriving the tier list:

- SQLAlchemy column-truthiness bugs: zero live instances (the Tier 1 fixes
  held).
- `get_agent_branch_path`'s silent main-repo fallback: still returns `None`
  correctly.
- UTC invariant: one exception found this week — `state.py`'s `saved_at`
  field, a DB-persisted timestamp written in local time, the same shape as
  the `prompt_human` bug this project already treats as an invariant. Zero
  readers today, fixed anyway rather than left for the next comparison to
  hit it.

## Phase 4 — Dead code deletion: **done, three of twelve bullets were wrong before execution**

Not started when this week's correctness review began; executed same day
(2026-08-19), immediately after the review caught that three bullets would
have broken the build if run as originally written:

- `TrajectoryContext` — the plan called it "unreachable"; it was dead
  *state*, still imported and constructed by `monitor.py` (and, one hop
  further, passed into `GuardianDispatcher`). All three sites removed
  together.
- `api_get`/`api_post` — the plan lumped them as one dead pair in a file that
  no longer exists (`orchestrator.py`). `api_post` had two live callers in
  the queue repair path. Split: `api_get` deleted, `api_post` kept.
- `EmbeddingService` type-hint location was stale post-Phase-1c (moved with
  the file split).

**One item the plan called dead code but wasn't**, found during execution
rather than review: `SteeringIntervention` has zero DB writers, exactly as
the plan said — but its read endpoint is live, polled every 10s by a real,
working (if permanently empty) frontend dashboard card. Deleting the backend
would have 404'd real requests. Asked the user directly rather than assumed;
left alone.

All ten other items landed as their own commits, each independently
re-verified against current HEAD rather than trusted from the plan's
original grep evidence — the same discipline the plan asked for and the same
discipline that caught the three wrong bullets in the first place.

## §7 Out of scope: **held**

None of the seven explicitly-deferred items (human-input DB table, WS/SSE
streaming, DB-backed `PipelineState`, `OrchestratorLogger` split, queue
unification, v2-horizon items, the 16 process-lifecycle findings) were
touched, and none needed to be — no dependency from any executed phase
reached into them.

---

## What the delta actually shows

The plan's own self-correction rate (roughly 1 in 3 "Done" markers needed a
later correction) is not a sign the plan was poorly written — the corrections
came from the plan's own discipline of re-verifying against shipped code
before trusting a commit message, the same discipline this week's review
applied one layer up. Three concrete patterns recur across almost every
phase:

1. **A consolidation's own cascade re-introduces the exact bug class it was
   built to prevent** (§4.7's ordering bug, §4.8's terminal-feature re-pause,
   this week's `kill_tmux`/`reason` gaps). Building the primitive is not the
   same work as auditing every path that reaches it.
2. **"Zero callers" and "unreachable" are claims about *symbols*, and the
   code that breaks when they're wrong is about *state*** (`manager.py`'s
   silently dropped methods, `output_capture.py`'s never-wired construction,
   `TrajectoryContext`'s import-but-never-read pattern, Phase 4's `api_post`).
   A grep for the name is not the same check as tracing whether anything
   still constructs or imports it.
3. **The plan corrects itself fastest when it re-reads the shipped code
   instead of the commit message**, and slowest when a claim goes
   unchallenged because it "sounds done." Every correction in this document
   was found that way, including the two this week's session added.

## By the numbers

- **40 commits** carrying `Phase`/`autopilot` refactor work, 2026-08-15 → 08-19.
- **546 symbols** moved across 7 file splits, 2 relocations initially
  misread as drops, 0 actual losses.
- **2,568 tests** currently collected (167 files) — up from whatever existed
  before Phase 0 required characterization tests as a precondition for every
  later phase.
- **1 failed → 0** on the full suite this week, after root-causing an
  import-order test-isolation bug that had the test silently opening the
  production database on every full-suite run (see
  [per_phase_correctness_review.md](per_phase_correctness_review.md)).
- **9 `Done` / 5 `Corrected` / 5 `Verified`** markers in the plan text
  itself — the paper trail of a document that kept checking its own work.

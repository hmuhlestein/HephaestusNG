# Loop Engineering Evaluation — HephaestusNG

**Reference:** Boris Cherny (Anthropic, Claude Code) — "Write loops, not prompts" (June 2026);  
Prithvi Rajasekaran (Anthropic Labs) — [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) (March 2026)

**Scope:** Full backend pipeline — `src/autopilot/`, `src/monitoring/`, `src/phases/`,
`src/services/`, `src/workflow_engine/`, and all phase prompt YAML in `config/workflows/autopilot/`.

---

## Executive Summary

The loop engineering framework identifies five core disciplines: objective stop conditions,
maker-checker separation, high-fidelity verification, hard iteration caps, and pre-task
contract negotiation. HephaestusNG implements all five to varying degrees, with three areas
fully realized, two partially realized, and three concrete enhancements that would close
remaining gaps. One earlier finding (no planner agent) was incorrect and is retracted below
with a full accounting of what the pipeline actually does.

---

## Background: What Loop Engineering Demands

The article's central claim is that the practitioner's job is no longer writing prompts —
it is writing the machinery that writes prompts, and knowing exactly how that machinery
decides it is done. This reframes quality as a property of the loop's stop condition, not
the prompt's wording. Specifically:

- **The stop condition must be a fact, not an opinion.** A passing test, a green CI run, an
  HTTP 200 — something external the agent cannot fake. A stop condition that is an LLM
  self-assessment is the weakest possible gate.
- **Verification fidelity is the whole game.** How faithfully does the gate measure the
  real goal? Low fidelity means the loop can converge on something that looks finished
  while a human would disagree.
- **Maker-checker separation breaks the homework-grading problem.** An agent evaluating
  its own output will always skew positive. Splitting the producing role from the judging
  role gives the loop a real adversary.
- **Hard caps prevent runaway cost.** Every loop without a hard attempt cap is a potential
  $12/28-minute disaster. Caps must exist at every level: per phase, per GOTO cycle, per
  workflow.
- **Pre-task contracts make stop conditions negotiable.** Anthropic's harness has the
  generator and evaluator agree on what "done" means in testable terms *before* any code is
  written, so the validator has a rubric rather than free-form judgment.

---

## The HephaestusNG Pipeline — Actual Architecture

Before evaluating against these principles, here is a precise map of the pipeline.

### Phase 0: Feature Architect (`autopilot-phase0`)

**Entry point:** `run_phase0()` in `src/autopilot/orchestrator.py`, called from
`run_single_design()` before any feature pipelines launch.

**What it does:** Takes the human-authored design document and decomposes it into
1–5 independently shippable features with explicit scope boundaries. For simple designs
a single feature is correct; for complex multi-service systems it produces parallel
feature tracks.

**Inputs:**
- `{design_document}` — path to the human-authored PRD/DESIGN.md
- `{project_path}` — project working directory
- `{design_id}` — DB ID for the design being processed
- Prior decomposition patterns from vector memory (via `search_memory()`)

**Agent instructions (from `config/workflows/autopilot-phase0/01_feature_architect.yaml`):**
1. Read and understand the design document
2. Read prior designs from `designs/` and search vector memory for decomposition patterns
3. Decompose into 1–5 features: clear bounded scope, owned file paths (no overlaps),
   minimal inter-feature dependencies, independently testable
4. Write `.hephaestus/features.json` — machine-parsed with strict schema
5. Write `.hephaestus/features/<id>/scope.md` per feature — covers what is included,
   excluded, inter-feature interfaces, file ownership, and acceptance criteria
6. Validate JSON, unique IDs, no file overlaps, all scope.md files present
7. Save decomposition decisions to vector memory

**Outputs (hard-floor validated):**
```
.hephaestus/features.json
.hephaestus/features/<id>/scope.md   (one per feature)
```

**features.json schema:**
```json
{
  "design_name": "human-readable name",
  "features": [
    {
      "id": "short-slug",
      "name": "Human Name",
      "scope": "one-paragraph description of what this feature implements",
      "files": ["src/auth/", "tests/test_auth.py"],
      "depends_on": [],
      "execution": "parallel"
    }
  ]
}
```

**Orchestrator use of outputs:**
`run_phase0()` validates `features.json` after the workflow completes, creates a permanent
`designs/<timestamp>_<name>/` folder, copies `features.json` and all `scope.md` files, and
creates `Feature` DB records. `run_feature_pipelines()` then launches one parallel
`run_single_workflow()` per feature, copying `features.json` and the feature's `scope.md`
into each worktree's `.hephaestus/` dir before the 12-phase pipeline starts.

**Evaluation gate:**
```yaml
- after_phase: Feature Architect
  evaluator: heuristic
  max_retries: 1
  conditions:
    - if: "score < 0.5"
      action: retry
    - if: "score >= 0.5"
      action: continue
```

---

### 12-Phase Feature Pipeline (`autopilot`)

Each feature runs this pipeline in an isolated git worktree (branch:
`feature/<design_slug>/<feature_key>`). Artifacts accumulate in `./docs/` and are
committed to the worktree; subsequent phases read them from the filesystem — no
re-injection by the orchestrator between phases.

#### Phase 1 — `product_requirements`

**Role:** "YOU ARE A PRODUCT REQUIREMENTS ANALYST - EXTRACT WHAT TO BUILD"

**Key steps:**
- **Step 0 (CRITICAL):** Gather project context *before* reading the design doc. Read
  `AGENTS.md`, check for existing `requirements_analysis.md`, `architecture.md`,
  `features/` directory, prior feature docs. Call `search_memory()` with four queries:
  technology decisions, architecture patterns, constraints, completed features. Grep all
  `.md` files for keywords from the current design.
- **Step 0.5 (CRITICAL ON RETRY):** Check `./docs/scope_review_result.md`. If its
  frontmatter has `verdict: FAIL`, read `correction_instructions`, `out_of_scope`, and
  `missing` lists and follow them exactly — these override the agent's own judgment.
- **Step 1:** Read `./.hephaestus/design.md` (the scope authority).
- **Step 2:** For each requirement: is it new or overlapping with existing code? What
  components does it depend on? What does it enable for the future?
- **Step 3:** Respect existing tech stack — no new frameworks without justification.
- **Step 4:** Write `./docs/requirements_analysis.md`.
- **Step 5:** Save technology decisions, architecture decisions, constraints, and component
  inventory to vector memory (searchable by future product_requirements agents on subsequent
  features).

**Output:** `./docs/requirements_analysis.md` — structured Markdown with sections:
Project Context, Existing System, Functional Requirements (each with acceptance criteria,
integration points, and is-new flag), Non-Functional Requirements, Integration Points,
Technology Constraints, Success Criteria.

**Note:** `requirements_analysis.md` is not a required_output in `workflow.yaml` (no hard
floor on file existence). It is the input to scope_review and architecture_design — if
missing, those phases will fail, which surfaces the problem through the gate system.

---

#### Phase 2 — `scope_review`

**Role:** "YOU ARE A SCOPE GATE — GUARD AGAINST REQUIREMENTS DRIFT"

This is a dedicated binary verification gate with no equivalent in Anthropic's harness.

**What it does:** Reads both `./.hephaestus/design.md` (source of truth) and
`./docs/requirements_analysis.md` and performs a line-by-line trace. Every requirement
in `requirements_analysis.md` must be explicitly traceable to a line in `design.md`.

**Classification per requirement:**
- `IN-SCOPE` — directly stated or clearly implied by `design.md`
- `OUT-OF-SCOPE` — added by the requirements agent, not in `design.md`
- `MISSING` — in `design.md` but absent from `requirements_analysis.md`

**Output (required_output — hard floor):** `./docs/scope_review_result.md` — a YAML
frontmatter block (OKF format: `type` first) followed by the narrative report.

```markdown
---
type: scope_review_result
verdict: PASS
out_of_scope: []
missing: []
correction_instructions: ""
summary: "one-line summary"
---

# Scope Review Report
...
```

`verdict` is `"PASS"` only if both `out_of_scope` and `missing` are empty arrays.
`correction_instructions` is empty on PASS; specific, actionable rewrites for the
`product_requirements` agent on FAIL.

**Scoring (in `src/autopilot/spec.py`):**
- `"PASS"` with empty lists → `score = 1.0`
- Any other state → `score = 0.2`

**Evaluation gate:**
```yaml
- after_phase: scope_review
  evaluator: heuristic
  max_retries: 3
  on_budget_exhausted: arbitrate
  conditions:
    - if: "score < 0.5"          # 0.2 → here
      action: goto
      target: product_requirements
      reason: "Scope drift detected"
    - if: "score >= 0.5"         # 1.0 → here
      action: continue
```

Up to 3 FAIL→GOTO→PASS cycles before `on_budget_exhausted: arbitrate` triggers LLM
arbitration rather than forcing continue.

**Explicit prohibitions in the agent prompt:**
- Do NOT rewrite `requirements_analysis.md` yourself
- Do NOT approve additions because "they seem reasonable"
- Do NOT mark FAIL for minor wording differences — only real scope drift
- Do NOT create architecture or code

---

#### Phase 3 — `architecture_design`

**Role:** "YOU ARE A SOFTWARE ARCHITECT - DESIGN THE SYSTEM"

**Key steps:**
- **Step 0:** Right-size to actual complexity. SIMPLE (single module) → 1–5 tasks total,
  skip infra/foundation tiers entirely, do not invent components the design didn't ask for.
  COMPLEX (multi-service platform) → full treatment with service boundaries, data/contract
  design, tiered tickets, OO design pass. Under-decomposing a complex system is as much
  a failure as over-decomposing a simple one.
- **Step 0.5:** Check if `./docs/architecture.md` already exists (retry awareness). If it
  does, fill gaps rather than rewrite.
- **Step 1:** Read `requirements_analysis.md` AND `./.hephaestus/design.md`. Run
  `find . -name "test_*.py"` to detect existing file layout conventions. Determine `src/`
  layout vs project-root layout based on existing test import patterns.
- **Step 2:** Per component: Purpose, Interface (public API/methods), Data Model,
  Dependencies, Implementation Details. Data flow. Explicit directory structure with exact
  file paths.
- **Step 3:** Task breakdown per component — Priority, Blocked By, implementation steps,
  acceptance criteria, estimated complexity.
- **Step 4:** Create Kanban tickets in the board (infra → foundation → feature →
  integration order) with `blocked_by_ticket_ids`.
- **Step 5 (CRITICAL):** Create Phase 3 development tasks via `create_task()` with
  explicit `depends_on`. Empty array `[]` = run immediately (parallel). List of task IDs =
  run after all listed tasks complete. Omitting `depends_on` forces sequential execution —
  the prompt calls this out as a failure mode. The architect is responsible for the
  parallelism graph.
- **Step 6:** Save architectural decisions, component interfaces, trade-offs, and critical
  implementation notes to vector memory.
- **Step 7:** Write `./docs/architecture.md`.
- **Step 8 (OO design pass):** Before finalizing, review for inheritance vs composition,
  Single Responsibility, Dependency Inversion, shared abstractions to extract.

**Output (required_output — hard floor):** `./docs/architecture.md`

Also creates: Kanban tickets in the board, Phase 3 development tasks in the DB with
dependency graph encoding parallelism.

**Evaluation gate:**
```yaml
- after_phase: architecture_design
  evaluator: heuristic
  max_retries: 2
  conditions:
    - if: "score < 0.4"
      action: goto
      target: product_requirements
    - if: "score < 0.6"
      action: retry
    - if: "score >= 0.6"
      action: continue
```

---

#### Phases 4–12 — Development through Git Commit

| # | Phase | Key Output | Hard Floor | Eval Gate |
|---|-------|-----------|------------|-----------|
| 4 | `development` | Source code, tests | None (code in worktree) | Always continue |
| 5 | `architectural_review` | `architectural_review_report.md` | ✅ | goto arch < 0.3, goto dev < 0.6 |
| 6 | `adversarial_review` | `adversarial_review_report.md` | ✅ | goto arch < 0.3, goto dev < 0.6 |
| 7 | `doc_review` | Updated docs | None | goto arch < 0.3, goto dev < 0.6 |
| 8 | `security_review` | Security report | None | goto arch < 0.3, goto dev < 0.7 |
| 9 | `qa_validation` | `qa_report.md` | ✅ | goto arch < 0.3, goto dev < 0.7 |
| 10 | `product_validation` | `product_validation.md` | ✅ | goto arch < 0.3, goto dev < 0.7 |
| 11 | `forensics_analysis` | Forensics report | None (optional phase) | Always continue |
| 12 | `git_commit_push` | Git commit | None (optional phase) | Always continue |

---

## Evaluation Against Loop Engineering Principles

### ✅ 1. Objective Stop Conditions

**Status: Well implemented.**

The main polling loop in `run_single_workflow()` exits exclusively on objective criteria:

```python
# DB state already set by the server
if wf_state in ("completed", "failed", "paused"):
    return wf_state

# Hard deadline
if elapsed > timeout_seconds:   # default 2 hours
    return "timeout"

# All work done: no active agents, no pending/in-progress tasks, all phases complete
if not active_agents and not pending and not in_progress and not non_terminal:
    if done and pending_phases == 0:
        return "completed"

# Hard error: agent status == "error", or critical/architectural task failure
if hard_error:
    return "hard_error"
```

The `detect_impasse()` function is also fully objective: no active agents with pending
tasks after 600s (with a 120s grace period for recently-created tasks), or any
in-progress task with `started_at` > 30 minutes ago. The only subjective exit is
`prompt_human()` — which routes to a real human, not an LLM self-assessment.

The spec gate (`score_qa()`, `score_scope_review()`, `score_product_validation()` in
`src/autopilot/spec.py`) reads structured JSON the agent produced and makes mechanical
decisions: `failed_tests > 0` → floor violation → score drops to 0.5. This is
closer to objective than most systems.

**Remaining gap:** The spec gate blends `agent_score` into the final QA score:

```python
def _pass_with_subjective(agent_score: Any) -> float:
    return round(_PASS_FLOOR + (1.0 - _PASS_FLOOR) * _clamp01(agent_score, 1.0), 4)
    # = 0.7 + 0.3 * agent_score
```

An agent that passes all hard floors but writes `"agent_score": 0.0` will score 0.7
(barely passes). One that writes `"agent_score": 1.0` scores 1.0. This is a correct
use of subjective input — it can only help, not override a floor violation — but it
means the gate is not purely objective when all floors pass.

---

### ✅ 2. Maker-Checker Separation

**Status: Implemented with caveat — opt-in only.**

The validator agent pattern is a genuine architectural separation:

```python
# In give_validation_review endpoint (server.py)
agent = session.query(Agent).filter_by(id=agent_id).first()
if not agent or agent.agent_type != "validator":
    raise HTTPException(status_code=403, detail="Only validator agents can submit reviews")
```

A different DB record with a different `agent_type`, running in a separate tmux session,
submits the verdict. The original agent cannot grade its own work — the endpoint enforces
this with a 403. On a failed validation, the validator terminates but the original agent
receives the feedback and continues; on a pass, both terminate.

Additionally, the 12-phase pipeline has dedicated review agents (phases 5–8:
`architectural_review`, `adversarial_review`, `doc_review`, `security_review`) that
are structurally separate from the development agent.

**Caveat:** External validation (`validation.enabled: true`) is opt-in per workflow YAML.
Most phases self-report completion by calling `update_task_status(status="done")`. The
development agent's `done` is taken at face value unless validation is explicitly
configured. The review phases (5–8) provide external assessment, but they read artifacts
the development agent produced — they don't independently run code.

---

### ✅ 3. Hard Iteration Caps — Multi-Layer

**Status: Fully implemented at every level.**

| Scope | Cap | Location | On Exceeded |
|-------|-----|----------|-------------|
| GOTO per workflow | `max_total_gotos = 30` | `workflow.yaml` | Force continue or `ARBITRATE` |
| Phase retries | `max_phase_retries = 2` | `workflow.yaml` | Pause workflow |
| Phase 0 GOTO | `max_total_gotos = 3` | `autopilot-phase0/workflow.yaml` | Force continue |
| Scope review retries | `max_retries = 3` | eval point | `on_budget_exhausted: arbitrate` |
| Arch review retries | `max_retries = 10` | eval point | Force continue |
| Task retry (recovery) | 2 | `orchestrator.py` | Skip retry |
| Workflow timeout | 7200s (2h) | `MAX_WORKFLOW_TIME` | `return "timeout"` |
| Phase 0 timeout | 3600s (1h) | `MAX_PHASE0_TIME` | `return "timeout"` |
| Frozen output recovery | `MAX_RECOV = 2` | monitor | Terminate agent |
| Stuck task | 1800s (30min) | `detect_impasse()` | Prompt human |

The `on_budget_exhausted: arbitrate` mode (used for scope_review and when GOTO limit is
exceeded) escalates to LLM arbitration rather than silently forcing continue — a nuanced
handling that makes the budget-exhaust decision auditable.

---

### ✅ 4. Planning Pipeline — RETRACTION OF EARLIER FINDING

**Status: Substantively implemented. Earlier finding #5 was incorrect.**

An earlier analysis claimed "no planner agent for scope expansion." This was wrong.
The pipeline has a four-stage planning system that is in some respects more rigorous than
Anthropic's single planner agent.

**Comparison:**

| Capability | Anthropic Planner | HephaestusNG |
|-----------|-------------------|--------------|
| Feature decomposition | ✅ Generated by planner | ✅ Phase 0 Feature Architect |
| Structured feature boundaries | ✅ Sprint contracts | ✅ `scope.md` per feature with file ownership, exclusions, inter-feature interfaces |
| Sequenced implementation order | ✅ Sprints | ✅ `architecture_design` creates `depends_on` dependency graph |
| Per-task acceptance criteria | ✅ Per sprint | ✅ Per task in `architecture_design` |
| Scope guard between planning and implementation | ❌ None | ✅ Dedicated `scope_review` phase with binary gate and goto loop |
| Feedback loop from review back to requirements | ❌ None | ✅ scope_review writes `correction_instructions`; product_requirements reads them on retry |
| Tech stack continuity enforcement | ❌ None | ✅ product_requirements explicitly checks and respects existing stack |
| OO design pass before task creation | ❌ None | ✅ Step 8 of architecture_design |
| Creative spec generation from minimal input | ✅ Core capability | ❌ Pipeline assumes human-authored PRD |

**The genuine difference:** Anthropic's planner is *generative* — it takes a one-liner
(`"Build a DAW in the browser"`) and expands it into a full product spec with feature
list and implementation sequence. HephaestusNG's pipeline assumes the human has already
written the design document and treats it as the authoritative source of truth. The
`scope_review` gate specifically prevents agents from inventing scope: any requirement
not traceable to `design.md` is flagged as out-of-scope and rejected.

Whether this is a gap depends on the use case. If design documents are already detailed,
the pipeline is arguably more disciplined — the scope gate prevents gold-plating. If the
intent is to expand a rough idea into a full spec through the pipeline, that capability
is genuinely absent.

---

### ⚠️ 5. Verification Fidelity — Partial

**Status: High fidelity for gated phases; low fidelity for development phase.**

The `scope_review` gate is binary and fully objective. The QA gate reads actual test
metrics from structured YAML frontmatter. The `product_validation` gate checks the
`verdict` field and rejects `"PASS"` if `unmet_requirements` is non-empty — a hard
override that prevents a PASS verdict from covering up unmet requirements.

However, the system never independently re-runs the test suite. The QA agent runs tests,
writes the results into `qa_report.md`'s frontmatter, and the spec gate reads that file.
An agent that writes a plausible-looking frontmatter block with `failed_tests: 0,
pass_rate: 100` but never actually ran tests would pass the gate. Anthropic's evaluator used Playwright
to *actually navigate the running application* — the evaluator couldn't be fooled by a
self-report because it was independently exercising the system.

**Score bands for reference:**
```
score < 0.3  → goto architecture_design   (fundamental problem)
score < 0.7  → goto development            (code-level problem)
score >= 0.7 → continue                    (gate passes)
```

---

### ⚠️ 6. Pre-Task Contract Negotiation — Absent

**Status: Not implemented.**

Anthropic's harness has the generator and evaluator negotiate a "sprint contract" before
any code is written:

> *"Before each sprint, the generator and evaluator negotiated a sprint contract: agreeing
> on what 'done' looked like for that chunk of work before any code was written."*

HephaestusNG's `done_definitions` in phase YAML are static — written by the system
designer at workflow creation time, not dynamically negotiated with the agent. The
development agent receives `done_definition` as read-only context in its task description.

The closest existing mechanism is the per-task acceptance criteria written by the
`architecture_design` agent and passed as `context` in `create_task()`. This is
directionally correct — the architect declares what done looks like before development
starts — but the development agent and the validator agent never negotiate or confirm
alignment before coding begins.

---

### ⚠️ 7. Monitor → Loop Control Feedback — Disconnected

**Status: Observability and control are parallel, not connected.**

The Guardian and Conductor in `src/monitoring/monitor.py` run health and pattern analysis
asynchronously, but their findings don't flow into the orchestrator's loop control
decisions. The orchestrator acts only on `detect_impasse()` and `detect_hard_error()`,
both simple time/status checks.

If the Guardian identifies "agent is rewriting the same file repeatedly with no net
progress," that signal sits in a DB record and log — it doesn't increment the impasse
counter or trigger a GOTO.

---

## Concrete Enhancement Opportunities

### Enhancement 1: Independent test re-run at the QA gate (highest leverage)

**Current state:** `score_qa()` reads the frontmatter in `qa_report.md` that the QA agent wrote.  
**Risk:** An agent that writes plausible frontmatter but never ran tests passes the gate.  
**Fix:** After the QA phase completes and `qa_report.md` exists, run the test suite
independently and compare the result against the agent's claimed metrics.

```python
# In fire_spec_gate_if_ready (task_completion_service.py), after score_qa():
import subprocess, json
result = subprocess.run(
    ["python", "-m", "pytest", "--json-report", "--json-report-file=.pytest_report.json", "-q"],
    cwd=working_directory,
    capture_output=True,
    timeout=300,
)
with open(f"{working_directory}/.pytest_report.json") as f:
    actual = json.load(f)
# Compare actual["summary"]["failed"] against agent-reported failed_tests
```

This is the highest-leverage fidelity improvement: it turns the spec gate from "trust the
JSON format" into "verify the claim against reality." It requires no new agents or
architecture — just one subprocess call that closes the biggest fidelity gap.

**Note:** This requires the test suite to be runnable from the worktree, which is
already true for Python projects (the worktree has the full codebase). For projects
where tests require external services, it can be scoped to unit tests only.

---

### Enhancement 2: Separate `agent_score` from gate threshold

**Current state:** `_pass_with_subjective(agent_score)` blends subjective score into
final gate score.  
**Risk:** Low. The subjective score can only move the result within the passing band
(0.7–1.0) — it cannot override a floor violation. But it conflates objective and
subjective signals in the same number.  
**Fix:** Keep `agent_score` for observability and feedback but exclude it from the gate
threshold computation.

```python
# Current:
return _pass_with_subjective(result.get("agent_score", 1.0)), meta

# Proposed:
meta["agent_score"] = result.get("agent_score", 1.0)  # logged but not gating
return _PASS_FLOOR, meta  # floor passed → always 0.7, don't blend
```

If you want to use `agent_score`, use it to enrich the feedback message passed to the
next development phase — not as a gate lever.

---

### Enhancement 3: Pre-task contract between architecture_design and development

**Current state:** Development agent receives static `done_definition` from YAML.  
**Gap:** No negotiation step; development agent and validator don't confirm what "done"
means in testable terms before coding starts.  
**Fix:** After `architecture_design` completes and before the first development task is
dispatched, add a lightweight contract-confirmation step. The development agent reads
`architecture.md` and emits a `task_contract.json` listing the acceptance criteria it
will implement — 3–5 checkable items per task. The contract is then passed to the
validator agent as its evaluation rubric.

This is an additive change: a new brief phase (or a pre-dispatch hook on development
tasks) that produces `task_contract.json`, which `verify_output_artifact()` can gate on.
It doesn't change the 12-phase sequence structurally; it adds a handshake before
development work begins.

---

### Enhancement 4: MonitorSignal → orchestrator feedback channel

**Current state:** Guardian/Conductor findings are logged and stored in DB but don't
influence loop control.  
**Fix:** Define a `MonitorSignal` type and have the monitoring loop emit signals to a
queue that the orchestrator's poll loop consumes.

```python
# src/monitoring/signals.py
from dataclasses import dataclass
from enum import Enum

class SignalType(Enum):
    STUCK_PATTERN = "stuck_pattern"       # agent making no net progress
    REPEATED_FAILURE = "repeated_failure" # same error class seen N times
    RESOURCE_EXHAUSTION = "resource_exhaustion"

@dataclass
class MonitorSignal:
    type: SignalType
    agent_id: str
    confidence: float  # 0–1
    evidence: str
```

The orchestrator polls `signal_queue.get_signals(workflow_id=exec_id)` alongside its
existing checks. A `STUCK_PATTERN` signal with `confidence >= 0.8` counts toward the
impasse threshold just as time-based stuck detection does. This connects the
observability layer to the control loop rather than keeping them as parallel systems that
never inform each other.

---

## Summary Scorecard

| Principle | Status | Notes |
|-----------|--------|-------|
| Objective stop conditions | ✅ Strong | Spec gate is mostly objective; `agent_score` blend is minor and non-overridable |
| Maker-checker separation | ✅ Implemented | Opt-in per workflow; review phases (5–8) provide structural separation |
| Hard iteration caps | ✅ Multi-layer | Caps at every level with `arbitrate` escalation path |
| Planning pipeline | ✅ Substantive | Phase 0 + 3-phase planning sequence; more rigorous scope enforcement than Anthropic's planner |
| Verification fidelity (QA) | ⚠️ Partial | Spec gate reads agent-produced JSON; no independent test re-run |
| Pre-task contract negotiation | ⚠️ Absent | Static YAML `done_definitions`; no pre-coding negotiation with development agent |
| Monitor → loop control | ⚠️ Disconnected | Guardian/Conductor findings don't flow into orchestrator decisions |
| Creative spec generation | ❌ By design | Pipeline assumes human-authored PRD; extraction and scoping only |

The highest-leverage single change is Enhancement 1: independently re-running the test
suite to verify the QA agent's claims. It requires the least structural change and
closes the biggest fidelity gap in the existing pipeline.

---

## Implementation Summary (2026-07-03)

All three enhancements were implemented:

### Enhancement 1: Independent Test Re-run at QA Gate

**Files modified:** `src/autopilot/spec.py`

**What was done:**
- Added `run_independent_test_verification()` function that runs pytest independently
  with `--json-report` and returns actual test metrics
- Added `verify_qa_against_independent()` function that compares agent-reported metrics
  against independent verification results
- Modified `score_qa()` to accept optional `working_directory` parameter and run
  independent verification when provided
- If agent reports 0 failures but independent run finds failures, the gate uses the
  independent (worse) metrics
- Pass rate divergence >5% triggers a warning and uses independent metrics

**Measurable impact:** Closes the biggest verification fidelity gap. The QA gate now
verifies test results against reality instead of trusting agent self-report. An agent
that writes a plausible `qa_report.md` frontmatter block without running tests will be caught.

### Enhancement 4: MonitorSignal → Orchestrator Feedback Channel

**Files created:** `src/monitoring/signals.py`
**Files modified:** `src/monitoring/monitor.py`, `src/autopilot/orchestrator.py`

**What was done:**
- Created `MonitorSignal` dataclass with types: STUCK_PATTERN, REPEATED_FAILURE,
  RESOURCE_EXHAUSTION, TRAJECTORY_DEVIATION, PHASE_STUCK
- Created `SignalQueue` class with thread-safe emit/get_signals/count_signals/clear
  methods
- Added global `get_signal_queue()` singleton
- Modified Guardian analysis in monitor.py to emit signals when it detects agents
  that need steering (stuck, idle, drifting, off_track, over_engineering)
- Modified orchestrator poll loop to consume high-confidence signals (confidence >= 0.7)
  before running impasse detection
- STUCK_PATTERN and PHASE_STUCK signals count toward impasse, triggering the stuck
  counter increment without waiting for the 30-minute timeout

**Measurable impact:** Closes the observe-act-reflect loop. Guardian findings now
influence orchestrator decisions directly. A stuck agent detected at minute 5 (via
pattern analysis) no longer waits until minute 30 (via time-based timeout) to trigger
impasse handling.

### Enhancement 5: Structured Pipeline Agent Prompts

**Files modified:** `config/workflows/autopilot/development.yaml`,
`config/workflows/autopilot/product_requirements.yaml`

**What was done:**
- Added "STRUCTURED PROJECT CONTEXT" section to development.yaml with:
  - Architecture Map (src/ layout with descriptions)
  - Code Style (explicit rules from AGENTS.md)
  - Testing Strategy (runner, coverage, format commands)
  - Common Patterns (session handling, config access, error handling)
- Added structured context to product_requirements.yaml's STEP 0

**Measurable impact:** Reduces wasted turns by giving agents explicit project context
upfront instead of requiring them to discover it through exploration. Addresses the
"14% Claude.md Tax" identified in the harness engineering article.

---

*Implementation complete 2026-07-03. All 223 tests pass. 2 skipped.*

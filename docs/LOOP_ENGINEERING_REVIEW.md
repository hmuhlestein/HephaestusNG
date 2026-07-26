# Review — Loop Engineering Enhancements + Phase 0 Architecture

Scope: commits `2db5698`, `eca8666`, `811f830` ("loop engineering enhancements
1, 4, 5" and their review-fix follow-up), plus a structural question about
how "Phase 0" (feature decomposition) fits into the pipeline architecture.
These commits were bundled alongside the SOLID refactor fix cycle but are
unrelated new feature work — reviewed separately here.

---

## Enhancement 1 — Independent test verification at the QA gate

**Files:** `src/autopilot/spec.py` (`run_independent_test_verification`,
`verify_qa_against_independent`, `score_qa`)

**What it does:** At the QA-gated phase, instead of trusting the agent's
self-reported `qa_report.md` verbatim, re-runs `python -m pytest
--json-report ...` in the agent's worktree and overrides the agent's reported
failure count if the independent run found *more* failures (one-directional:
never overrides toward a better result, so an agent can't game it by
under-reporting failures).

### Findings

1. **Hard dependency on `pytest-json-report`, which isn't declared anywhere
   in this repo, and hardcodes Python/pytest as the only verifiable stack.**
   `--json-report`/`--json-report-file` are flags from the `pytest-json-report`
   plugin, not built into pytest. Since Hephaestus builds *arbitrary target
   projects* (not just Python ones), for any non-Python project — or a Python
   project that doesn't have this specific plugin installed — the subprocess
   call fails, `report_file` never appears, and the function logs a warning
   and returns `None`. This fails safe (falls back to trusting the agent), but
   means the feature is silently a no-op for most target projects, which
   somewhat undercuts the stated goal ("agent claiming 0 failures when tests
   actually fail will be caught") for exactly the general case this tool is
   meant to operate in. **Recommendation:** either vendor/require
   `pytest-json-report` in whatever venv gets set up for Python target
   projects, or document the limitation explicitly (currently undocumented).

2. **False-positive risk from environment drift between the agent's test run
   and the independent re-run.** `run_independent_test_verification` invokes
   `subprocess.run(cwd=working_directory, ...)` with the orchestrator
   process's own environment — not necessarily the same virtualenv/env vars/
   services the agent had active when its own tests passed. If the
   independent run fails for environment reasons unrelated to the agent's
   actual code (e.g. a fixture requiring a service not started in this
   subprocess), `score_qa` will interpret that as "agent lied" and force a
   worse score/retry on **good work**. This is a plausible, not confirmed,
   failure mode — worth a canary test in a project with a nontrivial test
   environment before trusting this in production.

3. **Blocking subprocess call reaches into an async call path.** `score_qa`
   (called via `build_phase_output`) runs `subprocess.run(...,
   timeout=timeout_seconds)` with a **300-second default timeout** — a
   worst-case 5-minute blocking call. `build_phase_output` has two callers:
   - `TaskCompletionService.fire_spec_gate_if_ready` — correctly wraps it in
     `loop.run_in_executor(...)`, so this path doesn't block the async event
     loop (this was one of the earlier review-fix commits, done correctly).
   - `src/autopilot/orchestrator.py:2672` (`_fire_phase_transition`, called
     from `_advance_phases`, called from `run_single_workflow`'s polling
     loop) — this is a **plain synchronous call chain**, not wrapped in an
     executor. Whether this is a real problem depends on whether
     `run_single_workflow`'s poll loop runs on its own thread/process
     (in which case blocking here is consistent with other pre-existing
     blocking calls in that same loop, e.g. `subprocess.run(["git", ...])` in
     `attempt_recovery`) or whether it's ever invoked from the same event loop
     that serves HTTP traffic. Given the existing pattern of blocking git
     calls in this exact code path, this is likely consistent with
     established practice rather than a new regression — but worth a quick
     confirmation, since a QA-gated phase now blocks phase advancement for up
     to 5 minutes on every completion, doubling the pipeline's exposure to
     this kind of stall (once via the task-completion path, now again via the
     orchestrator's own advance-phases path).

4. **No test coverage.** `run_independent_test_verification`,
   `verify_qa_against_independent`, and the `score_qa` integration have no
   corresponding test file — the only claim is "all 223 tests pass," which
   just means nothing broke, not that this new logic is exercised.

---

## Enhancement 4 — MonitorSignal → orchestrator feedback channel

**Files:** `src/monitoring/signals.py` (new), `src/monitoring/monitor.py`,
`src/autopilot/orchestrator.py`

**What it does:** Guardian emits a `MonitorSignal` to a global, thread-safe
`SignalQueue` when it detects a stuck/idle/drifting agent; the orchestrator's
poll loop consumes high-confidence signals and can now count 2+ stuck signals
toward its own impasse detection (previously "closes the loop" — the
monitoring loop's findings had no path back into orchestrator decisions).

### Findings

1. **The specific bug the review-fix commit claims to have fixed is not
   actually fixed.** `monitor.py`'s signal-emission code builds
   `metadata={"consecutive_flags": locals().get("consecutive_stuck", 0)}` at
   line ~1101 — but `consecutive_stuck` isn't computed until line ~1116,
   **15 lines later in the same function**, after the signal has already been
   emitted. `locals()` reflects the current binding state at the point it's
   called; the variable genuinely doesn't exist yet at that point regardless
   of lookup mechanism. The commit message for `811f830` states this exact
   bug was fixed ("use `locals().get()` instead of `'consecutive_stuck' in
   dir()` which reads before assignment") — the underlying ordering problem
   was never addressed, just the symptom's failure mode was changed from "no
   value" to "always the default `0`," which is functionally identical to the
   original bug. **Fix:** move the `consecutive_stuck` computation (the
   `past = self._get_past_summaries_for_agent(...)` block) to before the
   signal-emission block, or compute it once at the top of the
   `if analysis.get("needs_steering", False):` branch.

2. **`SignalQueue.get_signals(..., consume=True)` uses value equality
   (`s not in filtered`) to determine which signals remain in the queue.**
   `MonitorSignal` is a plain `@dataclass` with an auto-generated `__eq__`
   comparing every field including a microsecond-precision `timestamp`.
   Removal-by-value instead of removal-by-identity/index is fragile — in the
   general case of two structurally-identical objects (unlikely given the
   timestamp field, but not otherwise guarded against), this could
   double-remove or under-remove. Low likelihood of triggering in practice,
   but a `list.remove` guard by object identity or index would be more robust
   than relying on dataclass value equality here.

3. **No test coverage** for `SignalQueue`/`MonitorSignal` at all — no
   `tests/test_signals.py` or equivalent exists.

4. **Design note, not a bug:** the `>= 2 stuck signals` threshold (added in
   the review-fix commit specifically to address false positives from a
   single Guardian assessment) is a reasonable heuristic, but its behavior
   depends on the relative polling cadence of `MonitoringLoop` vs.
   `run_single_workflow`'s poll loop — if the monitor polls much more
   frequently than the orchestrator drains the queue, "2+ signals" could
   reflect the *same* underlying stuck incident detected twice in a row
   rather than two independent corroborating observations. This is probably
   fine (repeated detection of the same stuck state is itself a legitimate
   signal), but it's worth knowing this is what the threshold actually means.

---

## Enhancement 5 — Structured pipeline agent prompts

**Files:** `config/workflows/autopilot/development.yaml`,
`config/workflows/autopilot/product_requirements.yaml`

**What it does:** Injects a "STRUCTURED PROJECT CONTEXT" block into the
prompts sent to pipeline agents, meant to reduce wasted turns re-discovering
project conventions (architecture map, code style, testing strategy, common
patterns).

### Findings

**🔴 The review-fix commit only partially removed HephaestusNG-specific
content from `development.yaml` — three of four sub-sections are still
hardcoded to *this* repository's own conventions, and this prompt is sent to
agents building arbitrary target projects.**

The commit message for `811f830` says: *"replace HephaestusNG-specific
architecture map with generic instruction to read AGENTS.md and
architecture.md (agents run against user projects, not this codebase)"* — and
the **ARCHITECTURE MAP** sub-section was indeed fixed correctly (now says
"read AGENTS.md and architecture.md for this project's specific layout").

But the three sub-sections immediately below it in the same YAML file were
never touched, and are still 100% specific to *this* codebase
(`config/workflows/autopilot/development.yaml`, current content):

```
CODE STYLE (from AGENTS.md — follow exactly):
- Python: Black (line length 88), flake8, mypy
...

TESTING STRATEGY (from AGENTS.md):
- Test runner: python tests/run_all_tests.py (or pytest for targeted)
- Smoke pass: python tests/run_all_tests.py --quick
- Coverage: pytest --cov=src
- Format: ruff check . && ruff format --check .
- Types: mypy src/
- Frontend: cd frontend && npm run type-check

COMMON PATTERNS:
- Database sessions: use try/finally with session.close()
- Config access: from src.core.simple_config import get_config
- App state: from src.core.app_context import get_app_state
- Error handling: log with logger.error(), rollback session, re-raise
- Agent communication: use MCP tools (hephaestus_*) not direct imports
```

**Impact:** every agent working the `development` phase of the pipeline —
regardless of what project it's actually building — is now told:
- to format with Black/ruff and type-check with mypy (wrong for non-Python
  projects, and an unwarranted assumption even for Python ones),
- to run a test runner at `tests/run_all_tests.py` that almost certainly
  doesn't exist in the target project,
- and — most seriously — **to `from src.core.app_context import
  get_app_state` for "app state"**, which is a real import path in
  *this* repository (`src/core/app_context.py`, extracted earlier this
  session) that has no meaning whatsoever in an arbitrary target codebase.

This directly undermines Enhancement 5's own stated goal (give agents
accurate context to avoid wasted turns) — an agent told to import a
module that doesn't exist will waste a turn discovering the `ImportError`,
or worse, silently fabricate a similarly-named local module to satisfy the
instruction. The last bullet ("Agent communication: use MCP tools
(hephaestus_*) not direct imports") is legitimately project-agnostic and
should stay — it's the only one of the four sub-sections that's actually
correct to hardcode, since it describes how agents talk to Hephaestus itself,
not the target project.

**Recommendation:** apply the same fix already done for the ARCHITECTURE MAP
section to CODE STYLE and TESTING STRATEGY — replace hardcoded specifics with
"read AGENTS.md for this project's actual conventions" — and remove the
`src.core.app_context`/`src.core.simple_config` bullets entirely from
COMMON PATTERNS (or move them to a HephaestusNG-internal-only prompt if one
exists separately from what target-project-building agents receive).

`product_requirements.yaml` does not have this problem — it was written (or
fixed) generically from the start.

---

## Phase 0 architecture concern — feature decomposition as a bolted-on special case

**User's concern (verbatim):** *"I'm concerned about phase 0 not being a
real phase yaml that is a bolt-on."*

**Finding: partially correct, worth refining.** Phase 0 *does* have a real
YAML definition — `config/workflows/autopilot-phase0/workflow.yaml` +
`01_feature_architect.yaml` — structured the same way as every other pipeline
workflow (`orchestrator.type: evaluating`, `evaluation_points`, done
definitions, launch template). So it's not literally hardcoded prompt text
with no config backing it.

**But the *orchestration* of Phase 0 is a hand-written special case, not
integrated into the same machinery that drives the numbered phases.** The
numbered phases (`product_requirements` → `architecture_design` →
`scope_review` → `development` → ... ) all advance through the generic,
shared engine: `_advance_phases` → `PhaseManager.mark_phase_complete` →
the `_EVALUATION_HANDLERS` registry (the dispatch table extracted earlier
this session). Phase 0, by contrast, is driven by `run_phase0()` — a
standalone ~180-line function in `orchestrator.py` that:

- manually checks the DB for existing `Feature` rows to decide whether to
  skip re-running Phase 0 at all (a bespoke idempotency check, not the
  `already_completed` guard `mark_phase_complete` uses for every other phase),
- manually creates its own dedicated worktree and branch name
  (`autopilot-phase0/{design_id}`) rather than participating in the shared
  worktree the numbered phases use,
- launches an **entirely separate workflow execution**
  (`run_single_workflow(sdk, "autopilot-phase0", ...)`) rather than being one
  phase *within* the main autopilot workflow — meaning Phase 0's "workflow"
  and the numbered-phase pipeline's "workflow" are two different `Workflow`
  DB rows, two different evaluating-orchestrator instances, with no shared
  state or transition history between them,
- manually searches the filesystem for `features.json` with ad hoc fallback
  path-globbing if it's not where expected, rather than the generic
  `PHASE_OUTPUT_ARTIFACTS`/`load_phase_output_artifacts` mechanism the QA/
  scope-review/product-validation gates use,
- and is *sequenced* into the overall pipeline entirely in code —
  `run_single_design`'s docstring literally calls itself a "Three-stage
  coordinator: Phase 0 → per-feature pipelines → design aggregate," i.e. the
  three-stage structure is an `if`/sequential-call shape in Python, not
  something the declarative phase-config engine understands as "there are
  three stages and here's how they chain."

**Why this matters (this is exactly the review's "Altitude" antipattern):**
special-cased infrastructure sitting *alongside* generic infrastructure,
rather than the generic infrastructure being extended to cover the new case.
Concretely:
- A bug fix to `mark_phase_complete`'s retry/goto/idempotency logic (like the
  ones made earlier this session) does not automatically apply to Phase 0,
  since Phase 0 never calls `mark_phase_complete` at all.
- Diagnostics/observability built around "workflow status," "phase
  execution," etc. (the DB tables and derivation logic this session spent a
  lot of effort centralizing) has a second, parallel code path to reason
  about for Phase 0 specifically — e.g. `is_design_fully_complete` and the
  status-derivation fixes from earlier in this session apply to the
  *numbered* phases' workflow, not to whatever workflow row Phase 0 created.
- The feature-detection retry-skip logic (checking existing `Feature` rows)
  is a bespoke idempotency mechanism that doesn't share code with — and could
  drift independently from — `mark_phase_complete`'s `already_completed`
  guard used everywhere else.

**This is a real architectural debt, not a bug currently causing incorrect
behavior** (verified `run_phase0` correctly handles its happy path and several
failure modes with reasonable care) — but it means every future change to
"how does a phase complete/retry/get evaluated" has to remember Phase 0 is a
parallel universe with its own version of that logic, which is precisely the
kind of drift-prone duplication this session's SOLID review was working to
eliminate everywhere else.

**Possible directions** (not fully scoped, worth a design discussion before
committing to one):
1. Model Phase 0 as an actual `order=0` phase within the same `Workflow`/
   `Phase` rows as the numbered pipeline, reusing `mark_phase_complete`'s
   generic advance/retry logic, with `features.json` as a declared
   `PHASE_OUTPUT_ARTIFACTS` entry like the other gated phases.
2. If Phase 0 genuinely needs to be a separate workflow execution (e.g.
   because per-feature pipelines that follow it are themselves separate
   `run_single_workflow` calls, which appears to be the actual reason), at
   minimum extract the shared "launch a workflow, wait for it, validate its
   declared output artifact, handle idempotent skip" logic into something
   `run_phase0` and the numbered-phase engine both call, rather than
   `run_phase0` reimplementing all of it standalone.

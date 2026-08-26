# Quickstart: Validating Spec Kit-Aware Autopilot Input

Once implemented, these scenarios prove the feature end-to-end. Each maps to an acceptance scenario in spec.md — see there for the full Given/When/Then.

## Prerequisites

- A Hephaestus dev instance running (`heph status` healthy).
- A test project with `specify init --here --integration claude` already run against it (this repo's own `.specify/` setup, from earlier this session, works as a live example).

## Scenario 1 — Single-feature auto-start (User Story 1, SC-001)

```bash
cd <test-project>
mkdir -p specs/001-demo-feature
cp .specify/templates/spec-template.md specs/001-demo-feature/spec.md
# ...fill in spec.md with a real, minimal feature...
cp .specify/templates/plan-template.md specs/001-demo-feature/plan.md
# ...fill in plan.md...
heph autopilot start --project-path .
```

**Expected**: No prompt for `design.md`; the run's `product_requirements.md` output is traceable to `spec.md`'s content, and `architecture.md` to `plan.md`'s.

## Scenario 2 — Existing design.md-only project is unaffected (User Story 1, SC-002)

```bash
cd <project-with-only-design-md>
heph autopilot start --project-path .
```

**Expected**: Identical behavior to before this feature shipped — same prompt for/use of `design.md`. Verified in CI by the existing bugfix/autopilot workflow test suites passing unchanged.

## Scenario 3 — Ambiguous multi-feature project requires an explicit choice (User Story 2, SC-005)

```bash
cd <test-project>
mkdir -p specs/001-first specs/002-second   # both with spec.md
heph autopilot start --project-path .        # no --feature
```

**Expected**: Non-zero exit, error output lists both `001-first` and `002-second`, no build started.

```bash
heph autopilot start --project-path . --feature 002-second
```

**Expected**: Builds `002-second` specifically.

## Scenario 4 — Readiness check never gates start (User Story 4, SC-006)

```bash
# spec.md deliberately left with a [NEEDS CLARIFICATION: ...] marker
heph autopilot check --project-path . --feature 001-first
```

**Expected**: Reports the unresolved marker, `ready: false`, non-zero exit — but is read-only.

```bash
heph autopilot start --project-path . --feature 001-first
```

**Expected**: Still runs — the check above had no effect on this command.

## Scenario 5 — Automatic scanning builds without manual start (User Story 6, SC-007/SC-008)

```bash
# Enable the setting for the project (via dashboard toggle, or:)
curl -X PUT localhost:8300/projects/<project-id> \
  -H "X-Agent-ID: quickstart" -H "Content-Type: application/json" \
  -d '{"spec_kit_auto_scan": true}'

mkdir -p <project>/specs/003-auto-demo
cp .specify/templates/spec-template.md <project>/specs/003-auto-demo/spec.md
# ...fill in a real minimal spec...

# Wait one design-queue scan interval (60s, DESIGN_QUEUE_SCAN_INTERVAL)
```

**Expected**: A build starts for `003-auto-demo` without any `heph autopilot start` call, visible in the dashboard's design queue / feature gallery the same way a dropped-in `design.md` already appears there today.

```bash
curl -X PUT localhost:8300/projects/<project-id> \
  -H "X-Agent-ID: quickstart" -H "Content-Type: application/json" \
  -d '{"spec_kit_auto_scan": false}'

mkdir -p <project>/specs/004-should-not-auto-build
cp .specify/templates/spec-template.md <project>/specs/004-should-not-auto-build/spec.md
# wait one scan interval
```

**Expected**: No build starts for `004-should-not-auto-build` — it is only visible via the dashboard picker / `spec-kit-features` API, never built without an explicit `heph autopilot start --feature 004-should-not-auto-build`.

## Scenario 6 — Prompt-quality convention applies regardless of input source (User Story 5, SC-003/SC-004)

```bash
diff <(yq '.additional_notes' config/workflows/bugfix/product_requirements.yaml | grep -A5 "NEEDS CLARIFICATION") \
     <(yq '.additional_notes' config/workflows/autopilot/product_requirements.yaml | grep -A5 "NEEDS CLARIFICATION")
```

**Expected**: Both workflows' `product_requirements.yaml` document the same bounded (max 3, scope > security > UX > technical) `NEEDS CLARIFICATION` convention and the same P1/P2/P3 independently-testable story framing — verifiable as a static content check, no running instance required (same pattern as `tests/test_qa_coverage_gate_is_diff_scoped.py`'s existing prompt-content assertions).

## Scenario 7 — `feature_architect` actually reads the full bundle (User Story 5, SC-009)

Scenario 6 only checks that the prompts *say* the right things — this scenario checks `feature_architect` behavior, closing the gap `/speckit-analyze` flagged (finding E1): User Story 5's own Independent Test has a second half that quickstart previously never covered.

```bash
cd <test-project>  # a Spec Kit feature with spec.md, plan.md, AND tasks.md, describing >1 discrete piece of work
heph autopilot start --project-path . --feature <NNN-name>
# let the feature_architect phase run to completion
cat .hephaestus/features.json  # or the equivalent path in the worktree
```

**Expected**: The decomposition's feature boundaries and `depends_on` relationships are traceable to specifics in `plan.md`'s technical breakdown and `tasks.md`'s existing dependency ordering — not a decomposition that reads as if only `spec.md` existed (e.g. it references components/phases `plan.md` named, or dependency ordering that matches `tasks.md`'s).

## Scenario 8 — Multi-repo project: repo-scoped detection and selection (FR-020, FR-021, FR-022, FR-023)

```bash
cd <multi-repo-test-project>  # AutopilotProject with child repos labeled "backend" and "frontend"
mkdir -p <backend-repo-path>/specs/001-shared-number
mkdir -p <frontend-repo-path>/specs/001-shared-number
cp .specify/templates/spec-template.md <backend-repo-path>/specs/001-shared-number/spec.md
cp .specify/templates/spec-template.md <frontend-repo-path>/specs/001-shared-number/spec.md

heph autopilot start --project-path . --feature 001-shared-number
```

**Expected**: Errors — lists both matches (`backend` and `frontend`) and requires `--repo` (FR-023).

```bash
heph autopilot start --project-path . --feature 001-shared-number --repo backend
```

**Expected**: Builds the `backend` repo's feature specifically (FR-022), never the `frontend` one.

```bash
# Bare-number shorthand, single-repo case:
heph autopilot start --project-path <single-repo-project> --feature 001
```

**Expected**: Resolves the same as passing the full `001-<name>` directory name would (FR-021) — no `--repo` needed, since there's only one repo to search.

```bash
# Auto-scan's plan.md gate (FR-020):
mkdir -p <project>/specs/005-spec-only && cp .specify/templates/spec-template.md <project>/specs/005-spec-only/spec.md
# spec_kit_auto_scan already enabled for <project> from Scenario 5
# wait one scan interval
```

**Expected**: `005-spec-only` is not built — it has no `plan.md` yet. It becomes visible via the dashboard/CLI (Awareness Model) but stays unqueued until `plan.md` is added and a later scan runs.

## Scenario 9 — One spec in the primary repo decomposes across sibling repos (User Story 5, SC-012)

Scenario 8 covers repo-scoped *detection*: two separate specs, same number, different repos. This scenario is the different case SC-012 actually describes — a *single* spec in the primary repo whose `plan.md` names work in more than one sibling repo.

```bash
cd <multi-repo-test-project>  # primary repo + a "backend" child repo + a "frontend" child repo
# specs/ lives only in the primary repo:
mkdir -p specs/002-shared-feature
cp .specify/templates/spec-template.md specs/002-shared-feature/spec.md
# plan.md's Technical Context / Project Structure explicitly describes work in both
# the backend and frontend sibling repos (see plan-template.md's Project Structure section)

heph autopilot start --project-path . --feature 002-shared-feature
# let the feature_architect phase run to completion
cat .hephaestus/features.json  # or the equivalent path in the worktree
```

**Expected**: `feature_architect` creates two `Feature` rows from this one spec — one bound to the `backend` repo, one bound to `frontend` (never a single `Feature` spanning both, FR-018/SC-012). When each feature's own `development` phase runs, its agent's injected `{project_context}` (`AgentManager._build_repo_context`, unchanged by this feature) marks its own repo WRITABLE and the sibling READ-ONLY with a real path — confirm the backend-scoped agent's context actually names the frontend repo's path (and vice versa for the frontend-scoped agent).

# HIPAA/PII Compliance Phase — Design & Placement Research

## Goal

Add a `hipaa_compliance` phase to the `autopilot` workflow that reviews a
feature's implementation for PHI/PII handling gaps (encryption at rest/in
transit, audit logging of PHI access, minimum-necessary-access, data
retention/right-to-deletion, PII redaction in logs/error messages,
BAA-relevant third-party integrations), plus light PII-awareness language in
the earlier phases so the codebase arrives at that gate already somewhat
compliance-conscious ("shift left").

Decided so far: **universal** (part of the standard `autopilot` workflow, not
an opt-in variant) — for now. `bugfix`/`feature_architect` workflows are out
of scope for this pass.

## Placement: after `security_review`, before `qa_validation`

New phase in the "hardening cluster": `adversarial_review` → `architectural_review`
→ `security_review` → **`hipaa_compliance`** → `qa_validation` → ... → `git_expert` → `deploy`.

Reasoning:
- **Needs security_review's output, not before it.** `security_review`
  already fixes generic vulnerabilities (auth, injection, data handling) via
  AWS ASH. Running compliance checks after that means `hipaa_compliance`
  evaluates already-hardened code and can focus on regulatory-specific gaps
  instead of re-finding the same issues security_review already caught.
- **Must be before `git_expert`/`deploy`** — non-compliant code reaching
  production is the exact risk this phase exists to prevent.
- **Should be before validation phases** so a compliance BLOCKER can `goto
  development` before QA/product validation spend time against code that's
  getting sent back anyway.

### Mechanics of the insertion

Each phase file declares its own `id:` (`security_review` is `id: 8`,
`qa_validation` is `id: 9`); `workflow.yaml`'s `execution_order` is just a
list of those ids. Plan: give the new phase `id: 15` (next unused number,
not a renumber of every downstream phase file) and insert it into
`execution_order` between 8 and 9:

```yaml
execution_order: [1, 2, 3, 4, 5, 6, 7, 8, 15, 9, 10, 11, 12, 13, 14]
```

Smaller diff than renumbering `qa_validation`..`deploy`'s `id:` fields.

## Gate style: report-only (adversarial_review-shaped), not self-fixing

`security_review` fixes what it finds itself, because AWS ASH gives it
concrete, mechanical findings. Compliance findings are more judgment-heavy
(is this field PHI? does this retention policy satisfy the requirement?) —
report-only, findings routed back to `development` via `goto`, matches
`adversarial_review`'s model better. **This is a reasonable-default choice
made under Auto Mode, not yet confirmed with the user — flag before/while
implementing.**

## Wiring checklist (traced through the actual code, not assumed)

New phase file: `config/workflows/autopilot/hipaa_compliance.yaml`
- `id: 15`, `name: hipaa_compliance`, `spec_gate: true`, `thinking_level: high`
- Structure mirrors `adversarial_review.yaml` closely: CRITICAL PATH RULE
  block with the task-ID-suffix requirement (`hipaa_compliance-<task-id>.md`,
  written to `.hephaestus/hipaa_compliance/`) — this codebase just fixed a
  live incident (GitHub-adjacent work this session) where every gated
  phase's suffix instruction got silently dropped at the actual WRITE step
  despite being defined correctly near the top of the prompt; the new
  phase's prompt must get this right from day one, not repeat that bug.
  BLOCKER/WARNING/NIT classification criteria specific to PHI/PII (missing
  encryption, missing audit trail, missing access control, missing
  retention/deletion support, PII leaking into logs/error messages/third-party
  calls without a BAA).
- `outputs: ["hipaa_compliance.md"]`, `done_definitions`, `next_steps`.
- STEP 0 retry-check should glob `hipaa_compliance-*.md`, not a bare name
  (same fix already applied to every other gated phase this session).

`src/autopilot/spec.py` changes:
- New `score_hipaa_compliance(result, report_text=None, prior_warning_count=None)`,
  copy of `score_adversarial_review`'s shape exactly (BLOCKER→0.4, unchanged
  WARNING vs. prior run→0.9 pass, new WARNING→0.5, clean→0.9) including the
  `prior_warning_count` anti-loop mechanism (`get_review_findings_history`
  is already fully generic by `phase_name` string — needs zero changes).
- `build_phase_output`: add `elif phase_name == "hipaa_compliance":` branch,
  `read_okf_report(working_directory, "hipaa_compliance.md", phase_name=phase_name)`,
  same prior_warning_count wiring as the `adversarial_review` branch.
- `GATE_RESULT_ARTIFACTS`: add `"hipaa_compliance": ("hipaa_compliance.md",)`.
- `gate_finding_count`: **no change needed** — its default branch
  (`result.get("blocker_count")`) already covers any blocker-count-style
  phase; hipaa_compliance uses that exact vocabulary by design.
- `synthetic_clean_result`: falls through to the existing
  `blocker-count schema` default (`{**base, "blocker_count": 0}`) —
  **no change needed** as long as hipaa_compliance uses `blocker_count`.
- `expected_gate_result_type`/`GATE_RESULT_TYPE_OVERRIDE` (referenced by
  `synthetic_clean_result`, not yet read in full) — needs checking: confirm
  whether a new phase name needs an explicit entry or falls through to a
  sane default matching its own report's `type:` frontmatter field.
- `GATE_RESULT_SUBDIR`: no entry needed (default `.hephaestus/<phase_name>/`
  convention applies).

`config/workflows/autopilot/workflow.yaml` changes:
- `execution_order`: insert `15` between `8` and `9` (see above).
- `session_roles`: add `hipaa_compliance: <role>` — needs a decision: reuse
  an existing role (e.g. `security-reviewer`) or define a new
  `compliance-reviewer` role. Not yet checked how `session_roles` maps to
  actual prompt/system-prompt config — do this before writing the entry.
- `required_output`: add `hipaa_compliance: .hephaestus/hipaa_compliance/hipaa_compliance.md`.
- `phase_inputs`: add a `hipaa_compliance:` entry (likely
  `required: [requirements.md, architecture.md]`,
  `optional: [adversarial.md, security.md, spec.md]`, mirroring
  `security_review`'s own inputs since it runs right after and wants the
  same context plus security_review's own findings).
- `development`'s existing `optional` input list
  (`[challenge.md, adversarial.md, review.md, security.md]`) should gain
  `hipaa_compliance.md` — on a goto-back-to-development retry triggered by
  a compliance BLOCKER, development needs to see the findings, same as it
  already does for the other three review gates.
- New `evaluation_points` entry `after_phase: hipaa_compliance`, mirroring
  `security_review`'s shape: `max_retries: 2`, `max_review_runs: 4`,
  conditions `score < 0.3 → goto architecture_design`,
  `score < 0.7 → goto development`, `score >= 0.7 → continue`. (Note:
  per workflow.yaml's own documented threshold-band analysis, the blocker-count
  scorers can only ever emit 0.4 or 0.9 today — the `< 0.3`
  architecture-redesign band is dead-but-intentionally-kept, same as
  design_review/adversarial_review/architectural_review. Keep it for
  consistency, don't be surprised it's unreachable.)

Not yet checked, should be checked before implementing:
- `phase_transitions.py` lines ~3492-3746 (`GATE_RESULT_ARTIFACTS` consumers)
  — confirm no other phase-name allowlist needs a new entry beyond the dict
  itself.
- `verification.py:351-373` — same `GATE_RESULT_ARTIFACTS.get(phase.name)`
  pattern; likely automatic once the dict entry exists, but not yet traced
  end to end.
- `docs/autopilot.md`'s phase table (currently documents 14 phases,
  numbered 1-14 in the README/docs prose) needs a new row once this ships —
  the numbering in prose will need to read "15 phases" or similar, not
  necessarily phase *number* 9 in the printed table, to avoid conflicting
  with the `id: 15` internal identifier chosen to avoid renumbering.

## "General PII language" for earlier phases (shift-left prep)

Light-touch, not a rewrite — a short reminder in each phase's own voice, not
full HIPAA instructions (that's `hipaa_compliance`'s job). Candidate phases,
in pipeline order:

- **`product_requirements`** — when extracting structured requirements, note
  any fields that look like PII/PHI (names, DOB, SSN, medical record
  numbers, diagnoses, health plan IDs, etc.) so downstream phases inherit
  that awareness via the requirements doc itself, not just tribal
  knowledge.
- **`architecture_design`** — if the requirements flag PII/PHI, the
  architecture should account for encryption at rest/in transit, access
  control, and audit logging for those data paths as part of the normal
  design, not as an afterthought bolted on at `hipaa_compliance` time.
- **`design_review`** — the adversarial challenge should also ask "does this
  architecture handle any flagged PII/PHI correctly" alongside its existing
  adversarial checks, catching a design gap before development starts.
- **`development`** — general reminder: don't log PII/PHI values, encrypt
  sensitive fields at rest, use parameterized queries, avoid putting PII in
  URLs/query strings.

Not planned: `scope_review`, `qa_validation`, `product_validation`,
`doc_review`, `forensics_analysis`, `git_expert`, `deploy` — these either
don't touch design/implementation decisions directly, or are downstream of
where the awareness needs to be seeded.

## Open questions before implementing

1. Confirm report-only gate style (current plan) vs. self-fixing like
   `security_review` — this was a judgment call made under Auto Mode, not a
   confirmed decision.
2. `session_roles` value for the new phase — needs tracing how that map is
   consumed before picking a value.
3. Whether `expected_gate_result_type`/`GATE_RESULT_TYPE_OVERRIDE` needs a
   new entry for `hipaa_compliance` (not yet read in full).
4. Exact wording/scope of the "general PII language" additions — draft
   above is a starting point, not final copy.

This document is research/planning output only — no code has been changed
yet.

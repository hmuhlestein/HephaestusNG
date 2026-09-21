---
sidebar_position: 1
---

# Autopilot Pipeline

Everything else in these docs — [Phases](../guides/phases-system), Guardian,
tickets, the Python SDK — describes Hephaestus's underlying framework: a
generic mechanism for workflows that spawn and branch their own tasks.
**Autopilot is a specific product built on top of that framework.** Where
the generic phase system is job descriptions agents fill dynamically,
Autopilot is a fixed, named, 15-phase pipeline that takes a design document
and drives it all the way to a merged, deployed pull request with no human
in the loop.

If you're looking for the self-organizing Phase 1/2/3 task-spawning model,
see [Understanding the Phases System](../guides/phases-system) instead —
this page is about the concrete pipeline, not the general mechanism.

## How it fits together

A **Design** is a single `.md` file describing a product change — anything
from "add a calculator" to "add auth, a dashboard, and an admin panel."
Before any code is written, a one-time **Feature Architect** pass
decomposes the design into one or more **Features**: vertically-scoped,
independently shippable slices, each with its own file ownership and
dependency ordering (`parallel` or `sequential` relative to its siblings).

Every Feature then runs the full 15-phase pipeline below, end to end, in
its own isolated git worktree — independent pass/fail status, independent
commit history, merged to `main` on its own.

```bash
heph autopilot start --project-path ~/my-project
heph autopilot status
heph autopilot queue --project-path ~/my-project
```

## The 15 phases

| # | Phase | Agent | What it does |
|---|-------|-------|---------------|
| 1 | Product Requirements | Product Requirements Analyst | Turns the feature's scope into functional/non-functional requirements |
| 2 | Scope Review | Scope Reviewer | Gates that requirements are a faithful, complete extraction of scope — nothing added or dropped |
| 3 | Architecture & Design | Software Architect | Produces the technical design: components, data models, API contracts |
| 4 | Design Review | Architecture Challenger | Adversarially attacks the architecture *before* any code exists |
| 5 | Development | Software Developer | Implements the design, writes tests, stays within the feature's file scope |
| 6 | Adversarial Code Review | Adversarial Code Reviewer | Reviews the implementation for correctness, races, and security failure modes |
| 7 | Architectural Review | Software Architect (re-invoked) | Checks the implementation against the original design decisions |
| 8 | Security Review | Security Reviewer | OWASP Top 10, auth, input validation, secrets — fixes critical/high issues directly |
| 9 | HIPAA/PII Compliance | HIPAA Compliance Reviewer | Inventories every PHI/PII field and checks encryption, access control, audit logging, retention, third-party exposure, and log redaction |
| 10 | QA Validation | QA Engineer | Runs/creates tests, validates requirements with a compliance matrix |
| 11 | Product Validation | Product Validator | Final check of the implementation against the feature's original scope |
| 12 | Documentation Review | Documentation Reviewer | Fixes stale docs, README, docstrings against the actual code |
| 13 | Forensics Analysis | Forensics Analyst | Only runs after a run with errors; proposes prompt rewrites for human review |
| 14 | Git Commit & Push | Git Operator | Branches, commits, pushes, opens and merges the PR |
| 15 | Deploy | Deployer | Only runs if the project has a `DEPLOY.md`; a failure here doesn't fail the feature |

Gated phases (2, 4, 6, 7, 8, 9) can send work backward — a failing Scope
Review returns to Phase 1, a Design Review finding returns to Phase 3, and
so on — bounded by the workflow's own retry and arbitration limits so a
feature can't loop forever on one phase.

## Where PII awareness starts

HIPAA/PII compliance isn't only Phase 9's job. Product Requirements (Phase
1) identifies and lists PHI/PII fields up front, Architecture & Design
(Phase 3) requires an explicit encryption/access-control/redaction decision
per field, and Development (Phase 5) follows through with a named
redaction library for logging — so by the time Phase 9 runs, it's checking
decisions that were actually made, not discovering the problem cold.

## Further reading

- [Autopilot Pipeline (full reference)](https://github.com/hmuhlestein/HephaestusNG/blob/main/docs/autopilot.md) —
  worktree strategy, cost tracking, the design queue, HTML reports, and the
  full data model, in the main repo's `docs/` folder.
- [Spec Kit Support](https://github.com/hmuhlestein/HephaestusNG/blob/main/docs/speckit.md) —
  building a feature directly from a GitHub Spec Kit `specs/<NNN>-<name>/`
  directory instead of a hand-written design `.md`.
- [Understanding the Phases System](../guides/phases-system) — the generic,
  self-organizing framework Autopilot is built on top of.

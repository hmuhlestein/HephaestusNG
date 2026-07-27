---
type: product_validation_result
feature_id: des-91c8-pi-extension
verdict: PASS
unmet_requirements: []
---

# Product Validation Report: Pi Cost Tracker Extension (des-91c8-pi-extension)

## 1. Design Intent Re-Read

`.hephaestus/design.md`'s "Pi Extension Collector" section (lines 621-647) describes a pi extension that hooks `turn_end`, captures `message.usage.cost.total` in real time, shows a running cost in the pi TUI, and POSTs each turn's cost to Hephaestus's API — as a real-time complement to (not replacement for) the JSONL-tailing fallback, with the two paths described as "complementary, not exclusive."

That extension, its install/build wiring, and the API endpoint it posts to were all already implemented and merged before this feature's requirements phase began (`git log main..HEAD` was empty at that point — confirmed in `docs/requirements_analysis.md`). This feature's actual, narrower design intent (`docs/architecture.md`) was: fix one wrong line in `extensions/hephaestus-cost-tracker/README.md` (documented `POST /cost-entries`, real route is `POST /api/autopilot/cost-entries`), and verify — not re-implement — the existing collection pipeline.

## 2. What Actually Happened (Scope Supersession, Documented and Justified)

Adversarial review, exercising the pipeline's "assume it's broken, prove it" mandate against the pre-existing `collect_task_cost` function that this feature's README documents, found two BLOCKER data-integrity bugs the design's own "complementary, not exclusive" claim did not actually hold for in code:

- **B-1 (double-counting):** the real-time extension path and the JSONL fallback both wrote cost data unconditionally — every turn was recorded twice whenever the extension was active, directly contradicting the design's "complementary" framing and the README's now-corrected "prevents double-counting" claim.
- **B-2 (batch loss):** a single bad `CostEntry` in a task's batch rolled back every entry already recorded in that batch, with no retry path — silent, permanent cost data loss.

Per this pipeline's gate rules (open bug tickets must be resolved, not just filed), both were fixed in-branch rather than deferred, superseding `docs/architecture.md`'s original "README-only" scope boundary. I verified this supersession is real and justified, not scope creep dressed up as necessity: both bugs are genuine defects in code this feature's own deliverable (the README) documents, both have passing regression tests, and the fixes are narrowly targeted at exactly the two defects found — no unrelated refactoring rode along.

Security review then found a High-severity consequence of the B-1 fix itself: the "does any `source=\"pi\"` entry exist for this task" check didn't verify the entry belonged to the task's own assigned agent, so a forged `CostEntry` under an unrelated `agent_id` (both `task_id` and `agent_id` are enumerable via existing unauthenticated `GET` endpoints) could permanently suppress a victim task's real cost collection. This was also fixed in-branch (`agent_id=agent.id` added to the filter) with a dedicated regression test, and the deeper systemic root cause (the POST endpoint not binding caller-supplied IDs to the authenticated identity) was correctly ticketed rather than silently absorbed into this feature's scope, since fixing it would require touching `autopilot_api.py`, outside this feature's file boundary.

## 3. Functional Requirements Verified Against Working Code

- **FR-1 (README POST path fix):** verified directly — `extensions/hephaestus-cost-tracker/README.md:44` now reads `POST /api/autopilot/cost-entries`, matching `index.ts:123` and the real FastAPI route (`/api/autopilot` prefix + `@router.post("/cost-entries")` in `autopilot_api.py:2144`). DONE.
- **FR-2 (live pi-install verification):** correctly downgraded to an accepted, explicitly documented risk — no `pi` binary is available in this sandboxed worktree, a constraint noted consistently across `docs/requirements_analysis.md` §10, `docs/architecture.md` §6, and `docs/implementation_status.md` Task 2. Not fabricated as "done"; static inspection of `index.ts`'s `initialize(ctx)`/`turn_end(ctx, turn)` shape against pi's documented extension hooks found no inconsistency, which is the correct ceiling for what can be verified without the actual binary.
- **FR-3 (regression check):** verified myself, independent of trusting the QA report's own count — ran `python -m pytest tests/test_cost_collection_service.py -p no:libtmux -q`: **24 passed, 0 failed**. The one pre-existing, unrelated `tests/test_cost_tracking.py` collection failure (`ImportError` on a function renamed in a prior merged feature) is correctly identified as present on `main` too, not a regression.

## 4. Non-Functional Requirements

- **Performance:** no change — this feature touches only a per-task existence check (indexed query pattern already used elsewhere in the file) and a doc line; no new hot path introduced.
- **Security:** the one High finding was fixed in code this pass (agent-ownership check), not just documented as accepted risk; the residual systemic gap (spoofable `X-Agent-ID` not bound to caller-supplied `task_id`/`agent_id`) is correctly ticketed (`ticket-5a75167a-27d3-4a9a-bb01-0409bd128cd7`) as out of this feature's file scope rather than either ignored or scope-crept into an endpoint rewrite. This is the right call: `autopilot_api.py` is untouched by this feature's diff, and expanding scope to fix it would itself be an unreviewed scope violation.
- **No new test tooling:** confirmed no JS/TS test framework was introduced — consistent with the requirements doc's explicit NFR that this would be inconsistent with the rest of the repo (no test tooling exists for `frontend/` either).

## 5. Integration With Existing System

Traced the full call path and confirmed no breakage: `task_completion_service.py::collect_cost_on_completion` → `cost_collection_service.py::collect_task_cost` → `cost_derivation.py::record_cost` → DB rollup. `tests/test_task_completion_service.py` (47 tests) and `tests/test_budget_enforcement_integration.py` (13 tests) are reported passing in `docs/qa_validation/qa_report.md`, exercising the caller and the downstream budget-enforcement consumer respectively — both are unaffected-by-diff regression checks, not new coverage invented to look thorough.

## 6. User Experience Flows

No user-facing UI in this feature's scope — the pi extension's own UX (TUI status bar cost display, `💰 $X.XX`) is unchanged by this diff (`index.ts` is untouched). The only user-facing artifact touched is the README a developer would read when installing/debugging the extension, which now describes the real endpoint and the real (fixed) fallback-skip behavior instead of a false claim.

## 7. Edge Cases From Design Doc Confirmed Handled

- **"Complementary, not exclusive" fallback behavior** (design.md lines 640-642): now actually true in code for the common case (extension active → fallback correctly skipped; extension inactive → fallback correctly runs), verified by `test_skips_jsonl_fallback_when_realtime_pi_entries_exist` and `test_jsonl_fallback_still_runs_when_no_realtime_entries_exist`.
- **Forged/unrelated-agent entry does not suppress a victim task's real costs:** verified by `test_unrelated_agent_entry_does_not_suppress_fallback`.
- **One bad entry in a batch does not discard the rest:** verified by `test_bad_entry_does_not_discard_rest_of_batch`.
- **Residual edge case, correctly not silently ignored:** adversarial review's W-1 (a partial-session real-time POST failure — some turns' POSTs succeed, one fails — now causes the *entire* task's JSONL fallback to be skipped since the check is "any entry exists," not per-turn) is real, was surfaced honestly, assessed as lower severity than the bug it replaces, and left open rather than blocking the gate or being fixed with an out-of-scope schema change (`CostEntry` has no per-turn identifier today). This is the correct call for this pass, not a gap being hidden.

## 8. Verdict

**PASS.** All three requirements from `docs/requirements_analysis.md` are met — two fully done (FR-1, FR-3), one correctly downgraded to an explicit, non-fabricated accepted risk (FR-2) rather than either skipped silently or falsely claimed complete. Two BLOCKER-severity pre-existing bugs and one High-severity security finding, all found during this feature's own review chain, were fixed in-branch with passing regression tests rather than deferred past the gate. No unmet requirements.

## 9. Recommendations for Human Reviewer

1. **No action required to merge this feature.** The scope supersession (README-only → README + two data-integrity fixes + one security fix) is well-documented, narrowly targeted, and each fix has a dedicated passing regression test — worth a quick skim of `docs/implementation_status.md` and `docs/security_review/security_report.md` to confirm you're comfortable with that supersession, but it does not need re-litigating from scratch.
2. **Track `ticket-5a75167a-27d3-4a9a-bb01-0409bd128cd7`** (High priority, systemic `POST /api/autopilot/cost-entries` identity-binding gap) — it's correctly out of scope here but is the actual root cause behind this pass's High finding and deserves its own future feature/pass rather than falling off the radar.
3. **FR-2 (live pi-install smoke test) is still genuinely unverified end-to-end** — if there's ever an environment with a real `pi` binary available to this pipeline, it would be worth spending 10 minutes confirming the extension actually loads and posts successfully, since that's the one claim in the whole feature that's verified only by static code reading, not execution.
4. **W-1 (partial-session POST failure drops a fallback for the whole task)** is a reasonable, low-severity residual gap to leave open, but if pi extension usage becomes widespread and mid-session API restarts turn out to be non-rare in practice, revisit adding a per-turn identifier to `CostEntry` to make the fallback's coverage check granular instead of a per-task boolean.

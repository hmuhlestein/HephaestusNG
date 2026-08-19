# Phase 3, Tier 3 — config/documentation-shaped, cheap, opportunistic findings

## Which items needed work vs. which didn't

| Item | Status | Note |
|---|---|---|
| 22 (`git_commit_push.yaml` contradictory description) | **Fixed** | |
| 23 (8 dead `Workflow.status.in_([..., "running"])` sites) | **Fixed** | Count corrected from the plan's 7 to 8. |
| 24 (ticket-creation prompt/exemption plumbing) | **Verified correct, no fix needed** | Both halves intact and accurate through decomposition. |
| 25 (`MonitoringLoop` collaborator compat debt) | **Documented, not fixed** | Explicitly out of this tier's scope per the plan's own words. |
| 26 (stale cross-file comments) | **Fixed** | One of three original sub-claims was actually still stale. |
| 27a (`manual_validation_test.py` import-time HTTP) | **Fixed** | |
| 27b (flaky ambient-filesystem-state test) | **Fixed** | |
| 28 (`libtmux` deprecated API migration) | **NOT implemented — the plan's premise doesn't hold** | See below. This is the headline finding of this pass. |

## Item 22 — `git_commit_push.yaml`'s contradictory description

Rewrote the `description` field from "Human-only Git hand-off. The pipeline must not commit, push, create pull requests, or merge branches" (flatly false — the body's STEP 1-5 has the agent stage, commit, push, and merge autonomously) to accurately state the real, `review_mode`-gated behavior: the agent always commits and pushes; whether it also merges (vs. creating a PR and stopping for human review) depends on `review_mode`. Verified against STEP 4's actual body text ("MERGE TO MAIN (or CREATE PR if in review mode)") to make sure the rewrite matches reality, not just sounds better. No test — text-only change, confirmed no test asserts the old string.

## Item 23 — 8 dead `Workflow.status.in_(["active", "running"])` sites

The plan said 7; a fresh grep found 8 across 5 files: `control_routes.py:107,135,644`, `queue_routes.py:156,390`, `orphan_reaper.py:85`, `engine_client.py:433`, and `src/autopilot/orchestrator/queue.py:63` (a plain Python tuple membership check, `if wf_status not in ("active", "running", "paused")`, not a DB filter — same dead value via a different mechanism, which is likely why the plan's original count missed it). `Workflow.status`'s `CheckConstraint` only permits `active`/`completed`/`paused`/`failed` — `"running"` has never been a reachable value at any of these sites. Removed it from all 8; `"active"` (and `"paused"` where present) unchanged, so no query's actual matching behavior changes. Full regression run across the 5 touched files' test coverage (`test_orphan_reaper.py`, `test_worktree_manager.py`, `test_autopilot_api.py`, `test_autopilot_api_helpers.py`, `test_blocking_calls_offloaded.py`, `test_queue_requeue_scoping.py`, `test_orchestrator.py`, `test_orchestrator_helpers.py`, `test_phase0_idempotency.py`): all green.

## Item 24 — ticket-creation friction prompt/schema plumbing

Both halves the plan asked to verify are intact and correct, no code changed:
- The `ticket_id` precondition warning (originally added inline in `d715120`, since refactored into `get_ticket_note()` → `get_prompt("ticket_note")` → `config/prompts/system_prompts.yaml`'s `ticket_note` key) correctly names the current real tool names (`create_task`, `heph_create_ticket`).
- The phase-agent exemption logic from `9d6bb78` (`is_sdk_agent`/`is_phase_agent`, `if not is_sdk_agent and not is_phase_agent`) is still present verbatim at `src/mcp/server.py:1990-1995`, unchanged in shape through all of this session's decomposition work.

## Item 25 — `MonitoringLoop` collaborator compat debt

Confirmed still present exactly as described (`MAX_STUCK_TASK_NUDGES` duplicated in `monitor.py`/`health_audit.py`, `UNCONFIRMED_COMPLETION_ESCALATE_AFTER` duplicated in `monitor.py`/`mechanical_recovery.py`). Not fixed, per the plan's own explicit framing — this is deliberate compatibility-shim debt from Phase 1b's 26-delegator-stub approach, and actually collapsing it means porting ~180 tests to construct the new collaborator classes directly, a much bigger undertaking than this tier's "cheap, opportunistic" scope.

## Item 26 — stale cross-file comments

Only one of the plan's three cited instances was still actually stale: `src/phases/phase_manager.py:1207` said `"task_completion_service.py's fire_spec_gate_if_ready"` — `fire_spec_gate_if_ready` is at `src/autopilot/orchestrator/phase_transitions.py:3288`. Fixed, and also corrected the same comment's adjacent reference to `"autopilot/orchestrator.py's periodic sweep"` (that flat file doesn't exist either — the periodic sweep lives in the same `phase_transitions.py` file) since both stale references were in the same comment block. The other two instances the plan named (`worktree_integration.py:669`, `phase_manager.py:1477`, and `queue_service.py`'s docstring) no longer match what the plan describes — no file-path claim to correct in any of them.

## Item 27 — two test-hygiene items

Decided to fix both rather than continue deferring — both are small, well-scoped, and directly improve suite reliability, matching this tier's own "cheap, do opportunistically" framing.

- **27a**: `tests/manual_validation_test.py` fired two `requests.post(...)` calls at module level (against `localhost:8300`, meant for manual runs against a live server). Wrapped the whole body in `def main()` behind `if __name__ == "__main__":` — preserves `python tests/manual_validation_test.py` as a direct-run script exactly as before, while making `pytest --collect-only` (which matches this filename via the default `*_test.py` pattern) a true no-op. Verified: `pytest tests/manual_validation_test.py --collect-only` now reports "no tests collected" in 0.16s instead of attempting a network call.
- **27b**: `test_passes_when_no_workflow_id_and_files_in_feature_dir` genuinely depended on this repo's own real `.hephaestus/features/` directory having at least one entry — confirmed by temporarily moving that directory aside and watching the test fail, then safely restoring it. Root cause: the test's `patch("pathlib.Path.exists", return_value=True)` covers `feature_dir.exists()`, but `feature_dir.iterdir()` is never mocked and still hits the real filesystem — an empty or missing real directory makes the fallback loop find nothing, independent of the `Path.exists` lie. Rewrote using `tmp_path` and a real, controlled file at the exact path the code's own fallback logic checks (`d / "docs" / declared_output`, with `declared_output` itself already `"docs/output.md"` — the real file needs both `docs` segments, a detail that cost one iteration to get right, verified against the actual function's search logic directly rather than guessing). Verified isolation the same way as the original bug: moved the real `.hephaestus/features/` directory aside, reran, passed; restored it. Full file: 46 passed.

## Item 28 — `libtmux`'s deprecated `attached_window`/`attached_pane` — NOT IMPLEMENTED

**The plan's premise does not hold against this environment's actually-installed libtmux.** `pyproject.toml` pins `libtmux = "^0.23.0"`; the version genuinely installed in `.venv` (confirmed via `.venv/bin/python3 -c "import libtmux; print(libtmux.__version__)"`, twice, to rule out a wrong-environment mistake) is `0.23.2` — the floor of that range. On this version, `Session.attached_window` and the corresponding `.attached_pane` are **normal, fully-functional, non-deprecated properties** — confirmed by reading the actual property source (`inspect.getsource`), which is a plain active-window lookup with no deprecation warning, and by running with `-W error::DeprecationWarning` (no warning raised). `active_window`/`active_pane` — the plan's prescribed replacement — **do not exist at all** on this installed version (`AttributeError: type object 'Session' has no attribute 'active_window'. Did you mean: 'attached_window'?`).

Migrating any of the 12 call sites (across `src/agents/messenger.py`, `launch_pipeline.py` ×4, `terminator.py`, `output_capture.py`, `manager.py` ×2, `src/monitoring/mechanical_recovery.py`, `src/autopilot/orchestrator/engine_client.py`, `src/services/agent_communication.py`) to `active_window`/`active_pane` right now would immediately break every one of them at runtime — not a test-only regression, a production one, since these are the actual tmux-pane-writing code paths every agent launch goes through.

**The plan's own cited evidence doesn't hold up either.** Ran all four test files the plan names as currently failing because of this: `test_stable_transcript.py` (all pass), `test_transcript_processing.py` (1 failure, unrelated — a transcript content-parsing assertion about token-usage strings leaking into output, nothing to do with tmux panes), `test_agent_manager.py` (32 passed, 0 failed), `test_orphan_reaper.py` (21 passed, 0 failed, confirmed earlier this session's own Tier 2 work). None show a libtmux-API-related failure in this environment.

**Conclusion**: either the plan's original investigation ran against a different environment where a newer libtmux had been installed (satisfying `pyproject.toml`'s permissive `^0.23.0` range but not what's actually pinned/installed here), or the finding was speculative and never fully verified against this repo's real dependency state. Not fixed. If this genuinely needs doing, it's a two-step change — bump the installed libtmux to ≥0.31 first, confirm nothing else in the dependency tree breaks, *then* migrate the 12 call sites — not a drop-in string replacement against the current environment.

## Verification

- Items 22, 24, 25: no code change, verified by direct reading (text content, exemption logic, duplication state).
- Item 23: full regression run across all 5 touched files' test coverage (9 test files), all green.
- Item 26: read the changed comment back, confirmed it now names the real current file.
- Item 27a: `pytest --collect-only` no longer triggers network I/O.
- Item 27b: isolation re-verified the same way the bug was originally reproduced (moving the real directory aside), full file green (46 passed).
- Item 28: not applicable — see above. No regression risk since nothing was changed.
- `ruff check` clean on every touched file (compared against `git show HEAD:<file>` baseline where any pre-existing findings exist — `control_routes.py`, `queue_routes.py`, `queue.py` each have identical pre-existing counts before and after).

## Explicitly out of scope

- Item 25's actual test-porting work (~180 tests) — bigger than this tier, per the plan's own words.
- Item 28's migration — blocked on a dependency version decision the user should make, not something to force through against a stale plan claim.
- Phase 4 (delete dead code) — the next and final phase of the plan.

No commits — left in the working tree for review.

# Remaining test failures — diagnosis and next steps

Written 2026-08-19, at the end of the Phase 1/2 gap audit
(`design_docs/phase1_phase2_gap_audit_findings.md`).

The suite went from **82 failing entries to a single-digit tail** over that
audit. Everything closed along the way was systematic — dead pytest
configuration, one fixture leaking global app state, timezone-dependent
tests, refactor drift, test-DB isolation. Those classes are now guarded by
structural tests rather than conventions.

What is left is different in kind: individually-rooted failures, each needing
its own decision. Several are **not test bugs** — they are places where
production behaviour and a test's encoded contract genuinely disagree, and
someone has to say which is right. That is why they are written up rather than
fixed.

**Verification note.** Each entry below was reproduced by running its file in
isolation, so none of these are the order-dependent artefacts that made up the
bulk of the original 82. Three files that appeared in earlier failure lists —
`test_monitoring_integration.py`, `test_heal_orphaned_agent_branches.py`,
`test_task_completion_service.py` — now pass cleanly on their own and are
**not** listed here; they were pollution victims, already fixed.

---

## 1. `test_goto_reconvergence.py::test_start_next_phase_honors_action_target_phase_skipping_intermediates`

**A product decision, not a bug.**

```
assert execution.status == "pending"
E  AssertionError: assert 'skipped' == 'pending'
```

When a goto skips forward past intermediate phases, `_start_next_phase`
(`phase_manager.py:1414`) now marks those intermediates `"skipped"`. The test
asserts they stay `"pending"`, and its comment states the intent plainly:
*"Intermediate phases must be left untouched -- not started."*

Both behaviours are defensible. `"skipped"` is more informative — it records
that the phases were consciously bypassed rather than merely not reached yet.
`"pending"` preserves the option of the workflow coming back to them later,
which is exactly what a reconvergence test is about.

**Needed:** a decision on whether a skipped-past phase is terminal. If
`"skipped"` is intended, update the test and its comment. If `"pending"` is,
the production change is a regression. Do not "fix" this by changing whichever
side is easier.

## 2. `test_self_review_migration.py::test_backfills_phases_whose_self_review_is_the_json_null_literal`

**A real bug in the migration.** Smallest, most clear-cut item here.

`_migrate_self_review_columns` (`database.py:1841`) backfills with:

```sql
UPDATE phases SET self_review = :value
WHERE name = 'development' AND self_review IS NULL
```

A row whose `self_review` column holds the JSON **string** `'null'` is not SQL
`NULL`, so the `WHERE` never matches and the row is never backfilled. Its
sibling test (`..._with_a_true_sql_null`) passes, confirming the SQL-NULL half
works and only the JSON-null-literal case is unhandled.

Consequence: a phase stored that way silently never gets self-review enabled.

**Fix:** widen the predicate to also match the literal, e.g.
`AND (self_review IS NULL OR self_review = 'null')`. Low risk — it is an
idempotent backfill guarded by phase name.

## 3. `test_validation_system.py::TestValidatorAgent::test_spawn_validator_agent`

**Test-mock issue, but it documents a sharp edge worth keeping.**

```
src/validation/validator_agent.py:177: in spawn_validator_agent
    f"[spawn_validator_agent] Skipping: phase {validation_task.phase_id[:8]} "
E  TypeError: 'Mock' object is not subscriptable
```

The test's `validation_task` is a `Mock`, so `phase_id` is a `Mock` too. That
makes `check_phase_sibling_active` (added by Phase 2 §4.3) return a truthy
"sibling", the guard fires, and the log line then slices a `Mock`.

Production is safe: `check_phase_sibling_active` returns `None` early when
`phase_id` is falsy, so the guard cannot fire with an unsliceable `phase_id`.
The crash is reachable only from a mock that is truthy but not a string.

**Fix:** give the mock a real `phase_id` string, or stub the sibling lookup.
Note this is the *same* class as the drift fixed during the audit — §4.3 added
a guard and this test never learned about it.

## 4–7. `tests/integration/test_task_deduplication_flow.py` (4 failures)

**Needs real investigation. The most substantive item here.**

```
assert 0 == 1
  where 0 = AgentManager.create_agent_for_task.call_count
```

Creating a task no longer dispatches an agent in this integration flow. All
four failures share that shape (`0 == 1`, `0 == 2`, and one
`Expected 'mock' to have been called once`). Three tests in the same file pass,
so the fixture and DB wiring are sound — it is specifically dispatch that is
not happening.

Plausible causes, in order of likelihood:

1. **A dispatch guard added during Phase 2 §4.3.** `check_phase_sibling_active`
   now blocks dispatch when another task on the same phase is active. If these
   fixtures put sibling tasks on one phase, dispatch is correctly suppressed
   and the tests encode a pre-guard world.
2. **`_check_duplicate_active_agent`**, the sibling guard's older cousin.
3. **Queue gating** — the task may be queued rather than dispatched, in which
   case the assertion should target the queue, not `create_agent_for_task`.

**Do not** simply assert `call_count == 0`. If (1) is the cause the tests
should be rewritten around the guard; if dispatch is genuinely broken for
deduplicated tasks, that is a live bug in the dispatch path and the more
important finding.

## 8. `test_mcp_server_tickets.py::TestCreateTaskValidation::test_create_task_requires_ticket_id_when_tracking_enabled`

**Product decision, and it touches a Tier 3 item.**

```
assert 200 in [400, 422]
```

With ticket tracking enabled, `create_task` accepts a request with no
`ticket_id` instead of rejecting it. The gate still exists at
`server.py:1975`, so either the enabling condition is not met under test, or
the requirement was deliberately relaxed.

This is the same subject as **Phase 3 Tier 3 item 24** ("ticket-creation
friction"), which records three separate historical fixes to this boundary and
notes that none of them consolidated it. Resolve them together: decide whether
a missing `ticket_id` is an error, and make the gate and this test agree.

## 9. `test_transcript_processing.py::TestReadTranscriptLogReal::test_streaming_chrome_separators_do_not_become_blanks`

**Fixture-vs-parser drift.**

```
At index 1 diff: '' != "sys.path.insert(0, 'src')"
Left contains 10 more items, first extra item:
  '↑62k ↓4.6k R629k CH88.3% $0.033 6.1%/1.0M (auto)'
```

The parser is leaving CLI status-bar chrome (`↑62k ↓4.6k R629k CH88.3% …`) in
the output the test expects to be stripped, and is emitting a blank where a
real source line belongs. That status-line format is a *CLI vendor's* output,
which changes independently of this repo.

**Fix:** confirm against a current real transcript whether the parser or the
fixture is stale. This is the same "hand-grown pattern list grown one
alternative at a time" shape as §4.9 and §4.10 — the durable fix is a declared
pattern set, not another regex alternative.

---

## Suggested order

1. **#2** (self-review JSON-null) — smallest, clearest, real user impact.
2. **#4–7** (dedup dispatch) — largest, and may expose a live dispatch bug.
3. **#1** and **#8** — need product decisions; batch #8 with Tier 3 item 24.
4. **#3** and **#9** — test/fixture hygiene, low risk.

## What not to do

Do not fix these by relaxing assertions to match current behaviour. Four of the
nine are cases where a test encodes a contract that production has since
changed, and the whole value of this tail is that it names those disagreements
explicitly. Six weeks of a suite sitting red is what let the earlier 82 hide a
real regression (`ba202c0`, finding 12) — the tail is small enough now that
each item can be decided on its merits.

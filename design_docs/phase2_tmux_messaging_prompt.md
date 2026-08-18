# Prompt: Phase 2, §4.5 — tmux message delivery primitive

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.5 of `docs/AUTOPILOT_REFACTOR_PLAN.md`: route `AgentCommunicationService`'s tmux calls through `AgentMessenger`. Fifth item in this session's Phase 2 sequence — §4.1 through §4.4 are done. Read their findings docs (`design_docs/phase2_dedup_findings.md`, `phase2_termination_findings.md`, `phase2_dispatch_findings.md`, and whatever §4.4's is named) for the established rigor and format before starting. This item is smaller than the prior four — a single-direction consolidation, not a multi-way merge — but don't skip the freshness check or the characterization tests on that basis.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.5 (full text, short).

## Freshness check — confirmed locations, verify before relying on them

- **`AgentMessenger`** — `src/agents/messenger.py`. Not touched by any decomposition. Has `_pane_is_wedged` stuck-shell detection and a consistent quote-escaping scheme — this is the audited, correct implementation and the migration target; don't change its behavior, route callers to it.
- **`AgentCommunicationService`** — `src/services/agent_communication.py` (note: the plan's naming implied `agent_communication_service.py`; the actual filename is `agent_communication.py` — re-verify this hasn't moved again since this handoff). Two methods to migrate:
  - `get_child_logs(...)` — line 75, its `subprocess.run(cmd, capture_output=True, text=True, timeout=5)` call at line 111.
  - `send_message_to_child(...)` — line 120, its `subprocess.run(cmd, capture_output=True, timeout=5)` call at line 157.
  
  Both build `cmd` as a raw argv list for `tmux` directly (not via `libtmux`, unlike `AgentMessenger`) with their own per-character escaping scheme — read both methods in full to understand exactly what `cmd` construction and escaping they currently do before replacing it, so you can confirm `AgentMessenger`'s approach is a strict behavioral superset (handles every case the raw-subprocess version did, plus stuck-shell detection it didn't).

## Target

Both `AgentCommunicationService` methods should call into `AgentMessenger` instead of shelling out to `tmux` directly. Two things this closes in one change, per the plan: the escaping-strategy divergence (two different quote-escaping implementations for the same underlying operation), and the missing stuck-shell (`_pane_is_wedged`) detection on the parent-child messaging path — `AgentCommunicationService`'s callers currently have no protection against writing into a wedged shell that `AgentMessenger`'s callers already get.

**Also close the async/executor gap while you're in this code, since it's directly adjacent and the plan's own text flags it**: `AgentCommunicationService`'s methods are reached from `async def` routes in `agents_api.py` with no executor offload — a synchronous `subprocess.run` blocking the event loop. Check whether `AgentMessenger`'s own methods are already async-safe (offloaded via `run_in_executor` or genuinely async under the hood) — if so, routing through it may close this gap for free; if not, note it as a separate finding rather than silently leaving it unaddressed after claiming the migration is done.

## Verification

- Write characterization tests for `AgentCommunicationService`'s *current* behavior first (both methods, including their escaping edge cases — special characters, newlines, whatever the current per-character scheme was built to handle) — these should pass against the current raw-subprocess implementation, then keep passing (same external behavior, different internals) once routed through `AgentMessenger`.
- Locate and keep green whatever existing tests cover `AgentMessenger.send_message_to_agent` and `AgentCommunicationService`'s two methods — search fresh, don't assume file names.
- If `AgentMessenger` genuinely can't handle some case the raw-subprocess version did (verify, don't assume it's a strict superset), that's a real finding — log it, don't paper over it by keeping a fallback path that defeats the point of the consolidation.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.4, all five decompositions).
- Any other Phase 2 item (§4.6 onward). Log anything found belonging to one of those.
- Don't touch `AgentMessenger` itself beyond what's needed to serve `AgentCommunicationService`'s callers — it's already audited and correct; this item is about routing callers to it, not modifying it.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions. `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>` before flagging anything as introduced by this work. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_tmux_messaging_findings.md` or similar) for anything out of scope, including the async/executor finding either way it resolves. No commits — leave everything in the working tree for review.

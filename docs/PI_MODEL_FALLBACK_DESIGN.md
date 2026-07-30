# Pi Model Fallback Design

## Goal

`agents.cli_model: Qwen3.6-27B-UD-Q4_K_XL.gguf` (`hephaestus_config.yaml:76`) is
served locally with a single inference slot — only one agent can actually be
generating against it at a time. Every other pi agent dispatched while that
slot is busy sits queued, its tmux pane frozen, for however long the slot
takes to free up. With several agents dispatched concurrently (this repo
self-hosts its own multi-project autopilot pipeline), that queue empties
slowly and development throughput drops accordingly — agents aren't failing,
they're just waiting their turn, but "waiting" and "idle" look identical from
outside the queue.

The fix: when a pi agent has been frozen too long, the monitor should switch
that agent's model in-place, over `/model`, to a configured fallback model so
it keeps working instead of sitting idle. Same tmux session, same
`--session-id`, same conversational context — just a different model backing
new turns.

The mechanism is deliberately CLI-agnostic, not pi-specific: the monitor
(`src/monitoring/monitor.py`) never checks `agent.cli_type` directly.
Whether/how a CLI supports an in-session model switch is entirely
polymorphic, via a new `CLIAgentInterface.model_fallback_keystrokes` method
(`src/interfaces/cli_interface.py`) — empty by default (no support), only
`PiAgent` overrides it today. The monitor stays unaware of pi specifically,
same as it already is for `recovery_keystrokes`/`mcp_reconnect_instructions`.

## Current State

### The existing frozen/nudge mechanism already (probably) fires on this

`_mechanical_recovery_for_agent` (`src/monitoring/monitor.py:218-558`) already
detects "frozen" by diffing the last-40-lines tmux transcript, SGR-stripped,
with volatile lines (spinners, `%`, `$`, `MCP:`, `Took`) filtered out
(`monitor.py:229,388-397,425`), across polling cycles
(`monitoring_interval_seconds`, default 60s). If the filtered signature is
unchanged for `frozen_seconds = 300` (`monitor.py:229`), it sends recovery
keystrokes + a nudge message; after `max_recov = 2` such attempts
(`monitor.py:230`) with no change, it fails the task and terminates the agent.

Pi's own status bar/spinner chrome (`PiAgent.strip_tui_chrome`,
`cli_interface.py:757-784`, and `_PI_SPINNERS`, line 753) is exactly the kind
of thing the volatile-line filter already discards — so a pi agent sitting
queued behind another agent's request, spinner animating but nothing else
changing, almost certainly already reads as "frozen" today. The problem isn't
detection; it's the response. A generic nudge does nothing for an agent that
isn't stuck, just waiting — worse, an unlucky nudge could look like a new
request and shuffle it further back in an FCFS queue. And two full 300s
cycles (10 minutes) before the task is failed and the agent torn down is a
poor outcome for "the model server was just busy."

### An existing fallback mechanism exists, but it's the wrong shape

`hephaestus_config.yaml:77-78` already has `default_fallback_cli_tool: claude`
/ `default_fallback_cli_model: sonnet`, and `monitor.py:255-330` (inside the
same `_mechanical_recovery_for_agent`, gated on hitting Claude's
session/spend-limit error text) already implements a fallback: terminate the
agent, mark its task pending, and `create_agent_for_task` a **new** agent
under the fallback `cli_type`/`cli_model`.

This is architecturally the wrong tool for what's being asked here — it
throws away the pi session (`--session-id`, all prior conversational context,
`get_session_args` at `cli_interface.py:671-682`) and launches an entirely
different CLI tool. What's wanted instead is **pi stays pi**, only the model
backing it changes, in place, mid-session — closer to `_detect_bad_model_error`
below than to this.

### The closest real precedent: Claude's in-session `/model` injection

`_detect_bad_model_error` (`monitor.py:932-975`) already does an in-session
model switch for Claude Code: on detecting Claude's "issue with the selected
model" rejection text, it sends `/model <config default>` as literal pane
input via `AgentMessenger.send_message_to_agent`
(`src/agents/messenger.py:39-100`), because `/model` is a client-side slash
command intercepted before it reaches the model — only real keystrokes work,
not a message the agent "reads and acts on." This is one-shot per agent
(`self._fixed_bad_model` set, line 955-958) and Claude-only (`cli_type !=
"claude": return False`, line 953).

`AgentMessenger.send_message_to_agent` already has a built-in two-step
send/wait/send pattern (send string → `asyncio.sleep(1)` → send bare Enter
again, `messenger.py:94-98`) — a workable template for "send `/model`, wait,
send the model name" as two separate calls rather than one line.

### Pi's own model display, and the confirmed `/model` interaction

Pi's status bar shows the live model in its chrome:
`↑221k ↓9.0k R1.2M CH99.6% $0.037  xiaomi/mimo-v2.5 • high`
(`cli_interface.py:763`, `strip_tui_chrome`'s docstring). That's the one
existing, confirmed signal for "which model is pi currently on" — useful both
to scope the detector (only fire for agents actually on the configured
default model, `agent.cli_model` — DB column, `database.py:179` — matching
`config.cli_model`) and to confirm a switch landed (status bar shows the new
model string afterward).

Confirmed against a real pi session: `/model` opens a fuzzy-searchable
picker, not a one-line `/model <name>` command like Claude's. Two separate
sends, not one:

1. Send `/model` — opens the picker.
2. Wait 1 second.
3. Send `mimo-v2.5-pro` (the search text, not the full `provider/model`
   path) — narrows the picker to a single match and selects it. Confirmed
   output: `Model: xiaomi/mimo-v2.5-pro`. Note the resolved provider is
   `xiaomi`, not `openrouter` as originally assumed — `xiaomi/mimo-v2.5-pro`
   is the real model id (Xiaomi's MiMo model, routed through OpenRouter as
   the API backend, per the existing `xiaomi/mimo-v2.5-pro` reference already
   in a comment at `manager.py:468`). The fallback config value is the
   search text `mimo-v2.5-pro`, not a full path.

This maps directly onto `AgentMessenger.send_message_to_agent`'s existing
behavior (send text, press Enter — `messenger.py:94-98`) with no raw
`tmux send-keys` choreography needed: call it once for `/model`, sleep 1s,
call it again for `mimo-v2.5-pro`.

## Design (implemented)

### New config

Mirrors the existing `default_fallback_cli_tool`/`default_fallback_cli_model`
pair (`hephaestus_config.yaml:77-78`) rather than hardcoding the fallback
model into `monitor.py` — it's project/deployment-specific, not a constant.
Deliberately named without "local" — the mechanism triggers on any sustained
freeze on pi's configured model, not specifically a "local model" concept:

```yaml
agents:
  pi_model_fallback_wait_seconds: 120  # shorter than frozen_seconds=300
  pi_model_fallback: mimo-v2.5-pro     # picker search text, not a full
                                        # provider/model path -- resolves
                                        # to xiaomi/mimo-v2.5-pro
```

Read via `simple_config.py` alongside the existing `cli_model`/
`default_fallback_cli_model` reads.

### New mechanical check: `_detect_pi_model_fallback`

Added to the Phase-0 mechanical-recovery list in `_monitoring_cycle`,
directly after `_mechanical_recovery_for_agent` — same one-shot-per-agent
bookkeeping pattern as `_detect_bad_model_error` (a
`self._switched_to_fallback_model` set, mirroring `_fixed_bad_model`).

Trigger conditions, all required:
- `agent.cli_type == "pi"`
- `config.pi_model_fallback` is set (feature is opt-in via config;
  absent/empty disables it entirely)
- `agent.cli_model == config.cli_model` (i.e. still on the configured
  default model — an agent already running something else, including a
  prior fallback switch, is left alone)
- agent not already in the one-shot set
- the agent's frozen duration has reached `pi_model_fallback_wait_seconds`

Placed **after**, not before, `_mechanical_recovery_for_agent` in the Phase-0
list — deliberately, not merely following `_detect_bad_model_error`'s
position: the frozen-duration signal is read from
`_mechanical_recovery_for_agent`'s own `self._stuck_state[agent.id]`
bookkeeping (`since` timestamp) rather than a second, independent signature
comparison, and that state is only current for the running cycle once
`_mechanical_recovery_for_agent` has actually run for this agent. Running
after it means every check reads freshly-updated state; no risk of a
one-cycle-stale read. This ordering doesn't cause redundant double-
intervention in practice either: `pi_model_fallback_wait_seconds` (120s
default) is well under `frozen_seconds` (300s), so this check fires and
one-shots the agent well before `_mechanical_recovery_for_agent`'s own
nudge/fail path would ever trigger for the same freeze.

Action: `send_message_to_agent(agent.id, "/model")`, `asyncio.sleep(1)`,
`send_message_to_agent(agent.id, config.cli_model_fallback)`. Logs a
`[CLI-MODEL-FALLBACK]` line matching the existing `[BAD-MODEL]`/
`[SESSION-LIMIT]` logging convention. The agent is added to the one-shot set
before sending, and its `_stuck_state` entry is popped afterward — so the
fallback model's own first turn gets a fresh 300s window from
`_mechanical_recovery_for_agent`, rather than being judged against a
signature captured while still on the original model.

### Capturing why, on the agent/task record

Both this mechanism and the existing session-limit terminate+relaunch
fallback (`monitor.py:255-395`) now write an `AgentLog` entry (`agent_id`,
`log_type`, `message`, `details` JSON) via a shared `_log_agent_event`
helper, matching the existing convention already used for Guardian/Conductor
writes. This matters because `Task.failure_reason` — the other place these
paths briefly touch — gets cleared again once a task is successfully
redispatched (session-limit path) or was never set at all (this mechanism
doesn't fail the task), so without a separate durable record there was no
queryable trace of *why* an agent's model changed or it got terminated,
only a transient process-log line. `_log_agent_event` accepts an optional
already-open `session` (used by the session-limit call sites, which already
hold one) to avoid a second nested `session_scope()`; called standalone
(this mechanism's own call sites) it opens its own.

`log_type` values: `cli_model_fallback` (switch sent), `session_limit_terminated`
(both the redispatch and no-fallback-available cases).

### Verifying the switch landed (implemented)

`_detect_cli_model_fallback` records `self._pending_fallback_verification[agent.id]
= (model, original_model, switched_at)`. A new `_verify_cli_model_fallback`,
called for every agent each cycle, checks pending entries via
`CLIAgentInterface.model_fallback_confirmed(output, model)` (polymorphic —
PiAgent's override regex-matches `Model: <provider>/<model>`, requiring the
`Model: ` prefix rather than a bare substring search, so the search text
merely being echoed back as typed input — if the picker never actually
opened — doesn't read as a false confirmation). `None` (CLI can't verify) or
`True` (confirmed) both clear the pending entry immediately, logging success
via `_log_agent_event` in the `True` case (`cli_model_fallback_confirmed`).
`False` stays pending until `2 * monitoring_interval_seconds` has elapsed
since the switch, then logs a warning + `AgentLog` (`cli_model_fallback_unconfirmed`).

### Persisting the switch, and two gaps that fell out of doing so

`agent.cli_model` is surfaced directly in API responses (`mcp/api.py`,
`mcp/autopilot_api.py`) for UI display, and `get_active_agents()` re-fetches
a fresh row every cycle — so `_detect_cli_model_fallback` also writes the new
model to the `Agent.cli_model` DB column (in the same session as its
`AgentLog` write), not just the in-memory one-shot set. Without this, the UI
would keep showing the stale original model as "current" indefinitely after
a real switch.

That persistence interacts with two things that needed fixing once added:

1. **A confirmed-failed switch must not permanently strand the agent.**
   `_verify_cli_model_fallback`'s unconfirmed-past-grace-period branch also
   clears the agent from `_switched_to_fallback_model` — the one-shot
   restriction is meant to stop a *successful* switch from being re-sent,
   not to burn the agent's only chance at recovery on a single failed
   picker interaction (e.g. a transient timing miss). If it freezes again
   later on the still-unswitched original model, `_detect_cli_model_fallback`
   can try again.
2. **That retry would otherwise be immediately blocked by the optimistic
   `Agent.cli_model` write from the first (failed) attempt** —
   `_detect_cli_model_fallback`'s own gate (`agent.cli_model !=
   config.cli_model`) would see the agent as already switched, even though
   the CLI session itself never actually changed. So the same unconfirmed
   branch also reverts `Agent.cli_model` back to `original_model` (which is
   why the pending-verification tuple carries it) before clearing the
   one-shot set.

A confirmed switch, by contrast, leaves both `Agent.cli_model` and the
one-shot set alone — no automatic switch-back (see below).

### Extended to the secondary/fallback CLI (implemented)

`default_fallback_cli_tool: claude` (`hephaestus_config.yaml`) is itself a
primary agent from this mechanism's point of view — if pi hits a session
limit and gets terminated+relaunched under claude (the existing
`monitor.py:255-395` path), that claude agent can equally sit frozen too
long and benefit from the same in-place switch. Two gate bugs had to be
fixed to make that actually reachable, not just add a Claude-specific
keystroke sequence:

- **`_detect_cli_model_fallback` read a single global
  `config.cli_model_fallback`.** That value (`mimo-v2.5-pro`, a picker
  search text) is pi's own vocabulary — Claude Code's `/model` doesn't
  recognize it, and more to the point, hardcoding one shared config value
  across every CLI is wrong in general (each CLI's valid model strings are
  its own namespace). Replaced with polymorphic
  `CLIAgentInterface.fallback_model(config)` — `PiAgent` reads
  `config.cli_model_fallback`, `ClaudeCodeAgent` reads the new
  `config.secondary_cli_model_fallback` (named for its *role* — "whichever
  CLI serves as the fallback tier" — not the literal CLI product, matching
  `default_fallback_cli_tool`'s own naming; default `sonnet`... though note
  every claude-dispatched phase in this repo's own workflow YAMLs already
  sets `cli_model: sonnet` as its primary, so with the shipped default this
  particular fallback is presently a same-model no-op for claude — a
  different value should be picked once there's a real escalation tier to
  fall back to).
- **The "already off default model" gate compared against pi's global
  default unconditionally.** `agent.cli_model != config.cli_model` is only
  the right comparison when `agent.cli_type == config.default_cli_tool`
  (pi) — for any other CLI, `config.cli_model` is meaningless (it's not
  claude's baseline). A claude agent's `cli_model` (typically `sonnet`, set
  per-phase) would never equal `"Qwen3.6-27B-UD-Q4_K_XL.gguf"`, so the gate
  silently excluded every claude agent regardless of whether
  `model_fallback_keystrokes` was implemented for it. Fixed to compare
  against `cli_agent.default_model` (the per-CLI class attribute) whenever
  `agent.cli_type` isn't the primary `default_cli_tool` — mirroring
  `manager.py`'s own `global_model` resolution for the identical reason.

`ClaudeCodeAgent.model_fallback_keystrokes` reuses the one-line `/model
<name>` syntax already confirmed working in `_detect_bad_model_error` (no
picker step, unlike pi) — see that method's own related fix below.
`ClaudeCodeAgent.model_fallback_confirmed` is deliberately left
unimplemented (inherits the base class `None`): there's no confirmed
evidence of what Claude Code echoes after a successful `/model` switch, so
guessing a regex risks a false verdict either way — `_verify_cli_model_fallback`
correctly treats `None` as "can't verify, skip silently" rather than
fabricating a check.

**Related fix in `_detect_bad_model_error` (Claude-only, pre-existing,
unrelated code path but the identical bug):** it computed its own recovery
model as `getattr(config, "cli_model", None) or "sonnet"` — the same
pi-specific global, sent to Claude via `/model` on a bad-model rejection.
Now reads `config.secondary_cli_model_fallback` (falling back to `"sonnet"`
same as before if unset), for the same reason as the gate fix above.

## What This Does Not Do

- **No automatic switch-back.** Once an agent falls back to
  `xiaomi/mimo-v2.5-pro`, it stays there for the rest of that agent's task,
  even if the original model frees up moments later. Decided, not deferred:
  switching back would require observing the original model's availability
  independently (not just "is *this* agent frozen"), which is meaningfully
  more machinery for a benefit that's marginal — an agent's task is usually
  a small fraction of a pipeline run.
- **No slot-aware scheduling.** This is purely reactive (an agent already
  frozen too long) — it doesn't change dispatch order or try to keep the
  local slot's queue short in the first place.
- **No change to the existing Claude session-limit fallback** (`monitor.py:
  255-395`) or `_detect_bad_model_error` — this is a new, narrower, third
  mechanism alongside them (now sharing only the `_log_agent_event` helper),
  not a replacement.

## Testing Plan

- Unit: `_detect_cli_model_fallback` fires only when all trigger conditions
  hold (CLI without model-fallback support, already-fallback `cli_model`,
  feature disabled via missing config, frozen duration under threshold,
  already one-shot — each as a separate negative test), git-stash-verified.
- Unit: one-shot semantics — a second cycle for the same agent does not
  re-send even if still frozen.
- Unit: the two-step send happens in the right order with the right content
  (mock `send_message_to_agent`, assert call args/order), and
  `_stuck_state` is cleared afterward.
- Unit: the switch writes an `AgentLog` entry with the right `agent_id`/
  `log_type`/`details`, and persists the new model onto `Agent.cli_model`
  (asserted against a mocked session's `.add()`/query-row calls).
- Unit: `_verify_cli_model_fallback` — no pending entry is a no-op; a CLI
  that can't verify (`None`) clears pending without logging; confirmed logs
  success and leaves the one-shot set alone; unconfirmed within the grace
  period stays pending with no warning; unconfirmed past the grace period
  warns, logs, clears the one-shot set (retry re-enabled), and reverts
  `Agent.cli_model` back to the original. All git-stash-verified.
- Unit: a CLI genuinely without support (`opencode`, base-class defaults)
  stays a no-op regardless of freeze duration.
- Unit: claude, as the secondary/fallback CLI, gets its own fallback via
  `config.secondary_cli_model_fallback` and isn't blocked by the
  pi-specific gate — the gate fix is exercised through the real
  `get_cli_agent("claude")` resolution, not mocked.
- Unit: `_detect_bad_model_error` (the pre-existing, unrelated Claude-only
  path) reads `config.secondary_cli_model_fallback`, falling back to
  `"sonnet"` when unset.
- Manual/live: watch one real frozen pi agent switch over on this repo's own
  self-hosted pipeline, confirm the transcript shows `Model:
  xiaomi/mimo-v2.5-pro`, an `AgentLog` row exists for the switch, and the
  agent resumes producing output.

## Open Questions

1. **`cli_model_fallback_wait_seconds` value** — shipped default 120s (well
   under `frozen_seconds=300` so this preempts the generic nudge/fail path)
   is a guess. What's an actual observed typical queue wait on this
   deployment's local slot? Worth tuning once there's real data.
2. **`secondary_cli_model_fallback: sonnet`** — as shipped, this is a
   same-model no-op for every claude-dispatched phase in this repo's own
   workflow YAMLs (all already primary on `sonnet`). Needs a real
   escalation target (e.g. `opus`) once claude's own freeze behavior is
   observed live and there's a concrete "next tier" to fall back to.

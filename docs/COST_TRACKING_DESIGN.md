# Cost Tracking Design

## Goal

Track actual dollar cost per `Task`, rolled up to `Feature` → `AutopilotDesign`
→ `AutopilotProject`, sourced from real usage data reported by whichever CLI
tool (`pi`, Claude Code, OpenCode, Codex) ran the agent, plus the backend's
own direct OpenRouter calls (task enrichment, guardian, conductor, etc. — see
`src/interfaces/langchain_llm_client.py`). A per-project spend limit,
configured in project settings, automatically pauses that project's autopilot
pipeline once cumulative cost crosses it.

## Current State

There is no real cost tracking today, despite OpenRouter calls happening
constantly. What exists is dead or unpopulated:

- `src/interfaces/cost_tracker.py` (`CostTracker`) queries a LiteLLM proxy's
  spend endpoints. Not imported anywhere in `src/`. `hephaestus_config.yaml`
  has `# litellm_proxy:` commented out — this project talks to OpenRouter
  directly, no proxy sits in front of it.
- `src/interfaces/openrouter_client.py` — also unused/orphaned, same dead-code
  family as `cost_tracker.py`.
- `orchestrator.py:183` has a `cost_total: float = 0.0` field on a report
  dataclass, surfaced at `autopilot_api.py:2801` (`"cost_total":
  report.cost_total`) — but nothing ever sets it above 0.0. The UI-facing
  field exists; the pipeline that would fill it doesn't.

## What's actually obtainable, per source

`pi`, Claude Code, and Codex are launched as **persistent interactive tmux
sessions**, not one-shot `--print`/`-p` invocations — see `get_launch_command`
in `src/interfaces/cli_interface.py` (`ClaudeCodeAgent`, `CodexAgent`,
`PiAgent`). That rules out capturing a single JSON result blob per task from
stdout for those three; a session can span many tasks (shared `session_role`,
see below) or a task can span many turns. Cost for those has to come from
**tailing each CLI's own session transcript file**, not from parsing tmux
pane output. **OpenCode is the exception** — verified below to be invoked
one-shot in this codebase, which changes its collection strategy entirely.

### `pi` — confirmed against real session files, ready to use

Verified directly against `~/.pi/agent/sessions/` on this machine (not just
inferred). Two things the original pass got wrong, corrected here:

**Directory key is the agent's sanitized `cwd` (its worktree path), not a
"project hash".** e.g. for an agent working in
`/Users/hmuhlestein/code/applitnator/.worktrees/wt_feature-des-7618-status-streaming`,
the directory is
`~/.pi/agent/sessions/--Users-hmuhlestein-code-applitnator-.worktrees-wt_feature-des-7618-status-streaming--/`
(slashes → dashes, wrapped in a leading/trailing `--`).

**Filename is `<ISO-creation-timestamp>_<session-id>.jsonl`, not just
`<session-id>.jsonl`.** e.g.
`2026-07-10T02-17-55-211Z_hephaestus-usershmuhlesteincodeapplitnato-status-streaming-developer-44c673f4.jsonl`.
The deterministic session ID `src/autopilot/phases.py:48` generates
(`get_session_id`, passed via `--session-id` at launch) is the suffix after
the timestamp, not the whole filename — a collector must glob
`*_<session_id>.jsonl` inside the cwd-keyed directory, not expect an exact
match. The file's own first line is `{"type": "session", "id":
"<session-id>", "cwd": "...", ...}` — read it to confirm the glob matched the
right file rather than trusting the filename alone (cheap, and the codebase's
existing habit of verifying rather than assuming, per e.g.
`_get_phase0_completion`'s design elsewhere in this doc's sibling designs).

**Confirmed exact schema for cost-bearing lines** (`type: "message"`,
`message.role: "assistant"` — other line types seen: `session`,
`model_change`, `thinking_level_change`, none of which carry usage):

```json
{
  "type": "message",
  "id": "fbc56bac",
  "timestamp": "2026-07-10T02:18:22.869Z",
  "message": {
    "role": "assistant",
    "api": "openai-completions",
    "provider": "openrouter",
    "model": "xiaomi/mimo-v2.5",
    "usage": {
      "input": 9430, "output": 222, "cacheRead": 512, "cacheWrite": 0,
      "reasoning": 99, "totalTokens": 10164,
      "cost": {
        "input": 0.00099015, "output": 0.00006216,
        "cacheRead": 0, "cacheWrite": 0, "total": 0.0010523099999999999
      }
    }
  }
}
```

`message.usage.cost.total` is the per-turn dollar cost to sum. `model` and
`provider` are present per-turn (useful for the `CostEntry.model` column —
capture directly rather than inferring from workflow config, since a session
can in principle span a model override mid-conversation). `reasoning` token
count is worth capturing alongside input/output even though it has no
separate cost line item (rolled into `output` cost) — useful signal for
"which phases burn the most reasoning" later.

`src/agents/manager.py` already generates the exact deterministic session ID
and passes it via `--session-id` at launch — so for any given task, the
collector knows the session ID and the agent's cwd, and can find the right
file via the glob-and-verify approach above. Nothing in `manager.py`
currently reads this file for cost; it's a pure addition.

### Claude Code — verified, and materially harder than `pi` in two ways

Confirmed directly against real transcripts at
`~/.claude/projects/-Users-hmuhlestein-code-HephaestusNG/*.jsonl` (this
session's own transcript directory — same underlying mechanism a spawned
autopilot Claude Code agent uses, just a different `cwd`). Two gaps the
original "probably the same shape as pi" assumption missed:

**1. No dollar cost anywhere in the transcript — only raw tokens.** A real
assistant-turn line's `message.usage` looks like:

```json
{
  "input_tokens": 4736,
  "cache_creation_input_tokens": 2976,
  "cache_read_input_tokens": 8118,
  "output_tokens": 560,
  "cache_creation": {
    "ephemeral_1h_input_tokens": 2976,
    "ephemeral_5m_input_tokens": 0
  }
}
```

No `cost`/`cost_usd` field exists at any level (checked every line type in a
10k-line real transcript: `queue-operation`, `user`, `attachment`,
`file-history-snapshot`, `ai-title`, `assistant`, `last-prompt`, `mode`,
`system` — cost appears in none of them). `claude --help`'s
`--output-format json` / `total_cost_usd` is real but only applies to
one-shot `-p` mode, which this repo doesn't use. **This means the Claude Code
collector needs a maintained per-model price table** (`$/M input`, `$/M
output`, `$/M cache-write`, `$/M cache-read` — and Anthropic prompt caching
has two cache-write tiers with different prices, `ephemeral_1h` vs
`ephemeral_5m`, both present in the schema above) to convert
`message.usage.*_tokens` into a dollar figure, unlike `pi` which just hands
back `cost.total` pre-computed. More moving parts, and a table that goes
stale whenever Anthropic repricing happens — the `pi` collector has no
equivalent maintenance burden.

**2. No deterministic session ID today — Claude Code agents aren't
correlatable to a task at all yet.** `PiAgent.get_session_args` (§ above)
passes `--session-id <hephaestus-generated-string>` at launch, so a `pi`
session file's name is predictable before it's even created.
`ClaudeCodeAgent.get_launch_command` (`src/interfaces/cli_interface.py:245`)
passes **no session flag at all** — `claude` mints its own random UUID
filename, invisible to Hephaestus until after the fact. Worse: `claude
--help` confirms `--session-id <uuid>` exists, but it **"must be a valid
UUID"** — `get_session_id`'s current format
(`hephaestus-usershmuhlesteincodeapplitnato-status-streaming-developer-44c673f4`)
is not a valid UUID and can't be reused as-is for Claude Code. Closing this
gap requires two real code changes, not just a collector:
  - Derive a valid UUID from the same deterministic inputs (e.g. `uuid.uuid5(NAMESPACE, f"{project_id}:{design_slug}:{role}")` instead of the current hash-and-slugify scheme), and
  - Add a `session_id` kwarg + `--session-id {uuid}` to `ClaudeCodeAgent.get_launch_command`, matching what `PiAgent` already does.

  Without this, correlating a Claude Code transcript file to a specific task
  falls back to a much weaker heuristic (glob the whole cwd-keyed directory
  for files whose first-line `timestamp` falls inside the agent's known
  launch window) — workable, but meaningfully less exact than `pi`'s
  filename-is-the-answer lookup, and worth doing the UUID5 fix instead of
  living with the heuristic long-term.

### OpenCode — different shape entirely, and genuinely simpler to collect from

`OpenCodeAgent` exists in `src/interfaces/cli_interface.py:325` as a
supported `cli_type` (unclear how actively used vs. `pi` in the current
deployment — worth checking `config/workflows/autopilot/workflow.yaml`'s
`phase_cli_tool` overrides before prioritizing this). `opencode` is installed
on this machine; verified directly.

**Correction to the "all three CLIs run as persistent tmux sessions"
claim earlier in this doc — it's wrong for OpenCode specifically.**
`OpenCodeAgent.get_launch_command` runs `opencode run "$(cat
{prompt_file})" --model {model}` — no `-i`/`--interactive` flag.
`opencode run --help` confirms `-i, --interactive` defaults to `false`,
meaning this is a genuine one-shot invocation (send the message, get the
response, process exits) unless interactive mode is explicitly requested.
This is architecturally different from `pi` and Claude Code, both invoked
here to stay open across an entire task via tmux.

**This makes OpenCode's cost collection simpler, not harder, if it's
actually in use**: `opencode run` supports `--format json` ("raw JSON
events" per `--help`) for exactly this one-shot invocation pattern —
Hephaestus could capture the per-invocation result (including cost) directly
from the process's own stdout, no transcript-file tailing or session-ID
correlation needed at all. Not yet confirmed what the JSON payload actually
contains (would take one real, cheap `opencode run --format json "hi"` call
to verify — didn't run it, since that spends real API money without
explicit sign-off).

**Real dollar cost is directly available, unlike Claude Code.** Verified via
`opencode export <sessionID>` on an existing session on this machine:
assistant messages have a top-level `cost` field (a real dollar figure, not
tokens-only) plus a full `tokens: {input, output, reasoning, cache: {read,
write}}` breakdown, `modelID`, `providerID`, and `path.cwd`. Also verified
`opencode stats` is a working built-in command that aggregates real
cost/token totals across all local sessions (`$25.68` total, `--days`/
`--project` filters exist) — useful as an independent cross-check tool during
implementation, not something to build a collector around directly (it's a
CLI aggregate view, not a task-scoped data source).

**Storage is SQLite (`~/.local/share/opencode/opencode.db`), not JSONL** —
architecturally different from both `pi` and Claude Code. If OpenCode ends up
in scope, the collector queries this DB directly (or shells out to `opencode
export`) rather than tailing a text file, which also sidesteps the
line-count-checkpoint mechanism built for `pi`/Claude Code entirely — a
DB query can filter by timestamp/session directly.

**Same session-ID correlation gap as Claude Code, unconfirmed whether it's
fixable the same way.** No session flag is passed in `get_launch_command`
today. `-s, --session <id>` is documented as "session id to **continue**"
(not "create with this id") — unlike `pi`'s `--session-id` (freely
create-or-resume with an arbitrary string) and Claude Code's `--session-id
<uuid>` (confirmed to accept a caller-chosen new UUID), it's unverified
whether OpenCode's `-s` can mint a *new* session under a caller-chosen ID or
only resumes a pre-existing one — needs one live test before assuming the
same UUID5 fix from the Claude Code section applies here too.

### Codex — unresolved, follow-up needed

`codex` isn't installed on this machine; couldn't inspect its transcript
format or `--help` output directly. Before building a Codex collector,
someone needs to check (a) whether it's actually used as an agent CLI in
practice in this deployment (`cli_type` default vs. per-phase override — see
`config/workflows/autopilot/workflow.yaml`'s `phase_cli_tool`), and (b) what
local transcript file, if any, it writes. Scope this collector as a stub that
logs "unsupported" rather than silently reporting zero cost as if it were
accurate.

### Backend's own direct OpenRouter calls — one-line opt-in, output path unverified

`src/interfaces/langchain_llm_client.py` already plumbs `extra_body` through
to the `openrouter` provider's `ChatOpenAI` construction (~lines 239-289).
OpenRouter's non-standard `usage.cost` field (returned when the request body
includes `usage: {include: true}`) is a one-line addition:

```python
model_kwargs={"extra_body": {..., "usage": {"include": True}}}
```

Whether that then survives LangChain's response parsing into somewhere
readable (`response.response_metadata["token_usage"]`, which preserves raw
non-standard fields rather than dropping them, per `ChatOpenAI`'s general
behavior) needs a live test call to confirm — LangChain normalizes *known*
OpenAI fields into `usage_metadata` but is not guaranteed to promote
provider-specific extensions the same way. Treat this as needing one
confirmatory smoke test before relying on it, not requiring new
infrastructure.

## Data Model

### New table: `cost_entries` (append-only ledger, source of truth)

One row per LLM turn/call, not per task — a task can span many turns (a `pi`
conversation turn, a Claude Code turn, one `enrich_task` OpenRouter call).
Aggregates are derived from this table, not hand-maintained, mirroring this
codebase's existing self-healing derivation pattern
(`src/core/status_derivation.py`) rather than trusting a single mutable
running-total column that can drift under concurrent writes.

```python
class CostEntry(Base):
    __tablename__ = "cost_entries"

    id = Column(String, primary_key=True)  # cost-<uuid8>
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)

    # 'pi' | 'claude_code' | 'opencode' | 'codex' | 'openrouter_direct'
    source = Column(String, nullable=False)
    model = Column(String, nullable=True)  # e.g. "anthropic/claude-sonnet-4"

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, nullable=False)

    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Raw source line/turn, kept for debugging discrepancies without needing
    # to re-derive from the original transcript file (which may rotate/get
    # cleaned up) -- same rationale as AgentLog keeping raw output.
    raw_usage = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_cost_entries_task_id", "task_id"),
        Index("ix_cost_entries_workflow_id", "workflow_id"),
    )
```

`task_id` is nullable because some calls genuinely aren't task-scoped (a
`guardian`/`conductor` housekeeping call with no task context) — those roll up
to workflow-level or a project-wide "overhead" bucket, never silently dropped.

### Denormalized rollup columns (fast reads, self-healing like status derivation)

Add `cost_total_usd = Column(Float, default=0.0, nullable=False)` to `Task`,
`Feature`, `AutopilotDesign`, and `AutopilotProject`. Populated by a
`derive_cost_totals` module (`src/core/cost_derivation.py`), same shape as
`src/core/status_derivation.py`: `SUM(cost_entries.cost_usd)` grouped by
`task_id`, then rolled up through `Feature.workflow_id == Task.workflow_id`,
`Feature.design_id`, `AutopilotDesign.project_id` — recomputed on write (when
a new `CostEntry` lands) rather than trusted as independently-maintained
state, so a missed update never permanently desyncs the displayed total from
the ledger.

## Budget Enforcement

**Scope assumption, stated explicitly since the request was terse:** "set in
project config" is read as a single cumulative-spend cap per
`AutopilotProject` (not per-feature or per-design), matching the top of the
rollup chain this whole design already builds toward. "A link in the
Autopilot ui design screen" is read as: the Autopilot page's design view
gets a visible cost-so-far indicator with a link/button into the existing
project settings surface (`ProjectSettingsModal.tsx`) where the limit is
actually configured, rather than a second, separate place to set it. If
either reading is wrong, the schema/enforcement pieces below are unaffected
either way — only the UI subsection would need adjusting.

### Schema

Add directly to `AutopilotProject` (not `ProjectContext`'s generic key-value
store — that table's `key` column is globally unique with no per-project
namespacing, which would force an awkward `f"cost_limit:{project_id}"` key
scheme for what is, structurally, just a real per-row field on a table that
already exists and already has a `PUT /projects/{project_id}` endpoint to
extend):

```python
cost_limit_usd = Column(Float, nullable=True)  # None = no limit
```

`cost_total_usd` (added to `AutopilotProject` in the Data Model section
above) is what gets compared against it — no new "current spend" field
needed, the rollup already produces it.

### Enforcement trigger

Hooks into the same `cost_derivation.py` recompute-on-write path (Data Model
section above) that already updates `AutopilotProject.cost_total_usd` on
every new `CostEntry`. After that update, one additional check:

```python
if project.cost_limit_usd is not None and project.cost_total_usd >= project.cost_limit_usd:
    _trigger_budget_pause(project.id)
```

`_trigger_budget_pause` does what the (now-fixed) `/autopilot/stop` endpoint
does — terminate active agents (with `terminated_at` set, per the invariant
fixed earlier tonight) and mark active workflows `paused` — with one
difference: `paused_by = "budget"` instead of `"user"`.

**Gap review caught a real hole here: don't literally reuse `/autopilot/stop`'s
query as-is — it misses Phase 0.** That endpoint filters
`Workflow.definition_id == "autopilot"`. But the Feature Architect (Phase 0)
launches its own separate workflow under `definition_id == "autopilot-phase0"`
(`orchestrator.py:4628`, confirmed by grep — `run_single_workflow(sdk,
"autopilot-phase0", ...)`). A budget pause that only matches `"autopilot"`
would leave an in-progress Feature Architect run untouched, still spending,
completely defeating the cap for that slice of work — exactly the kind of gap
this design is supposed to close. Fix: extract the actual pause/terminate
logic out of the `/autopilot/stop` route handler into a plain function (e.g.
`_pause_project_workflows(project_id: str, paused_by: str) ->
int`, in `orchestrator.py` alongside `pause_workflow_direct`) that both the
HTTP endpoint and `_trigger_budget_pause` call, filtering
`Workflow.definition_id.in_(["autopilot", "autopilot-phase0"])` — not one
hardcoded string — so the endpoint gets the same fix for free instead of the
two call sites silently drifting apart over time.

**Idempotency, given concurrent `CostEntry` writes:** up to
`MAX_PARALLEL_FEATURES` (4, `orchestrator.py:86`) features can be recording
cost concurrently. Multiple near-simultaneous writes could each independently
observe `cost_total_usd >= cost_limit_usd` and call
`_trigger_budget_pause` redundantly. Make `_pause_project_workflows` a no-op
past the first call (it already only matches `status.in_(["active",
"running"])` — once every matching workflow is `paused`, a second concurrent
call simply finds nothing left to pause and returns 0, which is naturally
idempotent as long as the query-then-update happens inside one transaction
per call — no extra locking needed beyond what the existing DB session
already provides).

**Spend will always land at-or-slightly-over the limit, never exactly at
it** — worth stating so nobody designs a test or the UI expecting an exact
cutoff. The `CostEntry` that pushes `cost_total_usd` past `cost_limit_usd`
represents an LLM call that already happened (cost is only knowable after
the fact, per every source investigated in this doc); enforcement can only
stop the *next* one, not retroactively cap the one that crossed the line.

### Generalizing the `paused_by` guard (small, necessary change to code fixed earlier tonight)

Every self-heal/auto-resume guard added or fixed earlier in this session
checks `wf.paused_by == "user"` specifically (`_try_auto_resume_paused_workflow`,
`_create_corrective_task`, the stuck-workflow restart in `attempt_recovery`,
and `AutopilotService.start()`'s own resume-on-play logic). A literal string
match means a `"budget"`-paused workflow would sail right through every one
of those checks as if it were a normal active workflow — none of them
currently ask "is this paused for *any* deliberate reason," only "is this
paused specifically by a user click." **Fix: change each of those checks from
`== "user"` to `is not None`** — any non-null `paused_by` means *something*
deliberately paused this and no automated path should silently revert it,
regardless of which specific reason triggered it. This is a strict
generalization (every workflow the old check protected is still protected;
`"budget"`-paused workflows now are too) — not a behavior change for the
existing `"user"` case, so it doesn't need to touch anything shipped earlier
tonight beyond widening the string comparison.

`AutopilotService.start()`'s resume-on-play logic (added earlier tonight)
specifically must **not** be widened the same way — clicking "play" should
resume a `"user"`-paused project (that's the whole point of that fix) but
should **not** silently clear a `"budget"`-pause just because the pipeline
restarted; the limit is still exceeded, and clicking play again shouldn't be
a backdoor around the cap the user configured. So `start()` keeps its
existing `paused_by == "user"` filter unchanged — it's the one place that
must stay strict rather than generalize. Raising `cost_limit_usd` (or setting
it back to `None`) is what should clear `paused_by == "budget"`, via a
one-line check added to the same `PUT /projects/{project_id}` handler that
sets the new limit: if the new limit is null or higher than
`cost_total_usd`, clear `paused_by` on that project's `"budget"`-paused
workflows so the next sweep or an explicit "play" click can resume them.

### Blocking new work, not just pausing existing work

Pausing already-active workflows isn't sufficient on its own — the queue
loop (`pick_next_design`, `orchestrator.py`) could still pick up a *new*
design or launch a *new* feature workflow for an over-budget project,
immediately exceeding the cap further before the next cost-derivation cycle
even notices. Add the same `cost_total_usd >= cost_limit_usd` check as a
guard at the top of `pick_next_design` (skip this project's designs
entirely while over budget) and in `_run_one_feature` before calling
`run_single_workflow` for a feature that hasn't started yet — cheap checks,
already have `project_id` in scope at both call sites.

### UI

- **`ProjectSettingsModal.tsx`**: add a `cost_limit_usd` number input per
  project (optional — blank/cleared means no limit), wired to the existing
  `PUT /projects/{project_id}` mutation (`apiService` already has this
  pattern for `name`/`base_dir`/`is_default` — extend `ProjectUpdate`
  similarly on the backend).
- **Autopilot design screen** (`DesignQueuePanel.tsx` or
  `PipelineStatusCard.tsx` — whichever already renders project-level status,
  matching the assumption stated at the top of this section): a small
  "$current / $limit" indicator (or just "$current spent" when no limit is
  set) with a link that opens `ProjectSettingsModal` scoped to the active
  project, so a user who sees the pipeline auto-paused for budget reasons has
  an immediate, obvious path to either raise the limit or leave it and
  investigate spend first.
- When a workflow shows `paused_by == "budget"` specifically (vs. `"user"`),
  surface that distinction in whatever status text/badge already exists for
  paused workflows — "Paused: budget limit reached" reads very differently
  from a generic "Paused," and matters for a user trying to figure out why
  their pipeline stopped without having clicked pause themselves.

## Collection Architecture

### Per-CLI transcript tailing (pi, Claude Code; Codex stub)

Note: OpenCode does **not** use this mechanism — see its own subsection
below, after Claude Code's checkpoint design.

New module `src/services/cost_collection_service.py`. One `CostCollector`
per `source`, each implementing:

```python
class CostCollector(ABC):
    def collect_since(
        self, session_file: Path, checkpoint: Optional[str]
    ) -> tuple[list[CostEntry], str]:
        """Return new cost entries since `checkpoint`, and the new checkpoint
        (e.g. byte offset or last-seen turn id) to persist."""
```

**Why a checkpoint, not "just sum the whole file":** `SESSION_ROLES`
(`src/autopilot/phases.py:45`) deliberately makes multiple tasks share one
`pi`/Claude Code session (e.g. `architecture_design` and
`architectural_review` both map to role `architect`) so the agent keeps prior
conversational context — this is the exact mechanism behind the
resumed-session bug fixed earlier in `src/agents/prompt_builder.py`. Summing
an entire session file's cost and attributing it to whichever task happens to
be running "now" would double-count every earlier task sharing that session.

**Gap review caught a real bug in the first draft of this checkpoint design:
it must be keyed by `session_id`, not by `Agent.id`.** The original draft put
`cost_checkpoint` on the `Agent` row. But `get_session_id(project_id,
design_slug, phase_name)` is a pure function of project/design/role — it has
**no dependency on which `Agent` row is currently driving it**. When an agent
dies mid-phase and `attempt_recovery`/a retry creates a *new* `Agent` row for
the same role, that new agent gets the exact same deterministic session ID
and resumes the exact same `pi`/Claude Code session file the dead agent was
using — the file already contains the dead agent's turns. A checkpoint stored
on the new `Agent` row starts at 0 and would re-read and re-bill every turn
the dead agent already ran, double-counting on every single retry. This is
the identical shape of bug as the resumed-session issue — state that should
be scoped to the durable session, mistakenly scoped to the ephemeral thing
driving it at any given moment.

**Fix: a new small table, keyed by `session_id` (which outlives any one
`Agent` row), not a column on `Agent`:**

```python
class SessionCostCheckpoint(Base):
    __tablename__ = "session_cost_checkpoints"

    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

The collector reads `lines_processed` for the session ID it's about to tail
(not the calling agent's ID), sums `message.usage.cost.total` from
`type: "message"` lines after that count where `message.role == "assistant"`,
writes new `CostEntry` rows tagged with the *current* `task.id`, and advances
`lines_processed` — correct across any number of agent-row retries within the
same role, because the checkpoint's identity matches the file's identity, not
whichever process happens to be reading it this time.

For tasks with no `session_id` at all (`PiAgent.get_session_args` falls back
to `--no-session` when none is passed — e.g. standalone, non-phase tasks; see
`src/interfaces/cli_interface.py:501`), there's no discoverable session file
either, so no checkpoint is created; that class of task's cost is unavoidably
unattributed at the `pi`-transcript level under this design (it would still
be caught if it goes through `enrich_task`/OpenRouter-direct, but not if the
whole task ran inside an unnamed `pi` session). Worth deciding whether
standalone tasks should be forced to always pass a session ID (a small change
to whatever creates them) rather than accepting this as a permanent gap —
flagged here, not resolved.

**When to collect:** on task completion (`update_task_status` handler, where
the codebase already does end-of-task bookkeeping — see
`task_completion_service.py`), not on a timer. A task's session activity is
fully written to disk by the time `update_task_status(done)` lands (the
agent's own tool call), so there's no torn-read risk, and it avoids a
separate polling loop.

### OpenCode collector (one-shot capture, no checkpoint needed)

Structurally simpler than `pi`/Claude Code precisely because
`OpenCodeAgent.get_launch_command` invokes `opencode run` one-shot (§ above)
rather than as a persistent session Hephaestus has to tail over time. No
`SessionCostCheckpoint` mechanism applies here at all — there's nothing to
double-count, because each `opencode run` process corresponds to exactly one
task-scoped invocation with a clean start and end, not a long-lived session
shared across multiple tasks via `SESSION_ROLES`.

Two viable mechanisms, in order of preference (both need the one live
`--format json` smoke test flagged above before committing to either):

1. **Capture from the process's own stdout at launch time.** Add `--format
   json` to `OpenCodeAgent.get_launch_command`, and have
   `src/agents/manager.py` parse the final JSON event off the tmux pane
   output when the run completes (it already parses pane output for other
   purposes — see `parse_output` in each `CLIAgentInterface` subclass). This
   avoids touching the SQLite DB or filesystem at all; the cost figure comes
   from the same process invocation Hephaestus already launched and is
   watching. Contingent on `--format json`'s payload actually including a
   `cost`/`tokens` field per invocation — unconfirmed, needs the smoke test.
2. **Fallback: read `opencode.db` directly after the process exits.**
   `~/.local/share/opencode/opencode.db` is a real SQLite file; if stdout
   capture proves unreliable (e.g. truncated by tmux pane scrollback limits
   on a long response), query the DB for the message row(s) belonging to the
   session the just-completed `opencode run` created, keyed by matching
   `path.cwd` (the agent's worktree — visible in the exported JSON schema
   confirmed above) and a timestamp window bounding the launch. Messier than
   option 1 (needs the DB schema mapped out, which wasn't done in this
   research pass — only the export CLI's JSON output was inspected) but
   available as a backstop.

Either way, collection happens once, right after the `opencode run` process
exits — not on a timer, not at task-completion time the way `pi`/Claude
Code's collector does (there's no session to wait for; the process itself
*is* the task's entire cost, since one-shot mode means no cross-task
sharing).

### Backend's own OpenRouter calls (task enrichment, guardian, conductor)

Verified: `src/monitoring/guardian.py` and `src/monitoring/conductor.py` both
genuinely call the LLM (`self.llm_provider.analyze_agent_trajectory(...)`,
`get_llm_provider()...`), routing through `MultiProviderLLM`
(`src/interfaces/multi_provider_llm.py:13`), which wraps
`LangChainLLMClient` (`langchain_llm_client.py:28`) — so all three (task
enrichment, Guardian, Conductor) really do funnel through one class, as
assumed.

**Correction to the original "wrap the existing call sites" framing:**
`LangChainLLMClient` has **~9 separate `model.ainvoke(...)` call sites**
(`classify_complexity`, `enrich_task`, `resolve_ticket_clarification`,
`analyze_agent_state`, `analyze_agent_trajectory`,
`analyze_system_coherence`, `review_qa_report`, plus 2 more), not one shared
choke point — wrapping each individually means duplicating the same
"extract usage from response, write a `CostEntry`" logic 9 times. Better:
add one private helper, e.g. `_invoke_and_record(model, messages,
component: str, task_id: Optional[str] = None)`, and route all 9 call sites
through it. This is a reasonable, narrowly-scoped refactor *because* cost
capture is being added to all 9 anyway — not scope creep, since the
alternative is copy-pasting the same block 9 times.

**`task_id` isn't currently threaded into most of these methods and will
need to be.** Checked concretely: `enrich_task`'s signature
(`task_description, done_definition, context, phase_context`) has no
`task_id` parameter at all — its caller (`TaskEnrichmentService.enrich`,
called from `process_queue` in `src/mcp/server.py:1347`) knows the task ID
one level up but doesn't pass it down today. `analyze_agent_trajectory`
receives a `task_info: Dict[str, Any]` that likely already contains a task
ID (needs a one-line check of its actual keys, not assumed). Each of the 9
methods needs this checked individually before the `_invoke_and_record`
helper can tag entries correctly — some calls are genuinely
task-scoped, some are workflow-scoped (Conductor's system-wide analysis),
some may have neither and roll up to the "overhead" bucket the data-model
section already accounts for.

After the `usage.include=true` opt-in is confirmed working (§ above), wire
`_invoke_and_record` to write a `CostEntry` with `source="openrouter_direct"`
directly — no transcript-tailing needed here, this path already has the
response object in hand.

## Pi Extension Collector (preferred over raw JSONL tailing for pi sessions)

A pi extension (`extensions/hephaestus-cost-tracker.ts`) hooks `turn_end`
events to capture `message.usage.cost.total` in real-time as each pi turn
completes. This is cleaner than the raw JSONL tailing approach described in
the Collection Architecture section because:

1. **No file-system access needed** — the extension runs inside the pi process
   and sees usage data directly from the provider response.
2. **Real-time TUI display** — the extension can show running cost in the pi
   status bar via `ctx.ui.setStatus()`, turning the previously-deferred
   "real-time streaming cost display" into a free side-effect.
3. **No checkpoint table needed for pi** — the extension POSTs each turn's
   cost to Hephaestus's API immediately, so there's no byte-offset to track.
   The `SessionCostCheckpoint` table is still needed for Claude Code's
   file-tailing collector.

The extension reads `session_id` from the pi session (available via
`ctx.sessionManager`), and includes it in the POST so Hephaestus can attribute
cost to the correct task/workflow. When the extension is not loaded (e.g.
standalone pi sessions outside Hephaestus), the JSONL tailing fallback still
works — the two mechanisms are complementary, not exclusive.

The extension is installed globally at `~/.pi/agent/extensions/hephaestus-cost-tracker/`
by `scripts/install.sh` when pi is detected. It connects to Hephaestus's API
at `http://localhost:8080` (configurable via `HEPHAESTUS_API_URL` env var).

## Non-Goals (explicitly deferred)

- **Real-time streaming cost display mid-task for non-pi CLIs.** The pi
  extension provides real-time cost display in the pi TUI. For Claude Code
  and OpenCode, collection still happens at task completion — no live ticker
  for those sources.
- **Codex collector implementation.** Stubbed only; needs the CLI installed
  somewhere to inspect its actual transcript format first.
- **Historical backfill.** No cost data exists for tasks that already ran
  before this lands; rollups start from zero at deploy time, not retroactive.

## Implementation Phases

1. **Schema**: `cost_entries` and `session_cost_checkpoints` tables +
   `cost_total_usd` columns on
   `Task`/`Feature`/`AutopilotDesign`/`AutopilotProject`. Migration following
   this codebase's existing `_migrate_*_column`/new-table pattern in
   `database.py` (e.g. `_migrate_workflow_paused_by_column`).
2. **`pi` collector** (verified data source, checkpoint-by-session_id design
   verified against the retry/shared-session bug above) + `cost_derivation.py`
   rollup + wiring into task completion. Land and verify against a real
   running pipeline before touching the other sources — this is the only
   source confirmed end-to-end right now.
3. **Budget enforcement.** `AutopilotProject.cost_limit_usd` column +
   extracting `_pause_project_workflows(project_id, paused_by)` out of the
   `/autopilot/stop` route handler (fixing that endpoint's own
   `definition_id == "autopilot"`-only gap to also match
   `"autopilot-phase0"` in the process — a real bug caught in gap review, not
   hypothetical) so both the endpoint and `_trigger_budget_pause` share one
   correct implementation + enforcement check in `cost_derivation.py`'s
   rollup path calling it with `paused_by="budget"` + the `is not None`
   generalization of every `paused_by == "user"` self-heal guard (leaving
   `start()`'s resume check deliberately un-generalized, per the Budget
   Enforcement section above) + the `pick_next_design`/`_run_one_feature`
   new-work guards. Land right after the `pi` collector (phase 2) since
   that's the earliest point real, non-zero cost data exists to actually test
   enforcement against — doesn't need to wait for Claude Code/OpenCode/Codex.
4. **Claude Code — session-ID correlation already done, build the collector.**
   The UUID5 session-ID fix is already landed (`cli_interface.py:393-403` —
   `ClaudeCodeAgent.get_launch_command` passes `--session-id` with a UUID5
   derived from the same deterministic inputs as pi). Remaining work:
   build the price-table-based collector (`$/M` rates per model, including
   the two cache-write tiers), mirroring `pi`'s collector structure but
   converting tokens → dollars instead of reading a pre-computed total.
5. **OpenRouter direct** — confirm `usage.include=true` surfaces in
   `response_metadata` via one live smoke-test call, then wire the
   enrichment/guardian/conductor call sites.
6. **OpenCode collector — gate on actual usage first.** Before building
   anything, check `config/workflows/autopilot/workflow.yaml` and any
   `phase_cli_tool` overrides for whether `cli_type: opencode` is set on any
   live phase; if nothing in the current deployment uses it, this phase is
   dead weight and should stay deferred indefinitely rather than land
   speculatively. If it *is* in use: run the one confirmatory `opencode run
   --format json "..."` smoke test to see the actual payload shape, then
   implement option 1 (stdout capture) from the Collection Architecture
   section above, falling back to the SQLite-read option only if stdout
   capture proves unreliable. No schema changes needed beyond what phase 1
   already added — `CostEntry.source="opencode"` fits the existing table.
7. **UI**: budget config input + design-screen indicator/link (Budget
   Enforcement section above), plus surfacing `cost_total_usd` on feature
   cards / design rows /
   project-level summary in the autopilot dashboard (the field already flows
   through `autopilot_api.py`'s existing report shape at line 2801 — this is
   additive to plumbing that already exists, not new plumbing).
8. **Codex collector** — once the CLI is available to inspect.

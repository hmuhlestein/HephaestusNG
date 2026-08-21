# Task-Level Parallelism Design

## Status

**Not implemented.** This document designs the second half of a two-part fix;
the first half has shipped (see [What's already fixed](#whats-already-fixed)
below). This is the harder half, and is deliberately being designed before
being built.

## Goal

This document covers two distinct axes of parallelism within one feature,
evaluated separately because they have different safety profiles and
different implementation costs:

1. **Subtasks within one phase** — a phase agent (most concretely, Phase 5
   development) decomposing its own work via `create_task`, declaring
   `depends_on` and `parallel_group` on each subtask. This is the deep dive
   the rest of this document is about.
2. **The 14 main pipeline phases themselves** — could any of
   `product_requirements` through `deploy` run concurrently with each other,
   instead of the strictly sequential `execution_order` they run in today?
   Analyzed in its own section immediately below, since the answer turns out
   to be phase-specific and the reasoning is worth having in one place before
   anyone reaches for it.

## What's already fixed

`Task.depends_on` was written to the database on every `create_task` call and
never read again — a task with an unmet dependency dispatched an agent
immediately, identically to one with none. That gate is now real:

- `_has_unmet_dependencies` blocks dispatch at creation time when any listed
  dependency isn't `"done"` yet (fails closed on an unknown/vanished
  dependency id).
- `_dispatch_ready_dependents` is fired as a background task from every one of
  the three places a task can reach `"done"` (`_complete_task_normally`, the
  human-completion endpoint, and the orphan-recovery path in `lifecycle.py`),
  and dispatches any sibling whose dependencies just cleared.
- A **failed** dependency is deliberately never treated as satisfying
  anything downstream — its dependents stay pending. A stuck chain needs a
  human or a retry to unstick; proceeding on top of a known-broken
  prerequisite is the less safe default.

This closes the ordering bug. It does **not** give `parallel_group` siblings
any actual concurrency — with no `depends_on` between them, they already
dispatched independently before this fix, and still do after it, one
`create_task` call after another, each picking up whatever capacity slot is
free at that moment. That's not true parallelism; it's uncoordinated
sequential dispatch that happens to interleave. The gap this document
addresses is making it *actually* concurrent, safely.

## Which of the 14 main pipeline phases could run concurrently?

None do today — `workflow.yaml`'s `execution_order` and the orchestrator's
`_advance_phases` model one active phase per workflow at a time, full stop
("find the next pending phase by order"). Making any two phases run
concurrently is a real orchestrator-engine change (tracking and advancing
multiple concurrently-active phases per workflow), not a config toggle —
worth saying plainly, since it's a materially bigger change than the
subtask design below, which reuses `create_task`'s existing dispatch
machinery unchanged.

Whether it's *worth* that change is phase-specific. Three criteria decide
it, using the phase-input graph and mutation contracts confirmed against
the actual phase YAMLs (`workflow.yaml`'s `phase_inputs:`, and each phase's
own "ONLY output" / "do NOT edit source" / "FIXES ... in the code"
language):

- **Real data dependency** — does the candidate phase's own declared
  `required` input include an artifact the other phase produces? If yes,
  they cannot run concurrently; this isn't a policy choice, the input
  doesn't exist yet.
- **Mutates the shared worktree** — a phase that writes source/doc fixes
  (not just its own report file) cannot run concurrently with anything
  else reading or writing that same worktree, the identical shared-mutable
  -state hazard the subtask design below exists to solve.
- **Gate cost** — even with no data dependency, a phase upstream may be an
  approval gate whose entire purpose is to stop wasted downstream work
  before it happens. Running past it concurrently trades "maybe save
  wall-clock" for "definitely risk redoing the downstream work if the gate
  fails" — a real tradeoff, stated per case below rather than resolved
  once for the whole pipeline.

### Pre-development (1–4): genuinely sequential

| Pair | Data dependency? | Verdict |
|---|---|---|
| 1 `product_requirements` → 2 `scope_review` | Yes — `scope_review` requires `requirements.md` (1's output) | Sequential, hard requirement |
| 2 `scope_review` → 3 `architecture_design` | **No** — `architecture_design` requires only `requirements.md` (1's output); `scope.md` isn't in its declared inputs at all | Data-independent, but `scope_review` is exactly the gate checking requirements against the design doc (`score < 0.5 → goto product_requirements`). Running 3 concurrently risks a full architecture pass on requirements the gate is about to reject. |
| 3 `architecture_design` → 4 `design_review` | Yes — `design_review` requires `architecture.md` (3's output); its entire job is challenging what 3 just produced | Sequential, hard requirement |

### 4 → 5: no data dependency, deliberately gated anyway

`development.yaml` doesn't declare `challenge.md` as an input at all — by
data alone, development could start the moment architecture_design
finishes. `workflow.yaml`'s own comment on the `design_review` evaluation
point states the reason it doesn't: *"development hasn't run yet, so
there's no code to send a fix to... looping architecture_design once more
is cheap; discovering the same gap after development has already built on
top of it is not."* This is the clearest case in the whole pipeline of a
gate that exists purely to bound the cost of being wrong — concurrency
here would be optimizing away the exact protection that comment describes
choosing on purpose.

### The post-development review cascade (6–11): where real opportunity is

All six of `adversarial_review`, `architectural_review`, `security_review`,
`qa_validation`, `product_validation`, `doc_review` examine the **same**
code `development` (5) just finished. Classified by what each actually
does to the worktree, not by pipeline position:

| Phase | Mutates the worktree? | Hard-requires (from `phase_inputs`) |
|---|---|---|
| 6 `adversarial_review` | No — "Write ONE file... do NOT edit source" | `requirements.md` only |
| 7 `architectural_review` | No — "the developer will fix based on your report" | `architecture.md`, `requirements.md` |
| 8 `security_review` | **Yes** — "Critical and high vulnerabilities FIXED in the code" | `requirements.md`, `architecture.md` |
| 9 `qa_validation` | No (files tickets — "the developer fixes it, not you"), but **runs the live application** (`STEP 3: START APPLICATION`) | `requirements.md` only |
| 10 `product_validation` | No — "Do NOT edit any source code or tests" | `design.md`, `requirements.md` |
| 11 `doc_review` | **Yes** — "FIX it in place", stray-file cleanup | none required (all optional) |

Every hard-required input in this table is a **pre-development** artifact
(`requirements.md`, `architecture.md`, `design.md` — all from phases 1–3).
None of the six requires another phase *in this cascade's* output. That
means, by data dependency alone, all six are candidates to start the
instant `development` finishes — the strict 6→7→8→9→10→11 ordering today
is pipeline-authoring convention, not a stated data dependency.

**The real constraint is a different kind: two of the six mutate the
worktree**, the same shared-mutable-state hazard this whole document is
about at the subtask level:

- **`security_review` (8) cannot run concurrently with any of the other
  five.** It rewrites source files in place. A reviewer reading the same
  files at that moment — or `qa_validation` running tests against them —
  would see a mid-edit, inconsistent codebase. It needs to run either
  fully before or fully after the read-only siblings, not alongside them.
- **`doc_review` (11) cannot run concurrently either**, for two
  independent reasons: it also mutates (docs + stray-file cleanup), and
  its own prompt states its purpose — *"You are the last phase that
  touches docs before the feature ships"* — meaning it's meant to document
  the **final** state, including security fixes and QA results. Running
  it early wouldn't corrupt anything by itself, but it would produce
  documentation of an incomplete picture, defeating the phase's actual
  job even though nothing would crash.

**What's actually left as genuinely concurrent-safe: `adversarial_review`
(6), `architectural_review` (7), `qa_validation` (9), and
`product_validation` (10).** All four are pure readers of a stable
worktree (three static-analysis/report phases plus `qa_validation`, which
additionally needs the app to hold still while its tests run against it —
compatible with the other three, which never touch a running process). All
four's hard-required inputs already existed before `development` started.
This is the single clearest concurrency opportunity in the 14-phase
pipeline: four phases, all read-only, with no data dependency on each
other, whose only real coupling is that `qa_validation`'s *optional*
inputs (`adversarial.md`, `security.md`) mean running fully concurrently
means it forgoes context those siblings would otherwise have already
produced — a thoroughness tradeoff, not a safety one, since those inputs
are declared optional precisely because the phase already has to tolerate
their absence.

### 12–14: genuinely sequential, by definition

`forensics_analysis` reads the whole run's artifacts including every
review above; `git_expert` commits the code those reviews approved;
`deploy` needs what `git_expert` committed. Each requires its predecessor's
actual output, not just a prior pipeline slot. No case for concurrency
here.

### If this were ever built

The mutation split above (two mutators, four pure readers) suggests the
natural shape: dispatch the four read-only reviewers concurrently once
`development` completes, hold `security_review` until they've all
finished (or run it first — either ordering is safe, just not
*interleaved*), then run `doc_review` last once every other review's
output exists for it to document. That's a genuine wall-clock win on the
part of the pipeline with the most phases (six of fourteen), without
touching the parts where the sequencing is either a real data dependency
or a deliberate, already-justified gate. It is, however, exactly the
"materially bigger orchestrator-engine change" flagged at the top of this
section, not a small follow-on to the subtask design below — a decision
for a separate pass, not assumed here.

## Why "just let them run at the same time" isn't safe today

All subtasks within one feature currently execute in **the same shared git
worktree** — one worktree per feature, for the feature's entire 14-phase
pipeline (see `docs/autopilot.md`'s Worktree Strategy). Two agents writing to
that worktree at the same time is a real hazard for two separate reasons:

1. **Git operations race.** `development.yaml`'s prompt instructs the phase
   agent to `git add -p && git commit` after every component. Two separate
   agent *processes* (each its own tmux session, each freely running shell
   commands from its own prompt) committing in the same worktree at the same
   time will race on the git index — there is no code coordinating this. The
   one lock that exists, `MergeLockManager`
   (`src/core/worktree_merge_lock.py`), guards a *different* critical section
   (the orchestrator's own merge-to-main step) and is not something a
   shell-driven agent process can use from inside its own tmux session.
2. **File writes can collide.** Even without a git race, two agents editing
   the same file — or files with implicit coupling (e.g. both regenerating a
   shared `__init__.py`) — produce a result that depends on write order,
   which the design does not control today.

Both hazards are inherent to *sharing one worktree*, not to running two
LLM-driven agents concurrently in general — the codebase already runs many
agents concurrently across different features, each safely isolated in its
own worktree. That isolation is the template this design reuses.

## Design: one worktree per parallel_group sibling

Give each task in a `parallel_group` its own worktree, branched from the
feature worktree's current commit, exactly the way `WorktreeManager` already
branches a fresh worktree from an arbitrary `base_commit_sha` for any other
agent:

```python
WorktreeManager.create_agent_worktree(
    agent_id=subtask_agent_id,
    base_commit_sha=feature_worktree_head_sha,
)
```

No new worktree-creation code is needed — this is the same primitive that
already backs every agent worktree in the system, just pointed at a commit
inside a feature's history instead of at `main`.

### Lifecycle

```
Feature worktree @ commit C
   │
   ├── sibling A worktree (branched from C) ── agent A works, commits ── merges back
   ├── sibling B worktree (branched from C) ── agent B works, commits ── merges back
   └── sibling C worktree (branched from C) ── agent C works, commits ── merges back
                                                        │
                                        Feature worktree now @ commit C+A+B+C
                                        (only once EVERY sibling has merged)
```

1. When a `parallel_group`'s siblings are all creatable (their own
   `depends_on`, if any, are satisfied), create one worktree per sibling,
   all branched from the feature worktree's current HEAD.
2. Dispatch one agent per sibling worktree. They run concurrently — no
   shared mutable state between them, because they aren't sharing a
   worktree.
3. On each sibling's completion, merge its branch back into the feature
   worktree, through the **existing** merge machinery:
   `MergeLockManager` (serializes merges — sibling merges happen one at a
   time even though the work happened in parallel) +
   `worktree_conflict_resolution.py` (handles a real conflict if two
   siblings touched overlapping lines despite declaring disjoint files —
   see below).
4. The `parallel_group` is "done" only once every sibling has merged. The
   phase's next dependent step (or the next `parallel_group` in the chain)
   sees the feature worktree with all of them applied.
5. Delete the sibling worktrees once merged (same cleanup path feature
   worktrees already use after a successful merge).

### The safety condition: declared, disjoint file ownership

This design is safe *because* merges are serialized and conflicts are
handled by existing, tested machinery — but a conflict on every merge would
make "parallel" a false economy (serialized merge time added on top of
parallel work time, for no net gain). The real efficiency win requires
siblings to actually not collide, which means declaring it up front:

Extend `create_task` subtasks with a `files` field, the same shape
`Feature.files` already uses at the feature level (`architecture_design.yaml`
already asks the architect to specify "Files to create/modify" per
component — this makes that existing prompt convention a real, checked
field instead of prose). Before dispatching a `parallel_group` concurrently,
verify every sibling's declared `files` are disjoint from every other
sibling's in the same group — mirroring Phase 0's own existing invariant
("No file path overlaps between features," already a `done_definitions`
entry on the Feature Architect).

If the check fails (or a sibling declares no `files`), **fall back to
sequential dispatch** for that group — the exact behavior that exists today.
This makes disjointness an optimization gate, not a hard requirement:
unsafely-overlapping siblings still complete correctly, just one at a time,
never silently racing in a shared worktree.

## What this does *not* attempt to solve

- **Merge conflicts between siblings with genuinely overlapping edits.**
  Handled, not eliminated: `worktree_conflict_resolution.py`'s existing
  resolution path runs, same as any other worktree merge conflict today. The
  disjoint-`files` gate above is what keeps this the rare case rather than
  the common one.
- **Cross-sibling ordering *within* a still-parallel group.** `parallel_group`
  means "no ordering constraint between these" by definition. If two siblings
  need a partial order, that's what `depends_on` between individual tasks is
  for (already real, per the fix above) — express it as a dependency, not as
  same-group membership.
- **Nested parallelism** (a sibling itself spawning its own parallel
  subtasks). Not disallowed by this design, but not designed for either —
  worth a follow-up decision once flat groups are working, not before.

## Cost and rollout

Each concurrently-dispatched sibling is a full worktree (disk + a
`git worktree add` cost) and a full agent session, so a `parallel_group` of N
siblings costs roughly N× the worktree/session overhead of running them
sequentially, in exchange for wall-clock time roughly 1/N. That tradeoff is
only worth it for groups whose members do meaningfully independent work
(architecture_design.yaml's own "types" vs "handlers" example is the
intended shape); a group of tiny, fast subtasks would likely lose more to
worktree setup than it gains from parallel wall-clock time.

Suggested rollout, each step independently shippable and testable:

1. Add the `files` field to `create_task` subtasks and the disjointness
   check (inert on its own — informational until step 2 uses it).
2. Wire concurrent dispatch for a `parallel_group` whose siblings pass the
   disjointness check, falling back to today's sequential behavior
   otherwise. This is the smallest change that produces real concurrency.
3. Add the merge-back step and group-completion semantics.
4. Only then consider raising any per-feature concurrency cap — the
   existing global `max_concurrent_agents` cap already limits blast radius
   in the meantime.

## Open questions for whoever builds this

- Does a sibling's `parallel_group` worktree get its own `.hephaestus/`
  inbound context (design.md, requirements.md, etc.), or does it inherit the
  feature worktree's via `context_files` at creation? (Almost certainly the
  latter — the sibling needs the same phase context the parent development
  task has — but worth confirming against how `create_agent_worktree`'s
  `context_files` param is used elsewhere before assuming.)
- Should a sibling's OWN mid-work git commits (per development.yaml's
  "commit after each component") land as one commit per sibling on merge, or
  should the sibling's own commit history be preserved/rebased onto the
  feature branch? Squashing is simpler and matches how feature branches
  already merge into main; preserving history has no clear consumer today.
- What happens to a `parallel_group` if ONE sibling fails outright (not a
  merge conflict — a genuine task failure)? The group cannot be "done" with
  a failed member. Likely answer: same as a `depends_on` failure today (the
  group's completion stays blocked until a human or retry resolves the
  failed sibling), for consistency with the choice already made in the
  dependency-gating fix above — but this should be a deliberate decision at
  implementation time, not an accident of whatever order the code happens
  to check things in.

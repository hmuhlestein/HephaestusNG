# Task-Level Parallelism Design

## Status

**Not implemented.** This document designs the second half of a two-part fix;
the first half has shipped (see [What's already fixed](#whats-already-fixed)
below). This is the harder half, and is deliberately being designed before
being built.

## Goal

Within a single feature's pipeline, a phase agent (most concretely, Phase 5
development) can already decompose its work into subtasks via `create_task`,
declaring `depends_on` and `parallel_group` on each one —
`architecture_design.yaml`'s prompt teaches this explicitly, with worked
examples like a `"handlers"` group that must wait for a `"types"` group to
finish. This document designs how to make `parallel_group` siblings actually
run **at the same time**, safely, rather than one at a time in declaration
order.

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

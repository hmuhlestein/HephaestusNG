# Repo Rename Research: HephaestusNG → AutopilotNG

Status: research only. No files have been changed. This document inventories
everywhere the "Hephaestus" name appears, flags what's cheap vs. risky to
rename, and lists the decisions that need to be made before any rename work
starts.

**HephaestusNG has never been publicly released.** There is no existing
install base, no third-party scripts/aliases depending on the `heph`
command, and no on-disk data at other users' machines to migrate. Every
"deprecated alias" / "migration path" concern below only matters for this
one local checkout's own `.hephaestus/` and `hephaestus.db` files (trivially
deleted/regenerated) and any Docker Compose state on this machine — not for
a real user base. This means the rename can be a clean, hard cutover
end-to-end rather than a staged compatibility rollout.

## Chosen name: AutopilotNG

Collision check (GitHub, npm, PyPI):
- GitHub org/user `AutopilotNG` — available.
- npm `autopilotng` / `autopilot-ng` — both available.
- PyPI `autopilotng` / `autopilot-ng` — both available.
- Only near-match: `mrklingon/autopilotng`, an unrelated hobby MakeCode
  project with no real traction — not a practical conflict.

This avoids the bare-"Autopilot" collisions found in the broader search
(direct domain competitors `Claude-Autopilot`, `gpt-autopilot`,
`iam-policy-autopilot`; the word "autopilot" itself already taken on npm and
PyPI) while preserving the existing "NG" framing this repo already uses.

## Why rename

`README.md`'s own "Why NG?" section already says it plainly: NG's flagship
addition, Autopilot, is "a deliberate departure" from the original
Hephaestus premise (self-organizing branching), not a variation on it. Most
of what's shipped since — the fixed 14-phase pipeline, the FastAPI/SQLAlchemy
backend, the React dashboard, multi-project/multi-repo support, worktree
isolation, monitoring/recovery — was built on top of, not as an extension of,
Ido Levi's original framework. The repo name (`HephaestusNG`) no longer
describes what the project is; "Autopilot" does.

## Legal starting point

- License is AGPL-3.0 (`LICENSE`), already public-domain-compatible for a
  rename/relicense-as-same-license fork. `LICENSE`'s copyright header —
  `Copyright © 2025 Ido Levi (Ido-Levi)` plus the FSF boilerplate — **must
  stay intact**; AGPL requires preserving existing copyright notices. Add a
  second copyright line for new work rather than replacing the existing one.
- No `NOTICE` file exists today. Attribution to upstream currently lives
  only in `README.md`'s intro and "Why NG?" section (with a link to
  `github.com/Ido-Levi/Hephaestus`). Decide whether that's sufficient or
  whether a dedicated `NOTICE`/`ATTRIBUTION.md` is worth adding when the repo
  goes public.
- `.github/FUNDING.yml` currently points GitHub Sponsors at `Ido-Levi` —
  almost certainly copied by accident from upstream and should be removed or
  replaced, not carried forward under a new brand.

## Naming-collision risk

"Autopilot" is heavily used elsewhere (Tesla Autopilot, GitHub Copilot's
autopilot-style features, various CI/CD and DevOps tools already using the
name). Nothing in this repo's own dependencies collides, but before
finalizing the public name:
- Check npm/PyPI/GitHub org namespace availability if you intend to publish
  packages (not just rename the private repo) under "Autopilot" or a
  variant.
- Consider whether a more distinctive compound name (e.g. something
  Autopilot-adjacent but unique) avoids search/SEO collision with Tesla's
  trademark in an automotive-safety-adjacent product category.

This is a decision for the user, not something to resolve unilaterally.

## Inventory by surface

Headline number: **367 tracked files** (py/ts/tsx/md/yaml/yml/toml/json/html/sh/cfg/ini)
contain "hephaestus" in some casing, repo-wide (excluding `.venv`,
`node_modules`, `.git`, caches, `.worktrees`). Below is that same 367 broken
into what actually needs decisions vs. what's a mechanical find/replace.

### 1. Package/project identity
| Location | What | Difficulty |
|---|---|---|
| `pyproject.toml` | `[tool.poetry] name = "hephaestus"`, `authors`, `[tool.poetry.scripts] heph = "src.cli.main:main"` | Trivial edit, but see §2 — the console-script name is user-facing muscle memory |
| `frontend/package.json` | `"name": "hephaestus-frontend"` | Trivial |
| `extensions/hephaestus-cost-tracker/` | Entire extension directory + npm package name is brand-carrying | Needs a decision: rename the extension or leave as a legacy/compat shim |
| `website/package.json`, `website/docusaurus.config.ts` | `title: 'Hephaestus'`, `baseUrl: '/Hephaestus/'`, `projectName`, upstream GitHub Pages links (×3), copyright footer | `baseUrl` changes the deployed URL path, not just a string — a real migration, not cosmetic |

No `setup.py`/`setup.cfg`; Poetry is the only manifest. Vendored deps under
`lib/` (`turbovec`, `fastembed`) have zero Hephaestus references — clean.

### 2. CLI command surface (`heph`)
Single entry point: `heph = "src.cli.main:main"`, defined entirely in
`src/cli/main.py` — module docstring, `argparse` `prog="heph"`,
`--version` string, `logging.getLogger("heph.cli")`. Technically
concentrated in one file, but **every doc, script, and workflow YAML that
tells a user to type `heph ...` in prose** cascades from this decision. This
is the single highest-visibility naming choice in the whole rename: keep
`heph` as the command (least disruptive, matches "keep muscle memory") vs.
rename to something like `autopilot`/`ap` (matches the new brand, breaks
every existing install's habits and any shell aliases/scripts users have
written).

### 3. Config files & env vars
- `hephaestus_config.yaml` — the filename itself carries the name (190
  lines). Internally: `database: ./hephaestus.db`, `collection_prefix:
  hephaestus` (Qdrant vector collection prefix — **a live-data migration
  concern** if changed post-deployment, not just text).
- 15 distinct `HEPHAESTUS_*` env vars, 83 occurrences across 19 files under
  `src/` — `HEPHAESTUS_AGENT_ID`, `_API_BASE`, `_API_URL`, `_CLI_TOOL`,
  `_CONFIG`, `_DIR`, `_INSTALL_DIR`, `_LOGS_DIR`, `_PHASES_FOLDER`,
  `_PHASE_ID`, `_PIDS_DIR`, `_PORT`, `_TASK_ID`, `_TEST_DB`,
  `_WORKFLOW_ID`. **No single accessor** — each call site does
  `os.environ.get("HEPHAESTUS_X")` directly, so renaming the prefix means
  touching every one of those 19 files (or keeping the old prefix as a
  deprecated alias for one release).
- `.env.example`: `DATABASE_PATH=./hephaestus.db`,
  `QDRANT_COLLECTION_PREFIX=hephaestus`, prose comments pointing at
  `hephaestus_config.yaml`.

### 4. Filesystem/data directories (`.hephaestus/`)
Mostly centralized: `src/core/constants.py` defines `CONTEXT_DIR_NAME =
".hephaestus"` and derived constants (`DESIGN_CONTEXT_SUBDIR`,
`AUTOPILOT_STATE_DIR`, `HEPHAESTUS_LOGS_DIR`, `HEPHAESTUS_PIDS_DIR`,
`HEPHAESTUS_INSTALL_DIR`). 91 usages import the constant — but **11 files
hardcode the raw string `".hephaestus"`** instead:
`src/agents/output_capture.py`, `src/agents/launch_pipeline.py`,
`src/mcp/agents_api.py`, `src/mcp/autopilot/feature_review_routes.py`,
`src/autopilot/spec.py`, `src/autopilot/orchestrator/{pipeline,
phase_transitions,reporting}.py`, `src/services/cost_collection_service.py`,
`src/interfaces/cli_interface.py`. **Recommend cleaning up these holdouts to
route through `CONTEXT_DIR_NAME` before attempting the rename** — otherwise
a rename script will miss them silently.

This is also a **live migration concern**: existing local installs have
`~/.hephaestus/` and per-project `.hephaestus/` directories on disk already.
Renaming the constant doesn't move existing users' data — a migration
step (or a documented manual `mv`) is needed.

### 5. Database filename
`hephaestus.db` is a repeated literal default in **9 source locations**, no
shared constant: `src/core/database.py` (×3), `src/core/simple_config.py`,
`src/core/llm_config.py`, `src/sdk/config.py`, `src/cli/commands/config.py`
(×3), `src/cli/commands/init.py`, `src/cli/commands/project.py`,
`src/autopilot/orchestrator/pipeline.py` (×3). Same shape as §3/§4 — worth
introducing one constant before renaming. Live DB files already exist on
disk in multiple places in this checkout (`./hephaestus.db`,
`./data/hephaestus.db`, `./frontend/hephaestus.db`,
`./.hephaestus/hephaestus.db`) — again a migration concern for real
deployments, not just a source-code edit.

### 6. Docs
- `README.md`: 324 lines, 33 occurrences — full rewrite needed regardless of
  rename (it's the primary place the "why we forked and diverged" story
  needs to be told to a public AGPL audience).
- `docs/`: 24 files, 20 reference Hephaestus (titles/prose). `docs/heph-cli.md`
  carries the name in its filename.
- `design_docs/`: 20 files, same nature.
- `AGENTS.md` (341 lines, 8 occurrences), `CONTRIBUTING.md` (278 lines, 9
  occurrences) — **CONTRIBUTING.md's occurrences aren't cosmetic**: it has a
  `git clone https://github.com/Ido-Levi/Hephaestus.git` instruction, a docs
  URL, and a GitHub Discussions link, all pointing at the *upstream* repo,
  not this fork. These are functionally wrong for a contributor to this
  fork and need fixing regardless of what the new name ends up being.
- `website/docs/` (~19 files, Docusaurus site): one occurrence each; the
  branding load is in the config (§1), not per-file content.

### 7. Frontend
- `frontend/index.html:7`: `<title>HephaestusNG Dashboard</title>` — trivial.
- No favicon/logo asset under `frontend/`. Branded screenshots live at
  top level (`assets/hephaestus_observability.png`,
  `assets/hephaestus_overview.png`) — illustrative images used by the
  README, not an app icon; re-shooting them is a content task, not a code
  change.
- Hardcoded strings in 4 files: `frontend/src/components/Layout.tsx`,
  `frontend/src/components/autopilot/LoadDesignModal.tsx`,
  `frontend/src/hooks/useLayoutPersistence.ts`,
  `frontend/src/lib/promptAssember.ts`. Not yet released, so a
  `localStorage` key change (if `useLayoutPersistence.ts` has one) is a
  non-issue — no existing user's saved layout to worry about resetting.

### 8. Docker/deploy/CI
- `Dockerfile`: zero Hephaestus references. No-op.
- `docker-compose.yml`: `container_name: hephaestus-qdrant`,
  `hephaestus-server`, `hephaestus-monitor` (service keys and container
  names), `DATABASE_PATH=/app/data/hephaestus.db` (×2). Not released, so
  just `docker-compose down` this machine's stack before renaming — no
  other deployment to worry about.
- No systemd unit files anywhere in the repo.
- `.github/workflows/deploy-docs.yml`: no direct string, but deploys the
  heavily-branded `website/` Docusaurus config (§1).
- 5 of 6 `.github/ISSUE_TEMPLATE/*.yml` reference Hephaestus in prose/labels.
- `.github/FUNDING.yml` → points at `Ido-Levi` (see Legal section above —
  flag for removal).

### 9. Test suite
249 `test_*.py` files; 94 contain "hephaestus" somewhere, but overwhelmingly
in docstrings/comments and constant usage (`HEPHAESTUS_TEST_DB`,
`.hephaestus` paths) rather than literal string assertions on the word
itself. Low functional risk — once §3/§4/§5's underlying constants are
renamed once at the source, most of these 94 files need no per-file change.
The one file whose *name* encodes it: `tests/test_hephaestus_install_dir.py`.

### 10. Logging
Only one custom logger namespace: `logging.getLogger("heph.cli")` in
`src/cli/main.py:118`. Everything else follows the
`logging.getLogger(__name__)` convention per this repo's own CLAUDE.md
rule. Trivial once §2's CLI-name decision is made.

### 11. Git/GitHub metadata
```
origin    git@github.com-hmuhlestein:hmuhlestein/HephaestusNG.git
upstream  https://github.com/Ido-Levi/Hephaestus.git
```
GitHub's repo-rename feature auto-redirects the old URL, so this is a
Settings-page action, separate from any source change. No hardcoded
references to this fork's own URL (`HephaestusNG`) were found in docs/CI —
the only hardcoded upstream URLs found point at `Ido-Levi/Hephaestus`
correctly (§6/§8).

### 12. Secondary in-repo branding (informational)
`src/sdk/tui/` (a separate Textual-based TUI) has its own internal identity:
"Forge" (`src/sdk/tui/widgets/forge_art.py`, `screens/forge_main.py`). Not a
collision or a problem, just worth knowing if "Forge" was ever a candidate
name — it's already the SDK TUI's own sub-brand.

## What's already centralized vs. scattered

The rename's real difficulty isn't the 367-file count — most of that is
free-text prose a global find/replace handles safely. The two gaps worth
closing **before** attempting a mechanical rename:

1. **`.hephaestus` directory string** — 84% centralized behind
   `CONTEXT_DIR_NAME`, 11 raw-string holdouts (§4).
2. **`hephaestus.db` filename** — 0% centralized, 9 independent literal
   defaults (§5).

Everything else (env var prefix, CLI `prog`, package manifests,
Docker/compose names, docs) is either already single-sourced or is prose
that a global replace handles without a code-level refactor first.

## Open decisions before starting

Name is settled (AutopilotNG). No install base means no migration-path
question for any of these — every item below is "pick the clean name," not
"pick a rollout strategy":

1. **CLI command** — currently `heph`. Rename to match (`apng`? `autopilot`?
   keep `heph` as a nod to lineage?) — pure preference now, zero disruption
   either way since nobody has it installed.
2. **`extensions/hephaestus-cost-tracker/`** — rename or leave as-is/deprecate.
3. **`website/` Docusaurus site** — full rebrand (new `baseUrl`) vs. stand up
   fresh. Since the current site presumably isn't linked from anywhere
   public yet, full rebrand is low-risk.
4. **Attribution format** — is the existing README credit to Ido Levi's
   original Hephaestus sufficient, or add a dedicated `NOTICE`/
   `ATTRIBUTION.md` for the public AGPL release? (This one's independent of
   the no-install-base point — it's about the *public* release, not
   migration.)
5. **`.github/FUNDING.yml`** — remove or replace; almost certainly an
   accidental carry-over from upstream, unrelated to the rename itself.

## Suggested sequencing

1. Prep: introduce the missing constants (`.hephaestus` raw-string
   holdouts, `hephaestus.db` filename) so every downstream rename touches
   one place, not nine.
2. Source-level rename: package manifests, CLI `prog`/banner, env var
   prefix (`HEPHAESTUS_*` → `AUTOPILOTNG_*` or similar, straight cutover),
   config filename, directory constants (`.hephaestus` → new dir name),
   DB filename constant. Delete this checkout's own stale
   `.hephaestus`/`hephaestus.db`/Docker state as part of the same pass
   rather than migrating it.
3. Docs rewrite: README (the "why we diverged" story is already half-told
   in the current "Why NG?" section — reuse that framing), CONTRIBUTING.md's
   broken upstream-pointing instructions, AGENTS.md, docs/, design_docs/.
4. Frontend: page title, the 4 hardcoded strings, rebuild.
5. Deploy surfaces: docker-compose service/container names, `.github/`
   issue templates, `FUNDING.yml`.
6. `website/` Docusaurus rebrand (if in scope — open decision 3).
7. GitHub repo rename (Settings page) last, once source is consistent with
   the new name — avoids a window where the repo name and the code disagree.
8. Legal: confirm `LICENSE` copyright header untouched, add `NOTICE`/
   `ATTRIBUTION.md` if decided in open decision 4.

No changes have been made. This is the input for an implementation plan
once the remaining open decisions above are answered.

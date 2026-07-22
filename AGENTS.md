# CLAUDE.md — HephaestusNG

## Non-negotiables

- No flattery, no filler. Start with the answer or the action.
- Disagree when you disagree. If the user's premise is wrong, say so before doing the work.
- Never fabricate file paths, commit hashes, API names, test results, or library functions.
- Stop when confused. If the task has two plausible interpretations, ask.
- Touch only what you must. Every changed line must trace directly to the user's request.
- **Never commit or push changes unless the user explicitly asks you to.**

## Before writing code

- State your plan in one or two sentences before editing.
- Read the files you will touch. Read the files that call the files you will touch.
- Match existing patterns in the codebase.
- Surface assumptions out loud. Do not bury assumptions inside the implementation.

## Writing code

- No features beyond what was asked.
- No abstractions for single-use code.
- No error handling for impossible scenarios.
- If the solution runs 200 lines and could be 50, rewrite it before showing it.
- Bias toward deleting code over adding code.

## Surgical changes

- Do not "improve" adjacent code, comments, formatting, or imports that are not part of the task.
- Do not refactor code that works just because you are in the file.
- Do clean up orphans created by your own changes (unused imports, variables, functions).
- Match the project's existing style exactly.

## Communication style

- Direct, not diplomatic. "This won't scale because X" beats "That's an interesting approach, but have you considered...".
- Concise by default. Two or three short paragraphs unless the user asks for depth.
- No excessive bullet points, no unprompted headers, no emoji.

---

## Stack

- **Backend**: Python 3.12, FastAPI, Uvicorn, SQLite (SQLAlchemy), Pydantic
- **Frontend**: React 18, TypeScript, Tailwind CSS, Vite
- **Agents**: tmux sessions with CLI agents (pi, Claude Code, Codex)
- **LLM**: OpenRouter (default), OpenAI, Anthropic
- **Vector Store**: turbovec (default) or Qdrant
- **Config**: YAML (hephaestus_config.yaml, config/workflows/, config/prompts/)

## Commands

```bash
# Service management
heph start / stop / restart / status / init

# Tests
python tests/run_all_tests.py          # all tests
python tests/run_all_tests.py --quick  # smoke pass
pytest tests/test_foo.py               # single file
pytest --cov=src                       # coverage

# Frontend
cd frontend && npm run dev             # dev server
cd frontend && npm run build           # production build
cd frontend && npx tsc --noEmit        # type check

# Lint
black --line-length 88 src/
flake8 src/
mypy src/

# Autopilot
heph autopilot start --project-path ~/my-project
heph autopilot stop / status
heph autopilot queue --project-path ~/my-project

# Knowledge base
heph memory search "query"
heph memory save "content" --type discovery
```

## Project Layout

```
src/
  agents/       Agent lifecycle, tmux management, messaging
  autopilot/    Pipeline orchestrator, phase management
  core/         Database, config, constants, utilities
  interfaces/   LLM providers, CLI agent abstractions
  mcp/          FastAPI server, API routes
  monitoring/   Guardian, conductor, orphan reaper
  phases/       Phase manager, evaluation handlers
  prompts/      Prompt loader, YAML templates
  sdk/          Hephaestus SDK client
  services/     Agent dispatch, task blocking, enrichment
  workflow/     Workflow registry, termination handler
frontend/       React dashboard
config/         YAML config, workflows, prompts
docs/           Architecture docs, design docs
tests/          Unit and integration tests
scripts/        Setup helpers
```

## Conventions

- **Naming**: snake_case (Python), PascalCase (React components), camelCase (hooks)
- **Logging**: `logger = logging.getLogger(__name__)` at module level. Never create mock loggers. No logging in data return paths.
- **Database**: SQLAlchemy with StaticPool, expire_on_commit=False, use session_scope()
- **Imports**: Absolute from src root (`from src.core.database import ...`)
- **Commits**: `feat:`, `fix:`, `chore:` prefixes, <72 char subjects
- **Frontend**: Functional components, Tailwind classes, `npm run type-check` before review

## Vector Store

- **Default: turbovec** (local, in-process, zero Docker). Uses `data/turbovec/`.
- **Fallback: Qdrant** (requires Docker). Set `VECTOR_STORE_BACKEND=qdrant`.
- **Embeddings: fastembed** (local ONNX, 384-dim). Set `EMBEDDING_BACKEND=openai` for OpenAI API.
- Env vars: `VECTOR_STORE_BACKEND`, `EMBEDDING_BACKEND`, `TURBOVEC_DATA_DIR`, `FASTEMBED_MODEL`.

## Critical Invariants

- **Agent termination**: Every path that sets `status="terminated"` MUST also set `current_task_id=None` and `terminated_at=datetime.utcnow()`
- **No nested worktrees**: If `project_path` contains `.worktrees/`, use it directly
- **Design storage**: `.hephaestus/designs/` (not git-tracked)
- **No hardcoded timeouts**: Use `hephaestus_config.yaml`
- **Transcript logs**: `.hephaestus/tmux/*.transcript.log` for full agent output history
- **Always `datetime.utcnow()`, never bare `datetime.now()`**: every stored/compared timestamp in this codebase uses UTC. `datetime.now()` returns naive local time that depends on the calling process's ambient `TZ` — two processes (or the same process across a restart) can disagree by hours with no error. Root-caused a real incident: a task-creation claim's staleness check compared a `datetime.utcnow()`-stamped value against a `datetime.now()`-based cutoff, so the claim never registered as stale and a workflow sat silently stuck for hours. Applies to anything written to the DB and later compared against a fresh timestamp, not just this one case — a mixed clock only fails when the two calls happen to run under different `TZ`, so it won't show up in same-process testing.
- **`AutopilotProject.is_active` must stay exclusive**: exactly one project may have `is_active=True` at a time. Every reader does `.filter_by(is_active=True).first()` (with no ordering guarantee), including the phase-advancement sweep's project scoping — so creating or activating a project MUST clear every other project's flag first (`db.query(AutopilotProject).update({"is_active": False})`), same as `projects_api.py`'s `activate_project` already does. A write site that skips this (e.g. auto-creating a project without clearing others) leaves multiple "active" projects; `.first()`'s arbitrary pick among them can silently scope background work to the wrong project while looking like nothing is wrong.

## Security

- Store secrets in `.env`; never commit credentials
- Use `hephaestus_config.yaml` for config overrides
- For local-only: `VECTOR_STORE_BACKEND=turbovec` and `EMBEDDING_BACKEND=fastembed` (no Docker, no API keys)

## Forbidden

- Do not commit .env or API keys
- Do not create nested worktrees inside existing worktrees
- Do not set agent.current_task_id without clearing it on termination
- Do not store design files in git-tracked directories
- Do not use synchronous blocking calls in async endpoints without thread pool

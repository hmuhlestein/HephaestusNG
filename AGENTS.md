# Repository Guidelines

## Project Structure & Module Organization
- `src/` holds the orchestration stack: `agents/` (lifecycle), `memory/` (vector store + RAG), `mcp/` (FastAPI MCP server), `monitoring/` (Guardian & Conductor loops), and shared utilities in `core/`.
- `frontend/` is the Vite + React dashboard; run UI tooling from that directory.
- `tests/` contains integration suites and `run_all_tests.py`; `tests/mcp_integration/` targets protocol flows with local fixtures.
- `scripts/` provides setup helpers; architecture details live in `docs/` and `design_docs/`; configuration files sit in `config/`.

## Build, Test, and Development Commands
- `poetry install` (preferred) or `pip install -r requirements.txt` (installs turbovec + fastembed by default).
- `python scripts/init_db.py` and `python scripts/init_qdrant.py` initialize SQLite tables and vector collections.
- `python run_server.py` exposes the MCP API on port 8000 (`--reload` optional); `python run_monitor.py` enables the self-healing monitor.
- `cd frontend && npm install && npm run dev` serves the UI; `npm run build` produces production assets.

## Vector Store Backends
- **Default: turbovec** (local, in-process, zero Docker). Uses `data/turbovec/` for storage.
- **Fallback: Qdrant** (requires Docker). Set `VECTOR_STORE_BACKEND=qdrant` to use.
- **Embeddings: fastembed** (local ONNX, 384-dim). Set `EMBEDDING_BACKEND=openai` for OpenAI API.
- Configure via env vars: `VECTOR_STORE_BACKEND`, `EMBEDDING_BACKEND`, `TURBOVEC_DATA_DIR`, `FASTEMBED_MODEL`.

## Coding Style & Naming Conventions
- Format Python with Black (line length 88), lint via `flake8`, and type-check with `mypy`; use snake_case modules/functions, PascalCase classes, verb-first async names, and explicit type hints.
- Frontend code relies on functional components, camelCase hooks/utilities, Tailwind classes, and `npm run type-check` before review.

## Documentation-First Workflow
- Consult the relevant entries in `docs/` or `design_docs/` before coding and mirror established patterns.
- Update or add documentation when behavior changes, keeping `prompts/` and `templates/` aligned with code updates.

## Testing Guidelines
- Default to `python tests/run_all_tests.py`; use `--quick` for a smoke pass or run suites directly (e.g., `python tests/test_vector_store.py`). `pytest` and `pytest --cov=src` remain available for targeted coverage.
- Tests assume live Qdrant and valid API keys; note prerequisites in docstrings, guard optional integrations with `pytest.importorskip`, and clean up agent data deterministically.

## Commit & Pull Request Guidelines
- Match the repo history with `feat:`, `fix:`, `chore:` prefixes and <72 character subjects.
- PRs should state scope, configuration or credential assumptions, and linked issues/design docs; attach UI screenshots and paste key command outputs when relevant.
- Run backend suites plus `npm run type-check` before requesting review, calling out any skipped checks with rationale.

## Security & Configuration Tips
- Store secrets in `.env`; use `hephaestus_config.yaml` or `config/agent_config.yaml` for overrides and never commit credentials.
- Reset SQLite/Qdrant state through `scripts/` helpers to prevent orphaned agent records.
- For local-only deployments, use `VECTOR_STORE_BACKEND=turbovec` and `EMBEDDING_BACKEND=fastembed` (no Docker, no API keys needed).

# Unified Project System

## Problem

Hephaestus has no coherent "project" concept. The word "project" appears in three disconnected contexts:

1. **Config singleton** — `hephaestus_config.yaml` has `paths.project_root` and `git.main_repo_path`, but no way to switch between projects from CLI or UI
2. **AutopilotProject** — DB-backed CRUD for autopilot design queues, but isolated to the Autopilot page
3. **WorkflowExecution.working_directory** — per-execution path, no parent project entity

Users can't answer "which project is Hephaestus working on?" from the CLI or UI.

## Design

Promote `AutopilotProject` to be the single source of truth for projects. It already has the right fields (`name`, `base_dir`, `is_default`). Add `is_active` to track the currently-selected project, and expose it everywhere.

### Key Design Decisions (post adversarial review)

**D1: Active project is a startup-time concept, not a hot-swap.**
`WorktreeManager` caches `self.main_repo = Repo(config.main_repo_path)` at init (`worktree_manager.py:104`). Mutating `config.main_repo_path` later is a no-op — the cached `Repo` object still points at the old path. Rather than trying to hot-swap, the activate endpoint writes `is_active` to the DB and the server **re-initializes** `WorktreeManager` (and its dependents) with the new path. This is safe because no agents are mid-creation during a project switch.

**D2: The autopilot is a separate process — it ignores the active project.**
`heph autopilot start --project-path B` creates its own SDK with `main_repo_path=B` (`orchestrator.py:1235`). This is correct — the autopilot's project is set by its CLI flag, not by the server's active project. The UI should show both: the server's active project (sidebar) and the autopilot's project (autopilot page).

**D3: `heph project create` must work without the backend running.**
During initial setup, the user creates their first project before `heph start`. The CLI will write directly to the SQLite DB when the backend is unreachable, then the server reads it on startup.

**D4: `heph config show` overlays the active project from DB.**
Since we don't modify the YAML file, `heph config show` must query the DB (or API) and display `project_root` and `main_repo_path` from the active project, not just the YAML values.

**D5: No migration framework — use ALTER TABLE for existing DBs.**
`init_db.py` and server startup will check for the `is_active` column and add it if missing. SQLite's `ALTER TABLE ADD COLUMN` is safe and idempotent.

### Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  heph project CLI ──► /api/projects API ──► DB (autopilot_projects)  │
│       │                         │                                    │
│       │ (offline mode)          │ POST /activate                     │
│       ▼                         ▼                                    │
│  Direct SQLite write    WorktreeManager.reload(new_path)             │
│       │                         │                                    │
│       │                         ▼                                    │
│       │                  config.main_repo_path = new_path            │
│       │                  config.project_root = new_path              │
│       │                         │                                    │
│       └─────────┬───────────────┘                                    │
│                 ▼                                                     │
│         Sidebar ProjectSelector ◄── ProjectContext                   │
│         (localStorage + API)      (server is source of truth)        │
│                                                                      │
│  Single project selector used by ALL pages (autopilot, phases, etc)  │
│  Autopilot design queue management stays in autopilot API            │
└──────────────────────────────────────────────────────────────────────┘
```

### What Changes

#### 1. Database

- `AutopilotProject.is_active` column — already added to model
- **Migration for existing DBs**: Server startup and `init_db.py` run:
  ```python
  try:
      db.execute("ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0")
  except Exception:
      pass  # Column already exists
  ```
- After migration, if no project has `is_active=True` and projects exist, set the `is_default` one active

#### 2. Backend API — new `/api/projects` endpoints

New router at `/api/projects`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects` | List all projects (includes `is_active`, `design_count`) |
| `POST` | `/api/projects` | Create project (name, base_dir, is_default) |
| `PUT` | `/api/projects/{id}` | Update project (name, base_dir) |
| `DELETE` | `/api/projects/{id}` | Delete project |
| `POST` | `/api/projects/{id}/activate` | Set project as active |
| `GET` | `/api/projects/active` | Get the currently active project |

The `activate` endpoint:
1. Sets `is_active=True` on the target project, `False` on all others (single transaction)
2. Updates `config.main_repo_path` and `config.project_root` in-memory
3. Calls `server_state.worktree_manager.reload(new_path)` to reinitialize the git Repo
4. Returns the activated project

**Response model** — single `ProjectItem` with all fields:
```python
class ProjectItem(BaseModel):
    id: str
    name: str
    base_dir: str
    is_default: bool
    is_active: bool
    design_count: int
    created_at: str
    updated_at: str
```

The autopilot `/api/autopilot/projects` endpoints will also return `is_active` for consistency.

#### 3. `WorktreeManager.reload(new_path)`

Add a method to `WorktreeManager`:
```python
def reload(self, new_path: Path):
    """Reinitialize with a new main repository path."""
    self.config.main_repo_path = new_path
    self.config.project_root = new_path
    try:
        self.main_repo = Repo(new_path)
    except git.InvalidGitRepositoryError:
        raise ValueError(f"Not a git repository: {new_path}")
    logger.info(f"WorktreeManager reloaded with repo: {new_path}")
```

This is called by the activate endpoint. It mutates both the config singleton and the manager's cached state.

#### 4. Server Startup

In `server.py` lifespan, after DB init but before creating `WorktreeManager`:
1. Run `is_active` column migration
2. Query `AutopilotProject` for `is_active=True`
3. If found, set `config.main_repo_path` and `config.project_root` to its `base_dir`
4. Then create `WorktreeManager` (which reads config)

This ensures the active project's path is in config **before** any manager caches it.

#### 5. CLI — `heph project` commands

```
heph project list                    # List all projects, mark active with *
heph project create <name> <path>    # Create and activate a project
heph project activate <id-or-name>   # Switch active project
heph project current                 # Show active project
heph project delete <id-or-name>     # Delete a project
```

**Offline mode for `create`**: If backend is unreachable, write directly to SQLite:
```python
def create_project_offline(name: str, path: str):
    db_manager = DatabaseManager(str(HEPHAESTUS_DIR / "hephaestus.db"))
    db_manager.create_tables()  # Ensures table exists
    with db_manager.get_session() as session:
        # Clear other active/default
        session.query(AutopilotProject).update({"is_active": False, "is_default": False})
        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=name,
            base_dir=str(Path(path).resolve()),
            is_default=True,
            is_active=True,
        )
        session.add(proj)
        session.commit()
```

Other commands (`list`, `activate`, `delete`) require the backend — they call the API.

#### 6. `heph config show` — overlay active project

Update `config.py:show()` to also display the active project:
```python
def show(args):
    # ... existing YAML display ...
    # Also show active project from DB
    try:
        db_manager = DatabaseManager(str(HEPHAESTUS_DIR / "hephaestus.db"))
        with db_manager.get_session() as session:
            active = session.query(AutopilotProject).filter_by(is_active=True).first()
            if active:
                print(f"\n# Active project (from DB):")
                print(f"#   project_root: {active.base_dir}")
                print(f"#   main_repo_path: {active.base_dir}")
    except Exception:
        pass
```

#### 7. Frontend — `ProjectContext` + sidebar `ProjectSelector`

**`ProjectContext`** (`frontend/src/context/ProjectContext.tsx`):
- Initializes from localStorage (like `WorkflowContext` does for `selectedExecutionId`)
- Fetches `GET /api/projects/active` on mount — if localStorage disagrees with server, server wins
- Provides `activateProject(id)` mutation that calls `POST /api/projects/{id}/activate`
- Provides `projects` list via react-query
- Invalidates related queries on project change (tasks, agents, workflows)

**Sidebar `ProjectSelector`** (`frontend/src/components/ProjectSelector.tsx`):
- Compact dropdown at top of sidebar, below the "Hephaestus" title
- Shows active project name + truncated path
- Dropdown lists all projects, active one has checkmark
- "Create Project" at bottom
- Collapsed sidebar: just folder icon with tooltip
- Placed in `Layout.tsx` above nav items

**Autopilot page**: Remove the autopilot-specific `ProjectSelector`. The autopilot page uses the shared `ProjectContext` from the sidebar for project selection. Autopilot-specific features (design count, sync) are shown inline on the autopilot page but project selection is delegated entirely to the sidebar selector.

#### 8. `scripts/init_db.py` — migration support

Add to `init_db.py`:
```python
def migrate(db_manager):
    """Run schema migrations for existing databases."""
    with db_manager.get_session() as session:
        try:
            session.execute("ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0")
            session.commit()
            print("  - autopilot_projects.is_active (migrated)")
        except Exception:
            pass  # Column already exists
```

### Files to Create/Modify

| File | Action |
|------|--------|
| `src/core/database.py` | Already done — `is_active` field |
| `src/core/worktree_manager.py` | **Modify** — add `reload(new_path)` method |
| `src/mcp/projects_api.py` | **Create** — new `/api/projects` router |
| `src/mcp/server.py` | **Modify** — include projects router, startup migration + activation |
| `src/cli/commands/project.py` | **Create** — `heph project` commands (with offline mode) |
| `src/cli/main.py` | **Modify** — register project subparser |
| `src/cli/commands/config.py` | **Modify** — overlay active project from DB |
| `frontend/src/context/ProjectContext.tsx` | **Create** — project state management |
| `frontend/src/components/ProjectSelector.tsx` | **Create** — sidebar project selector |
| `frontend/src/components/Layout.tsx` | **Modify** — add ProjectSelector to sidebar |
| `frontend/src/App.tsx` | **Modify** — wrap with ProjectProvider |
| `frontend/src/services/api.ts` | **Modify** — add `/api/projects` methods |
| `frontend/src/components/autopilot/ProjectSelector.tsx` | **Delete** — replaced by sidebar ProjectSelector |
| `scripts/init_db.py` | **Modify** — add migration step |
| `src/mcp/autopilot_api.py` | **Modify** — add `is_active` to `ProjectItem` response |

### What We're NOT Doing

- NOT renaming `AutopilotProject` — table name stays for backward compatibility
- NOT modifying `hephaestus_config.yaml` on activation — DB is source of truth
- NOT creating a separate `projects` DB table — reuse `autopilot_projects`
- NOT removing autopilot project API endpoints — they stay for design queue management (CRUD, sync)
- NOT keeping autopilot's `ProjectSelector` component — replaced by the global sidebar selector
- NOT hot-swapping the autopilot's project — it has its own `--project-path` flag
- NOT adding Alembic — simple ALTER TABLE migration is sufficient for now

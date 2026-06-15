# Adversarial Review

**Date:** 2026-06-15
**Target:** Workflow Phases Merge implementation — prompt editor, versioning, assembler, API, and UI
**Diff stats:** 19 files, ~2,300 insertions, ~319 deletions (5 modified + 14 new)

## Summary

- **BLOCKERS:** 5 — must fix before proceeding
- **FIXES:** 9 — safe to apply without approval
- **DEFERRED:** 3 — optional or out of scope

## Test Results

- No dedicated tests exist for any new code (`PromptAssembler`, prompt endpoints, frontend components).
- `tests/run_all_tests.py` was not executed (requires live Qdrant + DB setup). No prompt-related test files found.
- **Risk: Zero test coverage on all new functionality.**

## Findings

### [BLOCKER] TypeScript `_buildSystemPrompt` produces fundamentally different output than Python

- **File:** `frontend/src/lib/promptAssember.ts:135-167`
- **Evidence:** The Python `_buildSystem_prompt` accepts `task_description`, `task_done_definition`, `project_context`, `memories`, `agent_id`, `task_id` and interpolates them into the system prompt. The TypeScript version hardcodes placeholders:
  ```typescript
  return `${identity}

  ═══ TASK ═══
  (task not yet assigned)

  COMPLETION CRITERIA:
  (assigned at task creation)
  ...
  IDs: Agent=unknown | Task=unknown`;
  ```
  The Python version:
  ```python
  return f"""{identity}
  ═══ TASK ═══
  {task_desc}
  COMPLETION CRITERIA:
  {task_done}
  ...
  IDs: Agent={agent_id or 'unknown'} | Task={task_id or 'unknown'}"""
  ```
- **Impact:** The "Prompt Preview" tab in the UI will show a system prompt that differs from what agents actually receive. Users will be misled about what they're editing. The TypeScript `render()` method also lacks parameters for `all_phases`, `task_system_prompt`, `task_user_prompt`, `task_description`, `task_done_definition`, `agent_id`, `task_id`, `memories`, and `project_context` — all of which the Python version supports. The `_buildUserPrompt` method similarly lacks cross-phase context output.
- **Fix:** Either (a) make the TS version accept the same parameters and produce identical output, or (b) remove the duplicate and have the preview endpoint always call the Python backend. Option (b) is simpler and eliminates drift risk.

### [BLOCKER] Variable substitution semantics differ between Python and TypeScript

- **File:** `src/prompts/assembler.py:42-44` vs `frontend/src/lib/promptAssember.ts:39-44`
- **Evidence:**
  Python uses iterative replacement (one variable at a time):
  ```python
  def substitute_variables(text: str, variables: Dict[str, str]) -> str:
      for name, value in variables.items():
          text = text.replace(f"{{{name}}}", str(value))
      return text
  ```
  TypeScript uses single-pass regex replacement:
  ```typescript
  export function substituteVariables(text: string, variables: Record<string, string>): string {
      return text.replace(/\{(\w+)\}/g, (_match, name) =>
          variables[name] !== undefined ? String(variables[name]) : `{${name}}`
      );
  }
  ```
- **Impact:** If a variable value itself contains `{other_var}`, Python will substitute it in a subsequent iteration (nested references), while TypeScript won't. More practically: the *order* of substitution in Python is dict insertion order (arbitrary in older Python), while TypeScript is left-to-right in the string. Given identical variable sets the output *usually* matches, but edge cases with overlapping patterns (e.g., `{foo}` vs `{foo_bar}`) may differ because Python replaces sequentially by key order while TS replaces by position.
- **Fix:** Use the same single-pass regex approach in Python, or at minimum add a test that asserts both produce identical output for the same inputs. Document the behavioral difference.

### [BLOCKER] `Task.status.in_()` called with positional args instead of list

- **File:** `src/mcp/api.py:1602`
- **Evidence:**
  ```python
  .filter(Task.status.in_("assigned", "in_progress", "pending"))
  ```
  Every other usage in the codebase passes a list:
  ```python
  Task.status.in_(["assigned", "in_progress"])  # lines 146, 590, 1018
  Task.status.in_(['assigned', 'in_progress', 'queued', 'pending'])  # line 2305
  ```
- **Impact:** In SQLAlchemy 2.0+, `.in_()` takes a single iterable. Passing multiple positional args may raise `TypeError` at runtime, crashing the phase reset endpoint. Even if it works in the current SQLAlchemy version, it's inconsistent and fragile.
- **Fix:** Change to `.filter(Task.status.in_(["assigned", "in_progress", "pending"]))`.

### [BLOCKER] `subprocess.run` blocks the async event loop

- **File:** `src/mcp/api.py:1587-1592`
- **Evidence:**
  ```python
  if agent.tmux_session_name:
      subprocess.run(
          ["tmux", "kill-session", "-t", agent.tmux_session_name],
          timeout=5, capture_output=True,
      )
  ```
  This is inside `async def reset_phase`. `subprocess.run` is synchronous and will block the entire FastAPI event loop for up to 5 seconds per agent.
- **Impact:** If multiple agents are active, the UI and all other API requests will hang during the reset operation. With 5 agents × 5s timeout = 25s of blocked event loop.
- **Fix:** Use `asyncio.create_subprocess_exec` or `asyncio.get_event_loop().run_in_executor(None, subprocess.run, ...)`.

### [BLOCKER] Race condition on version numbering — read-then-write without locking

- **File:** `src/mcp/api.py:1749-1755` and `1891-1897`
- **Evidence:**
  ```python
  max_version = (
      session.query(func.max(PhasePromptVersion.version))
      .filter_by(phase_id=phase_id)
      .scalar()
      or 0
  )
  new_version = max_version + 1
  # ... uses new_version ...
  session.commit()
  ```
  Both `create_phase_prompt_version` and `restore_phase_prompt_version` read the max version, compute `max+1`, then commit. There's no serialization or lock.
- **Impact:** Two concurrent requests to create/restore versions for the same phase will generate the same `new_version`, violating the `UniqueConstraint("phase_id", "version")` and causing a database integrity error (500).
- **Fix:** Add a `SELECT ... FOR UPDATE` (or use a SQLite-exclusive transaction), or use the `uq_phase_version` constraint as a guard with retry logic. Alternatively, use a sequence or timestamp-based versioning.

### [FIX] `json.loads` on user-supplied query parameter with no error handling

- **File:** `src/mcp/api.py:2378-2379`
- **Evidence:**
  ```python
  import json
  var_dict = json.loads(variables) if variables else None
  ```
  If `variables` is a malformed JSON string (e.g., `?variables={bad`), this throws `json.JSONDecodeError` which is unhandled in the route handler. FastAPI will return a generic 500 with a traceback.
- **Fix:** Wrap in try/except and return `HTTPException(status_code=400, detail="Invalid JSON in variables parameter")`.

### [FIX] `assemble_phase_prompt` and `assemble_task_prompt` create new `DatabaseManager` per call

- **File:** `src/prompts/assembler.py:505,521`
- **Evidence:**
  ```python
  db = DatabaseManager("hephaestus.db")
  ```
  Each invocation creates a new engine, connection pool, and session factory. The database path is also hardcoded as `"hephaestus.db"` rather than reading from config/env.
- **Impact:** Performance degradation under load (new pool per call). Hardcoded path will fail if the database is in a different location (e.g., `HEPHAESTUS_TEST_DB` env var, custom paths).
- **Fix:** Accept a `DatabaseManager` instance as a parameter (dependency injection), or use the module-level `frontend_api.db_manager` when called from API context.

### [FIX] `PhasePromptsTab` draft initialization only runs once — stale after publish

- **File:** `frontend/src/components/workflow/PhasePromptsTab.tsx:37-47`
- **Evidence:**
  ```typescript
  useEffect(() => {
      if (details && !draftPrompt) {  // ← guard: !draftPrompt
          setDraftPrompt({...});
      }
  }, [details]);
  ```
  After publishing (which invalidates `phase-details`), `details` updates but `draftPrompt` is already set, so the effect doesn't re-initialize. The user sees stale draft data until they manually discard.
- **Fix:** Either remove the `!draftPrompt` guard (and accept that typing is lost on refetch), or add `draftPrompt` as a dependency with a smarter diff, or add an explicit "reset to published" mechanism.

### [FIX] Preview query key includes `draftPrompt` but the request doesn't send draft data

- **File:** `frontend/src/components/workflow/PhasePromptsTab.tsx:56-62`
- **Evidence:**
  ```typescript
  const { data: previewData } = useQuery({
      queryKey: ['phase-prompt-preview', phaseId, draftPrompt],
      queryFn: () => apiService.getPhasePromptPreview(phaseId, {}),
      enabled: !!draftPrompt && activeSubTab === 'preview',
      staleTime: 500,
  });
  ```
  The query key includes `draftPrompt` (so it refetches on every keystroke within 500ms), but `getPhasePromptPreview` sends `{}` — it fetches the *committed* phase from the database, not the draft. The preview will never reflect unsaved edits.
- **Impact:** Users editing prompts will see the *old* prompt in the preview, not what they're currently typing. This defeats the purpose of the preview tab.
- **Fix:** Either pass the draft data to the preview endpoint (add a POST-based preview that accepts draft content), or render the preview client-side using the TypeScript assembler (but see BLOCKER #1 about drift).

### [FIX] `PhaseDetailPanel` uses hardcoded `localhost:8300` fetch

- **File:** `frontend/src/components/workflow/PhaseDetailPanel.tsx:35`
- **Evidence:**
  ```typescript
  const res = await fetch(`http://localhost:8300/api/phases/${phaseId}/yaml`);
  ```
  Every other API call in the codebase goes through the `apiService` axios instance (which is configured with a base URL). This raw fetch will fail in any deployment where the backend isn't on `localhost:8300`.
- **Fix:** Use `apiService` or at least the configured axios instance. The `/api/phases/{id}/yaml` endpoint should be added to `apiService` if it isn't already.

### [FIX] `update_phase` has no input validation on field values

- **File:** `src/mcp/api.py:1510-1522`
- **Evidence:**
  ```python
  for key, value in updates.items():
      if key not in mutable_fields:
          raise HTTPException(status_code=400, detail=f"Field '{key}' is not mutable")
      setattr(phase, key, value)
  ```
  `done_definitions` could be set to a string, integer, or null. `description` could be set to a list. No type checking on any values.
- **Fix:** Validate types before `setattr`: `done_definitions` must be `list`, `description` must be `str`, nullable fields should accept `None`, etc.

### [FIX] Missing `React` import in `PromptPreview.tsx`

- **File:** `frontend/src/components/workflow/prompts/PromptPreview.tsx:1`
- **Evidence:** The file uses `React.ReactNode[]` as a return type (line 67) but doesn't import `React`. With the modern JSX transform this may compile, but TypeScript may not resolve `React.ReactNode` without the import.
- **Fix:** Add `import React from 'react'` or change the return type to `JSX.Element[]`.

### [FIX] No confirmation for `restorePhasePromptVersion`

- **File:** `frontend/src/components/workflow/prompts/PromptVersionHistory.tsx:58-60`
- **Evidence:**
  ```typescript
  onClick={() => restoreMutation.mutate(v.version)}
  ```
  Clicking the restore button immediately creates a new active version and archives the current one. No confirmation dialog. A misclick in the version history will overwrite the active prompt.
- **Fix:** Add a confirmation step (confirm dialog or at minimum a second click).

### [FIX] `set_task_prompt_overrides` has N+1 session pattern — calls `assemble_task_prompt` after commit

- **File:** `src/mcp/api.py:1962-1967`
- **Evidence:**
  ```python
  session.commit()

  # Build effective prompt
  from src.prompts.assembler import assemble_task_prompt
  effective = assemble_task_prompt(task_id)
  ```
  After committing the override, this creates a *new* `DatabaseManager` and session to assemble the prompt. If the first session hasn't fully flushed, the new session might read stale data. This is also wasteful — the assembled prompt could be computed from data already in hand.
- **Fix:** Compute the effective prompt using the already-loaded phase/override data instead of re-querying. Or at minimum, move the assembly call before the commit.

## Design Observations (non-blocking)

### Duplicate prompt assembly logic (Python ↔ TypeScript drift)
The entire `PromptAssembler` class exists in both Python and TypeScript with the stated goal of producing "identical output." This is a maintenance burden and a guaranteed source of drift. The TypeScript version is already incomplete (missing cross-phase context, task overrides, memory injection). **Recommendation:** Remove the TypeScript port entirely. Always preview via the Python backend endpoint. The preview latency (<100ms for a DB read + string formatting) is negligible.

### Session management pattern is fragile
All 13 new API methods manually manage `session = self.db_manager.get_session()` / `try` / `finally` / `session.close()`. This is boilerplate-heavy and error-prone (as evidenced by 31 different session open patterns). **Recommendation:** Use a FastAPI dependency that yields a session (or use the existing `get_db` context manager from `database.py`) to ensure consistent lifecycle management.

### `Phase.id` format assumptions
`task.phase_id.isdigit()` appears in `assemble_task_prompt` and several other places, checking if a phase_id is a numeric string (to treat it as an order number). Phase IDs are UUIDs (generated via `uuid.uuid4()` in `phase_manager.py:201`), so `.isdigit()` is always `False`. This is dead code that obscures intent. **Recommendation:** Remove the `isdigit()` branch or document when numeric phase IDs are expected.

### `PhaseCard` status derivation is incomplete
```typescript
const phaseStatus = phase.active_agents > 0
    ? 'in_progress'
    : phase.completed_tasks === phase.total_tasks && phase.total_tasks > 0
        ? 'completed'
        : 'pending';
```
A phase with `failed_tasks > 0` and no active agents is shown as "pending" rather than "failed." **Recommendation:** Add a `failed_tasks` check.

### Hardcoded database filename
Both `assemble_phase_prompt` and `assemble_task_prompt` hardcode `"hephaestus.db"`. If the project uses a different database path (config override, test env), these functions will fail or create a shadow database.

### Version history restore should show diff preview
Restoring a version creates a new active version silently. Users have no visibility into what changed between the current active version and the one being restored. **Recommendation:** Show a diff preview before confirming restore, or at minimum log what fields changed.

### Component architecture is reasonable
The decomposition into `WorkflowCard → PhaseList → PhaseCard → PhaseDetailPanel → {Overview,Prompts,Tasks,Agents,Config}` is clean and follows a logical hierarchy. Mutual exclusion state is properly managed via callbacks. No unnecessary re-render concerns spotted (queries are properly keyed and gated with `enabled`).

### XSS risk is low
Prompt content is rendered via React's automatic escaping (no `dangerouslySetInnerHTML`). The `highlightVariables` function returns JSX elements with escaped text content. No XSS vectors identified.

### SQL injection risk is low
All queries use SQLAlchemy ORM methods (`filter_by`, `filter` with column comparisons), not raw SQL strings. User input flows into parameterized queries via the ORM. The `json.loads` on the query parameter is used to build a dict that's passed to `substitute_variables` (string replacement), not to SQL.

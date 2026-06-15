# Implementation Progress: Workflow Phases Merge

## Status: COMPLETE (all blockers and fixes addressed)

## Phase 0: Setup
- [x] Read design doc
- [x] Inspect current codebase
- [x] Run baseline type-check

## Phase 1: Backend - Database Schema
- [x] Create `phase_prompt_versions` table
- [x] Create `task_prompt_overrides` table
- [x] Create `phase_prompt_templates` table
- [x] Run DB migration
- [x] Verify schema with sqlite

## Phase 2: Backend - Prompt Assembler
- [x] Create `src/prompts/assembler.py` (Python)
- [x] Create `src/prompts/__init__.py`
- [x] Verified imports work

## Phase 3: Backend - API Endpoints
- [x] PATCH `/api/phases/{phase_id}` (partial update) — with type validation
- [x] POST `/api/phases/{phase_id}/reset` (reset status) — async subprocess, retry on race
- [x] GET `/api/phases/{phase_id}/agents`
- [x] GET `/api/phases/{phase_id}/prompt/versions`
- [x] GET `/api/phases/{phase_id}/prompt/versions/{version}`
- [x] POST `/api/phases/{phase_id}/prompt/versions` — with retry on IntegrityError
- [x] POST `/api/phases/{phase_id}/prompt/versions/{version}/publish`
- [x] POST `/api/phases/{phase_id}/prompt/versions/{version}/restore` — with retry on IntegrityError
- [x] GET `/api/phases/{phase_id}/prompt/preview` — with json.loads error handling
- [x] POST `/api/phases/{phase_id}/prompt/preview` — accepts draft content
- [x] GET `/api/phases/{phase_id}/prompt/diff`
- [x] GET `/api/tasks/{task_id}/prompt`
- [x] GET `/api/tasks/{task_id}/prompt/overrides`
- [x] PUT `/api/tasks/{task_id}/prompt/overrides` — no N+1 session
- [x] DELETE `/api/tasks/{task_id}/prompt/overrides`

## Phase 4: Frontend - Service Layer
- [x] Add new methods to `apiService` in `services/api.ts`
- [x] Add types to `types/index.ts` for prompts

## Phase 5: Frontend - Shared Prompt Assembler
- [x] Create `frontend/src/lib/promptAssember.ts`

## Phase 6: Frontend - Component Extraction
- [x] `components/workflow/WorkflowCard.tsx`
- [x] `components/workflow/WorkflowStats.tsx`
- [x] `components/workflow/PhaseList.tsx`
- [x] `components/workflow/PhaseCard.tsx`
- [x] `components/workflow/PhaseDetailPanel.tsx`
- [x] `components/workflow/PhaseOverview.tsx`
- [x] `components/workflow/PhasePromptsTab.tsx`
- [x] `components/workflow/PhaseConfigTab.tsx`
- [x] `components/workflow/PhaseTaskList.tsx`
- [x] `components/workflow/PhaseAgentList.tsx`
- [x] `components/workflow/TaskRow.tsx`

## Phase 7: Frontend - Prompt Editor Components
- [x] `components/workflow/prompts/PromptEditor.tsx`
- [x] `components/workflow/prompts/PromptPreview.tsx`
- [x] `components/workflow/prompts/PromptFieldList.tsx`
- [x] `components/workflow/prompts/PromptVersionHistory.tsx`

## Phase 8: Frontend - Rewrite WorkflowExecutions Page
- [x] Replace modal with inline expandable cards
- [x] Wire up state management with mutual exclusion invariants
- [x] Integrate all new components

## Phase 9: Adversarial Review Fixes
- [x] Fix `Task.status.in_()` bug (positional args → list)
- [x] Fix `subprocess.run` blocking event loop (use `run_in_executor`)
- [x] Fix race condition on version numbering (retry with IntegrityError)
- [x] Fix `json.loads` error handling in preview endpoint
- [x] Fix hardcoded DatabaseManager in assembler (accept db_manager param)
- [x] Fix preview to accept draft data (new POST endpoint)
- [x] Fix hardcoded localhost in PhaseDetailPanel
- [x] Fix input validation in update_phase
- [x] Fix missing React import in PromptPreview
- [x] Add confirmation for restore (two-click confirm)
- [x] Fix N+1 session in set_task_prompt_overrides
- [x] Fix draft initialization after publish (useRef for dedup)

## Phase 10: Testing
- [x] Run type-check (0 errors)
- [x] Run backend syntax check (OK)
- [x] Verify imports

## Phase 11: Cleanup
- [ ] Commit and push (pending)

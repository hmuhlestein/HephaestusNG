# Design: Merge Phases into Workflows

## Overview

Integrate the Phases view directly into the Workflows page, creating a drill-down experience where users can explore workflow phases, their tasks, agents, and rich metadata without leaving the Workflows context.

## Current State

### Workflows Page (`WorkflowExecutions.tsx`)
- Lists workflow executions as cards
- Clicking opens a modal with summary stats
- Phases shown as small summary blocks
- Tasks shown in a scrollable list
- Navigation to separate `/phases` page for details

### Phases Page (`Phases.tsx`)
- Standalone page for viewing phase details
- Shows phase cards with agent/task stats
- Dialog popup for rich phase data (description, done definitions, notes, outputs)
- Activity feed for real-time updates
- Requires separate navigation

### Data Available
```sql
-- Phases table
phases: id, workflow_id, order, name, description, done_definitions, 
        additional_notes, outputs, next_steps, working_directory,
        cli_tool, cli_model

-- Phase executions (per-execution status)
phase_executions: id, phase_id, workflow_execution_id, status, 
                  started_at, completed_at, completion_summary

-- Tasks linked to phases (note: phase_id can be UUID or order number)
tasks: id, phase_id, workflow_id, status, raw_description, 
       enriched_description, assigned_agent_id
```

## Design

### 1. Expandable Workflow Cards

Replace the modal-based workflow detail with an inline expandable card:

```
┌─────────────────────────────────────────────────────────────┐
│ ● ACTIVE  QA Testing                          3h 24m ago   │
│                                                             │
│ Diagnostic and remediation effort to unstick...             │
│                                                             │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │
│ │  Tasks  │ │  Agents │ │  Done   │ │  Failed │           │
│ │   47    │ │    6    │ │   32    │ │    5    │           │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘           │
│                                                             │
│ ▼ Phases (3)                                    [Collapse] │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ ┌─ Phase 1: test_planning ──────────────────────────────┐  │
│ │ ● IN PROGRESS                       2 agents, 1 task  │  │
│ │                                                         │  │
│ │ Description: QA Test Planning phase...                  │  │
│ │ Done: ✓ Test plan created ✓ CDP targets identified     │  │
│ │                                                         │  │
│ │ ┌─ Tasks ──────────────────────────────────────────┐   │  │
│ │ │ ● assigned  Create test_plan.md...        (80ch) │   │  │
│ │ │   Agent: a1b2c3d4... (opencode)                  │   │  │
│ │ └──────────────────────────────────────────────────┘   │  │
│ │                                                         │  │
│ │ [View Details] [View All Tasks]                         │  │
│ └─────────────────────────────────────────────────────────┘  │
│                                                             │
│ ┌─ Phase 2: test_implementation ────────────────────────┐  │
│ │ ○ PENDING                                               │  │
│ │ ...                                                     │  │
│ └─────────────────────────────────────────────────────────┘  │
│                                                             │
│ ┌─ Phase 3: test_execution ─────────────────────────────┐  │
│ │ ○ PENDING                                               │  │
│ │ ...                                                     │  │
│ └─────────────────────────────────────────────────────────┘  │
│                                                             │
│ [Go to Overview]                                            │
└─────────────────────────────────────────────────────────────┘
```

**Key behaviors:**
- Clicking a workflow card expands/collapses phases inline (no modal)
- Expanding a different workflow **automatically collapses any expanded phase** (mutual exclusion)
- Phase data lazy-loads on workflow expand (counts from parent, details on phase expand)
- "Go to Overview" button preserved for navigation to overview page

### 2. Phase Detail Panel

When clicking a phase, expand inline to show rich details:

```
┌─ Phase 1: test_planning ──────────────────────────────────┐
│ ● IN PROGRESS                                             │
│                                                            │
│ ┌─── Overview ──────────────────────────────────────────┐  │
│ │ Description:                                          │  │
│ │ You are a QA Test Planner for {project_name}.         │  │
│ │ Analyze the codebase and create a comprehensive...    │  │
│ │                                                       │  │
│ │ Done Definitions:                                     │  │
│ │ ✓ Test plan created with unit, integration, e2e       │  │
│ │ ✓ CDP targets identified                              │  │
│ │ ✓ Phase 2 task created                                │  │
│ │                                                       │  │
│ │ Additional Notes:                                     │  │
│ │ Focus on API endpoints and database models...         │  │
│ │                                                       │  │
│ │ Expected Outputs:                                     │  │
│ │ test_plan.md with comprehensive test cases            │  │
│ │                                                       │  │
│ │ Next Steps:                                           │  │
│ │ Proceed to Phase 2 implementation                     │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                            │
│ ┌─── Configuration ─────────────────────────────────────┐  │
│ │ CLI Tool:      opencode                               │  │
│ │ CLI Model:     anthropic/claude-sonnet-4              │  │
│ │ Working Dir:   /path/to/project                       │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                            │
│ ┌─── Tasks (1) ─────────────────────────────────────────┐  │
│ │ ┌──────────────────────────────────────────────────┐  │  │
│ │ │ ● assigned  Create test_plan.md with unit...     │  │  │
│ │ │   Agent: a1b2c3d4-e5f6-7890-abcd-ef1234567890   │  │  │
│ │ │   Priority: high | Started: 2h ago              │  │  │
│ │ │   [View Task] [Terminate Agent]                  │  │  │
│ │ └──────────────────────────────────────────────────┘  │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                            │
│ ┌─── Agents (1) ────────────────────────────────────────┐  │
│ │ ┌──────────────────────────────────────────────────┐  │  │
│ │ │ Agent a1b2c3d4...                               │  │  │
│ │ │ Status: working | Tool: opencode                 │  │  │
│ │ │ Started: 2h ago | Tasks: 1 assigned              │  │  │
│ │ │ [View Logs] [Terminate]                          │  │  │
│ │ └──────────────────────────────────────────────────┘  │  │
│ └───────────────────────────────────────────────────────┘  │
│                                                            │
│ [Close]                                                    │
└────────────────────────────────────────────────────────────┘
```

**Loading/Error/Empty states:**
- Loading: Spinner with "Loading phase details..."
- Error: Red box with "Failed to load phase details" + retry button
- Empty tasks: "No tasks in this phase yet"
- Empty agents: "No agents currently running"

### 3. Editing Phases

Editing is **disabled when agents are active** in the phase to prevent conflicts.

When no agents are active, clicking "Edit Phase" opens an edit mode:

```
┌─── Edit Phase ────────────────────────────────────────────┐
│                                                            │
│ Description:                                               │
│ ┌──────────────────────────────────────────────────────┐  │
│ │ You are a QA Test Planner for {project_name}.        │  │
│ │ Analyze the codebase and create a comprehensive...   │  │
│ └──────────────────────────────────────────────────────┘  │
│                                                            │
│ Done Definitions:                                          │
│ ┌──────────────────────────────────────────────────────┐  │
│ │ ✓ Test plan created with unit, integration, e2e      │  │
│ │ ✓ CDP targets identified                             │  │
│ │ ✓ Phase 2 task created                               │  │
│ └──────────────────────────────────────────────────────┘  │
│ [+ Add Item]                                               │
│                                                            │
│ CLI Tool:   [opencode ▾]                                   │
│ CLI Model:  [anthropic/claude-sonnet-4           ]         │
│ Working Dir:[/path/to/project                     ]        │
│                                                            │
│ ⚠️ Warning: 2 agents were active when editing started.    │
│ Changes will apply to new agents only.                     │
│                                                            │
│ [Cancel] [Save Changes]                                    │
└────────────────────────────────────────────────────────────┘
```

**Editing rules:**
- Editing disabled when phase status is `in_progress` with active agents
- If user force-enables editing (future), show warning about active agents
- `done_definitions` edited as a list with add/remove buttons per item
- `workflow_id`, `order`, `name` are immutable

**Force-edit confirmation (when phase has active agents):**

When the user clicks "Edit Phase" while active agents exist, show a
confirmation dialog before opening the editor:

```
┌─ Edit Phase With Active Agents ────────────────────────────┐
│                                                             │
│  ⚠️  This phase has 2 active agents.                       │
│                                                             │
│  Active agents have already captured the current prompt    │
│  and will continue using it for the rest of their run.     │
│                                                             │
│  Your changes will apply to:                                │
│    • 0 currently queued tasks (will use new prompt)         │
│    • 0 future tasks (will use new prompt)                   │
│    • 2 active agents (will keep old prompt)                │
│                                                             │
│  We recommend:                                              │
│    1. Wait for active agents to complete, then edit         │
│    2. Or terminate active agents first (loses their work)   │
│    3. Or proceed and accept the inconsistency               │
│                                                             │
│  [Cancel] [Terminate Agents & Edit] [Edit Anyway]          │
└─────────────────────────────────────────────────────────────┘
```

Three options:
- **Cancel** — closes the dialog, no edit
- **Terminate Agents & Edit** — calls the terminate endpoint for each
  active agent, then opens the editor (work is lost)
- **Edit Anyway** — opens the editor with the warning banner visible

### 4. Agent Prompt Editor

A dedicated editor for the agent prompt content that powers each phase. The phase
fields (description, done_definitions, additional_notes, outputs, next_steps) are
assembled into the context that gets injected into the LLM system/user prompts
for agents working in that phase. Editing these fields directly edits what
agents see.

#### Editor Locations

The prompt editor is exposed in three places:

1. **Phase Edit Form** — basic inline editor for the standard fields.
2. **Phase Detail Panel "Prompts" tab** — full-featured editor with template
   variable highlighting, preview, and versioning.
3. **Task Detail Modal** — per-task `system_prompt` / `user_prompt` editor
   (for individual task overrides).

#### Phase Detail Panel — Prompts Tab

When the user clicks the **"Prompts"** tab on a phase detail panel, they get a
richer editor than the basic form:

```
┌─ Phase 1: test_planning ── [Overview] [Prompts] [Tasks] [Agents] [Config] ─┐
│                                                                              │
│ ┌─ Prompt Preview ──────────────────────────────────────────────────────┐   │
│ │ This is what the LLM will see at runtime. Updates apply to new         │   │
│ │ tasks only — in-flight tasks keep their captured prompt.               │   │
│ │                                                                        │   │
│ │ ╭─ System Prompt ──────────────────────────────────────────────────╮   │   │
│ │ │ You are a QA Test Planner for {project_name}.                   │   │   │
│ │ │ ...                                                              │   │   │
│ │ ╰──────────────────────────────────────────────────────────────────╯   │   │
│ │                                                                        │   │
│ │ ╭─ User Prompt Template ───────────────────────────────────────────╮   │   │
│ │ │ ## WORKFLOW PHASE INFORMATION                                     │   │   │
│ │ │ ### Current Phase: Test Planning (Phase 1)                        │   │   │
│ │ │ ...                                                               │   │   │
│ │ ╰──────────────────────────────────────────────────────────────────╯   │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ┌─ Editable Fields ─────────────────────────────────────────────────────┐   │
│ │ Description (Phase System Prompt Root):                               │   │
│ │ ┌──────────────────────────────────────────────────────────────────┐ │   │
│ │ │ You are a QA Test Planner for {project_name}.                   │ │   │
│ │ │ Analyze the codebase and create a comprehensive...               │ │   │
│ │ └──────────────────────────────────────────────────────────────────┘ │   │
│ │ Variables: {project_name} {phase_number} {phase_name} {workflow_name}│   │
│ │                                                                        │   │
│ │ Done Definitions:                                                      │   │
│ │ ┌──────────────────────────────────────────────────────────────────┐ │   │
│ │ │ 1 │ Test plan created with unit, integration, e2e coverage       │ │   │
│ │ │ 2 │ CDP targets identified                                        │ │   │
│ │ │ 3 │ Phase 2 task created                                          │ │   │
│ │ └──────────────────────────────────────────────────────────────────┘ │   │
│ │ [+ Add criterion]                                                      │   │
│ │                                                                        │   │
│ │ Additional Notes:                                                      │   │
│ │ ┌──────────────────────────────────────────────────────────────────┐ │   │
│ │ │ Focus on API endpoints and database models...                   │ │   │
│ │ └──────────────────────────────────────────────────────────────────┘ │   │
│ │                                                                        │   │
│ │ Expected Outputs:                                                      │   │
│ │ ┌──────────────────────────────────────────────────────────────────┐ │   │
│ │ │ test_plan.md with comprehensive test cases                        │ │   │
│ │ └──────────────────────────────────────────────────────────────────┘ │   │
│ │                                                                        │   │
│ │ Next Steps:                                                            │   │
│ │ ┌──────────────────────────────────────────────────────────────────┐ │   │
│ │ │ Proceed to Phase 2 implementation                                 │ │   │
│ │ └──────────────────────────────────────────────────────────────────┘ │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ┌─ Version History ─────────────────────────────────────────────────────┐   │
│ │ v3 (current, unsaved)  by you     just now       [+12 chars, +1 item] │   │
│ │ v2 (active)            by alice   2026-06-13 14:02  [restore] [diff]  │   │
│ │ v1                     by alice   2026-06-13 09:15  [restore] [diff]  │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│ ⚠️ 2 active agents will NOT receive these changes.                        │
│    0 queued tasks WILL receive the new prompt.                             │
│                                                                              │
│ [Discard] [Save as Draft] [Save & Publish]                                  │
└─────────────────────────────────────────

#### Prompt Editor Features

**Live preview** — Renders the assembled prompt the LLM will see, with
template variables (`{project_name}`, `{phase_number}`, etc.) highlighted
in a distinct color. Updates as the user types (debounced 300ms).

**Template variable detection** — Recognizes `{var_name}` tokens and shows
a legend of available variables. Unknown variables flagged with a warning
icon.

**Per-field editing** — Each field is a separate editable widget:
- `description` — textarea with markdown preview toggle
- `done_definitions` — draggable list with add/remove/edit
- `additional_notes` — textarea with markdown preview
- `outputs` — textarea
- `next_steps` — textarea

**Versioning** — Every save creates a new version row. Users can:
- View diffs between any two versions
- Restore an older version (creates a new version with the old content)
- See who edited what and when

**Active agent impact warning** — Always displayed when the phase has
active agents. Shows the count of active agents vs queued tasks and
clarifies which will/won't receive the changes.

**Save modes**:
- **Discard** — throw away local edits
- **Save as Draft** — persist the version but mark it inactive (queued
  tasks still use the active version)
- **Save & Publish** — persist the version and mark it active (queued
  tasks immediately use it; active agents continue with the old one)

**Read-only enforcement** — Editor is fully read-only when:
- Phase is `in_progress` AND has active agents AND user has not confirmed
  the "force edit" acknowledgment
- User lacks the `phase.edit` permission

**Keyboard shortcuts**:
- `Cmd/Ctrl+S` — Save & Publish
- `Cmd/Ctrl+D` — Save as Draft
- `Esc` — Discard changes (with confirmation if dirty)
- `Tab` between fields
- `Cmd/Ctrl+/` — Toggle preview

#### Task-Level Prompt Editor

The Task Detail Modal gets a "Prompts" tab to edit per-task overrides:

```
┌─ Task: Create test_plan.md ── [Overview] [Prompts] [Results] ─┐
│                                                                 │
│ ┌─ System Prompt (override) ──────────────────────────────────┐│
│ │ ┌────────────────────────────────────────────────────────┐  ││
│ │ │ You are a Senior QA Engineer...                        │  ││
│ │ │ (leave empty to use phase default)                     │  ││
│ │ └────────────────────────────────────────────────────────┘  ││
│ │ [Clear override] [Reset to phase default]                   ││
│ └────────────────────────────────────────────────────────────┘│
│                                                                 │
│ ┌─ User Prompt (override) ───────────────────────────────────┐│
│ │ ┌────────────────────────────────────────────────────────┐  ││
│ │ │ Create a comprehensive test_plan.md...                 │  ││
│ │ └────────────────────────────────────────────────────────┘  ││
│ │ [Clear override] [Reset to phase default]                   ││
│ └────────────────────────────────────────────────────────────┘│
│                                                                 │
│ [Cancel] [Save]                                                │
└─────────────────────────────────────────────────────────────────┘
```

**Task override rules:**
- Empty override → falls back to phase default
- Non-empty override → replaces the relevant section in the assembled prompt
- Overrides are versioned per task, with the same diff/restore flow
- Editing a task's prompt while assigned is **blocked** (the prompt was
  captured at assignment time)

#### Prompt Assembly Pipeline

The frontend preview and the backend both run the same assembly logic.
Pipeline:

```
Phase Definition
  + Working Directory
  + Template Variables (project_name, phase_number, etc.)
  + CLI Tool / Model
  + Task Overrides (system_prompt, user_prompt)
    ↓
  Prompt Assembler
    ↓
  System Prompt + User Prompt sent to CLI agent
```

The `Prompt Assembler` is shared code (Python on backend, TypeScript port
on frontend) so the preview matches runtime exactly.

### 5. API Endpoints

**PATCH /api/phases/{phase_id}**
```json
// Request
{
  "description": "Updated description",
  "done_definitions": ["Item 1", "Item 2"],
  "cli_tool": "opencode",
  "cli_model": "anthropic/claude-sonnet-4",
  "working_directory": "/path/to/project"
}

// Response
{
  "success": true,
  "phase": { ... }
}
```

**POST /api/phases/{phase_id}/reset**
```json
// Request
{
  "target_status": "pending",  // Required: pending, in_progress, completed, failed
  "force": false               // Optional: override active agent check
}

// Response
{
  "success": true,
  "terminated_agents": 2,
  "reset_tasks": 5,
  "message": "Phase reset to pending"
}
```

**GET /api/phases/{phase_id}/agents**
```json
// Response
{
  "agents": [
    {
      "id": "a1b2c3d4...",
      "status": "working",
      "cli_type": "opencode",
      "current_task_id": "...",
      "started_at": "2026-01-01T00:00:00Z",
      "health_check_failures": 0
    }
  ]
}
```

**Prompt-related endpoints:**

```python
# Phase prompt versions
GET    /api/phases/{phase_id}/prompt/versions        # List versions (newest first)
GET    /api/phases/{phase_id}/prompt/versions/{v}    # Get specific version content
POST   /api/phases/{phase_id}/prompt/versions        # Create new version (draft or publish)
POST   /api/phases/{phase_id}/prompt/versions/{v}/publish  # Publish a draft version
POST   /api/phases/{phase_id}/prompt/versions/{v}/restore # Restore as new version
GET    /api/phases/{phase_id}/prompt/preview         # Rendered preview with variables
GET    /api/phases/{phase_id}/prompt/diff?v1=X&v2=Y  # Diff between two versions

# Task prompt overrides
GET    /api/tasks/{task_id}/prompt                   # Current prompt (with overrides applied)
GET    /api/tasks/{task_id}/prompt/overrides         # Active override values
PUT    /api/tasks/{task_id}/prompt/overrides         # Set overrides (system_prompt, user_prompt)
DELETE /api/tasks/{task_id}/prompt/overrides         # Clear overrides
```

**POST /api/phases/{phase_id}/prompt/versions**
```json
// Request
{
  "description": "Updated phase instructions",
  "done_definitions": ["Test plan created", "CDP targets identified"],
  "additional_notes": "Focus on API endpoints",
  "outputs": "test_plan.md",
  "next_steps": "Proceed to implementation",
  "change_summary": "Clarified scope to API endpoints only",
  "publish": false  // true = save & publish, false = save as draft
}

// Response
{
  "version": 3,
  "status": "draft",  // or "active"
  "created_at": "2026-06-14T10:23:00Z",
  "created_by": "ui-user",
  "diff": {
    "added_lines": 2,
    "removed_lines": 1,
    "changed_fields": ["description", "done_definitions"]
  }
}
```

**GET /api/phases/{phase_id}/prompt/preview**
```json
// Response
{
  "system_prompt": "You are a QA Test Planner for hephaestus.\n\n...",
  "user_prompt": "## WORKFLOW PHASE INFORMATION\n### Current Phase: Test Planning (Phase 1)\n\n...",
  "variables_used": ["project_name", "phase_number", "phase_name"],
  "warnings": [
    "Variable {missing_var} referenced in description but not defined"
  ]
}
```

**GET /api/phases/{phase_id}/prompt/diff?v1=1&v2=3**
```json
// Response
{
  "from_version": 1,
  "to_version": 3,
  "unified_diff": "--- v1\n+++ v3\n@@ -1,3 +1,4 @@\n...",
  "field_changes": {
    "description": {"from": "...", "to": "..."},
    "done_definitions": {"added": ["X"], "removed": ["Y"]}
  }
}
```

**PUT /api/tasks/{task_id}/prompt/overrides**
```json
// Request
{
  "system_prompt": "You are a Senior QA Engineer...",  // null to clear
  "user_prompt": "Create a comprehensive test_plan.md..."
}

// Response
{
  "success": true,
  "overrides": { "system_prompt": "...", "user_prompt": "..." },
  "effective_prompt": {
    "system_prompt": "...",  // includes phase defaults
    "user_prompt": "..."
  }
}
```

## Component Structure

Extract components to separate files to keep `WorkflowExecutions.tsx` manageable:

```
frontend/src/
├── pages/
│   └── WorkflowExecutions.tsx          # Main page (~200 lines)
├── components/
│   └── workflow/
│       ├── WorkflowCard.tsx            # Expandable workflow card
│       ├── WorkflowStats.tsx           # Stats grid (tasks, agents, done, failed)
│       ├── PhaseList.tsx               # List of phases in a workflow
│       ├── PhaseCard.tsx               # Expandable phase card
│       ├── PhaseDetailPanel.tsx        # Tabbed phase details (Overview/Prompts/Tasks/Agents/Config)
│       ├── PhaseOverview.tsx           # Overview tab content
│       ├── PhasePromptsTab.tsx         # Prompts tab with PromptEditor
│       ├── PhaseTasksTab.tsx           # Tasks tab content
│       ├── PhaseAgentsTab.tsx          # Agents tab content
│       ├── PhaseConfigTab.tsx          # Config tab content
│       ├── PhaseTaskList.tsx           # Tasks list within a phase
│       ├── PhaseAgentList.tsx          # Agents list within a phase
│       ├── PhaseEditForm.tsx           # Quick edit form for phase fields
│       ├── TaskRow.tsx                 # Single task display
│       └── prompts/
│           ├── PromptEditor.tsx        # Main prompt editor (preview + fields)
│           ├── PromptPreview.tsx        # Rendered prompt preview
│           ├── PromptFieldList.tsx     # Editable list of done_definitions
│           ├── PromptVersionHistory.tsx # Version list with diff/restore
│           ├── PromptDiffViewer.tsx     # Side-by-side diff display
│           ├── TaskPromptEditor.tsx    # Task-level prompt override editor
│           └── TemplateVariableLegend.tsx # Shows available {var} tokens
├── lib/
│   └── promptAssember.ts               # Shared prompt assembly (TS port of Python logic)
```

**Component patterns:** Use shadcn/ui components (`Card`, `Badge`, `Button`,
`ScrollArea`, `Tabs`, `Dialog`) consistently, matching `Phases.tsx` style.

## State Management

Use React Query for all data fetching and caching. No custom cache layer.

```typescript
// WorkflowExecutions.tsx state
const [expandedWorkflowId, setExpandedWorkflowId] = useState<string | null>(null);
const [expandedPhaseId, setExpandedPhaseId] = useState<string | null>(null);
const [editingPhaseId, setEditingPhaseId] = useState<string | null>(null);

// Invariant: changing expandedWorkflowId resets expandedPhaseId
const handleWorkflowClick = (workflowId: string) => {
  setExpandedPhaseId(null);
  setEditingPhaseId(null);
  setExpandedWorkflowId(prev => prev === workflowId ? null : workflowId);
};

// Invariant: changing expandedPhaseId resets editingPhaseId
const handlePhaseClick = (phaseId: string) => {
  setEditingPhaseId(null);
  setExpandedPhaseId(prev => prev === phaseId ? null : phaseId);
};

// Data fetching via React Query
const { data: phaseDetails } = useQuery({
  queryKey: ['phase-details', expandedPhaseId],
  queryFn: () => apiService.getPhaseDetails(expandedPhaseId!),
  enabled: !!expandedPhaseId,
});

const { data: phaseTasks } = useQuery({
  queryKey: ['phase-tasks', expandedPhaseId],
  queryFn: () => apiService.getTasks(0, 50, undefined, undefined, expandedPhaseId!),
  enabled: !!expandedPhaseId,
  refetchInterval: 10000,  // 10s for tasks (less critical)
});

const { data: phaseAgents } = useQuery({
  queryKey: ['phase-agents', expandedPhaseId],
  queryFn: () => apiService.getPhaseAgents(expandedPhaseId!),
  enabled: !!expandedPhaseId,
  refetchInterval: 5000,  // 5s for agents (real-time important)
});

// Prompt editor state (per-phase, in PromptEditor component)
const [draftPrompt, setDraftPrompt] = useState<PhasePrompt | null>(null);
const [previewVersion, setPreviewVersion] = useState<'active' | 'draft'>('active');
const [forceEditEnabled, setForceEditEnabled] = useState(false);

// Fetch active prompt version
const { data: activePrompt } = useQuery({
  queryKey: ['phase-prompt-active', expandedPhaseId],
  queryFn: () => apiService.getPhasePromptActive(expandedPhaseId!),
  enabled: !!expandedPhaseId,
});

// Fetch version history (lazy, on tab open)
const { data: versions } = useQuery({
  queryKey: ['phase-prompt-versions', expandedPhaseId],
  queryFn: () => apiService.getPhasePromptVersions(expandedPhaseId!),
  enabled: !!expandedPhaseId && promptTabOpen,
});

// Fetch rendered preview (debounced)
const { data: preview } = useQuery({
  queryKey: ['phase-prompt-preview', expandedPhaseId, draftPrompt],
  queryFn: () => apiService.getPhasePromptPreview(expandedPhaseId!, draftPrompt!),
  enabled: !!draftPrompt && promptTabOpen,
  staleTime: 1000,  // debounce
});

// Save mutation
const savePromptMutation = useMutation({
  mutationFn: (data: { prompt: PhasePrompt; publish: boolean; changeSummary: string }) =>
    apiService.savePhasePrompt(expandedPhaseId!, data),
  onSuccess: () => {
    queryClient.invalidateQueries(['phase-prompt-active', expandedPhaseId]);
    queryClient.invalidateQueries(['phase-prompt-versions', expandedPhaseId]);
  },
});
```

## Polling Strategy

| Data | Interval | Rationale |
|------|----------|-----------|
| Workflow list | 5s | Core data, always fresh |
| Phase counts (tasks, agents) | Inherited from workflow | Part of workflow response |
| Phase details (description, config) | On expand only | Static, no polling needed |
| Phase tasks | 10s | Less critical, batch updates |
| Phase agents | 5s | Real-time status important |

**Single expanded state endpoint:** Add `GET /api/workflows/{id}/expanded` that returns workflow + phases + task counts + agent counts in one call, avoiding N+1 queries.

## Styling

- Use existing Tailwind classes consistently
- Expand/collapse via CSS `transition: max-height` (avoid framer-motion layout thrashing)
- Color coding (aligned with conventions):
  - **Green** = completed/success
  - **Blue** = active/in-progress
  - **Yellow** = pending
  - **Red** = failed
  - **Gray** = skipped/inactive
- Phase cards inherit workflow color theme
- Task descriptions truncated to 80 chars with tooltip for full text

## Navigation

Existing routes preserved:
- `/workflows` — this page (primary)
- `/phases` — remains accessible, add note "Consider using Workflows page"
- `/tasks?phase={id}` — linked from phase task list
- `/agents/{id}` — linked from agent logs button
- `/overview` — linked from "Go to Overview" button

No redirects. Both `/phases` and `/workflows` remain functional.

## Migration

### Database Changes
- **New table: `phase_prompt_versions`** — version history
  - `id`, `phase_id`, `version`, `status` (active/draft), `description`,
    `done_definitions` (JSON), `additional_notes`, `outputs`, `next_steps`,
    `change_summary`, `created_at`, `created_by`, `parent_version`
- **New table: `task_prompt_overrides`** — per-task overrides
  - `task_id` (PK), `system_prompt`, `user_prompt`, `updated_at`, `updated_by`
- **New table: `phase_prompt_templates`** — available template variables
  - `id`, `name`, `description`, `example_value`, `resolver` (Python path)

### Code Changes
- Add `working_directory` to `get_phase_details` API response
- New `PromptAssembler` class in `src/prompts/assembler.py` (shared logic)
- TypeScript port at `frontend/src/lib/promptAssember.ts`
- Phases page remains accessible with note suggesting Workflows page
- Workflows page becomes primary interface for phase exploration

## Testing

1. **Unit Tests**:
   - Component rendering, state transitions, mutual exclusion invariant
   - `PromptAssembler` produces identical output in Python and TypeScript
   - Template variable resolution and warning detection
   - Diff computation between versions
2. **Integration Tests**:
   - API calls: PATCH, POST, GET endpoints
   - Phase prompt version CRUD
   - Task prompt override CRUD
   - Preview rendering with all template variables
3. **E2E Tests**:
   - Click flow: expand workflow → expand phase → switch to Prompts tab → edit → preview → save & publish
   - Version history: view diff, restore old version
   - Active agent impact warning display
   - Task prompt override edit
   - Error states: network failure, validation error, conflict
4. **Accessibility**:
   - Keyboard navigation: Tab through fields, Cmd+S to save
   - Focus management on tab switch
   - Aria labels on expandable regions and editable fields
   - Screen reader announcements for version publishes
5. **Prompt Fidelity Tests**:
   - Snapshot test: same phase + task inputs produce identical prompts in
     preview vs runtime (catches assembler drift)
   - Variable substitution: missing variable produces warning, not crash
   - Override precedence: task override > phase default

## Implementation Order

1. **Extract components** — Move workflow/phase components to `components/workflow/`
2. **Expandable cards** — Implement workflow expand/collapse with phase list
3. **Phase detail panel** — Lazy-load and display rich phase data (Overview tab)
4. **Agent integration** — Show agents, logs, terminate buttons (Agents tab)
5. **Basic editing** — PATCH endpoint + edit form with active agent checks
6. **Reset** — POST endpoint with confirmation dialog
7. **Prompt assembler** — Build shared `PromptAssembler` (Python + TS port)
8. **Prompt version schema** — DB migration for `phase_prompt_versions`
9. **Prompt preview endpoint** — `GET /api/phases/{id}/prompt/preview`
10. **PromptEditor component** — UI for the Prompts tab with preview
11. **Version history** — List, diff, restore endpoints + UI
12. **Task prompt overrides** — DB migration + endpoints + `TaskPromptEditor`
13. **Polish** — Error states, loading states, empty states, keyboard nav

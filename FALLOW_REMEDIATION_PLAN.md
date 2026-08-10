# Fallow Remediation Plan

> Generated from `fallow` analysis (v3.14.0) against HephaestusNG frontend.
> Health score: **36/100 (F)**. This plan targets all issues to reach ≥80.

---

## Phase 1: Delete Dead Code (low risk, high impact)

**Goal:** Remove 15 unused files, 10+ unused exports, 10 unused types, 2 unused props.  
**Estimated LOC removed:** ~2,900 lines  
**Risk:** Low — all files confirmed unreachable from entry points.

**Prerequisite:** `cd frontend && npm install` (fallow warns about missing node_modules; analysis is more accurate with dependencies installed).

### 1a. Delete unused files

| File | LOC | Notes |
|---|---|---|
| `frontend/src/components/AdvancedFilterBar.tsx` | 260 | No importers. `dead_code_ratio: 1.0` |
| `frontend/src/components/BudgetStatusCard.tsx` | 92 | No importers. `dead_code_ratio: 1.0` |
| `frontend/src/components/PanelSearch.tsx` | 182 | No importers. `dead_code_ratio: 1.0` |
| `frontend/src/components/WorkflowSelector.tsx` | 187 | No importers. `dead_code_ratio: 1.0` |
| `frontend/src/components/autopilot/ProjectSelector.tsx` | 259 | No importers. `dead_code_ratio: 1.0` |
| `frontend/src/hooks/useSocket.ts` | 6 | No importers. `dead_code_ratio: 1.0` |

### 1b. Delete `tools/tmux-viewer/` entirely

The frontend subdirectory is dead — all 6 files have `dead_code_ratio: 1.0`. The backend has zero references from the main codebase. The entire `tools/tmux-viewer/` directory is an unused standalone tool. Total: ~1,000 LOC.

| File | LOC |
|---|---|
| `tools/tmux-viewer/frontend/src/components/ObservabilityPanel.tsx` | 192 |
| `tools/tmux-viewer/frontend/src/components/RealTimeAgentOutput.tsx` | 323 |
| `tools/tmux-viewer/frontend/src/hooks/useMultiAgentOutput.ts` | 180 |
| `tools/tmux-viewer/frontend/src/hooks/useRealTimeAgentOutput.ts` | 117 |
| `tools/tmux-viewer/frontend/src/index.ts` | 6 |
| `tools/tmux-viewer/frontend/src/services/api.ts` | 70 |
| `tools/tmux-viewer/frontend/src/types.ts` | 40 |
| `tools/tmux-viewer/backend/` | ~70 |

**Action:** `rm -rf tools/tmux-viewer/`

**Also resolves:** 6 clone families (~675 duplicated lines between `frontend/` and `tools/tmux-viewer/`), 3 unresolved imports in `tools/tmux-viewer/frontend/src/services/api.ts`.

### 1c. Website files — SKIP

Fallow flagged `website/src/components/HomepageFeatures/` as unused, but `index.tsx` is imported by `website/src/pages/index.tsx:6`. These are live website files — **do not delete**. Follow up with `fallow init` and add the website entry point to the config so fallow can see it.

### 1d. Remove unused exports

Run: `fallow fix --dry-run` then `fallow fix`

**`fallow fix` auto-fixes 10 exports** (safe removals):

| File | Export | Fix |
|---|---|---|
| `frontend/src/components/ExecutionSelector.tsx:5` | `ExecutionSelector` (named) | Remove `export` keyword (default export at EOF is used by 4 pages) |
| `frontend/src/components/cost/index.ts:3` | `DesignCostRow` (re-export) | Remove re-export line |
| `frontend/src/components/ui/alert.tsx:58` | `AlertTitle` | Remove `export` |
| `frontend/src/components/ui/badge.tsx:35` | `badgeVariants` | Remove `export` |
| `frontend/src/components/ui/button.tsx:51` | `buttonVariants` | Remove `export` |
| `frontend/src/components/ui/card.tsx:78` | `CardFooter` | Remove `export` |
| `frontend/src/components/ui/scroll-area.tsx:34` | `ScrollBar` | Remove `export` |
| `frontend/src/hooks/useMultiAgentOutput.ts:250` | `useRealTimeAgentOutput` | Remove export |
| `frontend/src/hooks/useTaskRuntime.ts:96` | `formatRuntimeDuration` | Remove `export` |

**Suppress (do NOT remove) 4 shadcn/ui primitive exports:**

| File | Export | Action |
|---|---|---|
| `frontend/src/components/ui/dialog.tsx:110` | `DialogPortal` | Suppress: `// fallow-ignore-next-line unused-export` |
| `frontend/src/components/ui/dialog.tsx:111` | `DialogOverlay` | Suppress |
| `frontend/src/components/ui/dialog.tsx:112` | `DialogClose` | Suppress |
| `frontend/src/components/ui/dialog.tsx:113` | `DialogTrigger` | Suppress |

These are shadcn/ui component primitives — part of the public API surface for custom dialog composition. Removing them breaks the component library contract.

**Manual review needed (fallow fix skips these):**

| File | Export | Why fallow skips |
|---|---|---|
| `frontend/src/components/cost/DesignCostRow.tsx:35` | `default` | Default export — may be dynamically imported |
| `frontend/src/utils/markdown.tsx:26` | `default` | Default export — may be dynamically imported |

### 1e. Remove unused types

| File | Type |
|---|---|
| `frontend/src/components/LayoutManager.tsx:5` | `SavedLayout` |
| `frontend/src/components/ui/button.tsx:34` | `ButtonProps` |
| `frontend/src/context/WorkflowContext.tsx:6` | `WorkflowDefinition`, `WorkflowExecution` |
| `frontend/src/hooks/useLayoutPersistence.ts:16` | `LayoutHistory` |
| `frontend/src/types/index.ts:593` | `PhaseResetResponse` |

### 1f. Fix unused component props

| File | Prop | Fix |
|---|---|---|
| `frontend/src/components/cost/DesignCostRow.tsx:16` | `designId` | Prefix with `_` (already done: `_designId`) — fallow false positive? Verify. |
| `frontend/src/components/cost/ProjectCostSummary.tsx:19` | `projectId` | Prefix with `_` (already done: `_projectId`) — fallow false positive? Verify. |

**Note:** Both props are already prefixed with `_` in the destructuring (`designId: _designId`, `projectId: _projectId`). Fallow may still flag them. Options: (a) remove from interface + destructuring entirely, or (b) suppress with `// fallow-ignore-next-line unused-component-prop`.

### 1g. Resolve duplicate export

`SavedLayout` is exported from both `LayoutManager.tsx:5` and `useLayoutPersistence.ts:7`. After 1e removes the unused type exports, verify which one is actually used (if any) and remove the other.

**Note:** `fallow fix` will create a `.fallowrc.json` with `ignoreExports` rules for these files as a safety measure. Review the generated config before committing.

---

## Phase 2: Break Circular Dependencies (high impact, moderate risk)

**Goal:** Eliminate all 8 circular dependency cycles.  
**Approach:** Extract shared utilities into standalone modules; use lazy imports or render props to break remaining cycles.

### 2a. Autopilot utility extraction (fixes 6 cycles)

**Root cause:** `FeatureDetailModal`, `FeatureGallery`, `FeatureReviewModal`, and `PipelineStatusCard` all import `{ StatusBadge, StatusIcon, formatTime }` from `@/pages/Autopilot`. `Autopilot.tsx` imports those components back → cycle.

**Fix:** Extract `StatusBadge`, `StatusIcon`, and `formatTime` from `Autopilot.tsx` (lines 471-507) into a new file:

**Create:** `frontend/src/components/autopilot/utils.tsx`
```tsx
// Extracted from pages/Autopilot.tsx to break circular dependencies
// Shared by: FeatureDetailModal, FeatureGallery, FeatureReviewModal, PipelineStatusCard

export const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  // ... move from Autopilot.tsx:471-485
};

export const StatusIcon: React.FC<{ status: string }> = ({ status }) => {
  // ... move from Autopilot.tsx:487-492
};

export const formatTime = (seconds: number): string => {
  // ... move from Autopilot.tsx:494-507
};
```

**Then update imports in:**
- `FeatureDetailModal.tsx:11` → `import { StatusBadge, StatusIcon, formatTime } from './utils'`
- `FeatureGallery.tsx:9` → `import { StatusBadge, StatusIcon, formatTime } from './utils'`
- `FeatureReviewModal.tsx:6` → `import { StatusBadge, StatusIcon, formatTime } from './utils'`
- `PipelineStatusCard.tsx:4` → `import { formatTime } from './utils'`
- `Autopilot.tsx` → `import { StatusBadge, StatusIcon, formatTime } from '@/components/autopilot/utils'`

**This breaks all 6 Autopilot cycles in one move.**

### 2b. DesignQueuePanel ↔ FeatureRecordDetailModal (fixes 1 cycle)

**Root cause:** `DesignQueuePanel.tsx:32` imports `FeatureRecordDetailModal`. `FeatureRecordDetailModal.tsx:8` imports `{ FeatureStatusBadge }` from `./DesignQueuePanel`.

**Fix:** Move `FeatureStatusBadge` (currently at `DesignQueuePanel.tsx:787`) into the shared `autopilot/utils.tsx` file created in 2a.

**Update:**
- `FeatureRecordDetailModal.tsx:8` → `import { FeatureStatusBadge } from './utils'`
- `DesignQueuePanel.tsx` → import from `./utils` if needed internally, or keep local and re-export

### 2c. TaskDetailModal ↔ TicketDetailModal ↔ AgentDetailModal (fixes 1 cycle)

**Root cause chain:**
- `TaskDetailModal.tsx:54` imports `TicketDetailModal`
- `TicketDetailModal.tsx:27` imports `AgentDetailModal`
- `AgentDetailModal.tsx:22` imports `TaskDetailModal`

**Fix:** Use `React.lazy()` for the nested modal import in `AgentDetailModal`. `AgentDetailModal` renders `TaskDetailModal` at line 321 only when a user clicks on a task — this is a perfect lazy-load candidate.

**Update `AgentDetailModal.tsx`:**
```tsx
// Replace direct import with lazy
const TaskDetailModal = React.lazy(() => import('./TaskDetailModal'));

// Wrap the rendered instance in Suspense
{selectedTaskId && (
  <React.Suspense fallback={null}>
    <TaskDetailModal
      taskId={selectedTaskId}
      onClose={() => setSelectedTaskId(null)}
      onNavigateToTask={...}
    />
  </React.Suspense>
)}
```

**Alternative (cleaner):** Accept `TaskDetailModal` as a render prop from parent:
```tsx
interface AgentDetailModalProps {
  // ...existing props...
  renderTaskDetail?: (taskId: string, onClose: () => void) => React.ReactNode;
}
```

The lazy import approach is simpler and doesn't require changing the 7 call sites.

---

## Phase 3: Reduce Duplication (moderate impact)

**Goal:** Eliminate remaining ~2,200 duplicated lines (after tmux-viewer deletion removes ~500).  
**Remaining clone families:** 9

### 3a. `Results.tsx` internal duplication (154 lines)

4 clone groups within the same file (lines 251-303 ↔ 455-507, 160-197 ↔ 728-771).

**Fix:** Extract repeated rendering patterns into local helper functions within `Results.tsx`.

### 3b. `BroadcastMessageDialog.tsx` ↔ `SendMessageDialog.tsx` (92 lines)

4 clone groups of shared state management and submit handling.

**Fix:** Extract shared logic into `frontend/src/hooks/useMessageDialog.ts`:
```tsx
export function useMessageDialog() {
  const [message, setMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [statusMessage, setStatusMessage] = useState('');
  // ...shared submit/error handling
}
```

### 3c. `SidebarProjectSelector.tsx` ↔ `ProjectSelector.tsx` (40 lines)

3 clone groups of shared project selection logic.

**Fix:** Extract shared selector logic into `frontend/src/hooks/useProjectSelector.ts`.

### 3d. `AddDesignModal.tsx` ↔ `LoadDesignModal.tsx` (47 lines)

2 clone groups.

**Fix:** Extract shared form/validation logic into `frontend/src/components/autopilot/designModalUtils.ts`.

### 3e. `FeatureDetailModal.tsx` ↔ `FeatureRecordDetailModal.tsx` (57 lines)

3 clone groups. Both share similar status badge and tab rendering patterns.

**Fix:** After Phase 2a extracts shared utils, the remaining duplication is likely tab structure — extract a shared `DetailTabs` component.

### 3f. `DesignQueuePanel.tsx` internal duplication (237 lines)

The largest single clone group: `SortableDesignItem` (lines 538-774) has 237 lines duplicated with `LoadDesignModal` (lines 173-189). This is likely shared drag-drop/status rendering logic.

**Fix:** Extract shared status/action rendering into `DesignQueuePanel` sub-components.

---

## Phase 4: Decompose Complex Components (high impact, high effort)

**Goal:** Reduce the 230 functions above complexity threshold. Target the top 10 CRITICAL files.

### 4a. `TaskDetailModal.tsx` (CRAP 21,170 — worst offender)

**Stats:** 1,371 LOC, 60 functions, cyclomatic 145, cognitive 114, 14 hooks, JSX depth 15.

**Decompose into:**
| New Component | Responsibility | Est. LOC |
|---|---|---|
| `TaskDetailHeader.tsx` | Title bar, status badge, close button | ~60 |
| `TaskDetailInfo.tsx` | Task metadata grid (status, priority, dates) | ~100 |
| `TaskDetailActions.tsx` | Action buttons (terminate, copy, navigate) | ~80 |
| `TaskDetailTimeline.tsx` | Activity/steering event timeline | ~150 |
| `TaskDetailLinked.tsx` | Linked tasks, tickets, related items | ~200 |
| `TaskDetailTrajectory.tsx` | Alignment graph + trajectory viz | ~100 |
| `TaskDetailPrompts.tsx` | Prompt history display | ~80 |
| `TaskDetailOutput.tsx` | Real-time agent output panel | ~50 |

The modal shell (`TaskDetailModal.tsx`) becomes ~200 LOC orchestrating these sub-components.

### 4b. `DesignQueuePanel.tsx` (1,208 LOC, 103 functions, 11 above threshold)

**Decompose into:**
| New Component | Responsibility |
|---|---|
| `DesignQueueToolbar.tsx` | Search bar, reload/load/add buttons |
| `SortableDesignItem.tsx` | Single design row (already exists as function, extract to file) |
| `DesignStatusBadge.tsx` | Design status indicator |
| `DesignActionMenu.tsx` | Pause/stop/resume/rerun actions |

### 4c. `RealTimeAgentOutput.tsx` (CRAP 3,080)

**Stats:** 673 LOC, 35 functions, 27 hooks, cyclomatic 55, cognitive 67.

**Decompose into:**
| New Component | Responsibility |
|---|---|
| `AgentOutputToolbar.tsx` | Filter, search, controls |
| `AgentOutputList.tsx` | Scrollable output lines |
| `AgentOutputLine.tsx` | Single output line rendering |
| `AgentOutputStats.tsx` | Token/cost statistics |

### 4d. `TicketDetailModal.tsx` (CRAP 3,080)

**Stats:** 929 LOC, 53 functions, cyclomatic 55, cognitive 56.

**Decompose into:**
| New Component | Responsibility |
|---|---|
| `TicketDetailHeader.tsx` | Title, status, approval UI |
| `TicketDetailDescription.tsx` | Editable description |
| `TicketDetailRelations.tsx` | Related tasks, blocked-by, commits |
| `TicketDetailComments.tsx` | Comment thread |
| `TicketDetailActivity.tsx` | Activity log |

### 4e. `Results.tsx` (1,280 LOC, 85 functions, 10 above threshold)

**Decompose into:**
| New Component | Responsibility |
|---|---|
| `ResultsToolbar.tsx` | Search, filter, export buttons |
| `ResultsList.tsx` | Result items list |
| `ResultContentDialog.tsx` | Already exists as inner function — extract to file |
| `ResultValidationDialog.tsx` | Already exists as inner function — extract to file |

### 4f. Other high-complexity components (lower priority)

| File | LOC | CRAP | Action |
|---|---|---|---|
| `Agents.tsx:41 AgentCard` | 274 | 1,640 | Extract `AgentCard` to own file |
| `ObservabilityPanel.tsx` | 314 | 1,122 | Extract sub-panels |
| `Tasks.tsx:241 arrow fn` | 77 | 1,056 | Extract `TaskRow` to own file |
| `Autopilot.tsx:82 mutationFn` | 112 | 506 | Extract `togglePipeline` mutation to hook |
| `MessageCenter.tsx:170 getMessageActions` | 113 | 506 | Extract to utility file |

---

## Phase 5: Verify & Monitor

### 5a. Run fallow config init

```bash
fallow init
```

Creates `.fallowrc.json` to persist configuration and enable suppression tracking.

### 5b. Run full analysis after each phase

```bash
fallow dead-code
fallow dupes
fallow health
```

### 5c. Add fallow to CI

```bash
fallow hooks --install pre-commit
```

### 5d. Set health score target

Target: **≥80** after Phases 1-3, **≥90** after Phase 4.

---

## Execution Order

Fallow deductions (current): circular deps -25.0 · hotspots -10.0 · unit size -10.0 · maintainability -7.5 · duplication -3.7 · coupling -2.5 · dead exports -2.4 · dead files -2.1 · complexity -0.8 = **64 total deductions** (score: 36).

| Phase | Effort | Impact | Dependencies |
|---|---|---|---|
| **1. Dead code deletion** | 1 hour | +5.5 pts (dead files +2.1, dead exports +2.4, partial duplication from tmux-viewer ~+1) | None |
| **2. Break circular deps** | 2 hours | +25 pts (circular deps -25.0 → 0) | Phase 1 |
| **3. Deduplication** | 3 hours | +2.5 pts (duplication 8.7% → ~2%) | Phase 2 |
| **4. Component decomposition** | 8 hours | +27 pts (hotspots -10→0, unit size -10→0, maintainability -7.5→-2, coupling -2.5→-1, complexity -0.8→0) | Phase 2 |
| **5. Verify & monitor** | 30 min | Ongoing | All phases |

**Total estimated effort:** ~14.5 hours  
**Expected health score after all phases:** ~96/100 (36 + 5.5 + 25 + 2.5 + 27)

---

## Quick Wins (do first, <30 min total)

1. `rm frontend/src/hooks/useSocket.ts` (6 LOC, zero importers)
2. `rm frontend/src/components/BudgetStatusCard.tsx` (92 LOC, zero importers)
3. `rm frontend/src/components/PanelSearch.tsx` (182 LOC, zero importers)
4. `rm frontend/src/components/AdvancedFilterBar.tsx` (260 LOC, zero importers)
5. `rm frontend/src/components/WorkflowSelector.tsx` (187 LOC, zero importers)
6. `rm frontend/src/components/autopilot/ProjectSelector.tsx` (259 LOC, zero importers)
7. `rm -rf tools/tmux-viewer/` (entire directory, ~1,000 LOC, standalone tool with zero references)
8. Extract `StatusBadge`, `StatusIcon`, `formatTime` from `Autopilot.tsx` → `autopilot/utils.tsx` (breaks 6 cycles)
9. Move `FeatureStatusBadge` from `DesignQueuePanel.tsx` → `autopilot/utils.tsx` (breaks 1 cycle)
10. `React.lazy()` for `TaskDetailModal` in `AgentDetailModal.tsx` (breaks 1 cycle)

**After quick wins:** health score should jump from 36 → ~61 (dead code +5.5, circular deps +25 = +30.5 total → score 66.5, rounded conservatively).

**Note on unused props:** `DesignCostRow.designId` and `ProjectCostSummary.projectId` are already prefixed with `_` in the destructuring. Verify if fallow still flags them after prefix — if so, remove from the interface entirely or suppress.

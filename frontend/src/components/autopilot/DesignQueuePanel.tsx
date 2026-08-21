import React, { useState, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core';
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  Plus, Trash2, FileText, Clock, GripVertical, Search, ListOrdered, RefreshCw,
  Loader2, Pause, Play, Upload, ChevronRight, ChevronDown, Layers,
  Square, RotateCcw, FileBarChart2, Eye
} from 'lucide-react';
import { apiService, api } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';
import DesignDetailModal from './DesignDetailModal';
import TaskDetailModal from '../TaskDetailModal';
import FeatureRecordDetailModal from './FeatureRecordDetailModal';
import RealTimeAgentOutput from '../RealTimeAgentOutput';
import { Agent } from '@/types';
import { CostDisplay, FeatureCostBadge } from '@/components/cost';
import { DESIGN_FEATURE_STATUS_CONFIG, TASK_STATUS_CONFIG } from './statusConfig';
import { useProject } from '@/context/ProjectContext';
import BaseStatusBadge from '../StatusBadge';

interface DesignQueuePanelProps {
  projectId: string | null;
  onAddDesign: () => void;
  onLoadDesign: () => void;
  currentDesign?: string | null;
  onReviewFeature?: (featureId: string, feature: any) => void;
  onRefreshStatus?: () => void;
}

const DesignQueuePanel: React.FC<DesignQueuePanelProps> = ({ projectId, onAddDesign, onLoadDesign, currentDesign, onReviewFeature, onRefreshStatus }) => {
  const queryClient = useQueryClient();
  // Whether the project LIST itself is still loading -- projectId is
  // null both while that's in flight (too early to say "no project
  // selected") and once it's resolved with genuinely no project chosen.
  // ProjectContext.loading distinguishes the two.
  const { loading: projectsLoading } = useProject();
  const [search, setSearch] = useState('');
  const [localOrder, setLocalOrder] = useState<any[] | null>(null);
  const [detailFile, setDetailFile] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedFeature, setSelectedFeature] = useState<any | null>(null);

  // Fetch status for all designs to show badges (M-5 fix: migrated to React Query)
  const { data: designs, isLoading } = useQuery({
    queryKey: ['autopilot-project-designs', projectId],
    queryFn: () => projectId ? apiService.getAutopilotProjectDesigns(projectId) : Promise.resolve([]),
    enabled: !!projectId,
    refetchInterval: 5000,
  });

  // Fetch project status for review_mode flag
  const { data: projectStatus } = useQuery({
    queryKey: ['autopilot-status', projectId],
    queryFn: () => projectId ? apiService.getAutopilotStatus(projectId) : Promise.resolve(null),
    enabled: !!projectId,
  });
  const reviewMode = projectStatus?.review_mode ?? false;

  // Fetch design statuses using React Query (M-5 fix)
  const { data: designStatuses = {}, refetch: refetchDesignStatuses } = useQuery({
    queryKey: ['autopilot-design-statuses', projectId, designs?.length],
    queryFn: async () => {
      if (!projectId || !designs || designs.length === 0) return {};
      const statuses: Record<string, { status: string; workflowId?: string; error?: string | null; costTotal: number; costUnavailable?: boolean; pausedBy?: string | null; statusReason?: string | null; workflowType?: string; features: any[] }> = {};
      await Promise.all(
        designs.map(async (d: any) => {
          try {
            const status = await apiService.getAutopilotProjectDesignStatus(projectId, d.filename);
            statuses[d.filename] = {
              status: status.status || 'pending',
              workflowId: status.workflows?.[0]?.id,
              error: status.error || null,
              costTotal: status.cost_total_usd ?? 0,
              pausedBy: status.paused_by || null,
              statusReason: status.status_reason || null,
              workflowType: status.workflow_type || 'feature',
              // SOLID review 5.2: this endpoint was already being called
              // here every 10s for every design, but this field was
              // discarded -- SortableDesignItem then ran its OWN
              // per-expanded-row setInterval calling the identical
              // endpoint again just to get this. Capturing it here
              // eliminates that entire duplicate polling path.
              features: status.features || [],
            };
          } catch (err) {
            console.error(`Failed to fetch status for design ${d.filename}:`, err);
            statuses[d.filename] = { status: 'pending', costTotal: 0, costUnavailable: true, features: [] };
          }
        })
      );
      return statuses;
    },
    enabled: !!projectId && !!designs && designs.length > 0,
    refetchInterval: 10000,
    staleTime: 0,
    refetchOnWindowFocus: true,
  });

  // Periodically reload designs from disk every 30 seconds
  useEffect(() => {
    if (!projectId) return;
    // Guards against a reload for the PREVIOUS project resolving after
    // projectId has already changed -- clearInterval below stops future
    // ticks but doesn't cancel one already in flight, and localOrder isn't
    // namespaced per-project, so a stale response would overwrite the
    // just-reset localOrder (see the projectId-change effect below) with
    // the old project's designs.
    let cancelled = false;
    const interval = setInterval(async () => {
      try {
        const data = await apiService.reloadAutopilotProjectDesigns(projectId);
        if (cancelled) return;
        setLocalOrder(data);
        queryClient.setQueryData(['autopilot-project-designs', projectId], data);
      } catch {
        // Silently ignore reload failures during periodic refresh
      }
    }, 30000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [projectId, queryClient]);

  const items = localOrder ?? designs ?? [];

  useEffect(() => {
    if (designs) setLocalOrder(designs);
  }, [designs]);

  useEffect(() => {
    setLocalOrder(null);
  }, [projectId]);

  const reloadMutation = useMutation({
    mutationFn: () => {
      if (!projectId) throw new Error('No project selected');
      return apiService.reloadAutopilotProjectDesigns(projectId);
    },
    onSuccess: (data) => {
      setLocalOrder(data);
      queryClient.setQueryData(['autopilot-project-designs', projectId], data);
      onRefreshStatus?.();
      toast.success('Designs reloaded from disk');
    },
    onError: () => toast.error('Failed to reload designs'),
  });

  const removeMutation = useMutation({
    mutationFn: (filename: string) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.removeAutopilotProjectDesign(projectId, filename);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast.success('Design removed');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to remove design');
    },
  });

  const reorderMutation = useMutation({
    mutationFn: (designIds: string[]) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.reorderAutopilotProjectDesigns(projectId, designIds);
    },
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      toast.error('Failed to save order');
    },
  });

  // Handles Pause/Stop/Resume for a design row by applying the matching
  // workflow-execution endpoint to every one of the design's workflows in
  // the applicable status (a design can have more than one workflow run).
  const workflowActionMutation = useMutation({
    mutationFn: async ({ filename, action }: { filename: string; action: 'pause' | 'stop' | 'resume' }) => {
      const status = await apiService.getAutopilotProjectDesignStatus(projectId!, filename);
      const workflows = status.workflows || [];

      const results = [];
      for (const wf of workflows) {
        if (action === 'pause' && wf.status === 'active') {
          results.push(await apiService.pauseWorkflow(wf.id));
        } else if (action === 'stop' && ['active', 'paused'].includes(wf.status)) {
          results.push(await apiService.cancelWorkflow(wf.id));
        } else if (action === 'resume' && ['paused', 'failed'].includes(wf.status)) {
          results.push(await apiService.recoverWorkflow(wf.id));
        }
      }
      return results;
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-design-statuses', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      toast.success('Workflow updated');

      // Pausing/stopping this one design doesn't touch the continuously-running
      // autopilot pipeline for the project -- it can pick this design right back
      // up on its next queue scan. Offer to stop the whole pipeline too, since
      // that's usually what "pause/stop this design" actually meant.
      if ((variables.action === 'pause' || variables.action === 'stop') && projectId) {
        if (confirm(
          `This ${variables.action === 'pause' ? 'paused' : 'stopped'} just this one design. ` +
          `The autopilot pipeline is still running and may automatically pick it back up. ` +
          `Stop the whole pipeline too? Note: there's currently only one shared pipeline ` +
          `process for the whole backend, so this stops autopilot for every project, not just this one.`
        )) {
          apiService.stopAutopilot(projectId)
            .then(() => toast.success('Autopilot pipeline stopped'))
            .catch(() => toast.error('Failed to stop the autopilot pipeline'));
        }
      }
    },
    onError: () => {
      toast.error('Failed to update workflow');
    },
  });

  // Rerun: restarts the design's pipeline from scratch (Phase 0 onward).
  const rerunDesignMutation = useMutation({
    mutationFn: async (filename: string) => {
      const projects = await apiService.getProjects();
      const project = projects.find((p: any) => p.id === projectId);
      if (!project) throw new Error('Project not found');
      return api.post('/autopilot/queue/rerun', {
        filename,
        project_path: project.base_dir,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-design-statuses', projectId] });
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      toast.success('Pipeline restarted for this design');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || error?.message || 'Failed to rerun');
    },
  });

  const handleDetail = (filename: string) => {
    setDetailFile(filename);
  };

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    setLocalOrder((prev) => {
      if (!prev) return prev;
      const oldIndex = prev.findIndex((i) => i.id === active.id);
      const newIndex = prev.findIndex((i) => i.id === over.id);
      const reordered = arrayMove(prev, oldIndex, newIndex);
      reorderMutation.mutate(reordered.map((i) => i.id));
      return reordered;
    });
  };

  const filteredQueue = items.filter((item: any) =>
    !search || item.name.toLowerCase().includes(search.toLowerCase()) ||
    item.filename.toLowerCase().includes(search.toLowerCase())
  );

  if (!projectId && projectsLoading) {
    return (
      <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
        <Loader2 className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4 animate-spin" />
        <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">Loading project...</h3>
      </div>
    );
  }

  if (!projectId) {
    return (
      <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
        <ListOrdered className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">No project selected</h3>
        <p className="text-sm text-gray-400 dark:text-gray-500">Select or create a project to view its design queue</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search designs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 border border-gray-200 dark:border-gray-600 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500"
          />
        </div>
        <Button 
          onClick={() => reloadMutation.mutate()}
          disabled={reloadMutation.isPending}
          variant="outline"
          className="text-gray-600 dark:text-gray-400"
        >
          <RefreshCw className={`w-4 h-4 mr-1 ${reloadMutation.isPending ? 'animate-spin' : ''}`} />
          Reload
        </Button>
        <Button onClick={onLoadDesign} className="bg-violet-600 hover:bg-violet-700 text-white">
          <Upload className="w-4 h-4 mr-1" />
          Load Design
        </Button>
        <Button onClick={onAddDesign} variant="outline" className="text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/30">
          <Plus className="w-4 h-4 mr-1" />
          Add Design
        </Button>
      </div>
      <p className="text-xs text-gray-400">
        Sorted by filename by default. Drag to reorder manually. Or add to
        <code>docs/design-queue</code> which finds them automatically.
      </p>

      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
        </div>
      ) : filteredQueue.length > 0 ? (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext
            items={filteredQueue.map((i: any) => i.id)}
            strategy={verticalListSortingStrategy}
            disabled={!!search}
          >
            <div className="space-y-2">
              {filteredQueue.map((item: any, index: number) => (
                <SortableDesignItem
                  key={item.id}
                  item={item}
                  index={index}
                  isActive={item.name === currentDesign}
                  status={designStatuses[item.filename]?.status}
                  workflowId={designStatuses[item.filename]?.workflowId}
                  error={designStatuses[item.filename]?.error}
                  costTotal={designStatuses[item.filename]?.costTotal ?? 0}
                  costUnavailable={designStatuses[item.filename]?.costUnavailable ?? false}
                  pausedBy={designStatuses[item.filename]?.pausedBy}
                  workflowType={designStatuses[item.filename]?.workflowType}
                  features={designStatuses[item.filename]?.features ?? []}
                  onRefetchFeatures={refetchDesignStatuses}
                  statusReason={designStatuses[item.filename]?.statusReason}
                  projectId={projectId}
                  onDetail={handleDetail}
                  onTaskClick={setSelectedTaskId}
                  onSelectFeature={setSelectedFeature}
                  onReviewFeature={onReviewFeature}
                  onAction={(action) => {
                    if (action === 'rerun') {
                      // /autopilot/queue/rerun stops the orchestrator and
                      // terminates every active agent/workflow system-wide,
                      // not just this design's -- confirm before firing since
                      // this icon is one click away, unlike the modal's button.
                      if (confirm(`Rerun "${item.name}"? This restarts its pipeline from scratch, deletes its existing worktree (any uncommitted work in it is lost), and will also pause every other currently running pipeline.`)) {
                        rerunDesignMutation.mutate(item.filename);
                      }
                    } else {
                      workflowActionMutation.mutate({ filename: item.filename, action });
                    }
                  }}
                  actionPending={{
                    pause: workflowActionMutation.isPending && workflowActionMutation.variables?.filename === item.filename && workflowActionMutation.variables?.action === 'pause',
                    stop: workflowActionMutation.isPending && workflowActionMutation.variables?.filename === item.filename && workflowActionMutation.variables?.action === 'stop',
                    resume: workflowActionMutation.isPending && workflowActionMutation.variables?.filename === item.filename && workflowActionMutation.variables?.action === 'resume',
                    rerun: rerunDesignMutation.isPending && rerunDesignMutation.variables === item.filename,
                  }}
                  onRemove={(filename) => {
                    if (confirm(`Remove "${item.name}" from queue?`)) {
                      removeMutation.mutate(filename);
                    }
                  }}
                  reviewMode={reviewMode}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      ) : (
        <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
          <ListOrdered className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">Queue is empty</h3>
          <p className="text-sm text-gray-400 dark:text-gray-500 mb-4">
            Drop design documents into the queue to start processing
          </p>
          <Button onClick={onLoadDesign} className="bg-violet-600 hover:bg-violet-700 text-white">
            <Upload className="w-4 h-4 mr-1" />
            Load Design
          </Button>
          <Button onClick={onAddDesign} variant="outline" className="text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/30">
            <Plus className="w-4 h-4 mr-1" />
            Add Design
          </Button>
        </div>
      )}

      {/* Design Detail Modal */}
      {detailFile && projectId && (
        <DesignDetailModal
          projectId={projectId}
          filename={detailFile}
          onClose={() => setDetailFile(null)}
          onRerun={() => {
            queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
            queryClient.invalidateQueries({ queryKey: ['projects'] });
            queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
            setDetailFile(null);
          }}
        />
      )}

      {/* Task Detail Modal */}
      <TaskDetailModal
        taskId={selectedTaskId}
        onClose={() => setSelectedTaskId(null)}
      />

      {/* Feature Record Detail Modal */}
      <FeatureRecordDetailModal
        feature={selectedFeature}
        onClose={() => setSelectedFeature(null)}
      />
    </div>
  );
};

// ── Status Badge ───────────────────────────────────────────────

const StatusBadge: React.FC<{ status: string; pausedBy?: string | null }> = ({ status, pausedBy }) => {
  const config = DESIGN_FEATURE_STATUS_CONFIG[status];
  if (!config) return null;
  const label = status === 'paused' && pausedBy === 'budget' ? 'Paused: budget limit reached' : config.label;
  return (
    <BaseStatusBadge status={status} size="sm" icon={config.icon} label={label} colorClassName={config.color} />
  );
};

// ── Task Status Icon ─────────────────────────────────────────

const TaskStatusIcon: React.FC<{ status: string }> = ({ status }) => {
  const config = TASK_STATUS_CONFIG[status];
  if (!config) return <Clock className="w-4 h-4 text-gray-400" />;
  return <span className={config.color}>{config.icon}</span>;
};

// ── Shared row action icons (Pause / Stop / Resume / Rerun) ────
// Mirrors the four actions in DesignDetailModal's footer, exposed inline on
// design/feature/task rows so they don't require opening the modal.

interface RowActionIconsProps {
  canPause?: boolean;
  canStop?: boolean;
  canResume?: boolean;
  canRerun?: boolean;
  canDelete?: boolean;
  onPause?: () => void;
  onStop?: () => void;
  onResume?: () => void;
  onRerun?: () => void;
  onDelete?: () => void;
  pending?: { pause?: boolean; stop?: boolean; resume?: boolean; rerun?: boolean; delete?: boolean };
  size?: 'sm' | 'md';
}

const RowActionIcons: React.FC<RowActionIconsProps> = ({
  canPause, canStop, canResume, canRerun, canDelete, onPause, onStop, onResume, onRerun, onDelete, pending = {}, size = 'md',
}) => {
  const iconCls = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4';
  const btnCls = size === 'sm' ? 'p-1 rounded' : 'p-2 rounded-lg';

  const items: Array<{
    key: string;
    enabled?: boolean;
    isPending?: boolean;
    onClick?: () => void;
    icon: React.ReactNode;
    hoverColor: string;
    title: string;
  }> = [
    { key: 'pause', enabled: canPause, isPending: pending.pause, onClick: onPause, icon: <Pause className={iconCls} />, hoverColor: 'hover:bg-yellow-50 dark:hover:bg-yellow-900/20 hover:text-yellow-600 dark:hover:text-yellow-400', title: 'Pause' },
    { key: 'stop', enabled: canStop, isPending: pending.stop, onClick: onStop, icon: <Square className={iconCls} />, hoverColor: 'hover:bg-red-50 dark:hover:bg-red-900/20 hover:text-red-600 dark:hover:text-red-400', title: 'Stop' },
    { key: 'resume', enabled: canResume, isPending: pending.resume, onClick: onResume, icon: <Play className={iconCls} />, hoverColor: 'hover:bg-green-50 dark:hover:bg-green-900/20 hover:text-green-600 dark:hover:text-green-400', title: 'Resume' },
    { key: 'rerun', enabled: canRerun, isPending: pending.rerun, onClick: onRerun, icon: <RotateCcw className={iconCls} />, hoverColor: 'hover:bg-violet-50 dark:hover:bg-violet-900/20 hover:text-violet-600 dark:hover:text-violet-400', title: 'Rerun' },
    { key: 'delete', enabled: canDelete, isPending: pending.delete, onClick: onDelete, icon: <Trash2 className={iconCls} />, hoverColor: 'hover:bg-red-50 dark:hover:bg-red-900/20 hover:text-red-600 dark:hover:text-red-400', title: 'Delete task' },
  ];

  return (
    <>
      {items.map((item) => (
        <button
          key={item.key}
          disabled={!item.enabled || item.isPending}
          onClick={(e) => { e.stopPropagation(); item.onClick?.(); }}
          className={`${btnCls} transition-colors text-gray-300 dark:text-gray-600 ${
            item.enabled ? `${item.hoverColor} text-gray-400 dark:text-gray-500` : 'opacity-30 cursor-not-allowed'
          }`}
          title={item.title}
        >
          {item.isPending ? <Loader2 className={`${iconCls} animate-spin`} /> : item.icon}
        </button>
      ))}
    </>
  );
};

// ── Sortable Item ───────────────────────────────────────────────

interface SortableDesignItemProps {
  item: any;
  index: number;
  isActive?: boolean;
  onRemove: (filename: string) => void;
  onDetail: (filename: string) => void;
  onTaskClick: (taskId: string) => void;
  onSelectFeature: (feature: any) => void;
  onReviewFeature?: (featureId: string, feature: any) => void;
  onAction?: (action: 'pause' | 'stop' | 'resume' | 'rerun') => void;
  actionPending?: { pause?: boolean; stop?: boolean; resume?: boolean; rerun?: boolean };
  status?: string;
  workflowId?: string;
  error?: string | null;
  costTotal?: number;
  costUnavailable?: boolean;
  pausedBy?: string | null;
  statusReason?: string | null;
  workflowType?: string;
  projectId: string | null;
  reviewMode?: boolean;
  // SOLID review 5.2: features now come from the parent's already-polled
  // designStatuses query (which called this same endpoint every 10s
  // regardless) instead of this component running its own duplicate
  // per-row setInterval against the identical endpoint.
  features: any[];
  onRefetchFeatures?: () => void;
}

const SortableDesignItem: React.FC<SortableDesignItemProps> = ({ item, index, isActive, onRemove, onDetail, onTaskClick, onSelectFeature, onReviewFeature, onAction, actionPending, status, error, costTotal, costUnavailable, pausedBy, workflowType, projectId, reviewMode, features, onRefetchFeatures }) => {
  const [expanded, setExpanded] = useState(() => {
    // Restore expanded state from localStorage
    const saved = localStorage.getItem('autopilot-expanded-designs');
    if (saved) {
      try {
        const expandedSet = new Set(JSON.parse(saved));
        const isExpanded = expandedSet.has(item.filename);
        console.log('[DesignQueuePanel] Restoring expanded state:', { filename: item.filename, isExpanded, savedItems: [...expandedSet] });
        return isExpanded;
      } catch { return false; }
    }
    return false;
  });

  // Calculate elapsed time from features' tasks
  const designElapsedSeconds = features.reduce((acc: number, f: any) => {
    const tasks = f.tasks || [];
    return acc + tasks.reduce((taskAcc: number, t: any) => {
      if (t.created_at) {
        const start = new Date(t.created_at).getTime();
        const end = t.completed_at ? new Date(t.completed_at).getTime() : Date.now();
        return taskAcc + Math.max(0, (end - start) / 1000);
      }
      return taskAcc;
    }, 0);
  }, 0);

  const handleToggleExpand = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const newExpanded = !expanded;
    setExpanded(newExpanded);
    
    // Persist expanded state to localStorage
    const saved = localStorage.getItem('autopilot-expanded-designs');
    let expandedSet: Set<string>;
    try {
      expandedSet = new Set(saved ? JSON.parse(saved) : []);
    } catch {
      expandedSet = new Set();
    }
    if (newExpanded) {
      expandedSet.add(item.filename);
    } else {
      expandedSet.delete(item.filename);
    }
    localStorage.setItem('autopilot-expanded-designs', JSON.stringify([...expandedSet]));
    console.log('[DesignQueuePanel] Saved expanded state:', { filename: item.filename, newExpanded, items: [...expandedSet] });
  };

  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: item.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 50 : undefined,
    opacity: isDragging ? 0.8 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} {...attributes}>
      <motion.div
        initial={{ opacity: 0, x: -20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ delay: index * 0.03 }}
        className={`rounded-xl border shadow-sm transition-all ${
          isDragging ? 'shadow-lg border-violet-300 dark:border-violet-500 ring-2 ring-violet-200 dark:ring-violet-500' :
          isActive ? 'bg-gradient-to-r from-violet-50 to-purple-50 dark:from-violet-900/30 dark:to-purple-900/30 border-violet-300 dark:border-violet-500 shadow-md ring-1 ring-violet-200 dark:ring-violet-500' :
          'bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:shadow-md'
        }`}
      >
        <div 
          className="flex items-center gap-4 px-5 py-4 cursor-pointer"
          onClick={handleToggleExpand}
        >
          {/* Expand arrow */}
          <div className="p-1 text-gray-400 dark:text-gray-500">
            {expanded ? (
              <ChevronDown className="w-5 h-5" />
            ) : (
              <ChevronRight className="w-5 h-5" />
            )}
          </div>

          {/* Drag handle */}
          <button
            {...listeners}
            className="flex flex-col items-center gap-1 text-gray-300 dark:text-gray-600 hover:text-gray-500 dark:hover:text-gray-400 cursor-grab active:cursor-grabbing touch-none"
            onClick={(e) => e.stopPropagation()}
          >
            <GripVertical className="w-5 h-5" />
            <span className="text-xs font-mono text-gray-400 dark:text-gray-500">#{item.ordinal ?? index + 1}</span>
          </button>

          <div className={`p-2.5 rounded-lg ${isActive ? 'bg-violet-200 dark:bg-violet-800' : 'bg-violet-50 dark:bg-violet-900/30'}`}>
            <FileText className={`w-5 h-5 ${isActive ? 'text-violet-700 dark:text-violet-300' : 'text-violet-600 dark:text-violet-400'}`} />
          </div>

          <div className="flex-1 min-w-0">
            <h4
              className="text-sm font-semibold text-gray-800 dark:text-gray-200 truncate hover:text-violet-600 hover:underline w-fit"
              onClick={(e) => { e.stopPropagation(); onDetail(item.filename); }}
            >
              {item.name}
            </h4>
            <div className="flex items-center gap-3 mt-1">
              <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{item.filename}</span>
              <span className="text-xs text-gray-400 dark:text-gray-500">{formatBytes(item.size_bytes)}</span>
              {item.modified_at && (
                <span className="text-xs text-gray-400 dark:text-gray-500 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {formatDistanceToNow(new Date(item.modified_at), { addSuffix: true })}
                </span>
              )}
            </div>
            {status === 'failed' && error && (
              <p className="text-xs text-red-600 mt-1 truncate" title={error}>
                {error}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            {costUnavailable ? (
              <span className="text-xs text-gray-400 dark:text-gray-500" title="Cost unavailable — status fetch failed">—</span>
            ) : (
              costTotal !== undefined && costTotal > 0 && (
                <CostDisplay currentCost={costTotal} showProgress={false} className="text-xs" />
              )
            )}
            {designElapsedSeconds > 0 && (
              <span className="text-xs text-gray-400 dark:text-gray-500">
                {formatElapsed(designElapsedSeconds)}
              </span>
            )}
            {workflowType && (
              <span
                className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  workflowType === 'bugfix'
                    ? 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                    : 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
                }`}
              >
                {workflowType === 'bugfix' ? 'Bug Fix' : 'Feature'}
              </span>
            )}
            {status && status !== 'pending' && (
              <StatusBadge status={status} pausedBy={pausedBy} />
            )}
            <RowActionIcons
              canPause={status === 'active'}
              canStop={status === 'active' || status === 'paused'}
              canResume={status === 'paused' || status === 'failed'}
              canRerun={status === 'completed' || status === 'failed'}
              onPause={() => onAction?.('pause')}
              onStop={() => onAction?.('stop')}
              onResume={() => onAction?.('resume')}
              onRerun={() => onAction?.('rerun')}
              pending={actionPending}
            />
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDetail(item.filename);
              }}
              className="p-2 rounded-lg hover:bg-violet-50 dark:hover:bg-violet-900/20 transition-colors text-gray-400 dark:text-gray-500 hover:text-violet-600 dark:hover:text-violet-400"
              title="View design details"
            >
              <FileText className="w-4 h-4" />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onRemove(item.filename); }}
              className="p-2 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400"
              title="Remove"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </div>
      </motion.div>

        {/* Expanded features section */}
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="border-t border-gray-100 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-b-xl"
          >
            <div className="px-5 py-3">
              <h5 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Features</h5>
              {features.length > 0 ? (
                <div className="space-y-2">
                  {features.map((feature) => (
                    <FeatureRow
                      key={feature.id}
                      feature={feature}
                      onTaskClick={onTaskClick}
                      onSelectFeature={onSelectFeature}
                      onReviewFeature={onReviewFeature}
                      projectId={projectId ?? undefined}
                      onFeatureUpdate={() => onRefetchFeatures?.()}
                      reviewMode={reviewMode}
                    />
                  ))}
                </div>
              ) : (
                <p className="text-xs text-gray-400 dark:text-gray-500 text-center py-4">No features yet</p>
              )}
            </div>
          </motion.div>
        )}
    </div>
  );
};

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

// ── Feature Row (expandable, shows tasks under it) ──────────────

export const FeatureStatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const config = DESIGN_FEATURE_STATUS_CONFIG[status];
  if (!config) return null;
  return (
    <BaseStatusBadge status={status} size="sm" icon={config.icon} label={config.label} colorClassName={config.color} />
  );
};

const formatElapsed = (seconds: number): string => {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
};

const FeatureRow: React.FC<{
  feature: any;
  onTaskClick: (taskId: string) => void;
  onSelectFeature: (feature: any) => void;
  onReviewFeature?: (featureId: string, feature: any) => void;
  projectId?: string;
  onFeatureUpdate?: () => void;
  reviewMode?: boolean;
}> = ({ feature, onTaskClick, onSelectFeature, onReviewFeature, onFeatureUpdate, reviewMode }) => {
  const [expanded, setExpanded] = useState(() => {
    // Restore expanded state from localStorage
    const saved = localStorage.getItem('autopilot-expanded-features');
    if (saved) {
      try {
        const expandedSet = new Set(JSON.parse(saved));
        return expandedSet.has(feature.id);
      } catch { return false; }
    }
    return false;
  });
  const [actionPending, setActionPending] = useState<{ pause?: boolean; stop?: boolean; resume?: boolean; rerun?: boolean; delete?: boolean }>({});
  const tasks = feature.tasks || [];
  const doneCount = tasks.filter((t: any) => t.status === 'done').length;
  const activeCount = tasks.filter((t: any) => ['in_progress', 'assigned'].includes(t.status)).length;

  // Calculate elapsed time from tasks
  const elapsedSeconds = tasks.reduce((acc: number, t: any) => {
    if (t.created_at) {
      const start = new Date(t.created_at).getTime();
      const end = t.completed_at ? new Date(t.completed_at).getTime() : Date.now();
      return acc + Math.max(0, (end - start) / 1000);
    }
    return acc;
  }, 0);

  // The Feature Architect (Phase 0) and placeholder entries aren't real
  // `Feature` DB rows (see the synthetic ids built in autopilot_api.py), so
  // pauseFeature/resumeFeature (which look up Feature by id) don't apply to
  // them -- fall back to the workflow-level endpoints those synthetic
  // entries' own `workflow_id` supports instead.
  const isRealFeature = !feature.id.startsWith('phase0-') && !feature.id.startsWith('placeholder-');
  const hasWorkflow = !!feature.workflow_id;
  const reviewPending = !!feature.review_pending;
  // reviewPending alone only means git_expert's dispatch was
  // rejected pending approval -- it says nothing about whether OTHER
  // tasks in this same feature/workflow are still active. paused_by
  // "review" is workflow-wide (see phase_transitions.py's
  // _pause_for_manual_handoff), so a retried task in an unrelated phase
  // (e.g. adversarial_review) can still have a live agent running while
  // git_expert sits paused waiting on a human. Surfacing "Review"
  // in that window invites approving a push before all the work it
  // should include even exists yet -- only show it once every OTHER
  // task has resolved (done, failed, or superseded) and git_expert
  // is the sole thing left. 'duplicated' counts as resolved (a
  // superseded copy of another task, not real outstanding work) --
  // observed live on feat-bd683e83: 4 duplicated architecture_design/
  // architectural_review tasks that will never become 'done', which
  // would otherwise permanently block this check. Matches the Task.status
  // CHECK constraint in src/core/database.py; anything not in this set
  // (pending, queued, blocked, assigned, in_progress, under_review,
  // validation_in_progress, needs_work) still means real work remains.
  const TERMINAL_TASK_STATUSES = ['done', 'failed', 'duplicated'];
  const readyForGitPushReview = tasks.length > 0 && tasks
    .filter((t: any) => t.phase_name !== 'git_expert')
    .every((t: any) => TERMINAL_TASK_STATUSES.includes(t.status));
  // In review mode, flag the feature currently in flight too, not just
  // one already paused awaiting approval -- gives an at-a-glance "this is
  // the one that'll need your review soon" cue while it's still running.
  // Does NOT gate the "Review" badge/button below -- those stay tied to
  // reviewPending && readyForGitPushReview, since there's nothing to
  // approve yet (or, while other agents are still working, nothing final
  // to approve).
  const highlightForReview = reviewPending || (!!reviewMode && feature.status === 'active');

  const runFeatureAction = async (action: 'pause' | 'stop' | 'resume' | 'rerun' | 'delete') => {
    if (action === 'delete' && !confirm(
      `Permanently delete "${feature.name}" (${feature.feature_key})? This removes its workflow, all its tasks, and its worktree -- any uncommitted work in it is lost. This cannot be undone.`
    )) {
      return;
    }
    setActionPending((p) => ({ ...p, [action]: true }));
    try {
      if (action === 'pause') {
        if (isRealFeature) await apiService.pauseFeature(feature.id);
        else await apiService.pauseWorkflow(feature.workflow_id);
      } else if (action === 'resume') {
        if (isRealFeature) await apiService.resumeFeature(feature.id);
        else await apiService.recoverWorkflow(feature.workflow_id);
      } else if (action === 'stop') {
        await apiService.cancelWorkflow(feature.workflow_id);
      } else if (action === 'rerun') {
        // No true "restart this feature from scratch" endpoint exists yet;
        // recover non-destructively continues from the last committed phase.
        await apiService.recoverWorkflow(feature.workflow_id);
      } else if (action === 'delete') {
        await apiService.deleteFeature(feature.id);
      }
      onFeatureUpdate?.();
    } catch (err: any) {
      console.error(`Feature ${action} failed:`, err);
      if (action === 'delete') {
        toast.error(err?.response?.data?.detail || 'Failed to delete feature');
      }
    } finally {
      setActionPending((p) => ({ ...p, [action]: false }));
    }
  };

  return (
    <div className={`rounded-lg border overflow-hidden transition-colors ${
      highlightForReview
        ? 'bg-amber-50 dark:bg-amber-900/20 border-l-4 border-amber-400 border-t-amber-200 dark:border-t-amber-800 border-b-amber-200 dark:border-b-amber-800 border-r-amber-200 dark:border-r-amber-800'
        : 'bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700'
    }`}>
      {/* Pulse reserved for reviewPending specifically -- "needs your
          review right now" is more urgent than "will need it once this
          finishes", which gets the steady amber highlight only. */}
      <div className={`flex items-center gap-3 px-3 py-2 ${reviewPending ? 'animate-pulse-subtle' : ''}`}>
        <div
          className="flex items-center gap-3 flex-1 min-w-0 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors rounded"
          onClick={() => {
            const newExpanded = !expanded;
            setExpanded(newExpanded);
            // Persist expanded state to localStorage
            const saved = localStorage.getItem('autopilot-expanded-features');
            let expandedSet: Set<string>;
            try {
              expandedSet = new Set(saved ? JSON.parse(saved) : []);
            } catch {
              expandedSet = new Set();
            }
            if (newExpanded) {
              expandedSet.add(feature.id);
            } else {
              expandedSet.delete(feature.id);
            }
            localStorage.setItem('autopilot-expanded-features', JSON.stringify([...expandedSet]));
          }}
        >
          <div className="p-1 text-gray-400 dark:text-gray-500">
            {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
          <div className="p-1.5 rounded bg-violet-50 dark:bg-violet-900/30">
            <Layers className="w-3.5 h-3.5 text-violet-600 dark:text-violet-400" />
          </div>
          <div className="flex-1 min-w-0">
            <p
              className="text-sm font-medium text-gray-700 dark:text-gray-300 truncate hover:text-violet-600 hover:underline w-fit"
              onClick={(e) => { e.stopPropagation(); onSelectFeature(feature); }}
            >
              {feature.name}
            </p>
            <p className="text-xs text-gray-400 dark:text-gray-500 truncate">{feature.feature_key}</p>
            {feature.depends_on?.length > 0 && (
              <p className="text-xs font-mono text-gray-400 dark:text-gray-500 truncate">
                depends on: {feature.depends_on.join(', ')}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400 dark:text-gray-500">
            {doneCount}/{tasks.length} tasks
          </span>
          {elapsedSeconds > 0 && (
            <span className="text-xs text-gray-400 dark:text-gray-500">
              {formatElapsed(elapsedSeconds)}
            </span>
          )}
          {activeCount > 0 && (
            <span className="text-xs text-blue-500">
              {activeCount} active
            </span>
          )}
          <FeatureCostBadge cost={feature.cost_total_usd ?? 0} />
          <FeatureStatusBadge status={feature.status} />
          {reviewPending && readyForGitPushReview && (feature.status === 'completed' || feature.status === 'paused') && (
            <span className="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 border border-amber-300 dark:border-amber-700 animate-pulse">
              Review
            </span>
          )}
          <RowActionIcons
            size="sm"
            canPause={hasWorkflow && feature.status === 'active'}
            canStop={hasWorkflow && (feature.status === 'active' || feature.status === 'paused')}
            canResume={hasWorkflow && (feature.status === 'paused' || feature.status === 'failed')}
            canRerun={hasWorkflow}
            canDelete={isRealFeature}
            onPause={() => runFeatureAction('pause')}
            onStop={() => runFeatureAction('stop')}
            onResume={() => runFeatureAction('resume')}
            onRerun={() => runFeatureAction('rerun')}
            onDelete={() => runFeatureAction('delete')}
            pending={actionPending}
          />
          <button
            onClick={(e) => { e.stopPropagation(); onSelectFeature(feature); }}
            className="p-1.5 rounded-lg hover:bg-violet-50 dark:hover:bg-violet-900/20 transition-colors text-gray-400 dark:text-gray-500 hover:text-violet-600 dark:hover:text-violet-400"
            title="View feature details"
          >
            <FileText className="w-3.5 h-3.5" />
          </button>
          {feature.has_report && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                window.open(`/api/autopilot/workflows/${feature.workflow_id}/feature_report`, '_blank');
              }}
              className="p-1.5 rounded-lg bg-gradient-to-br from-emerald-400 to-teal-500 hover:from-emerald-500 hover:to-teal-600 transition-colors text-white shadow-sm"
              title="View feature report"
            >
              <FileBarChart2 className="w-3.5 h-3.5" />
            </button>
          )}
          {reviewPending && readyForGitPushReview && (feature.status === 'completed' || feature.status === 'paused') && onReviewFeature && (
            // Phase 0 (Feature Architect) isn't a real Feature row, but
            // review_feature's approve/request-changes endpoint now branches
            // on the "phase0-" id prefix to support it directly -- same
            // review modal, same reject-and-redo flow as a real feature.
            <button
              onClick={(e) => { e.stopPropagation(); onReviewFeature(feature.id, feature); }}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-600 text-white text-xs font-semibold shadow-sm transition-colors"
              title="Review this feature"
            >
              <Eye className="w-3.5 h-3.5" />
              Review
            </button>
          )}
        </div>
      </div>

      {expanded && tasks.length > 0 && (
        <div className="border-t border-gray-100 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2">
          <div className="space-y-1">
            {tasks.map((task: any) => (
              <TaskRow key={task.id} task={task} onTaskClick={onTaskClick} onTaskUpdate={onFeatureUpdate} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

// ── Task Row (Pause/Stop/Resume/Rerun mapped onto the task's agent lifecycle) ──

const TaskRow: React.FC<{
  task: any;
  onTaskClick: (taskId: string) => void;
  onTaskUpdate?: () => void;
}> = ({ task, onTaskClick, onTaskUpdate }) => {
  const [actionPending, setActionPending] = useState<{ pause?: boolean; stop?: boolean; resume?: boolean; rerun?: boolean; delete?: boolean }>({});
  const [tmuxAgent, setTmuxAgent] = useState<Agent | null>(null);

  const tmuxViewerOpenRef = useRef(false);

  const openTmuxView = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!task.agent_id) return;
    const agent = await apiService.getAgent(task.agent_id);
    if (agent) {
      tmuxViewerOpenRef.current = true;
      setTmuxAgent(agent);
    }
  };

  // Keep tmuxAgent's status fresh while the viewer is open -- it's only
  // fetched once, on open, otherwise. Left stale, RealTimeAgentOutput
  // keeps showing whatever status the agent had at that moment (e.g. its
  // "Live" indicator staying on) even after the agent actually finished
  // or was terminated -- the same bug Agents.tsx fixed for its own
  // selectedAgent state.
  useEffect(() => {
    if (!tmuxAgent) return;
    const interval = setInterval(async () => {
      try {
        const agent = await apiService.getAgent(tmuxAgent.id);
        // Guard: if the viewer was closed while the fetch was in-flight,
        // don't reopen it. Without this, a close→fetch→resolve race
        // sets tmuxAgent back to a value, re-rendering the viewer in a
        // disconnected state.
        if (agent && tmuxViewerOpenRef.current) setTmuxAgent(agent);
      } catch {
        // Keep showing the last known state on a transient fetch error.
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [tmuxAgent?.id]);

  // Once the task is finished, its own outcome is more useful than why it
  // was dispatched -- show completion_notes/failure_reason instead of
  // leaving the input-side goto_reason/phase_description up after the
  // work is actually done. While still running, show the goto reason (why
  // this task exists -- a gate sent it back with a specific finding to
  // fix) when there is one, else the phase's own config-sourced
  // description (Phase.description, from that phase's YAML) instead of
  // re-parsing it back out of the "Execute {phase}: ..." task text.
  // Collapsed to one line -- a multi-line reason/description would
  // otherwise wrap oddly in this compact, single-line-truncated row.
  const finishedMessage =
    task.status === 'done'
      ? task.completion_notes
      : task.status === 'failed'
        ? task.failure_reason
        : null;
  const whatItsDoing = (
    finishedMessage || task.goto_reason || task.phase_description || task.description || ''
  )
    .replace(/\s*\n+\s*/g, ' ')
    .trim();

  const runTaskAction = async (action: 'pause' | 'stop' | 'resume' | 'rerun' | 'delete') => {
    if (action === 'delete' && !confirm(
      `Permanently delete this task${task.phase_name ? ` (${task.phase_name})` : ''}? This removes it entirely -- it will not be resumable, and any assigned agent will be stopped.`
    )) {
      return;
    }
    setActionPending((p) => ({ ...p, [action]: true }));
    try {
      if (action === 'pause') {
        await apiService.pauseTask(task.id);
      } else if (action === 'stop') {
        // Terminating the agent preserves its WIP commit; if the task never
        // got an agent (still pending/queued), fall back to the plain cancel.
        if (task.agent_id) await apiService.terminateAgent(task.agent_id, 'Stopped by user');
        else await apiService.cancelTask(task.id);
      } else if (action === 'resume' || action === 'rerun') {
        // Same underlying action (reset + spawn a fresh agent) -- Resume
        // applies to a paused ('blocked') task, Rerun to a done/failed one.
        await apiService.restartTask(task.id);
      } else if (action === 'delete') {
        await apiService.deleteTask(task.id);
      }
      onTaskUpdate?.();
    } catch (err: any) {
      console.error(`Task ${action} failed:`, err);
      if (action === 'delete') {
        toast.error(err?.response?.data?.detail || 'Failed to delete task');
      }
    } finally {
      setActionPending((p) => ({ ...p, [action]: false }));
    }
  };

  const activeStatuses = ['pending', 'queued', 'assigned', 'in_progress', 'under_review', 'validation_in_progress', 'needs_work'];

  return (
    <div className={`flex items-center gap-2 px-2 py-1.5 rounded border transition-colors ${
      task.status === 'done' || task.status === 'duplicated'
        ? 'bg-gray-100 dark:bg-gray-700/50 border-gray-200 dark:border-gray-600 hover:bg-gray-200 dark:hover:bg-gray-700'
        : 'bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50 hover:border-gray-200 dark:hover:border-gray-600'
    }`}>
      <TaskStatusIcon status={task.status} />
      <div
        className="flex-1 min-w-0 cursor-pointer"
        onClick={() => onTaskClick(task.id)}
      >
        <div className="flex items-center gap-2 mb-0.5">
          {task.phase_name && (
            <span className="text-xs font-semibold text-gray-800 dark:text-gray-200">{task.phase_name}</span>
          )}
          {task.action === 'goto' && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 font-medium">
              ↩ goto{task.action_target_phase ? ` (${task.action_target_phase})` : ''}
            </span>
          )}
          {task.action === 'retry' && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 font-medium">
              ↻ retry
            </span>
          )}
          {task.agent_status && task.agent_status !== 'terminated' && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
              task.agent_status === 'working' ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400' :
              task.agent_status === 'idle' ? 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400' :
              'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400'
            }`}>
              {task.agent_status}
            </span>
          )}
          {task.agent_status === 'terminated' && task.status === 'done' && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 font-medium">
              done
            </span>
          )}
          {task.cli_type && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-400 font-medium">
              {task.cli_type}
            </span>
          )}
          {task.created_at && (
            <span className="text-[10px] text-gray-400">
              {formatElapsed(
                Math.max(0, Math.floor(
                  ((task.completed_at ? new Date(task.completed_at).getTime() : Date.now()) -
                    new Date(task.created_at).getTime()) / 1000
                ))
              )}
            </span>
          )}
          {task.cost_total_usd > 0 && (
            <span className="text-[10px] text-gray-400">
              ${task.cost_total_usd.toFixed(2)}
            </span>
          )}
        </div>
        <p
          className="text-xs text-gray-500 dark:text-gray-400 truncate leading-relaxed"
          title={task.description || undefined}
        >
          {whatItsDoing || task.id.substring(0, 8)}
        </p>
      </div>
      {task.agent_id && (
        <button
          onClick={openTmuxView}
          className="flex items-center gap-1 px-1.5 py-0.5 text-[10px] bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-400 rounded hover:bg-violet-200 dark:hover:bg-violet-900/50 transition-colors"
          title="View live tmux output"
        >
          <span className={task.agent_status === 'working' ? 'w-1 h-1 rounded-full bg-green-500' : 'w-1 h-1 rounded-full bg-gray-400 dark:bg-gray-500'}></span>
          {task.agent_id.substring(0, 6)}
        </button>
      )}
      <RowActionIcons
        size="sm"
        canPause={activeStatuses.includes(task.status)}
        canStop={activeStatuses.includes(task.status)}
        canResume={task.status === 'blocked'}
        canRerun={task.status === 'done' || task.status === 'failed'}
        canDelete
        onPause={() => runTaskAction('pause')}
        onStop={() => runTaskAction('stop')}
        onResume={() => runTaskAction('resume')}
        onRerun={() => runTaskAction('rerun')}
        onDelete={() => runTaskAction('delete')}
        pending={actionPending}
      />
      {tmuxAgent && (
        <RealTimeAgentOutput agent={tmuxAgent} onClose={() => { tmuxViewerOpenRef.current = false; setTmuxAgent(null); }} fallbackPhaseName={task.phase_name} />
      )}
    </div>
  );
};

export default DesignQueuePanel;

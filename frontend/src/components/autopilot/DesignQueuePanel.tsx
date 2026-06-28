import React, { useState, useEffect } from 'react';
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
  CheckCircle2, XCircle, Loader2, Pause, Play, Upload, ChevronRight, ChevronDown, ExternalLink
} from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';
import DesignDetailModal from './DesignDetailModal';
import TaskDetailModal from '../TaskDetailModal';

interface DesignQueuePanelProps {
  projectId: string | null;
  onAddDesign: () => void;
  onLoadDesign: () => void;
  currentDesign?: string | null;
}

const DesignQueuePanel: React.FC<DesignQueuePanelProps> = ({ projectId, onAddDesign, onLoadDesign, currentDesign }) => {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [localOrder, setLocalOrder] = useState<any[] | null>(null);
  const [detailFile, setDetailFile] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [designStatuses, setDesignStatuses] = useState<Record<string, { status: string; workflowId?: string }>>({});

  // Fetch status for all designs to show badges
  const { data: designs, isLoading } = useQuery({
    queryKey: ['autopilot-project-designs', projectId],
    queryFn: () => projectId ? apiService.getAutopilotProjectDesigns(projectId) : Promise.resolve([]),
    enabled: !!projectId,
    refetchInterval: 5000,
  });

  // Periodically reload designs from disk every 30 seconds
  useEffect(() => {
    if (!projectId) return;
    const interval = setInterval(async () => {
      try {
        const data = await apiService.reloadAutopilotProjectDesigns(projectId);
        setLocalOrder(data);
        queryClient.setQueryData(['autopilot-project-designs', projectId], data);
      } catch {
        // Silently ignore reload failures during periodic refresh
      }
    }, 30000);
    return () => clearInterval(interval);
  }, [projectId, queryClient]);


  // Fetch status for each design
  useEffect(() => {
    if (!projectId || !designs || designs.length === 0) return;
    
    const fetchStatuses = async () => {
      const statuses: Record<string, { status: string; workflowId?: string }> = {};
      await Promise.all(
        designs.map(async (d: any) => {
          try {
            const status = await apiService.getAutopilotProjectDesignStatus(projectId, d.filename);
            statuses[d.filename] = {
              status: status.status || 'pending',
              workflowId: status.workflows?.[0]?.id
            };
          } catch {
            statuses[d.filename] = { status: 'pending' };
          }
        })
      );
      setDesignStatuses(statuses);
    };
    
    fetchStatuses();
    
    // Periodically refresh statuses every 10 seconds
    const statusInterval = setInterval(fetchStatuses, 10000);
    return () => clearInterval(statusInterval);
  }, [projectId, designs]);

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

  const pauseResumeMutation = useMutation({
    mutationFn: async ({ workflowId, action }: { workflowId: string; action: 'pause' | 'resume' }) => {
      // workflowId is actually the design filename here
      const designName = workflowId;
      const status = await apiService.getAutopilotProjectDesignStatus(projectId!, designName);
      const workflows = status.workflows || [];
      
      // Pause/resume all active workflows
      const results = [];
      for (const wf of workflows) {
        if (action === 'pause' && wf.status === 'active') {
          results.push(await apiService.pauseWorkflow(wf.id));
        } else if (action === 'resume' && wf.status === 'paused') {
          results.push(await apiService.resumeWorkflow(wf.id));
        }
      }
      return results;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflows'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      // Force refetch of designs to trigger status re-fetch
      queryClient.refetchQueries({ queryKey: ['autopilot-project-designs', projectId] });
      toast.success('Workflow updated');
    },
    onError: () => {
      toast.error('Failed to update workflow');
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

  if (!projectId) {
    return (
      <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
        <ListOrdered className="w-12 h-12 text-gray-300 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 mb-2">No project selected</h3>
        <p className="text-sm text-gray-400">Select or create a project to view its design queue</p>
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
            className="w-full pl-10 pr-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent bg-white"
          />
        </div>
        <Button 
          onClick={() => reloadMutation.mutate()}
          disabled={reloadMutation.isPending}
          variant="outline"
          className="text-gray-600"
        >
          <RefreshCw className={`w-4 h-4 mr-1 ${reloadMutation.isPending ? 'animate-spin' : ''}`} />
          Reload
        </Button>
        <Button onClick={onAddDesign} className="bg-violet-600 hover:bg-violet-700 text-white">
          <Plus className="w-4 h-4 mr-1" />
          Add Design
        </Button>
        <Button onClick={onLoadDesign} variant="outline" className="text-violet-600 border-violet-200 hover:bg-violet-50">
          <Upload className="w-4 h-4 mr-1" />
          Load Design
        </Button>
      </div>
      <p className="text-xs text-gray-400">
        Sorted by filename by default. Drag to reorder manually.
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
                  projectId={projectId}
                  onDetail={handleDetail}
                  onTaskClick={setSelectedTaskId}
                  onPauseResume={(_workflowId, action) => pauseResumeMutation.mutate({ workflowId: item.filename, action })}
                  onRemove={(filename) => {
                    if (confirm(`Remove "${item.name}" from queue?`)) {
                      removeMutation.mutate(filename);
                    }
                  }}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      ) : (
        <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
          <ListOrdered className="w-12 h-12 text-gray-300 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-600 mb-2">Queue is empty</h3>
          <p className="text-sm text-gray-400 mb-4">
            Drop design documents into the queue to start processing
          </p>
          <Button onClick={onAddDesign} variant="outline" className="text-violet-600 border-violet-200 hover:bg-violet-50">
            <Plus className="w-4 h-4 mr-1" />
            Add Design
          </Button>
          <Button onClick={onLoadDesign} variant="outline" className="text-violet-600 border-violet-200 hover:bg-violet-50">
            <Upload className="w-4 h-4 mr-1" />
            Load Design
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
    </div>
  );
};

// ── Status Badge ───────────────────────────────────────────────

const STATUS_CONFIG: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'bg-gray-100 text-gray-600', icon: <Clock className="w-3 h-3" />, label: 'Pending' },
  active: { color: 'bg-blue-100 text-blue-700', icon: <Loader2 className="w-3 h-3 animate-spin" />, label: 'Active' },
  paused: { color: 'bg-yellow-100 text-yellow-700', icon: <Clock className="w-3 h-3" />, label: 'Paused' },
  completed: { color: 'bg-green-100 text-green-700', icon: <CheckCircle2 className="w-3 h-3" />, label: 'Done' },
  failed: { color: 'bg-red-100 text-red-700', icon: <XCircle className="w-3 h-3" />, label: 'Failed' },
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const config = STATUS_CONFIG[status];
  if (!config) return null;
  return (
    <span className={`px-2 py-0.5 text-xs font-semibold rounded-full flex items-center gap-1 ${config.color}`}>
      {config.icon}
      {config.label}
    </span>
  );
};

// ── Task Status Icon ─────────────────────────────────────────

const TASK_STATUS_CONFIG: Record<string, { color: string; icon: React.ReactNode }> = {
  pending: { color: 'text-gray-400', icon: <Clock className="w-4 h-4" /> },
  assigned: { color: 'text-blue-500', icon: <Loader2 className="w-4 h-4" /> },
  in_progress: { color: 'text-violet-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  done: { color: 'text-green-500', icon: <CheckCircle2 className="w-4 h-4" /> },
  failed: { color: 'text-red-500', icon: <XCircle className="w-4 h-4" /> },
};

const TaskStatusIcon: React.FC<{ status: string }> = ({ status }) => {
  const config = TASK_STATUS_CONFIG[status];
  if (!config) return <Clock className="w-4 h-4 text-gray-400" />;
  return <span className={config.color}>{config.icon}</span>;
};

// ── Sortable Item ───────────────────────────────────────────────

interface SortableDesignItemProps {
  item: any;
  index: number;
  isActive?: boolean;
  onRemove: (filename: string) => void;
  onDetail: (filename: string) => void;
  onTaskClick: (taskId: string) => void;
  onPauseResume?: (workflowId: string, action: 'pause' | 'resume') => void;
  status?: string;
  workflowId?: string;
  projectId: string | null;
}

const SortableDesignItem: React.FC<SortableDesignItemProps> = ({ item, index, isActive, onRemove, onDetail, onTaskClick, onPauseResume, status, workflowId, projectId }) => {
  const [expanded, setExpanded] = useState(false);
  const [tasks, setTasks] = useState<any[]>([]);
  const [loadingTasks, setLoadingTasks] = useState(false);

  const handleToggleExpand = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const newExpanded = !expanded;
    setExpanded(newExpanded);
    
    // Fetch tasks when expanding for the first time
    if (newExpanded && tasks.length === 0 && projectId) {
      setLoadingTasks(true);
      try {
        const statusData = await apiService.getAutopilotProjectDesignStatus(projectId, item.filename);
        setTasks(statusData.tasks || []);
      } catch {
        setTasks([]);
      } finally {
        setLoadingTasks(false);
      }
    }
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
          isDragging ? 'shadow-lg border-violet-300 ring-2 ring-violet-200' :
          isActive ? 'bg-gradient-to-r from-violet-50 to-purple-50 border-violet-300 shadow-md ring-1 ring-violet-200' :
          'bg-white border-gray-100 hover:shadow-md'
        }`}
      >
        <div 
          className="flex items-center gap-4 px-5 py-4 cursor-pointer"
          onClick={handleToggleExpand}
        >
          {/* Expand arrow */}
          <div className="p-1 text-gray-400">
            {expanded ? (
              <ChevronDown className="w-5 h-5" />
            ) : (
              <ChevronRight className="w-5 h-5" />
            )}
          </div>

          {/* Drag handle */}
          <button
            {...listeners}
            className="flex flex-col items-center gap-1 text-gray-300 hover:text-gray-500 cursor-grab active:cursor-grabbing touch-none"
            onClick={(e) => e.stopPropagation()}
          >
            <GripVertical className="w-5 h-5" />
            <span className="text-xs font-mono text-gray-400">#{item.ordinal ?? index + 1}</span>
          </button>

          <div className={`p-2.5 rounded-lg ${isActive ? 'bg-violet-200' : 'bg-violet-50'}`}>
            <FileText className={`w-5 h-5 ${isActive ? 'text-violet-700' : 'text-violet-600'}`} />
          </div>

          <div className="flex-1 min-w-0">
            <h4 className="text-sm font-semibold text-gray-800 truncate">{item.name}</h4>
            <div className="flex items-center gap-3 mt-1">
              <span className="text-xs text-gray-500 font-mono">{item.filename}</span>
              <span className="text-xs text-gray-400">{formatBytes(item.size_bytes)}</span>
              {item.modified_at && (
                <span className="text-xs text-gray-400 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {formatDistanceToNow(new Date(item.modified_at), { addSuffix: true })}
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            {status && status !== 'pending' && (
              <StatusBadge status={status} />
            )}
            {workflowId && status && (status === 'active' || status === 'paused') && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onPauseResume?.(workflowId, status === 'paused' ? 'resume' : 'pause');
                }}
                className={`p-2 rounded-lg transition-colors ${
                  status === 'paused'
                    ? 'hover:bg-green-50 text-gray-400 hover:text-green-600'
                    : 'hover:bg-yellow-50 text-gray-400 hover:text-yellow-600'
                }`}
                title={status === 'paused' ? 'Resume' : 'Pause'}
              >
                {status === 'paused' ? <Play className="w-4 h-4" /> : <Pause className="w-4 h-4" />}
              </button>
            )}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDetail(item.filename);
              }}
              className="p-2 rounded-lg hover:bg-violet-50 transition-colors text-gray-400 hover:text-violet-600"
              title="View design details"
            >
              <FileText className="w-4 h-4" />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onRemove(item.filename); }}
              className="p-2 rounded-lg hover:bg-red-50 transition-colors text-gray-400 hover:text-red-600"
              title="Remove"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </div>
      </motion.div>

        {/* Expanded tasks section */}
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="border-t border-gray-100 bg-gray-50 rounded-b-xl"
          >
            <div className="px-5 py-3">
              <h5 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Tasks</h5>
              {loadingTasks ? (
                <div className="flex items-center justify-center py-4">
                  <Loader2 className="w-5 h-5 text-violet-500 animate-spin" />
                </div>
              ) : tasks.length > 0 ? (
                <div className="space-y-2">
                  {tasks.map((task) => (
                    <div
                      key={task.id}
                      className="flex items-center gap-3 px-3 py-2 bg-white rounded-lg border border-gray-100 cursor-pointer hover:bg-gray-50 hover:border-gray-200 transition-colors"
                    >
                      <TaskStatusIcon status={task.status} />
                      <div 
                        className="flex-1 min-w-0"
                        onClick={() => onTaskClick(task.id)}
                      >
                        <p className="text-sm text-gray-700 truncate">{task.description || task.id.substring(0, 8)}</p>
                        <div className="flex items-center gap-2 mt-0.5">
                          {task.phase_name && (
                            <span className="text-xs text-gray-400">{task.phase_name}</span>
                          )}
                          {task.agent_status && (
                            <span className={`text-xs px-1.5 py-0.5 rounded ${
                              task.agent_status === 'working' ? 'bg-green-100 text-green-700' :
                              task.agent_status === 'idle' ? 'bg-gray-100 text-gray-600' :
                              'bg-gray-100 text-gray-500'
                            }`}>
                              {task.agent_status}
                            </span>
                          )}
                        </div>
                      </div>
                      {task.agent_id && (
                        <a
                          href={`/agents/${task.agent_id}`}
                          className="flex items-center gap-1 px-2 py-1 text-xs bg-violet-100 text-violet-700 rounded hover:bg-violet-200 transition-colors"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <span className={task.agent_status === 'working' ? 'w-1.5 h-1.5 rounded-full bg-green-500' : 'w-1.5 h-1.5 rounded-full bg-gray-400'}></span>
                          {task.agent_id.substring(0, 6)}
                          <ExternalLink className="w-3 h-3" />
                        </a>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-gray-400 text-center py-4">No tasks yet</p>
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

export default DesignQueuePanel;

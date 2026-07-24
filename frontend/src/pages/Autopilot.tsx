import React, { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Rocket, ListOrdered, History, MessageSquare,
  Plus, RefreshCw, Clock, CheckCircle2, XCircle, AlertTriangle,
  Terminal
} from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import PipelineStatusCard from '@/components/autopilot/PipelineStatusCard';
import DesignQueuePanel from '@/components/autopilot/DesignQueuePanel';
import FeatureGallery from '@/components/autopilot/FeatureGallery';
import FeatureDetailModal from '@/components/autopilot/FeatureDetailModal';
import MessageCenter from '@/components/autopilot/MessageCenter';
import AddDesignModal from '@/components/autopilot/AddDesignModal';
import LoadDesignModal from '@/components/autopilot/LoadDesignModal';
import HumanInputBanner from '@/components/autopilot/HumanInputBanner';
import ProjectSettingsModal from '@/components/ProjectSettingsModal';
import { useProject } from '@/context/ProjectContext';
import toast from 'react-hot-toast';

type Tab = 'queue' | 'features' | 'messages' | 'logs';
const VALID_TABS: Tab[] = ['queue', 'features', 'messages', 'logs'];

const Autopilot: React.FC = () => {
  const { tab: urlTab } = useParams<{ tab?: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>(
    VALID_TABS.includes(urlTab as Tab) ? (urlTab as Tab) : 'queue'
  );
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null);
  const [showAddDesign, setShowAddDesign] = useState(false);
  const [showLoadDesign, setShowLoadDesign] = useState(false);
  const [featureStatusFilter, setFeatureStatusFilter] = useState<'all' | 'validated' | 'needs_review' | 'failed'>('all');
  const { selectedProject } = useProject();
  const projectId = selectedProject?.id || null;
  const queryClient = useQueryClient();

  // Sync tab state to URL path
  const handleTabChange = (tab: Tab) => {
    setActiveTab(tab);
    navigate(`/autopilot/${tab}`, { replace: true });
  };

  // Sync from URL on mount
  useEffect(() => {
    if (urlTab && VALID_TABS.includes(urlTab as Tab)) {
      setActiveTab(urlTab as Tab);
    }
  }, [urlTab]);

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['autopilot-status', projectId],
    queryFn: () => apiService.getAutopilotStatus(projectId || undefined),
    refetchInterval: 3000,  // Poll every 3 seconds
    enabled: !!projectId,
    staleTime: 0,  // Always refetch when project changes
    placeholderData: undefined,  // Don't carry over stale data from previous project
  });

  // Tasks sitting at status="pending" with no agent assigned -- distinct
  // from status.queue_depth (design-level: designs not yet processed) AND
  // from getQueueStatus's queued_tasks_count (only tasks explicitly queued
  // because capacity was full at creation time -- a pending task with slots
  // currently available never enters that queue at all, which is exactly
  // the orphaned-pending-task class of bug this session spent a while
  // fixing elsewhere: task genuinely stuck pending/no-agent while
  // queue_status correctly reports 0 queued and slots available). This
  // queries actual DB task status directly so it reflects reality.
  const { data: pendingTasks } = useQuery({
    queryKey: ['autopilot-pending-tasks', projectId],
    queryFn: () => apiService.getTasks(0, 500, 'pending', undefined, projectId || undefined),
    refetchInterval: 3000,
    enabled: !!projectId,
  });

  const togglePipeline = useMutation({
    mutationFn: async () => {
      if (status?.running) {
        return apiService.stopAutopilot(projectId || undefined);
      } else if (selectedProject) {
        // Try to start; a 409 means max_concurrent_projects is already
        // reached -- find out what's running before deciding what to do.
        try {
          return await apiService.startAutopilot(selectedProject.base_dir);
        } catch (err: any) {
          const is409 = err?.response?.status === 409 || err?.status === 409;
          if (!is409) throw err;

          // Query the global (no project_id) status to learn what's actually
          // running -- the 409 alone doesn't say, and it's frequently this
          // same project (a self-conflict from status polling not having
          // caught up yet after a just-started run), not a genuine
          // cross-project conflict. Stopping-and-restarting your own
          // just-started pipeline in that case just interrupts it for no
          // reason and looks like "the button doesn't work."
          //
          // Pass project_path so the backend can do realpath-resolved
          // comparison (handles /tmp -> /private/tmp on macOS).
          const globalStatus = await apiService.getAutopilotStatus(undefined, selectedProject.base_dir);
          const isSelfConflict = globalStatus?.is_self_conflict ?? false;
          // running_projects reports EVERY currently-running project, not
          // just one -- with max_concurrent_projects > 1, hitting the cap
          // can mean two DIFFERENT other projects are blocking the start,
          // not just the single one running_project_name used to imply.
          const runningProjects: Array<{ id: string; name: string | null; base_dir: string | null }> =
            globalStatus?.running_projects || [];

          if (isSelfConflict) {
            // Already running (this project) despite the 409 -- the
            // optimistic "now running" flip from onMutate was actually
            // correct, just confirmed a different way. Not a revert case.
            toast.success(`${selectedProject.name} is already running — no action needed.`);
            return { confirmed: true };
          }

          const label = runningProjects.length > 0
            ? runningProjects.map((p) => p.name || 'Unnamed project').join(' and ')
            : 'Another project';
          const confirmed = window.confirm(
            `${label} ${runningProjects.length > 1 ? 'are' : 'is'} currently running.\n\nStop ${runningProjects.length > 1 ? 'them' : 'it'} and start ${selectedProject.name}?`
          );
          if (!confirmed) {
            // Genuinely nothing changed for this project -- revert the
            // optimistic flip.
            toast(`Left ${label} running. ${selectedProject.name} was not started.`);
            return { revert: true };
          }

          // Stop EVERY project actually blocking the start, by id.
          // stopAutopilot() with no id tells the backend "stop every
          // currently running project" (there's no single global service
          // to fall back to now that projects run concurrently) -- without
          // resolving specific ids here, confirming this dialog could
          // silently kill an unrelated project's pipeline the user was
          // never even told about, or (with the cap > 1) miss stopping a
          // second blocker entirely and leave the retry below 409-ing again.
          if (runningProjects.length > 0) {
            await Promise.all(runningProjects.map((p) => apiService.stopAutopilot(p.id)));
          } else {
            // Couldn't identify anyone specific (shouldn't normally happen
            // once the cap is actually hit) -- fall back to the old
            // stop-everything behavior rather than being stuck unable to
            // proceed at all.
            await apiService.stopAutopilot();
          }
          // Small delay to let the backend fully stop
          await new Promise(r => setTimeout(r, 500));
          // Retry start
          const result = await apiService.startAutopilot(selectedProject.base_dir);
          toast.success(`Stopped ${label}, started ${selectedProject.name}.`);
          return result;
        }
      }
    },
    onMutate: async () => {
      // Cancel outgoing refetches
      await queryClient.cancelQueries({ queryKey: ['autopilot-status', projectId] });
      // Optimistically toggle the running state
      const previous = queryClient.getQueryData<any>(['autopilot-status', projectId]);
      const wasRunning = !!previous?.running;
      queryClient.setQueryData(['autopilot-status', projectId], (old: any) => {
        if (!old) return old;
        return { ...old, running: !wasRunning, last_error: null };
      });
      return { previous, wasRunning };
    },
    onSuccess: (data: any, _vars, context) => {
      // data === undefined happens when mutationFn's implicit no-op path
      // ran (e.g. no selectedProject) -- nothing was actually requested.
      if (data?.revert || data === undefined) {
        // Nothing actually changed for this project (user declined to stop
        // another running project, or there was no project to act on) --
        // undo the optimistic flip.
        if (context?.previous) {
          queryClient.setQueryData(['autopilot-status', projectId], context.previous);
        }
        return;
      }
      // Trust that the request actually succeeded over whatever the next
      // poll happens to read -- a start/stop can briefly race a backend
      // status read that hasn't caught up yet, which would otherwise flip
      // the optimistic state back before the user ever sees it take effect.
      queryClient.setQueryData(['autopilot-status', projectId], (old: any) => ({
        ...(old || {}),
        running: !context?.wasRunning,
        last_error: null,
      }));
    },
    onError: (err: any, _vars, context) => {
      // Rollback on error
      if (context?.previous) {
        queryClient.setQueryData(['autopilot-status', projectId], context.previous);
      }
      const message =
        err?.response?.data?.detail || err?.message || 'Failed to toggle pipeline';
      toast.error(message);
    },
    onSettled: () => {
      // Refetch to get real state
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
    },
  });

  const { data: messages } = useQuery({
    queryKey: ['autopilot-messages', projectId],
    queryFn: () => apiService.getAutopilotMessages(500),
    refetchInterval: 15000,
    enabled: !!projectId,
  });

  const { data: projectCosts } = useQuery({
    queryKey: ['project-costs', projectId],
    queryFn: () => apiService.getProjectCosts(projectId!),
    refetchInterval: 30000,
    enabled: !!projectId,
  });
  const [showProjectSettings, setShowProjectSettings] = useState(false);

  const tabs: { id: Tab; label: string; icon: React.ElementType; badge?: number }[] = [
    { id: 'queue', label: 'Design Queue', icon: ListOrdered, badge: status?.queue_depth },
    { id: 'features', label: 'Completed', icon: History },
    { id: 'messages', label: 'Messages', icon: MessageSquare, badge: messages?.length },
    { id: 'logs', label: 'Logs', icon: Terminal },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-gradient-to-br from-violet-500 to-purple-600 rounded-lg">
            <Rocket className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-3xl font-bold text-gray-800">Autopilot</h1>
            <p className="text-gray-500 text-sm">Continuous design-to-deploy pipeline</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetchStatus()}
            className="text-gray-600"
          >
            <RefreshCw className="w-4 h-4 mr-1" />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => setShowAddDesign(true)}
            className="bg-violet-600 hover:bg-violet-700 text-white"
            disabled={!projectId}
          >
            <Plus className="w-4 h-4 mr-1" />
            New Design
          </Button>
        </div>
      </div>

      {/* Human Input Banner (shows when pipeline needs input) */}
      <HumanInputBanner onOpenMessages={() => setActiveTab('messages')} projectId={projectId} />

      {/* Pipeline Status Hero */}
      <PipelineStatusCard
        status={status}
        pendingAgents={pendingTasks?.length}
        projectName={selectedProject?.name}
        onToggle={() => togglePipeline.mutate()}
        onMetricClick={(metric) => {
          if (metric === 'agents' || metric === 'pending_agents') {
            navigate('/agents');
          } else {
            // Map metric to filter: processed → all, succeeded → validated, failed → failed
            const filterMap: Record<string, 'all' | 'validated' | 'needs_review' | 'failed'> = {
              processed: 'all',
              succeeded: 'validated',
              failed: 'failed',
            };
            setFeatureStatusFilter(filterMap[metric] || 'all');
            handleTabChange('features');
          }
        }}
        loading={togglePipeline.isPending}
        costTotal={projectCosts?.cost_total_usd}
        costLimit={projectCosts?.cost_limit_usd}
        onBudgetClick={() => setShowProjectSettings(true)}
      />

      {/* Tab Navigation */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-1 -mb-px">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => handleTabChange(tab.id)}
              className={`
                flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors
                ${activeTab === tab.id
                  ? 'border-violet-500 text-violet-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'}
              `}
            >
              <tab.icon className="w-4 h-4" />
              {tab.label}
              {tab.badge !== undefined && tab.badge > 0 && (
                <span className="ml-1 px-2 py-0.5 text-xs rounded-full bg-violet-100 text-violet-700 font-medium">
                  {tab.badge}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.2 }}
        >
          {activeTab === 'queue' && (
            <DesignQueuePanel 
              projectId={projectId} 
              onAddDesign={() => setShowAddDesign(true)}
              onLoadDesign={() => setShowLoadDesign(true)}
              currentDesign={status?.current_design}
            />
          )}
          {activeTab === 'features' && (
            <FeatureGallery
              onSelectFeature={setSelectedFeatureId}
              projectId={projectId}
              statusFilter={featureStatusFilter}
              onStatusFilterChange={setFeatureStatusFilter}
            />
          )}
          {activeTab === 'messages' && <MessageCenter projectId={projectId} />}
          {activeTab === 'logs' && <LogsPanel projectId={projectId} />}
        </motion.div>
      </AnimatePresence>

      {/* Modals */}
      <FeatureDetailModal
        featureId={selectedFeatureId}
        onClose={() => setSelectedFeatureId(null)}
      />
      <AddDesignModal
        open={showAddDesign}
        projectId={projectId}
        onClose={() => setShowAddDesign(false)}
      />
      <LoadDesignModal
        open={showLoadDesign}
        projectId={projectId}
        onClose={() => setShowLoadDesign(false)}
      />
      <ProjectSettingsModal
        isOpen={showProjectSettings}
        onClose={() => setShowProjectSettings(false)}
      />
    </div>
  );
};

// ── Logs Panel ───────────────────────────────────────────────

const LogsPanel: React.FC<{ projectId: string | null }> = ({ projectId }) => {
  const { data, isLoading } = useQuery({
    queryKey: ['autopilot-logs', projectId],
    queryFn: () => apiService.getAutopilotLogs(200),
    refetchInterval: 15000,
    enabled: !!projectId,
  });

  const logs = data?.lines || [];

  return (
    <div className="bg-gray-900 rounded-xl shadow-lg overflow-hidden">
      <div className="px-5 py-3 bg-gray-800 border-b border-gray-700 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-green-400" />
          <span className="text-sm font-medium text-gray-300">Orchestrator Logs</span>
        </div>
        <span className="text-xs text-gray-500">{logs.length} lines</span>
      </div>
      <div className="p-4 max-h-[600px] overflow-y-auto font-mono text-xs leading-relaxed">
        {isLoading ? (
          <div className="text-gray-500">Loading...</div>
        ) : logs.length > 0 ? (
          logs.map((line: string, i: number) => (
            <div key={i} className={`py-0.5 ${getLogLineStyle(line)}`}>
              {line}
            </div>
          ))
        ) : (
          <div className="text-gray-500">No logs available</div>
        )}
      </div>
    </div>
  );
};

// ── Shared Helpers ───────────────────────────────────────────

export const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const styles: Record<string, string> = {
    validated: 'bg-emerald-100 text-emerald-700 border-emerald-200',
    needs_review: 'bg-amber-100 text-amber-700 border-amber-200',
    failed: 'bg-red-100 text-red-700 border-red-200',
    in_progress: 'bg-blue-100 text-blue-700 border-blue-200',
    pending: 'bg-gray-100 text-gray-600 border-gray-200',
  };

  return (
    <span className={`px-2.5 py-0.5 text-xs font-medium rounded-full border ${styles[status] || styles.pending}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
};

export const StatusIcon: React.FC<{ status: string }> = ({ status }) => {
  if (status === 'validated') return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
  if (status === 'failed') return <XCircle className="w-5 h-5 text-red-500" />;
  if (status === 'needs_review') return <AlertTriangle className="w-5 h-5 text-amber-500" />;
  return <Clock className="w-5 h-5 text-gray-400" />;
};

export const formatTime = (seconds: number): string => {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
};

const getLogLineStyle = (line: string): string => {
  if (line.includes('[ERROR]')) return 'text-red-400';
  if (line.includes('[WARN]')) return 'text-amber-400';
  if (line.includes('SUCCESS') || line.includes('passed')) return 'text-emerald-400';
  if (line.includes('Phase') || line.includes('PHASE')) return 'text-blue-400';
  return 'text-gray-400';
};

export default Autopilot;

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { apiService } from '@/services/api';
import type { WorkflowExecution } from '@/types';
import WorkflowStats from './WorkflowStats';
import PhaseList from './PhaseList';
import { useNavigate } from 'react-router-dom';
import { useWorkflow } from '@/context/WorkflowContext';
import { ExternalLink, Layers, Play, Pause, Trash2, RotateCw } from 'lucide-react';

const statusColors: Record<string, string> = {
  active: 'bg-green-500',
  paused: 'bg-yellow-500',
  completed: 'bg-blue-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
};

const statusLabels: Record<string, string> = {
  active: 'ACTIVE',
  paused: 'PAUSED',
  completed: 'COMPLETED',
  failed: 'FAILED',
  cancelled: 'CANCELLED',
};

const getStatusLabel = (execution: WorkflowExecution): string => {
  if (execution.status === 'paused' && execution.paused_by === 'budget') {
    return 'PAUSED: BUDGET LIMIT REACHED';
  }
  return statusLabels[execution.status] || execution.status.toUpperCase();
};

const formatDuration = (startTime: string) => {
  const start = new Date(startTime);
  const now = new Date();
  const diffMs = Math.max(0, now.getTime() - start.getTime());
  const hours = Math.floor(diffMs / (1000 * 60 * 60));
  const minutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
};

interface WorkflowCardProps {
  execution: WorkflowExecution;
  isExpanded: boolean;
  onToggle: () => void;
  expandedPhaseId: string | null;
  onTogglePhase: (phaseId: string) => void;
  isSelected: boolean;
  onViewAgent?: (agentId: string) => void;
}

export default function WorkflowCard({
  execution,
  isExpanded,
  onToggle,
  expandedPhaseId,
  onTogglePhase,
  isSelected,
  onViewAgent,
}: WorkflowCardProps) {
  const navigate = useNavigate();
  const { selectExecution, definitions } = useWorkflow();
  const queryClient = useQueryClient();
  // Not execution.phases -- that's only populated once the details query
  // below has run, which is gated on isExpanded, so it's always empty on
  // a collapsed card. definitions loads independently (WorkflowContext),
  // so this is available immediately, collapsed or not.
  const definitionPhasesCount = definitions.find((d) => d.id === execution.definition_id)?.phases_count ?? 0;

  const stopMutation = useMutation({
    mutationFn: () => apiService.stopWorkflow(execution.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-executions'] }),
  });

  // Recover = non-destructive resume: reactivates the run and restarts any orphaned
  // phase agent on its existing worktree, continuing from the last committed phase.
  const recoverMutation = useMutation({
    mutationFn: () => apiService.recoverWorkflow(execution.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-executions'] }),
  });

  const cancelMutation = useMutation({
    mutationFn: () => apiService.cancelWorkflow(execution.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-executions'] }),
  });

  const handlePause = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await stopMutation.mutateAsync();
  };

  const handleRecover = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await recoverMutation.mutateAsync();
  };

  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Cancel this workflow and terminate all agents?')) return;
    await cancelMutation.mutateAsync();
  };

  // Fetch phase data when expanded
  const { data: details } = useQuery({
    queryKey: ['workflow-execution-detail', execution.id],
    queryFn: () => apiService.getWorkflowExecution(execution.id),
    enabled: isExpanded,
    refetchInterval: isExpanded ? 5000 : false,
  });

  const phases = details?.phases || [];

  const handleGoToOverview = () => {
    selectExecution(execution.id);
    navigate('/overview');
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`bg-white dark:bg-gray-800 rounded-lg shadow-md border-2 transition-all ${
        isSelected
          ? 'border-blue-500 ring-2 ring-blue-200 dark:ring-blue-900'
          : isExpanded
            ? 'border-blue-200 dark:border-blue-800'
            : 'border-transparent hover:border-gray-200 dark:hover:border-gray-700'
      }`}
    >
      {/* Card header */}
      <div
        className="p-4 cursor-pointer"
        onClick={onToggle}
      >
        {/* Status badge */}
        <div className="flex justify-between items-start mb-3">
          <div>
            <span
              className={`px-2 py-1 rounded text-xs font-medium ${statusColors[execution.status]} text-white`}
              title={execution.status_reason || undefined}
            >
              {getStatusLabel(execution)}
            </span>
            {execution.status === 'paused' && execution.status_reason && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-md">
                {execution.status_reason}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-gray-500 dark:text-gray-400 text-sm">{execution.definition_name}</span>
            {execution.status === 'active' && (
              <button
                onClick={handlePause}
                disabled={stopMutation.isPending}
                className="p-1.5 bg-yellow-500 hover:bg-yellow-600 text-white rounded transition-colors disabled:opacity-50"
                title="Pause workflow"
              >
                <Pause className="w-3 h-3" />
              </button>
            )}
            {execution.status === 'paused' && (
              <button
                onClick={handleRecover}
                disabled={recoverMutation.isPending}
                className="p-1.5 bg-green-500 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
                title="Resume — continue from the last committed phase"
              >
                <Play className="w-3 h-3" />
              </button>
            )}
            {(execution.status === 'active' || execution.status === 'failed') && (
              <button
                onClick={handleRecover}
                disabled={recoverMutation.isPending}
                className="p-1.5 bg-emerald-500 hover:bg-emerald-600 text-white rounded transition-colors disabled:opacity-50"
                title="Recover — restart a stalled/interrupted run from its last committed phase"
              >
                <RotateCw className={`w-3 h-3 ${recoverMutation.isPending ? 'animate-spin' : ''}`} />
              </button>
            )}
            {(execution.status === 'active' || execution.status === 'paused') && (
              <button
                onClick={handleCancel}
                disabled={cancelMutation.isPending}
                className="p-1.5 bg-red-500 hover:bg-red-600 text-white rounded transition-colors disabled:opacity-50"
                title="Cancel workflow"
              >
                <Trash2 className="w-3 h-3" />
              </button>
            )}
          </div>
        </div>

        {/* Title */}
        <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-100 mb-2 truncate">
          {execution.description?.split('\n')[0] || execution.definition_name}
        </h3>

        {/* Stats */}
        <WorkflowStats execution={execution} />

        {/* Phase count indicator -- links out to the Phases page's config
            view for this workflow's definition, separate from expanding
            this card (which shows the EXECUTION's own phase progress).
            Uses definitionPhasesCount (always available), not phases.length
            (only populated once the card has been expanded at least once,
            so that count is always 0 on a collapsed card -- this button
            would never render). */}
        {definitionPhasesCount > 0 && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              navigate(`/phases?definition=${encodeURIComponent(execution.definition_id)}`);
            }}
            className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:underline mb-2"
            title="View this workflow's phase configuration"
          >
            <Layers className="w-3 h-3" />
            {definitionPhasesCount} phases
          </button>
        )}

        {/* Footer */}
        <div className="flex justify-between items-center text-xs text-gray-500 dark:text-gray-400">
          <span>Started: {new Date(execution.created_at).toLocaleString()}</span>
          <span className="text-gray-400 dark:text-gray-500">{formatDuration(execution.created_at)}</span>
        </div>
      </div>

      {/* Expanded content: phases */}
      {isExpanded && (
        <div className="px-4 pb-4 border-t border-gray-200 dark:border-gray-700">
          <PhaseList
            phases={phases}
            expandedPhaseId={expandedPhaseId}
            onTogglePhase={onTogglePhase}
            onViewAgent={onViewAgent}
          />

          {/* Go to Overview */}
          <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 flex justify-end">
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleGoToOverview();
              }}
              className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 flex items-center gap-1"
            >
              Go to Overview <ExternalLink className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}
    </motion.div>
  );
}

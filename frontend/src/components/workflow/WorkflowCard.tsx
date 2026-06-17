import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { apiService } from '@/services/api';
import type { WorkflowExecution } from '@/types';
import WorkflowStats from './WorkflowStats';
import PhaseList from './PhaseList';
import { useNavigate } from 'react-router-dom';
import { useWorkflow } from '@/context/WorkflowContext';
import { ExternalLink, Layers, Play, Pause, Trash2 } from 'lucide-react';

const statusColors: Record<string, string> = {
  active: 'bg-green-500',
  paused: 'bg-yellow-500',
  completed: 'bg-blue-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
};

const formatDuration = (startTime: string) => {
  const start = new Date(startTime);
  const now = new Date();
  const diffMs = now.getTime() - start.getTime();
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
}

export default function WorkflowCard({
  execution,
  isExpanded,
  onToggle,
  expandedPhaseId,
  onTogglePhase,
  isSelected,
}: WorkflowCardProps) {
  const navigate = useNavigate();
  const { selectExecution } = useWorkflow();
  const queryClient = useQueryClient();

  const stopMutation = useMutation({
    mutationFn: () => apiService.stopWorkflow(execution.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-executions'] }),
  });

  const resumeMutation = useMutation({
    mutationFn: () => apiService.resumeWorkflow(execution.id),
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

  const handleResume = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await resumeMutation.mutateAsync();
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
      className={`bg-white rounded-lg shadow-md border-2 transition-all ${
        isSelected
          ? 'border-blue-500 ring-2 ring-blue-200'
          : isExpanded
            ? 'border-blue-200'
            : 'border-transparent hover:border-gray-200'
      }`}
    >
      {/* Card header */}
      <div
        className="p-4 cursor-pointer"
        onClick={onToggle}
      >
        {/* Status badge */}
        <div className="flex justify-between items-start mb-3">
          <span
            className={`px-2 py-1 rounded text-xs font-medium ${statusColors[execution.status]} text-white`}
          >
            {execution.status.toUpperCase()}
          </span>
          <div className="flex items-center gap-2">
            <span className="text-gray-500 text-sm">{execution.definition_name}</span>
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
                onClick={handleResume}
                disabled={resumeMutation.isPending}
                className="p-1.5 bg-green-500 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
                title="Resume workflow"
              >
                <Play className="w-3 h-3" />
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
        <h3 className="text-lg font-semibold text-gray-800 mb-2 truncate">
          {execution.description || execution.definition_name}
        </h3>

        {/* Stats */}
        <WorkflowStats execution={execution} />

        {/* Phase count indicator */}
        {phases.length > 0 && (
          <div className="flex items-center gap-1 text-xs text-gray-500 mb-2">
            <Layers className="w-3 h-3" />
            {phases.length} phases
          </div>
        )}

        {/* Footer */}
        <div className="flex justify-between items-center text-xs text-gray-500">
          <span>Started: {new Date(execution.created_at).toLocaleString()}</span>
          <span className="text-gray-400">{formatDuration(execution.created_at)}</span>
        </div>
      </div>

      {/* Expanded content: phases */}
      {isExpanded && (
        <div className="px-4 pb-4 border-t">
          <PhaseList
            phases={phases}
            expandedPhaseId={expandedPhaseId}
            onTogglePhase={onTogglePhase}
          />

          {/* Go to Overview */}
          <div className="mt-3 pt-3 border-t flex justify-end">
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleGoToOverview();
              }}
              className="text-xs text-blue-600 hover:text-blue-800 flex items-center gap-1"
            >
              Go to Overview <ExternalLink className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}
    </motion.div>
  );
}

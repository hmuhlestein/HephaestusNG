import React, { useState, useEffect } from 'react';
import { FileText, ExternalLink, Clock, User, DollarSign } from 'lucide-react';
import { apiService } from '@/services/api';
import { TaskFullDetails } from '@/types';
import { useTaskRuntime } from '@/hooks/useTaskRuntime';
import StatusBadge from './StatusBadge';
import { PhaseBadge } from './PhaseBadge';

interface ClickableTaskCardProps {
  taskId: string;
  onClick: (e?: React.MouseEvent) => void;
  compact?: boolean;
  className?: string;
  showPhaseInfo?: boolean;
}

const ClickableTaskCard: React.FC<ClickableTaskCardProps> = ({
  taskId,
  onClick,
  compact = false,
  className = '',
  showPhaseInfo = true,
}) => {
  const [task, setTask] = useState<TaskFullDetails | null>(null);
  const [cost, setCost] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    const fetchTask = async () => {
      try {
        const [taskDetails, costData] = await Promise.all([
          apiService.getTaskFullDetails(taskId),
          apiService.getTaskCosts(taskId).catch(() => null),
        ]);
        if (mounted) {
          setTask(taskDetails);
          if (costData?.cost_total_usd != null) {
            setCost(costData.cost_total_usd);
          }
          setLoading(false);
        }
      } catch (error) {
        if (mounted) {
          setLoading(false);
        }
      }
    };

    fetchTask();

    return () => {
      mounted = false;
    };
  }, [taskId]);

  // task is fetched once on mount and never refetched -- task.runtime_seconds
  // is a snapshot from that moment, so it goes stale (frozen) for any card
  // that stays mounted while the task keeps running, unlike TaskDetailModal
  // which ticks live via this same hook. Called unconditionally (before the
  // loading/no-task early returns) per the Rules of Hooks; null-safe against
  // task not being loaded yet.
  const runtime = useTaskRuntime(
    task?.started_at ?? null,
    task?.completed_at ?? null,
    task?.runtime_seconds
  );

  if (loading) {
    return (
      <div className={`animate-pulse bg-gray-100 dark:bg-gray-700 rounded-lg p-3 ${className}`}>
        <div className="h-4 bg-gray-200 dark:bg-gray-600 rounded w-32 mb-2"></div>
        <div className="h-3 bg-gray-200 dark:bg-gray-600 rounded w-40"></div>
      </div>
    );
  }

  if (!task) {
    return (
      <div className={`text-gray-500 dark:text-gray-400 text-sm font-mono ${className}`}>
        {taskId.substring(0, 8)}
      </div>
    );
  }

  if (compact) {
    return (
      <button
        onClick={onClick}
        className={`text-left hover:bg-green-50 dark:hover:bg-green-900/20 rounded-lg p-2 transition-all group flex items-start gap-2 w-full ${className}`}
      >
        <FileText className="w-4 h-4 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-0.5">
            <p className="font-mono text-xs text-gray-500 dark:text-gray-400">{taskId.substring(0, 8)}</p>
            <StatusBadge status={task.status} />
            {cost != null && cost > 0 && (
              <span className="flex items-center gap-0.5 text-xs text-emerald-600 dark:text-emerald-400 font-medium">
                <DollarSign className="w-3 h-3" />
                {cost < 0.01 ? '<0.01' : cost.toFixed(2)}
              </span>
            )}
          </div>
          <p className="text-sm text-gray-800 dark:text-gray-200 group-hover:text-green-600 dark:group-hover:text-green-400 transition-colors truncate">
            {task.enriched_description || task.raw_description}
          </p>
          {showPhaseInfo && task.phase_info && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              Phase {task.phase_info.order}: {task.phase_info.name}
            </p>
          )}
        </div>
        <ExternalLink className="w-3 h-3 text-gray-400 dark:text-gray-500 group-hover:text-green-600 dark:group-hover:text-green-400 opacity-0 group-hover:opacity-100 transition-all flex-shrink-0 mt-1" />
      </button>
    );
  }

  return (
    <button
      onClick={onClick}
      className={`text-left w-full hover:bg-green-50 dark:hover:bg-green-900/20 rounded-xl p-4 transition-all duration-200 border border-gray-200 dark:border-gray-700 hover:border-green-300 dark:hover:border-green-700 hover:shadow-md group ${className}`}
    >
      <div className="flex items-start gap-3">
        <div className="bg-green-100 dark:bg-green-900/40 p-2 rounded-lg group-hover:bg-green-200 dark:group-hover:bg-green-900/60 transition-colors">
          <FileText className="w-5 h-5 text-green-600 dark:text-green-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <p className="font-mono text-xs text-gray-500 dark:text-gray-400">{taskId.substring(0, 12)}</p>
            <StatusBadge status={task.status} />
            <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
              task.priority === 'high'
                ? 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                : task.priority === 'medium'
                ? 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-700 dark:text-yellow-300'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300'
            }`}>
              {task.priority}
            </span>
            {cost != null && cost > 0 && (
              <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 text-xs font-semibold">
                <DollarSign className="w-3 h-3" />
                {cost < 0.01 ? '<0.01' : cost.toFixed(2)}
              </span>
            )}
          </div>

          <p className="text-sm font-medium text-gray-800 dark:text-gray-200 group-hover:text-green-600 dark:group-hover:text-green-400 transition-colors line-clamp-2 mb-2">
            {task.enriched_description || task.raw_description}
          </p>

          {showPhaseInfo && task.phase_info && (
            <div className="mb-2">
              <PhaseBadge
                phaseOrder={task.phase_info.order}
                phaseName={task.phase_info.name}
                totalPhases={5}
              />
            </div>
          )}

          <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400 flex-wrap">
            {task.created_at && (
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {new Date(task.created_at).toLocaleString()}
              </span>
            )}
            {task.agent_info && (
              <span className="flex items-center gap-1">
                <User className="w-3 h-3" />
                Agent {task.agent_info.id.substring(0, 8)}
              </span>
            )}
            {runtime.runtimeSeconds > 0 && (
              <span className="font-semibold">
                {runtime.runtimeDisplay} runtime
              </span>
            )}
          </div>
        </div>
        <ExternalLink className="w-4 h-4 text-gray-400 dark:text-gray-500 group-hover:text-green-600 dark:group-hover:text-green-400 opacity-0 group-hover:opacity-100 transition-all flex-shrink-0 mt-1" />
      </div>
    </button>
  );
};

export default ClickableTaskCard;

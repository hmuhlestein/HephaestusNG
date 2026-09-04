import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import StatusBadge from '@/components/StatusBadge';
import { ExternalLink, Zap, DollarSign } from 'lucide-react';

interface TaskRowProps {
  task: any;
  onTerminateAgent?: (agentId: string) => void;
}

const priorityColor: Record<string, string> = {
  high: 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300',
  medium: 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-700 dark:text-yellow-300',
  low: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
};

export default function TaskRow({ task, onTerminateAgent }: TaskRowProps) {
  // Once a task is finished, its own outcome is more useful than the
  // prompt that started it -- show completion_notes/failure_reason
  // instead of leaving the input snippet up after the work is done. A
  // 'pending' task can also carry a failure_reason left over from a
  // prior attempt (e.g. a session-limit hit that's queued for retry
  // once its workflow un-pauses) -- surface that too, instead of
  // silently reverting to the original prompt text as if nothing had
  // gone wrong.
  const finishedMessage =
    task.status === 'done'
      ? task.completion_notes
      : task.status === 'failed' || task.status === 'pending'
        ? task.failure_reason
        : null;
  const displayText =
    finishedMessage || task.enriched_description || task.raw_description || task.description;
  const isPendingRetry = task.status === 'pending' && !!task.failure_reason;

  return (
    <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3 border border-gray-100 dark:border-gray-700">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <StatusBadge status={task.status} size="sm" />
            <Badge variant="outline" className={`text-[10px] ${priorityColor[task.priority || 'medium']}`}>
              {task.priority || 'medium'}
            </Badge>
          </div>
          {isPendingRetry && (
            <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400 mb-0.5">
              Last attempt failed — pending retry
            </div>
          )}
          <div
            className={`text-sm truncate ${
              isPendingRetry ? 'text-amber-800 dark:text-amber-300' : 'text-gray-800 dark:text-gray-200'
            }`}
          >
            {displayText}
          </div>
          {task.assigned_agent_id && (
            <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Agent: <code className="bg-gray-200 dark:bg-gray-700 px-1 rounded text-[10px]">{task.assigned_agent_id.slice(0, 8)}...</code>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          {task.cost_total_usd > 0 && (
            <span className="inline-flex items-center gap-0.5 text-xs font-mono text-gray-600 dark:text-gray-400">
              <DollarSign className="w-3 h-3" />
              {task.cost_total_usd < 0.01 ? task.cost_total_usd.toFixed(4) : task.cost_total_usd.toFixed(2)}
            </span>
          )}
          <a
            href={`/tasks?highlight=${task.id}`}
            className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 p-1"
            title="View task"
          >
            <ExternalLink className="w-3 h-3" />
          </a>
          {task.assigned_agent_id && task.status !== 'done' && task.status !== 'failed' && onTerminateAgent && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0 text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300"
              title="Terminate agent"
              onClick={(e) => {
                e.stopPropagation();
                onTerminateAgent(task.assigned_agent_id);
              }}
            >
              <Zap className="w-3 h-3" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

import type { WorkflowExecution } from '@/types';

interface WorkflowStatsProps {
  execution: WorkflowExecution;
}

export default function WorkflowStats({ execution }: WorkflowStatsProps) {
  return (
    <div className="grid grid-cols-4 gap-2 mb-3 text-center">
      <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-2">
        <div className="text-lg font-bold text-gray-800 dark:text-gray-100">{execution.stats?.total_tasks || 0}</div>
        <div className="text-xs text-gray-500 dark:text-gray-400">Tasks</div>
      </div>
      <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-2">
        <div className="text-lg font-bold text-gray-800 dark:text-gray-100">{execution.stats?.active_agents || 0}</div>
        <div className="text-xs text-gray-500 dark:text-gray-400">Agents</div>
      </div>
      <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-2">
        <div className="text-lg font-bold text-gray-800 dark:text-gray-100">{execution.stats?.done_tasks || 0}</div>
        <div className="text-xs text-gray-500 dark:text-gray-400">Done</div>
      </div>
      <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-2">
        <div className="text-lg font-bold text-gray-800 dark:text-gray-100">{execution.stats?.failed_tasks || 0}</div>
        <div className="text-xs text-gray-500 dark:text-gray-400">Failed</div>
      </div>
    </div>
  );
}

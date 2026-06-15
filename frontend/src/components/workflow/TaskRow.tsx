import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import StatusBadge from '@/components/StatusBadge';
import { ExternalLink, Zap } from 'lucide-react';

interface TaskRowProps {
  task: any;
  onTerminateAgent?: (agentId: string) => void;
}

const priorityColor: Record<string, string> = {
  high: 'bg-red-100 text-red-700',
  medium: 'bg-yellow-100 text-yellow-700',
  low: 'bg-gray-100 text-gray-600',
};

export default function TaskRow({ task, onTerminateAgent }: TaskRowProps) {
  return (
    <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <StatusBadge status={task.status} size="sm" />
            <Badge variant="outline" className={`text-[10px] ${priorityColor[task.priority || 'medium']}`}>
              {task.priority || 'medium'}
            </Badge>
          </div>
          <div className="text-sm text-gray-800 truncate">
            {task.enriched_description || task.raw_description || task.description}
          </div>
          {task.assigned_agent_id && (
            <div className="text-xs text-gray-500 mt-1">
              Agent: <code className="bg-gray-200 px-1 rounded text-[10px]">{task.assigned_agent_id.slice(0, 8)}...</code>
            </div>
          )}
        </div>
        <div className="flex items-center gap-1">
          <a
            href={`/tasks?highlight=${task.id}`}
            className="text-blue-600 hover:text-blue-800 p-1"
            title="View task"
          >
            <ExternalLink className="w-3 h-3" />
          </a>
          {task.assigned_agent_id && task.status !== 'done' && task.status !== 'failed' && onTerminateAgent && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0 text-red-500 hover:text-red-700"
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

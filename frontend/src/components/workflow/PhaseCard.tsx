import { Badge } from '@/components/ui/badge';
import { ChevronDown, ChevronRight, Users, ListTodo } from 'lucide-react';

interface PhaseCardProps {
  phase: any;
  isExpanded: boolean;
  onToggle: () => void;
}

const statusColor: Record<string, string> = {
  in_progress: 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300',
  completed: 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300',
  pending: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
  failed: 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300',
  skipped: 'bg-gray-100 dark:bg-gray-700 text-gray-400 dark:text-gray-500',
};

export default function PhaseCard({
  phase,
  isExpanded,
  onToggle,
}: PhaseCardProps) {
  // Determine phase status from metrics
  const phaseStatus = phase.active_agents > 0
    ? 'in_progress'
    : phase.completed_tasks === phase.total_tasks && phase.total_tasks > 0
      ? 'completed'
      : 'pending';

  const badge = statusColor[phaseStatus] || statusColor.pending;

  return (
    <div
      className={`border rounded-lg transition-all cursor-pointer ${
        isExpanded
          ? 'border-blue-300 dark:border-blue-700 bg-blue-50/30 dark:bg-blue-900/20'
          : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 bg-white dark:bg-gray-800'
      }`}
    >
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3 py-2"
        onClick={onToggle}
      >
        {isExpanded ? (
          <ChevronDown className="w-4 h-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
        )}
        <Badge variant="outline" className="text-[10px]">P{phase.order}</Badge>
        <Badge variant="outline" className={`text-[10px] ${badge}`}>
          {phaseStatus.replace('_', ' ')}
        </Badge>
        <span className="text-sm font-medium text-gray-800 dark:text-gray-100 truncate flex-1">
          {phase.name}
        </span>
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 flex-shrink-0">
          <span className="flex items-center gap-1">
            <Users className="w-3 h-3" />
            {phase.active_agents || 0}
          </span>
          <span className="flex items-center gap-1">
            <ListTodo className="w-3 h-3" />
            {phase.completed_tasks || 0}/{phase.total_tasks || 0}
          </span>
        </div>
      </div>

      {/* Summary (collapsed only) */}
      {!isExpanded && phase.description && (
        <div className="px-3 pb-2 -mt-1">
          <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
            {phase.description}
          </p>
        </div>
      )}
    </div>
  );
}

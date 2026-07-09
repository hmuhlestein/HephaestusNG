import { Button } from '@/components/ui/button';
import { Terminal, Clock, AlertTriangle } from 'lucide-react';

import StatusBadge from '../StatusBadge';

interface PhaseAgentListProps {
  agents: any[];
  onTerminateAgent: (agentId: string) => void;
  onViewAgent?: (agentId: string) => void;
}

export default function PhaseAgentList({ agents, onTerminateAgent, onViewAgent }: PhaseAgentListProps) {
  if (!agents || agents.length === 0) {
    return (
      <div className="text-center py-6 text-gray-500 text-sm">
        No agents currently running in this phase.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {agents.map((agent) => (
        <div
          key={agent.id}
          className={`bg-white rounded-lg border border-gray-200 p-3 ${onViewAgent ? 'cursor-pointer hover:border-blue-300 hover:shadow-sm transition-all' : ''}`}
          onClick={() => onViewAgent?.(agent.id)}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <StatusBadge status={agent.status} size="sm" />
                <span className="text-xs text-gray-500">
                  <Terminal className="w-3 h-3 inline mr-1" />
                  {agent.cli_type}
                </span>
                {agent.health_check_failures > 0 && (
                  <span className="text-xs text-yellow-600">
                    <AlertTriangle className="w-3 h-3 inline mr-1" />
                    {agent.health_check_failures} failures
                  </span>
                )}
              </div>
              <div className="text-sm font-mono text-gray-700 truncate">
                {agent.id.slice(0, 12)}...
              </div>
              {agent.started_at && (
                <div className="text-xs text-gray-500 mt-1">
                  <Clock className="w-3 h-3 inline mr-1" />
                  Started: {new Date(agent.started_at).toLocaleString()}
                </div>
              )}
            </div>
            <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-6 p-0 text-red-500 hover:text-red-700"
                title="Terminate agent"
                onClick={() => onTerminateAgent(agent.id)}
              >
                <AlertTriangle className="w-3 h-3" />
              </Button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

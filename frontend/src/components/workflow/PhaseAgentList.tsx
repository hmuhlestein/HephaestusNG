import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Terminal, Clock, AlertTriangle } from 'lucide-react';

interface PhaseAgentListProps {
  agents: any[];
  onTerminateAgent: (agentId: string) => void;
}

const statusColor: Record<string, string> = {
  working: 'bg-green-100 text-green-700',
  idle: 'bg-gray-100 text-gray-600',
  stuck: 'bg-yellow-100 text-yellow-700',
  terminated: 'bg-red-100 text-red-700',
};

export default function PhaseAgentList({ agents, onTerminateAgent }: PhaseAgentListProps) {
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
          className="bg-white rounded-lg border border-gray-200 p-3"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <Badge variant="outline" className={`text-[10px] ${statusColor[agent.status] || statusColor.idle}`}>
                  {agent.status}
                </Badge>
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
            <div className="flex items-center gap-1">
              <a
                href={`/agents/${agent.id}`}
                className="text-blue-600 hover:text-blue-800 p-1"
                title="View logs"
              >
                <Terminal className="w-3 h-3" />
              </a>
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

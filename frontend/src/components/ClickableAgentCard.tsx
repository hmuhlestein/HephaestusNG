import React, { useState, useEffect } from 'react';
import { Bot, ExternalLink, Clock } from 'lucide-react';
import { apiService } from '@/services/api';
import { Agent } from '@/types';
import StatusBadge from './StatusBadge';

interface ClickableAgentCardProps {
  agentId: string;
  onClick: (e?: React.MouseEvent) => void;
  compact?: boolean;
  className?: string;
  showTaskInfo?: boolean;
}

const ClickableAgentCard: React.FC<ClickableAgentCardProps> = ({
  agentId,
  onClick,
  compact = false,
  className = '',
  showTaskInfo = true,
}) => {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    const fetchAgent = async () => {
      try {
        const foundAgent = await apiService.getAgent(agentId);
        if (mounted) {
          setAgent(foundAgent);
          setLoading(false);
        }
      } catch (error) {
        if (mounted) {
          setLoading(false);
        }
      }
    };

    fetchAgent();

    return () => {
      mounted = false;
    };
  }, [agentId]);

  if (loading) {
    return (
      <div className={`animate-pulse bg-gray-100 dark:bg-gray-700 rounded-lg p-3 ${className}`}>
        <div className="h-4 bg-gray-200 dark:bg-gray-600 rounded w-24 mb-2"></div>
        <div className="h-3 bg-gray-200 dark:bg-gray-600 rounded w-32"></div>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className={`text-gray-500 dark:text-gray-400 text-sm font-mono ${className}`}>
        {agentId.substring(0, 8)}
      </div>
    );
  }

  if (compact) {
    return (
      <button
        onClick={onClick}
        className={`text-left hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-lg p-2 transition-all group inline-flex items-center gap-2 ${className}`}
      >
        <Bot className="w-4 h-4 text-blue-600 dark:text-blue-400 flex-shrink-0" />
        <div className="min-w-0">
          <p className="font-mono text-xs text-gray-500 dark:text-gray-400">{agentId.substring(0, 8)}</p>
          {showTaskInfo && agent.current_task && (
            <p className="text-sm text-gray-800 dark:text-gray-200 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors truncate">
              {agent.current_task.description.substring(0, 40)}...
            </p>
          )}
          {!agent.current_task && (
            <p className="text-sm text-gray-500 dark:text-gray-400 italic">No active task</p>
          )}
        </div>
        <ExternalLink className="w-3 h-3 text-gray-400 dark:text-gray-500 group-hover:text-blue-600 dark:group-hover:text-blue-400 opacity-0 group-hover:opacity-100 transition-all flex-shrink-0" />
      </button>
    );
  }

  return (
    <button
      onClick={onClick}
      className={`text-left w-full hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-xl p-4 transition-all duration-200 border border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-blue-700 hover:shadow-md group ${className}`}
    >
      <div className="flex items-start gap-3">
        <div className="bg-blue-100 dark:bg-blue-900/40 p-2 rounded-lg group-hover:bg-blue-200 dark:group-hover:bg-blue-900/60 transition-colors">
          <Bot className="w-5 h-5 text-blue-600 dark:text-blue-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <p className="font-mono text-xs text-gray-500 dark:text-gray-400">{agentId.substring(0, 12)}</p>
            <StatusBadge status={agent.status} size="sm" />
          </div>

          {showTaskInfo && agent.current_task && (
            <div className="mb-2">
              <p className="text-sm font-medium text-gray-800 dark:text-gray-200 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors line-clamp-2">
                {agent.current_task.description}
              </p>
              <div className="flex items-center gap-2 mt-1">
                <StatusBadge status={agent.current_task.status} size="sm" />
                {agent.current_task.action === 'goto' && (
                  <span className="px-1.5 py-0.5 text-[10px] font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300 rounded-full">
                    ↩ goto{agent.current_task.action_target_phase ? ` (${agent.current_task.action_target_phase})` : ''}
                  </span>
                )}
                {agent.current_task.action === 'retry' && (
                  <span className="px-1.5 py-0.5 text-[10px] font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full">
                    ↻ retry
                  </span>
                )}
                {agent.current_task.phase_info && (
                  <span className="text-xs text-gray-600 dark:text-gray-400">
                    Phase {agent.current_task.phase_info.order}: {agent.current_task.phase_info.name}
                  </span>
                )}
              </div>
            </div>
          )}

          {!agent.current_task && (
            <p className="text-sm text-gray-500 dark:text-gray-400 italic mb-2">No active task</p>
          )}

          <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
            {agent.last_activity && (
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {new Date(agent.last_activity).toLocaleTimeString()}
              </span>
            )}
            {agent.health_check_failures > 0 && (
              <span className="text-red-600 dark:text-red-400 font-semibold">
                ⚠️ {agent.health_check_failures} failures
              </span>
            )}
          </div>
        </div>
        <ExternalLink className="w-4 h-4 text-gray-400 dark:text-gray-500 group-hover:text-blue-600 dark:group-hover:text-blue-400 opacity-0 group-hover:opacity-100 transition-all flex-shrink-0 mt-1" />
      </div>
    </button>
  );
};

export default ClickableAgentCard;

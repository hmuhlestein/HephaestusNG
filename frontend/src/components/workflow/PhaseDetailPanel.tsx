import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Badge } from '@/components/ui/badge';
import { apiService } from '@/services/api';
import PhaseOverview from './PhaseOverview';
import PhasePromptsTab from './PhasePromptsTab';
import PhaseTaskList from './PhaseTaskList';
import PhaseAgentList from './PhaseAgentList';
import PhaseConfigTab from './PhaseConfigTab';

interface PhaseDetailPanelProps {
  phaseId: string;
  onViewAgent?: (agentId: string) => void;
}

type SubTab = 'overview' | 'prompts' | 'tasks' | 'agents' | 'config';

const subTabDefs: { key: SubTab; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'prompts', label: 'Prompts' },
  { key: 'tasks', label: 'Tasks' },
  { key: 'agents', label: 'Agents' },
  { key: 'config', label: 'Config' },
];

export default function PhaseDetailPanel({
  phaseId,
  onViewAgent,
}: PhaseDetailPanelProps) {
  const [activeTab, setActiveTab] = useState<SubTab>('overview');
  const queryClient = useQueryClient();

  // Phase details (YAML endpoint)
  const { data: details, isLoading: detailsLoading, error: detailsError } = useQuery({
    queryKey: ['phase-details', phaseId],
    queryFn: () => apiService.getPhaseYaml(phaseId),
  });

  // Tasks for this phase. Filtered server-side by phase_id -- pulling the
  // 100 most-recently-created tasks SYSTEM-WIDE and filtering client-side
  // by phase_id (the old approach) silently dropped this phase's own
  // tasks off a busy instance once 100 OTHER tasks elsewhere had been
  // created more recently, showing "No tasks in this phase yet" for a
  // phase that actually had tasks. Observed live on a 2,000+ task DB.
  const { data: tasks } = useQuery({
    queryKey: ['phase-tasks', phaseId],
    queryFn: () => apiService.getTasks(0, 100, undefined, undefined, undefined, phaseId),
    refetchInterval: 10000,
  });

  // Agents in this phase
  const { data: agentsData } = useQuery({
    queryKey: ['phase-agents', phaseId],
    queryFn: () => apiService.getPhaseAgents(phaseId),
    refetchInterval: 5000,
  });

  // Terminate agent mutation
  const terminateMutation = useMutation({
    mutationFn: (agentId: string) =>
      apiService.terminateAgent(agentId, 'Terminated from phase detail panel'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['phase-agents', phaseId] });
    },
  });

  const phaseTasks = tasks || [];
  const agents = agentsData?.agents || [];

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800">
      {/* Sub-tabs */}
      <div className="flex items-center gap-1 px-3 pt-3 border-b border-gray-200 dark:border-gray-700">
        {subTabDefs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-3 py-1.5 text-xs font-medium rounded-t transition-colors ${
              activeTab === tab.key
                ? 'bg-white dark:bg-gray-800 border border-b-0 border-gray-200 dark:border-gray-700 text-gray-800 dark:text-gray-100'
                : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
            }`}
          >
            {tab.label}
            {tab.key === 'tasks' && phaseTasks.length > 0 && (
              <Badge variant="outline" className="ml-1 text-[10px]">{phaseTasks.length}</Badge>
            )}
            {tab.key === 'agents' && agents.length > 0 && (
              <Badge variant="outline" className="ml-1 text-[10px] bg-green-50 dark:bg-green-900/40">{agents.length}</Badge>
            )}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="p-3">
        {activeTab === 'overview' && (
          <PhaseOverview
            details={details}
            loading={detailsLoading}
            error={detailsError ? String(detailsError) : null}
          />
        )}
        {activeTab === 'prompts' && (
          <PhasePromptsTab
            phaseId={phaseId}
            details={details}
            activeAgents={agents}
          />
        )}
        {activeTab === 'tasks' && (
          <PhaseTaskList
            tasks={phaseTasks}
            onTerminateAgent={(agentId) => terminateMutation.mutate(agentId)}
          />
        )}
        {activeTab === 'agents' && (
          <PhaseAgentList
            agents={agents}
            onTerminateAgent={(agentId) => terminateMutation.mutate(agentId)}
            onViewAgent={onViewAgent}
          />
        )}
        {activeTab === 'config' && (
          <PhaseConfigTab details={details} loading={detailsLoading} />
        )}
      </div>
    </div>
  );
}

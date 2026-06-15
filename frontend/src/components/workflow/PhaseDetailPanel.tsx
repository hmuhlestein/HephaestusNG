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
}: PhaseDetailPanelProps) {
  const [activeTab, setActiveTab] = useState<SubTab>('overview');
  const queryClient = useQueryClient();

  // Phase details (YAML endpoint)
  const { data: details, isLoading: detailsLoading, error: detailsError } = useQuery({
    queryKey: ['phase-details', phaseId],
    queryFn: () => apiService.getPhaseYaml(phaseId),
  });

  // Tasks for this phase
  const { data: tasks } = useQuery({
    queryKey: ['phase-tasks', phaseId],
    queryFn: () => apiService.getTasks(0, 100, undefined, undefined),
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

  const phaseTasks = tasks?.filter((t: any) => t.phase_id === phaseId) || [];
  const agents = agentsData?.agents || [];

  return (
    <div className="border border-gray-200 rounded-lg bg-white">
      {/* Sub-tabs */}
      <div className="flex items-center gap-1 px-3 pt-3 border-b">
        {subTabDefs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-3 py-1.5 text-xs font-medium rounded-t transition-colors ${
              activeTab === tab.key
                ? 'bg-white border border-b-0 border-gray-200 text-gray-800'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            {tab.label}
            {tab.key === 'tasks' && phaseTasks.length > 0 && (
              <Badge variant="outline" className="ml-1 text-[10px]">{phaseTasks.length}</Badge>
            )}
            {tab.key === 'agents' && agents.length > 0 && (
              <Badge variant="outline" className="ml-1 text-[10px] bg-green-50">{agents.length}</Badge>
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
          />
        )}
        {activeTab === 'config' && (
          <PhaseConfigTab details={details} loading={detailsLoading} />
        )}
      </div>
    </div>
  );
}

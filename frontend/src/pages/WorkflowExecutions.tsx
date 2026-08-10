import { useState, useCallback } from 'react';
import { useWorkflow } from '@/context/WorkflowContext';
import { Workflow, Layers, Rocket } from 'lucide-react';
import LaunchWorkflowModal from '@/components/LaunchWorkflowModal';
import WorkflowCard from '@/components/workflow/WorkflowCard';
import AgentDetailModal from '@/components/AgentDetailModal';

export default function WorkflowExecutions() {
  const {
    executions,
    definitions,
    loading,
    selectedExecutionId,
    selectExecution,
  } = useWorkflow();


  const [showLaunchModal, setShowLaunchModal] = useState(false);
  const [launchDefinitionId, setLaunchDefinitionId] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'active'>('active');
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 12;

  // Mutual exclusion state
  const [expandedWorkflowId, setExpandedWorkflowId] = useState<string | null>(null);
  const [expandedPhaseId, setExpandedPhaseId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  const handleWorkflowClick = useCallback((workflowId: string) => {
    // Invariant: expanding a workflow collapses any expanded phase
    setExpandedPhaseId(null);
    setExpandedWorkflowId((prev) => (prev === workflowId ? null : workflowId));
  }, []);

  const handlePhaseClick = useCallback((phaseId: string) => {
    setExpandedPhaseId((prev) => (prev === phaseId ? null : phaseId));
  }, []);


  const activeExecutions = executions.filter((e) => e.status === 'active');
  const filteredExecutions = filter === 'active' ? activeExecutions : executions;
  const totalPages = Math.ceil(filteredExecutions.length / PAGE_SIZE);
  const paginatedExecutions = filteredExecutions.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // Reset page when filter changes
  const handleFilterChange = (newFilter: 'all' | 'active') => {
    setFilter(newFilter);
    setPage(1);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-800 dark:text-gray-200 flex items-center">
            <Workflow className="w-8 h-8 mr-3 text-blue-600" />
            Workflows
          </h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">Manage workflow definitions and executions</p>
        </div>
        <button
          onClick={() => setShowLaunchModal(true)}
          className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg flex items-center gap-2 transition-colors"
        >
          <Rocket className="w-4 h-4" />
          Launch Workflow
        </button>
      </div>

      {/* Workflow Definitions Section */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-4">
        <h2 className="text-lg font-semibold text-gray-800 mb-3 flex items-center gap-2">
          <Layers className="w-5 h-5 text-purple-600" />
          Loaded Workflow Definitions ({definitions.length})
        </h2>
        {definitions.length === 0 ? (
          <p className="text-gray-500 dark:text-gray-400 text-sm">No workflow definitions loaded</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {definitions.map((def) => (
              <div
                key={def.id}
                onClick={() => { setLaunchDefinitionId(def.id); setShowLaunchModal(true); }}
                className="bg-purple-50 border border-purple-200 rounded-lg p-3 cursor-pointer hover:shadow-md hover:border-purple-400 transition-all"
              >
                <div className="font-medium text-gray-800 dark:text-gray-200">{def.name}</div>
                <div className="text-sm text-gray-600 dark:text-gray-400 line-clamp-2">{def.description}</div>
                <div className="text-xs text-purple-600 mt-1">
                  {def.phases_count} phases • Click to launch →
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-2">
        <button
          onClick={() => handleFilterChange('active')}
          className={`px-4 py-2 rounded-lg transition-colors ${
            filter === 'active'
              ? 'bg-blue-600 text-white'
              : 'bg-white text-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700 border border-gray-200 dark:border-gray-700'
          }`}
        >
          Active ({activeExecutions.length})
        </button>
        <button
          onClick={() => handleFilterChange('all')}
          className={`px-4 py-2 rounded-lg transition-colors ${
            filter === 'all'
              ? 'bg-blue-600 text-white'
              : 'bg-white text-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700 border border-gray-200 dark:border-gray-700'
          }`}
        >
          All ({executions.length})
        </button>
      </div>

      {/* Workflow Grid */}
      {paginatedExecutions.length > 0 ? (
        <div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {paginatedExecutions.map((execution) => (
              <WorkflowCard
                key={execution.id}
                execution={execution}
                isExpanded={expandedWorkflowId === execution.id}
                onToggle={() => handleWorkflowClick(execution.id)}
                expandedPhaseId={expandedPhaseId}
                onTogglePhase={handlePhaseClick}
                isSelected={selectedExecutionId === execution.id}
                onViewAgent={setSelectedAgentId}
              />
            ))}
          </div>
          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex justify-center items-center gap-2 mt-6">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="px-3 py-1 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                ← Prev
              </button>
              <span className="text-sm text-gray-600 dark:text-gray-400 dark:text-gray-500">
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="px-3 py-1 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Next →
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="text-center text-gray-500 py-8">
          {filter === 'active' ? 'No active workflows' : 'No workflows found'}
        </div>
      )}

      {/* Empty state */}
      {executions.length === 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md p-12 text-center">
          <Workflow className="w-16 h-16 mx-auto mb-4 text-gray-300" />
          <div className="text-gray-500 dark:text-gray-400 mb-4">No workflow executions yet</div>
          <button
            onClick={() => setShowLaunchModal(true)}
            className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors"
          >
            Start Your First Workflow
          </button>
        </div>
      )}

      {/* Launch Workflow Modal */}
      <LaunchWorkflowModal
        open={showLaunchModal}
        onClose={() => { setShowLaunchModal(false); setLaunchDefinitionId(null); }}
        onLaunch={(workflowId) => {
          selectExecution(workflowId);
          setLaunchDefinitionId(null);
        }}
        initialDefinitionId={launchDefinitionId ?? undefined}
      />

      {/* Agent Detail Modal */}
      <AgentDetailModal
        agentId={selectedAgentId}
        onClose={() => setSelectedAgentId(null)}
      />
    </div>
  );
}

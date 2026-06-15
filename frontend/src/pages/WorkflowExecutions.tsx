import { useState, useCallback } from 'react';
import { useWorkflow } from '@/context/WorkflowContext';
import { Workflow, Layers, Rocket } from 'lucide-react';
import LaunchWorkflowModal from '@/components/LaunchWorkflowModal';
import WorkflowCard from '@/components/workflow/WorkflowCard';

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
  const [filter, setFilter] = useState<'all' | 'active'>('all');

  // Mutual exclusion state
  const [expandedWorkflowId, setExpandedWorkflowId] = useState<string | null>(null);
  const [expandedPhaseId, setExpandedPhaseId] = useState<string | null>(null);

  const handleWorkflowClick = useCallback((workflowId: string) => {
    // Invariant: expanding a workflow collapses any expanded phase
    setExpandedPhaseId(null);
    setExpandedWorkflowId((prev) => (prev === workflowId ? null : workflowId));
  }, []);

  const handlePhaseClick = useCallback((phaseId: string) => {
    setExpandedPhaseId((prev) => (prev === phaseId ? null : phaseId));
  }, []);


  const activeExecutions = executions.filter((e) => e.status === 'active');
  const inactiveExecutions = executions.filter((e) => e.status !== 'active');

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
          <h1 className="text-3xl font-bold text-gray-800 flex items-center">
            <Workflow className="w-8 h-8 mr-3 text-blue-600" />
            Workflows
          </h1>
          <p className="text-gray-600 mt-1">Manage workflow definitions and executions</p>
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
      <div className="bg-white rounded-lg shadow-md p-4">
        <h2 className="text-lg font-semibold text-gray-800 mb-3 flex items-center gap-2">
          <Layers className="w-5 h-5 text-purple-600" />
          Loaded Workflow Definitions ({definitions.length})
        </h2>
        {definitions.length === 0 ? (
          <p className="text-gray-500 text-sm">No workflow definitions loaded</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {definitions.map((def) => (
              <div
                key={def.id}
                onClick={() => { setLaunchDefinitionId(def.id); setShowLaunchModal(true); }}
                className="bg-purple-50 border border-purple-200 rounded-lg p-3 cursor-pointer hover:shadow-md hover:border-purple-400 transition-all"
              >
                <div className="font-medium text-gray-800">{def.name}</div>
                <div className="text-sm text-gray-600 line-clamp-2">{def.description}</div>
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
          onClick={() => setFilter('all')}
          className={`px-4 py-2 rounded-lg transition-colors ${
            filter === 'all'
              ? 'bg-blue-600 text-white'
              : 'bg-white text-gray-700 hover:bg-gray-100 border border-gray-200'
          }`}
        >
          All ({executions.length})
        </button>
        <button
          onClick={() => setFilter('active')}
          className={`px-4 py-2 rounded-lg transition-colors ${
            filter === 'active'
              ? 'bg-blue-600 text-white'
              : 'bg-white text-gray-700 hover:bg-gray-100 border border-gray-200'
          }`}
        >
          Active ({activeExecutions.length})
        </button>
      </div>

      {/* Active Section */}
      {activeExecutions.length > 0 && (filter === 'all' || filter === 'active') && (
        <div>
          <h2 className="text-lg font-semibold text-gray-800 mb-4 flex items-center gap-2">
            <span className="w-2 h-2 bg-green-500 rounded-full" />
            Active ({activeExecutions.length})
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {activeExecutions.map((execution) => (
              <WorkflowCard
                key={execution.id}
                execution={execution}
                isExpanded={expandedWorkflowId === execution.id}
                onToggle={() => handleWorkflowClick(execution.id)}
                expandedPhaseId={expandedPhaseId}
                onTogglePhase={handlePhaseClick}
                isSelected={selectedExecutionId === execution.id}
              />
            ))}
          </div>
        </div>
      )}

      {/* Inactive Section */}
      {inactiveExecutions.length > 0 && filter === 'all' && (
        <div>
          <h2 className="text-lg font-semibold text-gray-500 mb-4">
            Completed/Failed ({inactiveExecutions.length})
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {inactiveExecutions.map((execution) => (
              <WorkflowCard
                key={execution.id}
                execution={execution}
                isExpanded={expandedWorkflowId === execution.id}
                onToggle={() => handleWorkflowClick(execution.id)}
                expandedPhaseId={expandedPhaseId}
                onTogglePhase={handlePhaseClick}
                isSelected={selectedExecutionId === execution.id}
              />
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {executions.length === 0 && (
        <div className="bg-white rounded-lg shadow-md p-12 text-center">
          <Workflow className="w-16 h-16 mx-auto mb-4 text-gray-300" />
          <div className="text-gray-500 mb-4">No workflow executions yet</div>
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
    </div>
  );
}

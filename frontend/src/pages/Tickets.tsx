import React, { useState, useEffect, useRef } from 'react';
import { Plus, LayoutGrid, Search, BarChart3, Loader2, Network, ChevronDown, Filter } from 'lucide-react';
import KanbanBoard from '@/components/tickets/KanbanBoard';
import TicketSearch from '@/components/tickets/TicketSearch';
import TicketStats from '@/components/tickets/TicketStats';
import TicketGraph from '@/components/tickets/TicketGraph';
import PendingReviewIndicator from '@/components/tickets/PendingReviewIndicator';
import CreateTicketModal from '@/components/tickets/CreateTicketModal';
import { useWorkflow } from '@/context/WorkflowContext';
import { useProject } from '@/context/ProjectContext';

const Tickets: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'kanban' | 'search' | 'stats' | 'graph'>('kanban');
  const [searchTabTag, setSearchTabTag] = useState<string | null>(null);
  const [showWorkflowDropdown, setShowWorkflowDropdown] = useState(false);
  const [showStatusFilter, setShowStatusFilter] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [showCreateTicket, setShowCreateTicket] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const statusFilterRef = useRef<HTMLDivElement>(null);

  const { executions, selectedExecutionId, selectExecution, loading } = useWorkflow();
  const { selectedProject } = useProject();
  const projectId = selectedProject?.id;

  // Close dropdowns when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowWorkflowDropdown(false);
      }
      if (statusFilterRef.current && !statusFilterRef.current.contains(event.target as Node)) {
        setShowStatusFilter(false);
      }
    };

    if (showWorkflowDropdown || showStatusFilter) {
      document.addEventListener('mousedown', handleClickOutside);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [showWorkflowDropdown, showStatusFilter]);

  // Get current selected workflow
  const selectedWorkflow = executions.find(e => e.id === selectedExecutionId);
  const selectedWorkflowId = selectedExecutionId;

  // Filter executions by status
  const filteredExecutions = statusFilter === 'all'
    ? executions
    : executions.filter(e => e.status === statusFilter);

  // Unique statuses from executions
  const availableStatuses = [...new Set(executions.map(e => e.status))].sort();

  // Clear selected execution if it's filtered out
  useEffect(() => {
    if (selectedExecutionId && statusFilter !== 'all') {
      const isSelected = filteredExecutions.some(e => e.id === selectedExecutionId);
      if (!isSelected) {
        selectExecution(null);
      }
    }
  }, [statusFilter, filteredExecutions, selectedExecutionId, selectExecution]);

  const handleNewTicket = () => {
    setShowCreateTicket(true);
  };

  const handleNavigateToSearchTab = (tag: string) => {
    setSearchTabTag(tag);
    setActiveTab('search');
  };

  const tabs = [
    { id: 'kanban', label: 'Kanban Board', icon: LayoutGrid },
    { id: 'search', label: 'Search', icon: Search },
    { id: 'stats', label: 'Statistics', icon: BarChart3 },
    { id: 'graph', label: 'Graph', icon: Network },
  ] as const;

  // Show loading state while fetching workflow
  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-12 h-12 text-blue-600 animate-spin mx-auto mb-4" />
          <p className="text-gray-600">Loading workflow...</p>
        </div>
      </div>
    );
  }

  // Show error if no workflow found
  if (!selectedWorkflowId) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center text-gray-500">
          <p className="text-lg font-semibold mb-2">No workflow found</p>
          <p className="text-sm">Please create a workflow first</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Ticket Tracking</h1>
          <p className="text-sm text-gray-600 mt-1">
            Manage and track tickets across your workflow
          </p>
        </div>

        <div className="flex items-center space-x-3">
          {/* Status Filter Dropdown */}
          <div className="relative" ref={statusFilterRef}>
            <button
              onClick={() => setShowStatusFilter(!showStatusFilter)}
              className="flex items-center px-3 py-2 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors shadow-sm text-sm"
            >
              <Filter className="w-4 h-4 mr-1.5 text-gray-500" />
              <span className="text-gray-700 capitalize">{statusFilter === 'all' ? 'All States' : statusFilter}</span>
              <ChevronDown className={`w-3.5 h-3.5 ml-1.5 text-gray-400 transition-transform ${showStatusFilter ? 'rotate-180' : ''}`} />
            </button>

            {showStatusFilter && (
              <div className="absolute right-0 mt-2 w-40 bg-white border border-gray-200 rounded-lg shadow-lg z-50">
                <button
                  onClick={() => { setStatusFilter('all'); setShowStatusFilter(false); }}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 rounded-t-lg ${statusFilter === 'all' ? 'bg-violet-50 text-violet-700 font-medium' : 'text-gray-700'}`}
                >
                  All States
                </button>
                {availableStatuses.map((status, i) => (
                  <button
                    key={status}
                    onClick={() => { setStatusFilter(status); setShowStatusFilter(false); }}
                    className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 capitalize ${statusFilter === status ? 'bg-violet-50 text-violet-700 font-medium' : 'text-gray-700'} ${i === availableStatuses.length - 1 ? 'rounded-b-lg' : ''}`}
                  >
                    <span className={`inline-block w-2 h-2 rounded-full mr-2 ${
                      status === 'active' ? 'bg-green-500' :
                      status === 'completed' ? 'bg-blue-500' :
                      status === 'failed' ? 'bg-red-500' :
                      'bg-gray-400'
                    }`} />
                    {status}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Workflow Selector Dropdown */}
          <div className="relative" ref={dropdownRef}>
            <button
              onClick={() => setShowWorkflowDropdown(!showWorkflowDropdown)}
              className="flex items-center px-4 py-2 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors shadow-sm min-w-[200px]"
            >
              <span className="flex-1 text-left text-sm text-gray-700 truncate">
                {!selectedExecutionId ? `📁 ${selectedProject?.name || 'Project'} Level` : selectedProject?.name || selectedWorkflow?.definition_name || 'Select Workflow'}
              </span>
              <ChevronDown className={`w-4 h-4 ml-2 text-gray-500 transition-transform ${showWorkflowDropdown ? 'rotate-180' : ''}`} />
            </button>

            {showWorkflowDropdown && (
              <div className="absolute right-0 mt-2 w-72 bg-white border border-gray-200 rounded-lg shadow-lg z-50 max-h-64 overflow-y-auto">
                {/* Project Level option */}
                <button
                  onClick={() => {
                    selectExecution(null);
                    setShowWorkflowDropdown(false);
                  }}
                  className={`w-full text-left px-4 py-3 hover:bg-gray-50 border-b border-gray-100 ${
                    !selectedExecutionId ? 'bg-violet-50' : ''
                  }`}
                >
                  <div className="text-sm font-medium text-violet-800">
                    📁 Project Level
                  </div>
                  <div className="text-xs text-gray-500 mt-1">
                    All tickets in {selectedProject?.name || 'project'}
                  </div>
                </button>
                
                {filteredExecutions.length === 0 ? (
                  <div className="px-4 py-3 text-sm text-gray-500">
                    {executions.length === 0 ? 'No workflows available' : `No ${statusFilter} workflows`}
                  </div>
                ) : (
                  filteredExecutions.map((execution) => (
                    <button
                      key={execution.id}
                      onClick={() => {
                        selectExecution(execution.id);
                        setShowWorkflowDropdown(false);
                      }}
                      className={`w-full text-left px-4 py-3 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 ${
                        execution.id === selectedExecutionId ? 'bg-blue-50' : ''
                      }`}
                    >
                      <div className="text-sm font-medium text-gray-800 truncate">
                        {execution.definition_name || execution.description?.split('\n')[0] || 'Unnamed Workflow'}
                      </div>
                      <div className="text-xs text-gray-400 mt-1">
                        {execution.id.slice(0, 12)}...
                      </div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Pending Review Indicator */}
          <PendingReviewIndicator />

          <button
            onClick={handleNewTicket}
            className="flex items-center px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors shadow-sm"
          >
            <Plus className="w-5 h-5 mr-2" />
            New Ticket
          </button>
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="border-b border-gray-200 mb-6">
        <nav className="flex space-x-8">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`
                flex items-center px-1 py-4 border-b-2 font-medium text-sm transition-colors
                ${
                  activeTab === id
                    ? 'border-blue-600 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }
              `}
            >
              <Icon className="w-5 h-5 mr-2" />
              {label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-auto">
        {activeTab === 'kanban' && (
          <KanbanBoard
            workflowId={selectedWorkflowId}
            projectId={projectId}
            onNavigateToSearchTab={handleNavigateToSearchTab}
          />
        )}
        {activeTab === 'search' && (
          <TicketSearch
            workflowId={selectedWorkflowId}
            initialTag={searchTabTag}
            onTagUsed={() => setSearchTabTag(null)}
          />
        )}
        {activeTab === 'stats' && (
          <TicketStats workflowId={selectedWorkflowId} />
        )}
        {activeTab === 'graph' && (
          <TicketGraph
            workflowId={selectedWorkflowId}
            onNavigateToSearchTab={handleNavigateToSearchTab}
          />
        )}
      </div>
      {/* Create Ticket Modal */}
      <CreateTicketModal
        isOpen={showCreateTicket}
        onClose={() => setShowCreateTicket(false)}
        workflowId={selectedWorkflowId}
        projectId={projectId}
      />
    </div>
  );
};

export default Tickets;

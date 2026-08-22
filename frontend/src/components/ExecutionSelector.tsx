import React, { useState, useRef, useEffect } from 'react';
import { useWorkflow } from '@/context/WorkflowContext';
import { ChevronDown, Workflow, Activity } from 'lucide-react';

export const ExecutionSelector: React.FC = () => {
  const { executions, selectedExecutionId, selectedExecution, selectExecution, loading } = useWorkflow();
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowDropdown(false);
      }
    };

    if (showDropdown) {
      document.addEventListener('mousedown', handleClickOutside);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [showDropdown]);

  if (loading) {
    return (
      <div className="flex items-center text-sm text-gray-400 dark:text-gray-500">
        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-gray-400 dark:border-gray-500 mr-2"></div>
        Loading...
      </div>
    );
  }

  if (executions.length === 0) {
    return (
      <div className="flex items-center text-sm text-gray-400 dark:text-gray-500">
        <Workflow className="w-4 h-4 mr-2" />
        No workflows available
      </div>
    );
  }

  // Separate active and inactive executions
  const activeExecutions = executions.filter(e => e.status === 'active');
  const inactiveExecutions = executions.filter(e => e.status !== 'active');

  return (
    <div className="flex items-center gap-3">
      {selectedExecution && selectedExecution.status === 'active' && (
        <span className="flex items-center px-2 py-0.5 bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300 rounded-full text-xs">
          <Activity className="w-3 h-3 mr-1" />
          {selectedExecution.stats?.active_tasks || 0} active
        </span>
      )}

      <div className="relative" ref={dropdownRef}>
        <button
          onClick={() => setShowDropdown(!showDropdown)}
          className="flex items-center px-4 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors shadow-sm min-w-[200px]"
        >
          <Workflow className="w-4 h-4 mr-2 text-gray-500 dark:text-gray-400" />
          <span className="flex-1 text-left text-sm text-gray-700 dark:text-gray-300 truncate">
            {selectedExecution?.definition_name || selectedExecution?.description?.split('\n')[0] || 'Select Workflow'}
          </span>
          <ChevronDown className={`w-4 h-4 ml-2 text-gray-500 dark:text-gray-400 transition-transform ${showDropdown ? 'rotate-180' : ''}`} />
        </button>

        {showDropdown && (
          <div className="absolute right-0 mt-2 w-80 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 max-h-80 overflow-y-auto">
            {/* Active Executions */}
            {activeExecutions.length > 0 && (
              <>
                <div className="px-3 py-2 text-xs font-semibold text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
                  Active ({activeExecutions.length})
                </div>
                {activeExecutions.map((execution) => (
                  <button
                    key={execution.id}
                    onClick={() => {
                      selectExecution(execution.id);
                      setShowDropdown(false);
                    }}
                    className={`w-full text-left px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700 border-b border-gray-100 dark:border-gray-700 last:border-b-0 ${
                      execution.id === selectedExecutionId ? 'bg-blue-50 dark:bg-blue-900/30' : ''
                    }`}
                  >
                    <div className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                      {execution.definition_name || execution.description?.split('\n')[0] || 'Unnamed Workflow'}
                    </div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-2 mt-1">
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">
                        {execution.status}
                      </span>
                      <span className="text-gray-400 dark:text-gray-500">{execution.definition_name}</span>
                      <span className="text-gray-400 dark:text-gray-500">•</span>
                      <span className="truncate">{execution.stats?.active_tasks || 0} tasks</span>
                    </div>
                  </button>
                ))}
              </>
            )}

            {/* Inactive Executions */}
            {inactiveExecutions.length > 0 && (
              <>
                <div className="px-3 py-2 text-xs font-semibold text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
                  Completed/Failed ({inactiveExecutions.length})
                </div>
                {inactiveExecutions.map((execution) => (
                  <button
                    key={execution.id}
                    onClick={() => {
                      selectExecution(execution.id);
                      setShowDropdown(false);
                    }}
                    className={`w-full text-left px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700 border-b border-gray-100 dark:border-gray-700 last:border-b-0 ${
                      execution.id === selectedExecutionId ? 'bg-blue-50 dark:bg-blue-900/30' : ''
                    }`}
                  >
                    <div className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                      {execution.definition_name || execution.description?.split('\n')[0] || 'Unnamed Workflow'}
                    </div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-2 mt-1">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                        execution.status === 'completed' ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300' :
                        execution.status === 'failed' ? 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-400' :
                        'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300'
                      }`}>
                        {execution.status}
                      </span>
                      <span className="text-gray-400 dark:text-gray-500">{execution.definition_name}</span>
                    </div>
                  </button>
                ))}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default ExecutionSelector;

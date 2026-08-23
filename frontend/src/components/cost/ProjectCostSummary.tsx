import React from 'react';
import { DollarSign, AlertTriangle, TrendingUp, Settings } from 'lucide-react';
import CostDisplay from './CostDisplay';

interface ProjectCostSummaryProps {
  projectId: string;
  projectName: string;
  costTotal: number;
  costLimit?: number | null;
  isOverBudget: boolean;
  onConfigureBudget?: () => void;
  className?: string;
}

/**
 * Displays project cost summary with optional budget configuration.
 */
const ProjectCostSummary: React.FC<ProjectCostSummaryProps> = ({
  projectId: _projectId,
  projectName,
  costTotal,
  costLimit,
  isOverBudget,
  onConfigureBudget,
  className = '',
}) => {
  const remaining = costLimit != null ? Math.max(0, costLimit - costTotal) : null;

  return (
    <div
      className={`border rounded-lg p-4 ${
        isOverBudget ? 'border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/20' : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800'
      } ${className}`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <DollarSign className="w-5 h-5 text-gray-600 dark:text-gray-400" />
          <h3 className="font-medium text-gray-900 dark:text-gray-100">{projectName}</h3>
        </div>
        {onConfigureBudget && (
          <button
            onClick={onConfigureBudget}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
            title="Configure budget"
          >
            <Settings className="w-4 h-4 text-gray-500 dark:text-gray-400" />
          </button>
        )}
      </div>

      <CostDisplay
        currentCost={costTotal}
        costLimit={costLimit}
        className="mb-2"
      />

      {costLimit != null && (
        <div className="flex items-center gap-2 text-sm">
          {isOverBudget ? (
            <>
              <AlertTriangle className="w-4 h-4 text-red-500 dark:text-red-400" />
              <span className="text-red-600 dark:text-red-400 font-medium">Over budget</span>
            </>
          ) : (
            <>
              <TrendingUp className="w-4 h-4 text-green-500 dark:text-green-400" />
              <span className="text-gray-600 dark:text-gray-400">
                ${remaining?.toFixed(2)} remaining
              </span>
            </>
          )}
        </div>
      )}

      {costLimit == null && (
        <button
          onClick={onConfigureBudget}
          className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 mt-1"
        >
          Set budget limit
        </button>
      )}
    </div>
  );
};

export default ProjectCostSummary;

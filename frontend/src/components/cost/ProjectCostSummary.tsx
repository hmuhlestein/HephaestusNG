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
        isOverBudget ? 'border-red-300 bg-red-50' : 'border-gray-200 bg-white'
      } ${className}`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <DollarSign className="w-5 h-5 text-gray-600" />
          <h3 className="font-medium text-gray-900">{projectName}</h3>
        </div>
        {onConfigureBudget && (
          <button
            onClick={onConfigureBudget}
            className="p-1 hover:bg-gray-100 rounded"
            title="Configure budget"
          >
            <Settings className="w-4 h-4 text-gray-500" />
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
              <AlertTriangle className="w-4 h-4 text-red-500" />
              <span className="text-red-600 font-medium">Over budget</span>
            </>
          ) : (
            <>
              <TrendingUp className="w-4 h-4 text-green-500" />
              <span className="text-gray-600">
                ${remaining?.toFixed(2)} remaining
              </span>
            </>
          )}
        </div>
      )}

      {costLimit == null && (
        <button
          onClick={onConfigureBudget}
          className="text-sm text-blue-600 hover:text-blue-800 mt-1"
        >
          Set budget limit
        </button>
      )}
    </div>
  );
};

export default ProjectCostSummary;

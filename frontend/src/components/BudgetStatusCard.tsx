import React from 'react';
import { DollarSign, AlertTriangle } from 'lucide-react';

interface BudgetStatusCardProps {
  projectId: string;
  costTotalUsd?: number | null;
  costLimitUsd?: number | null;
  onConfigureBudget?: () => void;
}

const BudgetStatusCard: React.FC<BudgetStatusCardProps> = ({
  projectId,
  costTotalUsd = 0,
  costLimitUsd = null,
  onConfigureBudget,
}) => {
  const isOverBudget = costLimitUsd !== null && (costTotalUsd || 0) >= costLimitUsd;
  const isNearBudget = costLimitUsd !== null && (costTotalUsd || 0) >= costLimitUsd * 0.9 && !isOverBudget;

  return (
    <div className={`rounded-lg border p-4 ${
      isOverBudget
        ? 'bg-red-50 border-red-200'
        : isNearBudget
          ? 'bg-yellow-50 border-yellow-200'
          : 'bg-white border-gray-200'
    }`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <DollarSign className={`w-5 h-5 ${
            isOverBudget ? 'text-red-600' : isNearBudget ? 'text-yellow-600' : 'text-gray-600'
          }`} />
          <h3 className="text-sm font-semibold text-gray-800">Budget Status</h3>
        </div>
        {onConfigureBudget && (
          <button
            onClick={onConfigureBudget}
            className="text-xs text-violet-600 hover:text-violet-800"
          >
            Configure
          </button>
        )}
      </div>

      <div className="mt-3">
        {costLimitUsd !== null ? (
          <>
            <div className="flex items-baseline gap-1">
              <span className={`text-2xl font-bold ${
                isOverBudget ? 'text-red-700' : isNearBudget ? 'text-yellow-700' : 'text-gray-900'
              }`}>
                ${(costTotalUsd || 0).toFixed(2)}
              </span>
              <span className="text-sm text-gray-500">/ ${costLimitUsd.toFixed(2)}</span>
            </div>

            {/* Progress bar */}
            <div className="mt-2 w-full bg-gray-200 rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all ${
                  isOverBudget ? 'bg-red-600' : isNearBudget ? 'bg-yellow-500' : 'bg-violet-600'
                }`}
                style={{ width: `${Math.min(((costTotalUsd || 0) / costLimitUsd) * 100, 100)}%` }}
              />
            </div>

            {isOverBudget && (
              <div className="mt-2 flex items-center gap-1 text-xs text-red-600">
                <AlertTriangle className="w-3 h-3" />
                <span>Budget limit exceeded - pipeline paused</span>
              </div>
            )}
            {isNearBudget && (
              <div className="mt-2 flex items-center gap-1 text-xs text-yellow-600">
                <AlertTriangle className="w-3 h-3" />
                <span>Approaching budget limit</span>
              </div>
            )}
          </>
        ) : (
          <div className="text-sm text-gray-500">
            <span className="text-lg font-medium text-gray-700">${(costTotalUsd || 0).toFixed(2)}</span>
            <span className="ml-1">spent</span>
            <p className="text-xs text-gray-400 mt-1">No budget limit set</p>
          </div>
        )}
      </div>
    </div>
  );
};

export default BudgetStatusCard;

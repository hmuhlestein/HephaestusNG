import React from 'react';
import { DollarSign, TrendingUp, AlertTriangle } from 'lucide-react';

interface CostDisplayProps {
  currentCost: number;
  costLimit?: number | null;
  className?: string;
  showProgress?: boolean;
}

/**
 * Displays cost information with optional budget progress indicator.
 * 
 * Shows current spend, optional limit bar, and over-budget warning.
 */
const CostDisplay: React.FC<CostDisplayProps> = ({
  currentCost,
  costLimit,
  className = '',
  showProgress = true,
}) => {
  const isOverBudget = costLimit != null && currentCost >= costLimit;
  const progressPercent = costLimit != null ? Math.min((currentCost / costLimit) * 100, 100) : null;

  const formatCost = (cost: number): string => {
    if (cost >= 1000) {
      return `$${(cost / 1000).toFixed(1)}k`;
    }
    return `$${cost.toFixed(2)}`;
  };

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <DollarSign className="w-4 h-4 text-gray-500" />
      <span className="font-mono text-sm font-medium">
        {formatCost(currentCost)}
      </span>
      {costLimit != null && (
        <span className="text-xs text-gray-500">
          / {formatCost(costLimit)}
        </span>
      )}
      {isOverBudget && (
        <AlertTriangle className="w-4 h-4 text-red-500" />
      )}
      {showProgress && progressPercent != null && (
        <div className="w-16 h-1.5 bg-gray-200 rounded-full overflow-hidden ml-2">
          <div
            className={`h-full rounded-full transition-all ${
              isOverBudget ? 'bg-red-500' : progressPercent > 80 ? 'bg-yellow-500' : 'bg-green-500'
            }`}
            style={{ width: `${Math.min(progressPercent, 100)}%` }}
          />
        </div>
      )}
    </div>
  );
};

export default CostDisplay;

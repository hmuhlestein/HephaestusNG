import React from 'react';
import { AlertCircle } from 'lucide-react';

interface BudgetPausedLabelProps {
  className?: string;
}

/**
 * Label component for workflows paused due to budget limits.
 * Shows "Paused: budget limit reached" with warning icon.
 */
const BudgetPausedLabel: React.FC<BudgetPausedLabelProps> = ({
  className = '',
}) => {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-red-100 text-red-800 ${className}`}
      title="Workflow paused: project budget limit reached"
    >
      <AlertCircle className="w-3 h-3" />
      Paused: budget limit reached
    </span>
  );
};

export default BudgetPausedLabel;

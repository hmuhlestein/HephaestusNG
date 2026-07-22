import React from 'react';
import { DollarSign } from 'lucide-react';

interface FeatureCostBadgeProps {
  cost: number;
  className?: string;
}

/**
 * Small badge showing cost for a feature.
 * Displays a dollar amount with color coding based on cost.
 */
const FeatureCostBadge: React.FC<FeatureCostBadgeProps> = ({
  cost,
  className = '',
}) => {
  if (cost <= 0) return null;

  const formatCost = (c: number): string => {
    if (c >= 100) return `$${Math.round(c)}`;
    if (c >= 10) return `$${c.toFixed(1)}`;
    return `$${c.toFixed(2)}`;
  };

  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium ${
        cost >= 5 ? 'bg-red-100 text-red-800' : 'bg-blue-100 text-blue-800'
      } ${className}`}
    >
      <DollarSign className="w-3 h-3" />
      {formatCost(cost)}
    </span>
  );
};

export default FeatureCostBadge;

import React from 'react';

interface FeatureCostBadgeProps {
  cost: number;
  className?: string;
}

/**
 * Small badge showing cost for a feature.
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
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-800 ${className}`}
    >
      {formatCost(cost)}
    </span>
  );
};

export default FeatureCostBadge;

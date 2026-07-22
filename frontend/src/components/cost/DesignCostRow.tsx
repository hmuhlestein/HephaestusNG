import React from 'react';
import { DollarSign } from 'lucide-react';
import CostDisplay from './CostDisplay';

interface DesignCostRowProps {
  designId: string;
  designName: string;
  costTotal: number;
  className?: string;
}

/**
 * Row displaying cost information for a design.
 * Shows design name and cost with a link to detailed breakdown.
 */
const DesignCostRow: React.FC<DesignCostRowProps> = ({
  designId,
  designName,
  costTotal,
  className = '',
}) => {
  if (costTotal <= 0) return null;

  return (
    <div
      className={`flex items-center justify-between p-2 hover:bg-gray-50 rounded ${className}`}
    >
      <span className="text-sm text-gray-700 truncate flex-1 mr-2">
        {designName}
      </span>
      <CostDisplay currentCost={costTotal} showProgress={false} />
    </div>
  );
};

export default DesignCostRow;

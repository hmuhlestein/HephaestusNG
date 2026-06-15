import PhaseCard from './PhaseCard';
import PhaseDetailPanel from './PhaseDetailPanel';
import { Layers } from 'lucide-react';

interface PhaseListProps {
  phases: any[];
  expandedPhaseId: string | null;
  onTogglePhase: (phaseId: string) => void;
}

export default function PhaseList({
  phases,
  expandedPhaseId,
  onTogglePhase,
}: PhaseListProps) {
  if (!phases || phases.length === 0) {
    return (
      <div className="text-center py-6 text-gray-500 text-sm">
        <Layers className="w-6 h-6 mx-auto mb-2 text-gray-300" />
        No phases in this workflow.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {phases.map((phase) => (
        <div key={phase.id}>
          <PhaseCard
            phase={phase}
            isExpanded={expandedPhaseId === phase.id}
            onToggle={() => onTogglePhase(phase.id)}
          />
          {expandedPhaseId === phase.id && (
            <div className="mt-2 ml-4">
              <PhaseDetailPanel phaseId={phase.id} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

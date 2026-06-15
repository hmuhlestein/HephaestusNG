import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { RotateCcw, Check } from 'lucide-react';
import { apiService } from '@/services/api';
import type { PhasePromptVersion } from '@/types';

interface PromptVersionHistoryProps {
  phaseId: string;
  versions: PhasePromptVersion[];
}

const statusBadge: Record<string, { color: string; label: string }> = {
  active: { color: 'bg-green-100 text-green-700', label: 'active' },
  draft: { color: 'bg-yellow-100 text-yellow-700', label: 'draft' },
  archived: { color: 'bg-gray-100 text-gray-500', label: 'archived' },
};

export default function PromptVersionHistory({
  phaseId,
  versions,
}: PromptVersionHistoryProps) {
  const queryClient = useQueryClient();

  const restoreMutation = useMutation({
    mutationFn: (version: number) =>
      apiService.restorePhasePromptVersion(phaseId, version),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['phase-prompt-versions', phaseId] });
      queryClient.invalidateQueries({ queryKey: ['phase-details', phaseId] });
    },
  });

  const [confirmingRestore, setConfirmingRestore] = useState<number | null>(null);

  const handleRestoreClick = (version: number) => {
    if (confirmingRestore === version) {
      restoreMutation.mutate(version);
      setConfirmingRestore(null);
    } else {
      setConfirmingRestore(version);
      // Auto-cancel after 3 seconds
      setTimeout(() => setConfirmingRestore((prev) => (prev === version ? null : prev)), 3000);
    }
  };

  return (
    <div className="space-y-1 max-h-[200px] overflow-y-auto">
      {versions.map((v) => {
        const badge = statusBadge[v.status] || statusBadge.archived;
        const isConfirming = confirmingRestore === v.version;
        return (
          <div
            key={v.version}
            className="flex items-center gap-2 text-xs py-1 px-2 rounded hover:bg-gray-50"
          >
            <Badge variant="outline" className={`text-[10px] ${badge.color}`}>
              v{v.version}
            </Badge>
            {v.status === 'active' && (
              <Check className="w-3 h-3 text-green-500" />
            )}
            <span className="text-gray-500 flex-1">
              {v.created_by && <span className="text-gray-600">{v.created_by}</span>}
              {v.created_at && (
                <span className="ml-1 text-gray-400">
                  {new Date(v.created_at).toLocaleString()}
                </span>
              )}
            </span>
            {v.change_summary && (
              <span className="text-gray-400 truncate max-w-[150px]">
                {v.change_summary}
              </span>
            )}
            <Button
              variant={isConfirming ? 'destructive' : 'ghost'}
              size="sm"
              className={`h-5 w-5 p-0 ${isConfirming ? 'text-white' : ''}`}
              title={isConfirming ? 'Click again to confirm restore' : 'Restore this version'}
              onClick={() => handleRestoreClick(v.version)}
              disabled={restoreMutation.isPending}
            >
              <RotateCcw className="w-3 h-3" />
            </Button>
          </div>
        );
      })}
    </div>
  );
}

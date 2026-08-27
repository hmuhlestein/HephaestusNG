import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { FileText } from 'lucide-react';
import { apiService } from '@/services/api';

export interface SpecKitFeatureDto {
  number: string;
  slug: string;
  repoLabel: string | null;
  hasPlan: boolean;
  hasTasks: boolean;
}

interface SpecKitFeaturePickerProps {
  projectId: string;
  onSelect: (feature: SpecKitFeatureDto) => void;
}

// REQ-10: surfaces the same feature list `--feature` accepts, with repo
// labels, so a Spec Kit project's features are pickable without a
// terminal. Renders nothing when the project has no Spec Kit features --
// callers decide whether to show this at all (e.g. only when the query
// resolves with a non-empty list).
const SpecKitFeaturePicker: React.FC<SpecKitFeaturePickerProps> = ({ projectId, onSelect }) => {
  const [selected, setSelected] = useState<string | null>(null);

  const { data: features, isLoading, isError, refetch } = useQuery({
    queryKey: ['speckit-features', projectId],
    queryFn: () => apiService.getAutopilotProjectSpeckitFeatures(projectId),
    enabled: !!projectId,
  });

  if (isLoading) {
    return <div className="text-sm text-gray-500 dark:text-gray-400 py-2">Scanning for Spec Kit features…</div>;
  }

  if (isError) {
    return (
      <div className="text-sm text-red-600 dark:text-red-400 py-2 flex items-center gap-2">
        Failed to load Spec Kit features.
        <button onClick={() => refetch()} className="underline hover:no-underline">
          Retry
        </button>
      </div>
    );
  }

  if (!features || features.length === 0) return null;

  const key = (f: SpecKitFeatureDto) => `${f.repoLabel ?? ''}/${f.number}-${f.slug}`;
  const repoLabels = Array.from(new Set(features.map(f => f.repoLabel)));
  const groupedByRepo = repoLabels.length > 1 || (repoLabels.length === 1 && repoLabels[0] !== null);

  const groups: { label: string | null; features: SpecKitFeatureDto[] }[] = groupedByRepo
    ? repoLabels.map(label => ({ label, features: features.filter(f => f.repoLabel === label) }))
    : [{ label: null, features }];

  const handleSelect = (f: SpecKitFeatureDto) => {
    const k = key(f);
    setSelected(k);
    onSelect(f);
  };

  return (
    <div className="space-y-3" data-testid="speckit-feature-picker">
      {groups.map(group => (
        <div key={group.label ?? '__flat__'}>
          {group.label && (
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">
              {group.label}
            </div>
          )}
          <div className="space-y-1.5">
            {group.features.map(f => {
              const k = key(f);
              const isSelected = selected === k;
              return (
                <button
                  key={k}
                  type="button"
                  onClick={() => handleSelect(f)}
                  aria-pressed={isSelected}
                  className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-sm transition-colors duration-150 ${
                    isSelected
                      ? 'bg-blue-50 dark:bg-blue-900/30 border-blue-400 dark:border-blue-500'
                      : 'bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-blue-600'
                  }`}
                >
                  <FileText className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
                  <span className="font-mono">
                    {f.number}-{f.slug}
                  </span>
                  {!f.hasPlan && (
                    <span className="ml-auto text-xs text-amber-600 dark:text-amber-400">no plan.md</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
};

export default SpecKitFeaturePicker;

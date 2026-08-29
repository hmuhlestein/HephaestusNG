import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { FileText } from 'lucide-react';
import { apiService } from '@/services/api';

export interface SpecKitFeatureDto {
  number: string;
  slug: string;
  repoLabel: string | null;
  hasPlan: boolean;
  hasTasks: boolean;
}

export interface SpecKitReadinessDto {
  features: {
    number: string;
    slug: string;
    repoLabel: string | null;
    needsClarification: string[];
    missingFiles: string[];
  }[];
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
  const [readiness, setReadiness] = useState<Record<string, SpecKitReadinessDto['features'][0] | 'error'>>({});

  const { data: features, isLoading, isError, refetch } = useQuery({
    queryKey: ['speckit-features', projectId],
    queryFn: () => apiService.getAutopilotProjectSpeckitFeatures(projectId),
    enabled: !!projectId,
  });

  // REQ-03: user-triggered, on-demand readiness check -- never fetched as
  // part of the list load above. One shared mutation across all features;
  // onSuccess/onError key off mutate()'s own variables argument so
  // cross-feature interleaving never corrupts another feature's result.
  const readinessMutation = useMutation({
    mutationFn: (f: SpecKitFeatureDto) =>
      apiService.getAutopilotProjectSpeckitReadiness(projectId, { number: f.number, repoLabel: f.repoLabel }),
    onSuccess: (result, f) => {
      setReadiness(prev => ({ ...prev, [key(f)]: result.features[0] }));
    },
    onError: (_err, f) => {
      setReadiness(prev => ({ ...prev, [key(f)]: 'error' }));
    },
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
              const isCheckingFeature = readinessMutation.isPending && readinessMutation.variables === f;
              const result = readiness[k];
              return (
                <div key={k}>
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => handleSelect(f)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        handleSelect(f);
                      }
                    }}
                    aria-pressed={isSelected}
                    className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-sm transition-colors duration-150 cursor-pointer ${
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
                    <button
                      type="button"
                      disabled={isCheckingFeature}
                      onClick={e => {
                        e.stopPropagation();
                        readinessMutation.mutate(f);
                      }}
                      className="text-xs text-blue-600 dark:text-blue-400 hover:underline shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isCheckingFeature ? 'Checking…' : 'Check readiness'}
                    </button>
                  </div>
                  {result && result !== 'error' && (
                    <div className="ml-6 mt-1 text-xs text-gray-600 dark:text-gray-400 space-y-0.5">
                      {result.missingFiles.map(m => (
                        <div key={m} className="text-amber-600 dark:text-amber-400">
                          Missing: {m}
                        </div>
                      ))}
                      {result.needsClarification.map((n, i) => (
                        <div key={i} className="text-amber-600 dark:text-amber-400">
                          NEEDS CLARIFICATION: {n}
                        </div>
                      ))}
                    </div>
                  )}
                  {result === 'error' && (
                    <div className="ml-6 mt-1 text-xs text-red-600 dark:text-red-400">Failed to check readiness.</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
};

export default SpecKitFeaturePicker;

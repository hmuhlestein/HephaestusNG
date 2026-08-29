import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
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
  /** Fired the moment the dropdown is opened (before a choice is made) --
   * lets a caller collapse other UI (e.g. a remote file browser) that
   * would otherwise compete with it for space/attention. */
  onOpen?: () => void;
}

const key = (f: SpecKitFeatureDto) => `${f.repoLabel ?? ''}/${f.number}-${f.slug}`;

// REQ-10: surfaces the same feature list `--feature` accepts, with repo
// labels, so a Spec Kit project's features are pickable without a
// terminal. A single <select> rather than an always-expanded card list --
// this is a secondary path (most designs come from the drop zone above),
// so it shouldn't cost vertical space for every feature at once. Renders
// nothing when the project has no Spec Kit features -- callers decide
// whether to show this at all (e.g. only when the query resolves with a
// non-empty list).
const SpecKitFeaturePicker: React.FC<SpecKitFeaturePickerProps> = ({ projectId, onSelect, onOpen }) => {
  const [selectedKey, setSelectedKey] = useState<string>('');
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

  const repoLabels = Array.from(new Set(features.map(f => f.repoLabel)));
  const groupedByRepo = repoLabels.length > 1 || (repoLabels.length === 1 && repoLabels[0] !== null);

  const groups: { label: string | null; features: SpecKitFeatureDto[] }[] = groupedByRepo
    ? repoLabels.map(label => ({ label, features: features.filter(f => f.repoLabel === label) }))
    : [{ label: null, features }];

  const selected = features.find(f => key(f) === selectedKey) ?? null;
  const isCheckingFeature = readinessMutation.isPending && !!selected && readinessMutation.variables === selected;
  const result = selected ? readiness[key(selected)] : undefined;

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const k = e.target.value;
    setSelectedKey(k);
    const f = features.find(candidate => key(candidate) === k);
    if (f) onSelect(f);
  };

  return (
    <div data-testid="speckit-feature-picker">
      <select
        value={selectedKey}
        onChange={handleChange}
        onMouseDown={onOpen}
        className="w-full appearance-none bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-800 dark:text-gray-200 text-sm rounded-lg px-3 py-2 hover:border-blue-300 dark:hover:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent cursor-pointer"
      >
        <option value="">Select a feature…</option>
        {groups.map(group =>
          group.label ? (
            <optgroup key={group.label} label={group.label}>
              {group.features.map(f => (
                <option key={key(f)} value={key(f)}>
                  {f.number}-{f.slug}
                  {!f.hasPlan ? ' (no plan.md)' : ''}
                </option>
              ))}
            </optgroup>
          ) : (
            group.features.map(f => (
              <option key={key(f)} value={key(f)}>
                {f.number}-{f.slug}
                {!f.hasPlan ? ' (no plan.md)' : ''}
              </option>
            ))
          )
        )}
      </select>

      {selected && (
        <div className="mt-2">
          <button
            type="button"
            disabled={isCheckingFeature}
            onClick={() => readinessMutation.mutate(selected)}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isCheckingFeature ? 'Checking…' : 'Check readiness'}
          </button>
          {result && result !== 'error' && (
            <div className="mt-1 text-xs text-gray-600 dark:text-gray-400 space-y-0.5">
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
            <div className="mt-1 text-xs text-red-600 dark:text-red-400">Failed to check readiness.</div>
          )}
        </div>
      )}
    </div>
  );
};

export default SpecKitFeaturePicker;

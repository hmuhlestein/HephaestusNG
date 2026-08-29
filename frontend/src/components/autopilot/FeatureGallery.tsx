import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  Search, Grid, List,
  Clock, DollarSign, Layers, Eye, FileText
} from 'lucide-react';
import { apiService } from '@/services/api';
import { StatusBadge, StatusIcon, formatTime } from '@/pages/Autopilot';
import { formatDistanceToNow } from 'date-fns';
import { isCompletedFeatureStatus } from '@/utils/featureStatus';

interface FeatureGalleryProps {
  onSelectFeature: (featureId: string) => void;
  projectId: string | null;
  statusFilter?: StatusFilter;
  onStatusFilterChange?: (filter: StatusFilter) => void;
}

type ViewMode = 'grid' | 'list';
type StatusFilter = 'all' | 'validated' | 'needs_review' | 'failed';

const FeatureGallery: React.FC<FeatureGalleryProps> = ({ onSelectFeature, projectId, statusFilter: externalFilter, onStatusFilterChange }) => {
  const [viewMode, setViewMode] = useState<ViewMode>('grid');
  const [internalFilter, setInternalFilter] = useState<StatusFilter>('all');
  const statusFilter = externalFilter ?? internalFilter;
  const setStatusFilter = onStatusFilterChange ?? setInternalFilter;
  const [search, setSearch] = useState('');

  const { data: features, isLoading } = useQuery({
    queryKey: ['autopilot-features', projectId],
    queryFn: () => apiService.getAutopilotFeatures(projectId || undefined),
    enabled: !!projectId,
  });

  const filtered = (features || []).filter((f: any) => {
    // This component is only ever mounted for the "Completed" tab -- a
    // feature that hasn't finished (pending: not started, active: still
    // running) must never show here regardless of statusFilter, or the
    // tab's own label is a lie. The Queue tab is the correct place for
    // those; getAutopilotFeatures() itself still returns everything
    // unfiltered since other consumers legitimately need the full list.
    if (!isCompletedFeatureStatus(f.status)) return false;
    if (statusFilter !== 'all' && f.status !== statusFilter) return false;
    if (search && !f.name.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  // Group by the spec each feature was decomposed from. A user queues a
  // spec, not a feature, so a flat list of a dozen feature names gives no
  // clue which ones belong to the same piece of work. Built by walking the
  // already-sorted list so both the groups and the features inside them stay
  // in the backend's newest-first order.
  const groups: { id: string; name: string; features: any[] }[] = [];
  const groupIndex = new Map<string, number>();
  filtered.forEach((f: any) => {
    const id = f.design_id || '__no_spec__';
    let idx = groupIndex.get(id);
    if (idx === undefined) {
      idx = groups.length;
      groupIndex.set(id, idx);
      groups.push({ id, name: f.design_name || 'No spec', features: [] });
    }
    groups[idx].features.push(f);
  });

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search features..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 border border-gray-200 dark:border-gray-600 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500"
          />
        </div>

        {/* Status filter pills */}
        <div className="flex items-center gap-1 bg-gray-100 dark:bg-gray-700 rounded-lg p-1">
          {(['all', 'validated', 'needs_review', 'failed'] as StatusFilter[]).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                statusFilter === s
                  ? 'bg-white dark:bg-gray-600 shadow-sm text-gray-800 dark:text-gray-100'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
              }`}
            >
              {s === 'all' ? 'All' : s.replace(/_/g, ' ')}
            </button>
          ))}
        </div>

        {/* View mode toggle */}
        <div className="flex items-center gap-1 bg-gray-100 dark:bg-gray-700 rounded-lg p-1">
          <button
            onClick={() => setViewMode('grid')}
            className={`p-1.5 rounded-md transition-colors ${viewMode === 'grid' ? 'bg-white dark:bg-gray-600 shadow-sm' : ''}`}
          >
            <Grid className="w-4 h-4 text-gray-600 dark:text-gray-400" />
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={`p-1.5 rounded-md transition-colors ${viewMode === 'list' ? 'bg-white dark:bg-gray-600 shadow-sm' : ''}`}
          >
            <List className="w-4 h-4 text-gray-600 dark:text-gray-400" />
          </button>
        </div>
      </div>

      {/* Results */}
      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
          <Layers className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">No features found</h3>
          <p className="text-sm text-gray-400 dark:text-gray-500">
            {search || statusFilter !== 'all'
              ? 'Try adjusting your filters'
              : 'Processed features will appear here'}
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {groups.map((group) => (
            <div key={group.id} className="space-y-3">
              <SpecHeader name={group.name} count={group.features.length} />
              {viewMode === 'grid' ? (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {group.features.map((feature: any, index: number) => (
                    <FeatureCard
                      key={feature.id}
                      feature={feature}
                      index={index}
                      onClick={() => onSelectFeature(feature.id)}
                    />
                  ))}
                </div>
              ) : (
                <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-100 dark:border-gray-700 shadow-sm overflow-hidden">
                  <div className="divide-y">
                    {group.features.map((feature: any) => (
                      <FeatureRow
                        key={feature.id}
                        feature={feature}
                        onClick={() => onSelectFeature(feature.id)}
                      />
                    ))}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

// ── Spec Group Header ──────────────────────────────────────

const SpecHeader: React.FC<{ name: string; count: number }> = ({ name, count }) => (
  <div className="flex items-center gap-2">
    <FileText className="w-4 h-4 text-violet-500 shrink-0" />
    <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 truncate" title={name}>
      {name}
    </h3>
    <span className="px-2 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 shrink-0">
      {count}
    </span>
    <div className="flex-1 h-px bg-gray-200 dark:bg-gray-700" />
  </div>
);

// ── Feature Card (Grid) ────────────────────────────────────

const FeatureCard: React.FC<{
  feature: any;
  index: number;
  onClick: () => void;
}> = ({ feature, index, onClick }) => {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      onClick={onClick}
      className="bg-white dark:bg-gray-800 rounded-xl border border-gray-100 dark:border-gray-700 shadow-sm hover:shadow-lg transition-all cursor-pointer group"
    >
      {/* Status stripe */}
      <div className={`h-1.5 rounded-t-xl ${
        feature.status === 'validated' ? 'bg-emerald-500' :
        feature.status === 'failed' ? 'bg-red-500' :
        'bg-amber-500'
      }`} />

      <div className="p-5">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            <StatusIcon status={feature.status} />
            <StatusBadge status={feature.status} />
          </div>
          {feature.has_report && (
            <div className="p-1.5 rounded-lg bg-violet-50 text-violet-600 opacity-0 group-hover:opacity-100 transition-opacity">
              <Eye className="w-3.5 h-3.5" />
            </div>
          )}
        </div>

        {/* Name */}
        <h3 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-2 line-clamp-2 group-hover:text-violet-700 transition-colors">
          {feature.name}
        </h3>

        {/* Metadata */}
        <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400 mb-3">
          <span className="flex items-center gap-1">
            <Layers className="w-3 h-3" />
            {feature.iterations} iter{feature.iterations !== 1 ? 's' : ''}
          </span>
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            {formatTime(feature.total_time_seconds)}
          </span>
          {feature.cost_total > 0 && (
            <span className="flex items-center gap-1">
              <DollarSign className="w-3 h-3" />
              ${feature.cost_total.toFixed(2)}
            </span>
          )}
        </div>

        {/* Footer */}
        <div className="text-xs text-gray-400 dark:text-gray-500">
          {formatDistanceToNow(new Date(feature.created_at), { addSuffix: true })}
        </div>
      </div>
    </motion.div>
  );
};

// ── Feature Row (List) ─────────────────────────────────────

const FeatureRow: React.FC<{
  feature: any;
  onClick: () => void;
}> = ({ feature, onClick }) => {
  return (
    <div
      onClick={onClick}
      className="flex items-center gap-4 px-5 py-4 hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer transition-colors group"
    >
      <StatusIcon status={feature.status} />

      <div className="flex-1 min-w-0">
        <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 truncate group-hover:text-violet-700 transition-colors">
          {feature.name}
        </h4>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
          {feature.iterations} iteration{feature.iterations !== 1 ? 's' : ''} ·{' '}
          {formatTime(feature.total_time_seconds)} ·{' '}
          {feature.stop_reason.replace(/_/g, ' ')}
        </p>
      </div>

      <StatusBadge status={feature.status} />

      {feature.cost_total > 0 && (
        <span className="text-xs text-gray-500 font-mono">${feature.cost_total.toFixed(2)}</span>
      )}

      <span className="text-xs text-gray-400">
        {formatDistanceToNow(new Date(feature.created_at), { addSuffix: true })}
      </span>

      {feature.has_report && (
        <button className="p-1.5 rounded-lg hover:bg-violet-50 text-gray-400 hover:text-violet-600 transition-colors">
          <Eye className="w-4 h-4" />
        </button>
      )}
    </div>
  );
};

export default FeatureGallery;

import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  Search, Grid, List,
  Clock, DollarSign, Layers, Eye
} from 'lucide-react';
import { apiService } from '@/services/api';
import { StatusBadge, StatusIcon, formatTime } from '@/pages/Autopilot';
import { formatDistanceToNow } from 'date-fns';

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
    queryFn: () => apiService.getAutopilotFeatures(),
    enabled: !!projectId,
  });

  const filtered = (features || []).filter((f: any) => {
    if (statusFilter !== 'all' && f.status !== statusFilter) return false;
    if (search && !f.name.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
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
            className="w-full pl-10 pr-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 bg-white"
          />
        </div>

        {/* Status filter pills */}
        <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-1">
          {(['all', 'validated', 'needs_review', 'failed'] as StatusFilter[]).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                statusFilter === s
                  ? 'bg-white shadow-sm text-gray-800'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              {s === 'all' ? 'All' : s.replace(/_/g, ' ')}
            </button>
          ))}
        </div>

        {/* View mode toggle */}
        <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-1">
          <button
            onClick={() => setViewMode('grid')}
            className={`p-1.5 rounded-md transition-colors ${viewMode === 'grid' ? 'bg-white shadow-sm' : ''}`}
          >
            <Grid className="w-4 h-4 text-gray-600" />
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={`p-1.5 rounded-md transition-colors ${viewMode === 'list' ? 'bg-white shadow-sm' : ''}`}
          >
            <List className="w-4 h-4 text-gray-600" />
          </button>
        </div>
      </div>

      {/* Results */}
      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
          <Layers className="w-12 h-12 text-gray-300 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-600 mb-2">No features found</h3>
          <p className="text-sm text-gray-400">
            {search || statusFilter !== 'all'
              ? 'Try adjusting your filters'
              : 'Processed features will appear here'}
          </p>
        </div>
      ) : viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((feature: any, index: number) => (
            <FeatureCard
              key={feature.id}
              feature={feature}
              index={index}
              onClick={() => onSelectFeature(feature.id)}
            />
          ))}
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
          <div className="divide-y">
            {filtered.map((feature: any) => (
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
  );
};

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
      className="bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-lg transition-all cursor-pointer group"
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
        <h3 className="text-base font-semibold text-gray-800 mb-2 line-clamp-2 group-hover:text-violet-700 transition-colors">
          {feature.name}
        </h3>

        {/* Metadata */}
        <div className="flex items-center gap-3 text-xs text-gray-500 mb-3">
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
        <div className="text-xs text-gray-400">
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
      className="flex items-center gap-4 px-5 py-4 hover:bg-gray-50 cursor-pointer transition-colors group"
    >
      <StatusIcon status={feature.status} />

      <div className="flex-1 min-w-0">
        <h4 className="text-sm font-semibold text-gray-800 truncate group-hover:text-violet-700 transition-colors">
          {feature.name}
        </h4>
        <p className="text-xs text-gray-500 mt-0.5">
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

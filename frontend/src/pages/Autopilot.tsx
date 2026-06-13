import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Rocket, ListOrdered, History, MessageSquare, Activity, ChevronRight,
  Plus, RefreshCw, FileText, Clock, CheckCircle2, XCircle, AlertTriangle,
  Terminal
} from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import PipelineStatusCard from '@/components/autopilot/PipelineStatusCard';
import DesignQueuePanel from '@/components/autopilot/DesignQueuePanel';
import FeatureGallery from '@/components/autopilot/FeatureGallery';
import FeatureDetailModal from '@/components/autopilot/FeatureDetailModal';
import MessageCenter from '@/components/autopilot/MessageCenter';
import AddDesignModal from '@/components/autopilot/AddDesignModal';
import HumanInputBanner from '@/components/autopilot/HumanInputBanner';
import { useProject } from '@/context/ProjectContext';

type Tab = 'overview' | 'queue' | 'features' | 'messages' | 'logs';

const Autopilot: React.FC = () => {
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null);
  const [showAddDesign, setShowAddDesign] = useState(false);
  const { activeProject } = useProject();
  const projectId = activeProject?.id || null;
  const queryClient = useQueryClient();

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['autopilot-status'],
    queryFn: () => apiService.getAutopilotStatus(),
    refetchInterval: 10000,
  });

  const togglePipeline = useMutation({
    mutationFn: async () => {
      if (status?.running) {
        return apiService.stopAutopilot();
      } else if (activeProject) {
        return apiService.startAutopilot(activeProject.base_dir);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
    },
  });

  const { data: messages } = useQuery({
    queryKey: ['autopilot-messages'],
    queryFn: () => apiService.getAutopilotMessages(500),
    refetchInterval: 15000,
  });

  const tabs: { id: Tab; label: string; icon: React.ElementType; badge?: number }[] = [
    { id: 'overview', label: 'Overview', icon: Activity },
    { id: 'queue', label: 'Design Queue', icon: ListOrdered, badge: status?.queue_depth },
    { id: 'features', label: 'Completed', icon: History },
    { id: 'messages', label: 'Messages', icon: MessageSquare, badge: messages?.length },
    { id: 'logs', label: 'Logs', icon: Terminal },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-gradient-to-br from-violet-500 to-purple-600 rounded-lg">
            <Rocket className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-3xl font-bold text-gray-800">Autopilot</h1>
            <p className="text-gray-500 text-sm">Continuous design-to-deploy pipeline</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetchStatus()}
            className="text-gray-600"
          >
            <RefreshCw className="w-4 h-4 mr-1" />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => setShowAddDesign(true)}
            className="bg-violet-600 hover:bg-violet-700 text-white"
            disabled={!projectId}
          >
            <Plus className="w-4 h-4 mr-1" />
            New Design
          </Button>
        </div>
      </div>

      {/* Human Input Banner (shows when pipeline needs input) */}
      <HumanInputBanner />

      {/* Pipeline Status Hero */}
      <PipelineStatusCard
        status={status}
        onToggle={() => togglePipeline.mutate()}
        loading={togglePipeline.isPending}
      />

      {/* Tab Navigation */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-1 -mb-px">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`
                flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors
                ${activeTab === tab.id
                  ? 'border-violet-500 text-violet-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'}
              `}
            >
              <tab.icon className="w-4 h-4" />
              {tab.label}
              {tab.badge !== undefined && tab.badge > 0 && (
                <span className="ml-1 px-2 py-0.5 text-xs rounded-full bg-violet-100 text-violet-700 font-medium">
                  {tab.badge}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.2 }}
        >
          {activeTab === 'overview' && (
            <OverviewTab
              status={status}
              onGoToQueue={() => setActiveTab('queue')}
              onGoToFeatures={() => setActiveTab('features')}
            />
          )}
          {activeTab === 'queue' && (
            <DesignQueuePanel projectId={projectId} onAddDesign={() => setShowAddDesign(true)} />
          )}
          {activeTab === 'features' && (
            <FeatureGallery onSelectFeature={setSelectedFeatureId} />
          )}
          {activeTab === 'messages' && <MessageCenter />}
          {activeTab === 'logs' && <LogsPanel />}
        </motion.div>
      </AnimatePresence>

      {/* Modals */}
      <FeatureDetailModal
        featureId={selectedFeatureId}
        onClose={() => setSelectedFeatureId(null)}
      />
      <AddDesignModal
        open={showAddDesign}
        projectId={projectId}
        onClose={() => setShowAddDesign(false)}
      />
    </div>
  );
};

// ── Overview Tab ─────────────────────────────────────────────

const OverviewTab: React.FC<{
  status: any;
  onGoToQueue: () => void;
  onGoToFeatures: () => void;
}> = ({ status, onGoToQueue, onGoToFeatures }) => {
  const { data: features } = useQuery({
    queryKey: ['autopilot-features'],
    queryFn: () => apiService.getAutopilotFeatures(),
  });

  const { data: queue } = useQuery({
    queryKey: ['autopilot-queue'],
    queryFn: () => apiService.getAutopilotQueue(),
  });

  const recentFeatures = (features || []).slice(0, 3);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Stats */}
      <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'Processed', value: status?.designs_processed || 0, color: 'bg-blue-500', icon: FileText },
          { label: 'Succeeded', value: status?.designs_succeeded || 0, color: 'bg-emerald-500', icon: CheckCircle2 },
          { label: 'Failed', value: status?.designs_failed || 0, color: 'bg-red-500', icon: XCircle },
          { label: 'In Queue', value: status?.queue_depth || 0, color: 'bg-amber-500', icon: ListOrdered },
        ].map((stat) => (
          <motion.div
            key={stat.label}
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            className="bg-white rounded-xl shadow-sm border border-gray-100 p-5"
          >
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">{stat.label}</p>
                <p className="text-3xl font-bold text-gray-800 mt-1">{stat.value}</p>
              </div>
              <div className={`p-3 rounded-xl ${stat.color} bg-opacity-10`}>
                <stat.icon className={`w-6 h-6 ${stat.color.replace('bg-', 'text-')}`} />
              </div>
            </div>
          </motion.div>
        ))}
      </div>

      {/* Quick Actions */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-5">
        <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-4">Quick Actions</h3>
        <div className="space-y-3">
          <button
            onClick={onGoToQueue}
            className="w-full flex items-center justify-between p-3 rounded-lg hover:bg-gray-50 transition-colors group"
          >
            <div className="flex items-center gap-3">
              <div className="p-2 bg-violet-50 rounded-lg group-hover:bg-violet-100 transition-colors">
                <ListOrdered className="w-4 h-4 text-violet-600" />
              </div>
              <span className="text-sm font-medium text-gray-700">View Design Queue</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-400">{queue?.length || 0} designs</span>
              <ChevronRight className="w-4 h-4 text-gray-400" />
            </div>
          </button>
          <button
            onClick={onGoToFeatures}
            className="w-full flex items-center justify-between p-3 rounded-lg hover:bg-gray-50 transition-colors group"
          >
            <div className="flex items-center gap-3">
              <div className="p-2 bg-emerald-50 rounded-lg group-hover:bg-emerald-100 transition-colors">
                <History className="w-4 h-4 text-emerald-600" />
              </div>
              <span className="text-sm font-medium text-gray-700">Feature Gallery</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-400">{features?.length || 0} features</span>
              <ChevronRight className="w-4 h-4 text-gray-400" />
            </div>
          </button>
        </div>
      </div>

      {/* Recent Features */}
      <div className="lg:col-span-3 bg-white rounded-xl shadow-sm border border-gray-100">
        <div className="px-5 py-4 border-b flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider">Recent Completed</h3>
          <button
            onClick={onGoToFeatures}
            className="text-xs text-violet-600 hover:text-violet-700 font-medium"
          >
            View all →
          </button>
        </div>
        {recentFeatures.length > 0 ? (
          <div className="divide-y">
            {recentFeatures.map((feature: any) => (
              <div key={feature.id} className="px-5 py-4 flex items-center justify-between hover:bg-gray-50">
                <div className="flex items-center gap-3">
                  <StatusIcon status={feature.status} />
                  <div>
                    <p className="text-sm font-medium text-gray-800">{feature.name}</p>
                    <p className="text-xs text-gray-500">
                      {feature.iterations} iteration{feature.iterations !== 1 ? 's' : ''} ·{' '}
                      {formatTime(feature.total_time_seconds)}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <StatusBadge status={feature.status} />
                  {feature.cost_total > 0 && (
                    <span className="text-xs text-gray-500">${feature.cost_total.toFixed(2)}</span>
                  )}
                  <span className="text-xs text-gray-400">
                    {formatDistanceToNow(new Date(feature.created_at), { addSuffix: true })}
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="p-8 text-center text-gray-400">
            <History className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No features processed yet</p>
          </div>
        )}
      </div>
    </div>
  );
};

// ── Logs Panel ───────────────────────────────────────────────

const LogsPanel: React.FC = () => {
  const { data, isLoading } = useQuery({
    queryKey: ['autopilot-logs'],
    queryFn: () => apiService.getAutopilotLogs(200),
    refetchInterval: 15000,
  });

  const logs = data?.lines || [];

  return (
    <div className="bg-gray-900 rounded-xl shadow-lg overflow-hidden">
      <div className="px-5 py-3 bg-gray-800 border-b border-gray-700 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-green-400" />
          <span className="text-sm font-medium text-gray-300">Orchestrator Logs</span>
        </div>
        <span className="text-xs text-gray-500">{logs.length} lines</span>
      </div>
      <div className="p-4 max-h-[600px] overflow-y-auto font-mono text-xs leading-relaxed">
        {isLoading ? (
          <div className="text-gray-500">Loading...</div>
        ) : logs.length > 0 ? (
          logs.map((line: string, i: number) => (
            <div key={i} className={`py-0.5 ${getLogLineStyle(line)}`}>
              {line}
            </div>
          ))
        ) : (
          <div className="text-gray-500">No logs available</div>
        )}
      </div>
    </div>
  );
};

// ── Shared Helpers ───────────────────────────────────────────

export const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const styles: Record<string, string> = {
    validated: 'bg-emerald-100 text-emerald-700 border-emerald-200',
    needs_review: 'bg-amber-100 text-amber-700 border-amber-200',
    failed: 'bg-red-100 text-red-700 border-red-200',
    in_progress: 'bg-blue-100 text-blue-700 border-blue-200',
    pending: 'bg-gray-100 text-gray-600 border-gray-200',
  };

  return (
    <span className={`px-2.5 py-0.5 text-xs font-medium rounded-full border ${styles[status] || styles.pending}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
};

export const StatusIcon: React.FC<{ status: string }> = ({ status }) => {
  if (status === 'validated') return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
  if (status === 'failed') return <XCircle className="w-5 h-5 text-red-500" />;
  if (status === 'needs_review') return <AlertTriangle className="w-5 h-5 text-amber-500" />;
  return <Clock className="w-5 h-5 text-gray-400" />;
};

export const formatTime = (seconds: number): string => {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
};

const getLogLineStyle = (line: string): string => {
  if (line.includes('[ERROR]')) return 'text-red-400';
  if (line.includes('[WARN]')) return 'text-amber-400';
  if (line.includes('SUCCESS') || line.includes('passed')) return 'text-emerald-400';
  if (line.includes('Phase') || line.includes('PHASE')) return 'text-blue-400';
  return 'text-gray-400';
};

export default Autopilot;

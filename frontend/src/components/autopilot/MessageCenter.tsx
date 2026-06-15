import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  Rocket, CheckCircle2, XCircle, AlertTriangle, FileText,
  Layers, Play, Pause, Zap, ArrowRight, MessageSquare,
  ExternalLink, RotateCcw, Eye, ChevronRight, Reply,
  FolderOpen, AlertCircle
} from 'lucide-react';
import { apiService } from '@/services/api';
import { formatDistanceToNow } from 'date-fns';
import FeatureDetailModal from './FeatureDetailModal';

const eventTypeConfig: Record<string, { icon: React.ElementType; color: string; bg: string; label?: string }> = {
  design_queued: { icon: FileText, color: 'text-blue-600', bg: 'bg-blue-100' },
  design_started: { icon: Play, color: 'text-violet-600', bg: 'bg-violet-100' },
  design_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  design_complete: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  design_failed: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-100' },
  phase_started: { icon: ArrowRight, color: 'text-indigo-600', bg: 'bg-indigo-100' },
  phase_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  workflow_started: { icon: Rocket, color: 'text-violet-600', bg: 'bg-violet-100' },
  workflow_launch: { icon: Rocket, color: 'text-violet-600', bg: 'bg-violet-100' },
  workflow_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  iteration_started: { icon: Layers, color: 'text-blue-600', bg: 'bg-blue-100' },
  iteration_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  pipeline_started: { icon: Zap, color: 'text-amber-600', bg: 'bg-amber-100' },
  pipeline_stopped: { icon: Pause, color: 'text-gray-600', bg: 'bg-gray-100' },
  pipeline_stop: { icon: Pause, color: 'text-gray-600', bg: 'bg-gray-100' },
  warning: { icon: AlertTriangle, color: 'text-amber-600', bg: 'bg-amber-100' },
  error: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-100' },
  stuck_agent: { icon: AlertTriangle, color: 'text-orange-600', bg: 'bg-orange-100' },
  credit_exhausted: { icon: AlertTriangle, color: 'text-red-600', bg: 'bg-red-100' },
  human_input_required: { icon: AlertCircle, color: 'text-amber-600', bg: 'bg-amber-100' },
  human_input: { icon: Reply, color: 'text-blue-600', bg: 'bg-blue-100' },
};

// Status-based display config for design_complete events
const designStatusConfig: Record<string, { icon: React.ElementType; color: string; bg: string; label: string }> = {
  completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100', label: 'Design Complete' },
  failed: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-100', label: 'Design Failed' },
  validating: { icon: AlertTriangle, color: 'text-amber-600', bg: 'bg-amber-100', label: 'Design Validating' },
};

const MessageCenter: React.FC = () => {
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null);
  const { data: messages, isLoading } = useQuery({
    queryKey: ['autopilot-messages'],
    queryFn: () => apiService.getAutopilotMessages(100),
    refetchInterval: 15000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
      </div>
    );
  }

  if (!messages || messages.length === 0) {
    return (
      <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
        <MessageSquare className="w-12 h-12 text-gray-300 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 mb-2">No messages yet</h3>
        <p className="text-sm text-gray-400">Pipeline events will appear here as they happen</p>
      </div>
    );
  }

  // Group messages by date
  const grouped: Record<string, any[]> = {};
  for (const msg of messages) {
    const date = new Date(msg.timestamp).toLocaleDateString();
    if (!grouped[date]) grouped[date] = [];
    grouped[date].push(msg);
  }

  const getMessageActions = (msg: any): MessageAction[] => {
    const actions: MessageAction[] = [];
    const data = msg.data || {};

    // Extract feature_id from feature_folder path if not directly provided
    const featureId = data.feature_id || (data.feature_folder ? data.feature_folder.split('/').pop() : null);

    // Design-related actions
    if (featureId) {
      if (msg.type === 'design_complete' || msg.type === 'design_completed') {
        actions.push({
          label: 'View Feature',
          icon: Eye,
          onClick: () => setSelectedFeature(featureId),
          color: 'violet',
        });
      }
      if (data.status === 'failed') {
        actions.push({
          label: 'Retry',
          icon: RotateCcw,
          onClick: () => {
            // TODO: Implement retry
            console.log('Retry feature:', featureId);
          },
          color: 'amber',
        });
      }
    }

    // Workflow actions
    if (data.workflow_id || data.workflow) {
      actions.push({
        label: 'View Workflow',
        icon: ExternalLink,
        onClick: () => {
          window.open(`/workflows`, '_blank');
        },
        color: 'blue',
      });
    }

    // Human input required
    if (msg.type === 'human_input_required' && data.request_id) {
      actions.push({
        label: 'Respond',
        icon: Reply,
        onClick: async () => {
          // TODO: Open human input modal
          console.log('Respond to input request:', data.request_id);
        },
        color: 'violet',
      });
    }

    // Stuck agent / warning / error actions
    if (msg.type === 'stuck_agent' || msg.type === 'warning' || msg.type === 'error') {
      actions.push({
        label: 'View Agents',
        icon: AlertCircle,
        onClick: () => {
          window.open(`/agents`, '_blank');
        },
        color: 'amber',
      });
    }

    // Design queued - view queue
    if (msg.type === 'design_queued') {
      actions.push({
        label: 'View Queue',
        icon: FolderOpen,
        onClick: () => {
          // Already on autopilot page, could scroll to queue tab
        },
        color: 'blue',
      });
    }

    // Iteration actions
    if (msg.type === 'iteration_completed' || msg.type === 'iteration_started') {
      if (featureId) {
        actions.push({
          label: 'View Feature',
          icon: Eye,
          onClick: () => setSelectedFeature(featureId),
          color: 'violet',
        });
      }
    }

    // Phase actions
    if (msg.type === 'phase_completed' || msg.type === 'phase_started') {
      if (featureId) {
        actions.push({
          label: 'View Feature',
          icon: Eye,
          onClick: () => setSelectedFeature(featureId),
          color: 'violet',
        });
      }
    }

    return actions;
  };

  return (
    <>
      <div className="space-y-6">
        {Object.entries(grouped).map(([date, msgs]) => (
          <div key={date}>
            <div className="flex items-center gap-3 mb-4">
              <div className="h-px flex-1 bg-gray-200" />
              <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">{date}</span>
              <div className="h-px flex-1 bg-gray-200" />
            </div>

            <div className="space-y-2">
              {msgs.map((msg, index) => {
                // Use status-based config for design_complete events
                const data = msg.data || {};
                const statusConfig = msg.type === 'design_complete' && data.status
                  ? designStatusConfig[data.status]
                  : null;
                
                const config = statusConfig || eventTypeConfig[msg.type] || {
                  icon: MessageSquare,
                  color: 'text-gray-600',
                  bg: 'bg-gray-100',
                };
                const Icon = config.icon;
                const actions = getMessageActions(msg);
                const hasActions = actions.length > 0;

                return (
                  <motion.div
                    key={`${msg.timestamp}-${index}`}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: index * 0.02 }}
                    className={`flex items-start gap-3 px-4 py-3 bg-white rounded-xl border border-gray-100 transition-all ${
                      hasActions ? 'hover:shadow-md hover:border-gray-200 cursor-pointer' : 'hover:shadow-sm'
                    }`}
                  >
                    <div className={`p-2 rounded-lg ${config.bg} flex-shrink-0`}>
                      <Icon className={`w-4 h-4 ${config.color}`} />
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-gray-800">
                          {statusConfig?.label || formatEventType(msg.type)}
                        </span>
                        <span className="text-xs text-gray-400 font-mono">{msg.type}</span>
                        {hasActions && <ChevronRight className="w-3 h-3 text-gray-300" />}
                      </div>
                      
                      {/* Error display - prominent red styling */}
                      {data.error && (
                        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded-lg">
                          <div className="flex items-center gap-1 text-xs font-medium text-red-700 mb-1">
                            <XCircle className="w-3 h-3" />
                            Error
                          </div>
                          <p className="text-xs text-red-600">{String(data.error)}</p>
                        </div>
                      )}
                      
                      {/* Reason display for human_input_required */}
                      {msg.type === 'human_input_required' && data.reason && (
                        <div className="mt-2 p-2 bg-amber-50 border border-amber-200 rounded-lg">
                          <div className="flex items-center gap-1 text-xs font-medium text-amber-700 mb-1">
                            <AlertCircle className="w-3 h-3" />
                            Reason
                          </div>
                          <p className="text-xs text-amber-600">{String(data.reason)}</p>
                        </div>
                      )}
                      
                      {/* Other data fields */}
                      {msg.data && Object.keys(msg.data).length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-2">
                          {Object.entries(msg.data)
                            .filter(([key]) => !['error', 'reason'].includes(key))
                            .map(([key, value]) => (
                            <span
                              key={key}
                              className="inline-flex items-center gap-1 text-xs text-gray-500 bg-gray-50 px-2 py-0.5 rounded"
                            >
                              <span className="text-gray-400">{key}:</span>
                              <span className="font-medium text-gray-600 truncate max-w-[200px]">
                                {String(value)}
                              </span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="flex items-center gap-2 flex-shrink-0">
                      {actions.map((action, i) => {
                        const ActionIcon = action.icon;
                        return (
                          <button
                            key={i}
                            onClick={(e) => {
                              e.stopPropagation();
                              action.onClick();
                            }}
                            className={`flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-lg transition-colors ${
                              action.color === 'violet'
                                ? 'bg-violet-50 text-violet-600 hover:bg-violet-100'
                                : action.color === 'amber'
                                ? 'bg-amber-50 text-amber-600 hover:bg-amber-100'
                                : 'bg-blue-50 text-blue-600 hover:bg-blue-100'
                            }`}
                            title={action.label}
                          >
                            <ActionIcon className="w-3 h-3" />
                            <span className="hidden sm:inline">{action.label}</span>
                          </button>
                        );
                      })}
                      <span className="text-xs text-gray-400">
                        {formatDistanceToNow(new Date(msg.timestamp), { addSuffix: true })}
                      </span>
                    </div>
                  </motion.div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {selectedFeature && (
        <FeatureDetailModal
          featureId={selectedFeature}
          onClose={() => setSelectedFeature(null)}
        />
      )}
    </>
  );
};

interface MessageAction {
  label: string;
  icon: React.ElementType;
  onClick: () => void;
  color: 'violet' | 'amber' | 'blue';
}

const formatEventType = (type: string): string => {
  return type
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
};

export default MessageCenter;

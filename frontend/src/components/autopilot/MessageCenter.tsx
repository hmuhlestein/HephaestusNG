import React, { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Rocket, CheckCircle2, XCircle, AlertTriangle, FileText,
  Layers, Play, Pause, Zap, ArrowRight, MessageSquare,
  ExternalLink, RotateCcw, Eye, ChevronRight, Reply,
  FolderOpen, AlertCircle, SkipForward, Send
} from 'lucide-react';
import { apiService } from '@/services/api';
import { formatDistanceToNow } from 'date-fns';
import FeatureDetailModal from './FeatureDetailModal';
import toast from 'react-hot-toast';

const eventTypeConfig: Record<string, { icon: React.ElementType; color: string; bg: string; label?: string }> = {
  design_queued: { icon: FileText, color: 'text-blue-600 dark:text-blue-400', bg: 'bg-blue-100 dark:bg-blue-900/30' },
  design_started: { icon: Play, color: 'text-violet-600 dark:text-violet-400', bg: 'bg-violet-100 dark:bg-violet-900/30' },
  design_completed: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
  design_complete: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
  design_failed: { icon: XCircle, color: 'text-red-600 dark:text-red-400', bg: 'bg-red-100 dark:bg-red-900/30' },
  phase_started: { icon: ArrowRight, color: 'text-indigo-600 dark:text-indigo-400', bg: 'bg-indigo-100 dark:bg-indigo-900/30' },
  phase_completed: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
  workflow_started: { icon: Rocket, color: 'text-violet-600 dark:text-violet-400', bg: 'bg-violet-100 dark:bg-violet-900/30' },
  workflow_launch: { icon: Rocket, color: 'text-violet-600 dark:text-violet-400', bg: 'bg-violet-100 dark:bg-violet-900/30' },
  workflow_completed: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
  iteration_started: { icon: Layers, color: 'text-blue-600 dark:text-blue-400', bg: 'bg-blue-100 dark:bg-blue-900/30' },
  iteration_completed: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
  pipeline_started: { icon: Zap, color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-100 dark:bg-amber-900/30' },
  pipeline_stopped: { icon: Pause, color: 'text-gray-600 dark:text-gray-400', bg: 'bg-gray-100 dark:bg-gray-700' },
  pipeline_stop: { icon: Pause, color: 'text-gray-600 dark:text-gray-400', bg: 'bg-gray-100 dark:bg-gray-700' },
  warning: { icon: AlertTriangle, color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-100 dark:bg-amber-900/30' },
  error: { icon: XCircle, color: 'text-red-600 dark:text-red-400', bg: 'bg-red-100 dark:bg-red-900/30' },
  stuck_agent: { icon: AlertTriangle, color: 'text-orange-600 dark:text-orange-400', bg: 'bg-orange-100 dark:bg-orange-900/30' },
  credit_exhausted: { icon: AlertTriangle, color: 'text-red-600 dark:text-red-400', bg: 'bg-red-100 dark:bg-red-900/30' },
  human_input_required: { icon: AlertCircle, color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-100 dark:bg-amber-900/30' },
  human_input: { icon: Reply, color: 'text-blue-600 dark:text-blue-400', bg: 'bg-blue-100 dark:bg-blue-900/30' },
};

// Status-based display config for design_complete events
const designStatusConfig: Record<string, { icon: React.ElementType; color: string; bg: string; label: string }> = {
  completed: { icon: CheckCircle2, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30', label: 'Design Complete' },
  failed: { icon: XCircle, color: 'text-red-600 dark:text-red-400', bg: 'bg-red-100 dark:bg-red-900/30', label: 'Design Failed' },
  validating: { icon: AlertTriangle, color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-100 dark:bg-amber-900/30', label: 'Design Validating' },
};

interface MessageCenterProps {
  projectId: string | null;
}

const MessageCenter: React.FC<MessageCenterProps> = ({ projectId }) => {
  const queryClient = useQueryClient();
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null);
  // The message row currently expanded for inline response -- replaces the
  // old separate showInputModal/currentRequestId pair. Responding now
  // happens in place in the message list rather than in a popup, so
  // "which request is open" and "which row is expanded" are the same
  // question and need only one id.
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);
  const [messageText, setMessageText] = useState('');
  const [showArchived, setShowArchived] = useState(false);

  // Without this, collapsing the response row without submitting leaves
  // messageText behind -- the next human_input_required row expanded
  // opens pre-filled with the previous one's leftover draft.
  useEffect(() => {
    setMessageText('');
  }, [expandedMessageId]);
  
  // Fetch archived message IDs from DB
  const { data: archivedData, refetch: refetchArchived } = useQuery({
    queryKey: ['autopilot-archived-messages', projectId],
    queryFn: () => apiService.getAutopilotArchivedMessages(),
    refetchInterval: 30000,
    enabled: !!projectId,
  });
  const archivedIds = new Set(archivedData?.archived_ids || []);
  
  const archiveMutation = useMutation({
    mutationFn: ({ msgId, msgType, timestamp }: { msgId: string; msgType: string; timestamp: string }) =>
      apiService.archiveAutopilotMessage(msgId, msgType, timestamp),
    onSuccess: () => {
      refetchArchived();
      toast.success('Message archived');
    },
  });
  
  const unarchiveMutation = useMutation({
    mutationFn: (msgId: string) => apiService.unarchiveAutopilotMessage(msgId),
    onSuccess: () => {
      refetchArchived();
      toast.success('Message restored');
    },
  });
  
  const unarchiveAllMutation = useMutation({
    mutationFn: () => apiService.unarchiveAllAutopilotMessages(),
    onSuccess: () => {
      refetchArchived();
      toast.success('All messages restored');
    },
  });
  
  const archiveMessage = (msg: any) => {
    const msgId = `${msg.timestamp}-${msg.type}`;
    archiveMutation.mutate({ msgId, msgType: msg.type, timestamp: msg.timestamp });
  };
  
  const unarchiveMessage = (msg: any) => {
    const msgId = `${msg.timestamp}-${msg.type}`;
    unarchiveMutation.mutate(msgId);
  };
  
  const { data: messages, isLoading } = useQuery({
    queryKey: ['autopilot-messages', projectId],
    queryFn: () => apiService.getAutopilotMessages(100),
    refetchInterval: 15000,
    enabled: !!projectId,
  });

  const { data: inputRequest } = useQuery({
    queryKey: ['autopilot-input', projectId],
    queryFn: () => apiService.getAutopilotInput(),
    refetchInterval: 5000,
    enabled: !!projectId,
  });

  const submitMutation = useMutation({
    // Targets inputRequest.id, not something derived from expandedMessageId:
    // the inline response controls only ever render when
    // inputRequest?.id === data.request_id for the expanded row (see the
    // render condition below), so the two are guaranteed equal at submit
    // time -- no separate lookup needed.
    mutationFn: ({ choice, message }: { choice: string; message?: string }) =>
      apiService.submitAutopilotInput(inputRequest!.id, choice, message),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-input'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-messages'] });
      toast.success('Response sent to pipeline');
      setExpandedMessageId(null);
    },
    onError: () => toast.error('Failed to submit response'),
  });

  // Auto-expand the message row for a NEWLY-arrived pending request, once
  // per distinct request id -- so answering it doesn't require hunting
  // through the list first. Does not fight a user who deliberately
  // collapses it afterward: this only fires again when inputRequest.id
  // itself changes to a different value, not on every 5s poll.
  useEffect(() => {
    if (!inputRequest || !messages) return;
    const match = messages.find(
      (m: any) => m.type === 'human_input_required' && m.data?.request_id === inputRequest.id
    );
    if (match) {
      setExpandedMessageId(`${match.timestamp}-${match.type}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputRequest?.id]);


  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
      </div>
    );
  }

  if (!messages || messages.length === 0) {
    return (
      <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
        <MessageSquare className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">No messages yet</h3>
        <p className="text-sm text-gray-400 dark:text-gray-500">Pipeline events will appear here as they happen</p>
      </div>
    );
  }

  // Group messages by date
  const filteredMessages = messages.filter((msg: any) => {
    const msgId = `${msg.timestamp}-${msg.type}`;
    // Filter out archived messages
    if (archivedIds.has(msgId)) {
      return showArchived;
    }
    // Filter out non-actionable system events
    if (msg.type === 'human_input' && msg.data?.choice === 'timeout') {
      return false;
    }
    return !showArchived;
  });

  const grouped: Record<string, any[]> = {};
  for (const msg of filteredMessages) {
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

    // human_input_required no longer goes through the generic actions[]
    // array -- it toggles the row's own inline response panel instead (see
    // the row's onClick and the expanded-content block below), since
    // "navigate somewhere else" and "respond in place" are different
    // interactions and the row needs to know which one it's dealing with
    // before rendering, not just on click.

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
        {/* Filter toggle */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowArchived(false)}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                !showArchived ? 'bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-400' : 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Active ({messages.length - archivedIds.size})
            </button>
            <button
              onClick={() => setShowArchived(true)}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                showArchived ? 'bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-400' : 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Archived ({archivedIds.size})
            </button>
          </div>
          {showArchived && archivedIds.size > 0 && (
            <button
              onClick={() => unarchiveAllMutation.mutate()}
              disabled={unarchiveAllMutation.isPending}
              className="text-xs text-gray-500 hover:text-gray-700"
            >
              Restore all
            </button>
          )}
        </div>

        {Object.entries(grouped).map(([date, msgs]) => (
          <div key={date}>
            <div className="flex items-center gap-3 mb-4">
              <div className="h-px flex-1 bg-gray-200" />
              <span className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">{date}</span>
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
                  color: 'text-gray-600 dark:text-gray-400',
                  bg: 'bg-gray-100 dark:bg-gray-700',
                };
                const Icon = config.icon;
                const actions = getMessageActions(msg);
                const hasActions = actions.length > 0;
                const msgId = `${msg.timestamp}-${msg.type}`;
                const isRespondable = msg.type === 'human_input_required';
                const isExpanded = expandedMessageId === msgId;
                // Whether THIS specific request is still the one the
                // pipeline is actually waiting on -- a human_input_required
                // row stays in the list forever as a record, but only the
                // current inputRequest (if any) can still be answered.
                const isLiveRequest = isRespondable && !!inputRequest && inputRequest.id === data.request_id;
                const clickable = hasActions || isRespondable;

                return (
                  <motion.div
                    key={`${msg.timestamp}-${index}`}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: index * 0.02 }}
                    onClick={() => {
                      if (isRespondable) {
                        setExpandedMessageId(isExpanded ? null : msgId);
                      } else if (hasActions) {
                        actions[0].onClick();
                      }
                    }}
                    className={`flex items-start gap-3 px-4 py-3 bg-white dark:bg-gray-800 rounded-xl border transition-all ${
                      isLiveRequest
                        ? 'border-amber-300 dark:border-amber-700 ring-1 ring-amber-200 dark:ring-amber-800'
                        : 'border-gray-100 dark:border-gray-700'
                    } ${clickable ? 'hover:shadow-md hover:border-gray-200 dark:hover:border-gray-600 cursor-pointer' : 'hover:shadow-sm'}`}
                  >
                    <div className={`p-2 rounded-lg ${config.bg} flex-shrink-0`}>
                      <Icon className={`w-4 h-4 ${config.color}`} />
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-gray-800 dark:text-gray-200">
                          {statusConfig?.label || formatEventType(msg.type)}
                        </span>
                        <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{msg.type}</span>
                        {isLiveRequest && (
                          <span className="px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400 animate-pulse">
                            Waiting on you
                          </span>
                        )}
                        {clickable && (
                          <ChevronRight
                            className={`w-3 h-3 text-gray-300 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                          />
                        )}
                      </div>
                      
                      {/* Error display - prominent red styling */}
                      {data.error && (
                        <div className="mt-2 p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                          <div className="flex items-center gap-1 text-xs font-medium text-red-700 dark:text-red-400 mb-1">
                            <XCircle className="w-3 h-3" />
                            Error
                          </div>
                          <p className="text-xs text-red-600 dark:text-red-400">{String(data.error)}</p>
                        </div>
                      )}
                      
                      {/* Reason display for human_input_required */}
                      {msg.type === 'human_input_required' && data.reason && (
                        <div className="mt-2 p-2 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
                          <div className="flex items-center gap-1 text-xs font-medium text-amber-700 dark:text-amber-400 mb-1">
                            <AlertCircle className="w-3 h-3" />
                            Reason
                          </div>
                          <p className="text-xs text-amber-600 dark:text-amber-400">{String(data.reason)}</p>
                        </div>
                      )}

                      {/* Inline respond panel -- replaces the old separate
                          modal. stopPropagation everywhere inside: this
                          whole block sits inside the row's own onClick
                          (which toggles expand/collapse), so a click on the
                          textarea or a button must not also re-toggle it. */}
                      <AnimatePresence>
                        {isRespondable && isExpanded && (
                          <motion.div
                            initial={{ height: 0, opacity: 0 }}
                            animate={{ height: 'auto', opacity: 1 }}
                            exit={{ height: 0, opacity: 0 }}
                            transition={{ duration: 0.15 }}
                            onClick={(e) => e.stopPropagation()}
                            className="overflow-hidden"
                          >
                            {isLiveRequest ? (
                              <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 space-y-3">
                                <div className="flex flex-wrap items-center gap-2">
                                  <button
                                    onClick={() => submitMutation.mutate({ choice: 'c' })}
                                    disabled={submitMutation.isPending}
                                    className="flex items-center gap-1.5 px-3 py-2 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
                                  >
                                    <Zap className="w-3.5 h-3.5" />
                                    Continue
                                  </button>
                                  <button
                                    onClick={() => submitMutation.mutate({ choice: 's' })}
                                    disabled={submitMutation.isPending}
                                    className="flex items-center gap-1.5 px-3 py-2 bg-amber-600 hover:bg-amber-700 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
                                  >
                                    <SkipForward className="w-3.5 h-3.5" />
                                    Skip
                                  </button>
                                  <button
                                    onClick={() => submitMutation.mutate({ choice: 'q' })}
                                    disabled={submitMutation.isPending}
                                    className="flex items-center gap-1.5 px-3 py-2 bg-red-600 hover:bg-red-700 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
                                  >
                                    <XCircle className="w-3.5 h-3.5" />
                                    Stop
                                  </button>
                                </div>
                                <div className="flex gap-2">
                                  <input
                                    type="text"
                                    value={messageText}
                                    onChange={(e) => setMessageText(e.target.value)}
                                    placeholder="Or type a message to the pipeline..."
                                    disabled={submitMutation.isPending}
                                    className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 disabled:opacity-50"
                                    onKeyDown={(e) => {
                                      if (e.key === 'Enter' && messageText.trim()) {
                                        submitMutation.mutate({ choice: 'm', message: messageText.trim() });
                                      }
                                    }}
                                  />
                                  <button
                                    onClick={() => messageText.trim() && submitMutation.mutate({ choice: 'm', message: messageText.trim() })}
                                    disabled={!messageText.trim() || submitMutation.isPending}
                                    className="px-3 py-2 bg-violet-600 hover:bg-violet-700 text-white rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                  >
                                    <Send className="w-3.5 h-3.5" />
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-400 dark:text-gray-500 italic">
                                This request has already been answered or expired -- nothing to respond to here anymore.
                              </div>
                            )}
                          </motion.div>
                        )}
                      </AnimatePresence>
                      
                      {/* Other data fields */}
                      {msg.data && Object.keys(msg.data).length > 0 && (() => {
                        // Filter out unhelpful fields based on message type
                        const excludeFields: Record<string, string[]> = {
                          'human_input': ['request_id', 'choice', 'source'],
                          'human_input_required': ['request_id'],
                        };
                        const excluded = excludeFields[msg.type] || [];
                        
                        const displayFields = Object.entries(msg.data)
                          .filter(([key]) => !['error', 'reason', ...excluded].includes(key))
                          .filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== '');
                        
                        if (displayFields.length === 0) return null;
                        
                        return (
                          <div className="mt-1 flex flex-wrap gap-2">
                            {displayFields.map(([key, value]) => (
                              <span
                                key={key}
                                className="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 px-2 py-0.5 rounded"
                              >
                                <span className="text-gray-400">{formatFieldLabel(key)}:</span>
                                <span className="font-medium text-gray-600 truncate max-w-[200px]">
                                  {formatFieldValue(key, value)}
                                </span>
                              </span>
                            ))}
                          </div>
                        );
                      })()}
                    </div>

                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className="text-xs text-gray-400">
                        {formatDistanceToNow(new Date(msg.timestamp), { addSuffix: true })}
                      </span>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (showArchived) {
                            unarchiveMessage(msg);
                          } else {
                            archiveMessage(msg);
                          }
                        }}
                        className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
                        title={showArchived ? 'Restore message' : 'Archive message'}
                      >
                        {showArchived ? (
                          <RotateCcw className="w-3.5 h-3.5" />
                        ) : (
                          <FolderOpen className="w-3.5 h-3.5" />
                        )}
                      </button>
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

const formatFieldLabel = (key: string): string => {
  const labels: Record<string, string> = {
    'workflow': 'Workflow',
    'workflow_id': 'Workflow',
    'path': 'Path',
    'design_document': 'Design',
    'feature_folder': 'Feature',
    'feature_id': 'Feature',
    'stop_reason': 'Reason',
    'iteration': 'Iteration',
    'phase': 'Phase',
    'phase_name': 'Phase',
    'agents_active': 'Agents',
    'tasks_pending': 'Pending',
    'tasks_done': 'Done',
    'tasks_failed': 'Failed',
  };
  return labels[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
};

const formatFieldValue = (key: string, value: any): string => {
  if (key === 'stop_reason') return String(value).replace(/_/g, ' ');
  if (key === 'workflow_id' || key === 'workflow') return String(value).substring(0, 8);
  return String(value);
};

export default MessageCenter;

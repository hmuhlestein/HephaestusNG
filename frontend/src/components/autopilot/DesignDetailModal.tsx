import React, { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X, FileText, GitBranch, Clock, CheckCircle2, XCircle, AlertTriangle,
  Loader2, RotateCcw, ChevronDown, ChevronRight, ExternalLink
} from 'lucide-react';
import { apiService, api } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';

interface DesignDetailModalProps {
  projectId: string;
  filename: string;
  onClose: () => void;
  onRerun?: (filename: string) => void;
}

const STATUS_CONFIG: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'bg-gray-100 text-gray-700', icon: <Clock className="w-3.5 h-3.5" />, label: 'Pending' },
  active: { color: 'bg-blue-100 text-blue-700', icon: <Loader2 className="w-3.5 h-3.5 animate-spin" />, label: 'Active' },
  completed: { color: 'bg-green-100 text-green-700', icon: <CheckCircle2 className="w-3.5 h-3.5" />, label: 'Completed' },
  failed: { color: 'bg-red-100 text-red-700', icon: <XCircle className="w-3.5 h-3.5" />, label: 'Failed' },
  paused: { color: 'bg-yellow-100 text-yellow-700', icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Paused' },
};

const TASK_STATUS_COLORS: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-600',
  assigned: 'bg-yellow-100 text-yellow-700',
  in_progress: 'bg-blue-100 text-blue-700',
  done: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  under_review: 'bg-purple-100 text-purple-700',
};

const DesignDetailModal: React.FC<DesignDetailModalProps> = ({ projectId, filename, onClose, onRerun }) => {
  const [showContent, setShowContent] = useState(false);
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());

  const { data: status, isLoading } = useQuery({
    queryKey: ['design-status', projectId, filename],
    queryFn: () => apiService.getAutopilotProjectDesignStatus(projectId, filename),
    refetchInterval: 5000,
  });

  const rerunMutation = useMutation({
    mutationFn: async () => {
      const project = await apiService.getActiveProject();
      if (!project) throw new Error('No active project');
      return api.post('/autopilot/queue/rerun', {
        filename,
        project_path: project.base_dir,
      });
    },
    onSuccess: () => {
      toast.success('Pipeline restarted for this design');
      onRerun?.(filename);
      onClose();
    },
    onError: (e: any) => {
      toast.error(e?.response?.data?.detail || e?.message || 'Failed to rerun');
    },
  });

  const requeueMutation = useMutation({
    mutationFn: () => apiService.requeueAutopilotDesign(filename),
    onSuccess: (data) => {
      toast.success(`Design moved to front of queue${data.paused_workflows > 0 ? ` (${data.paused_workflows} workflow(s) paused)` : ''}`);
      onClose();
    },
    onError: (e: any) => {
      toast.error(e?.response?.data?.detail || 'Failed to requeue');
    },
  });

  const toggleTask = (taskId: string) => {
    setExpandedTasks(prev => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  const overallStatus = status?.status || 'pending';
  const statusConfig = STATUS_CONFIG[overallStatus] || STATUS_CONFIG.pending;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
        onClick={onClose}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] overflow-hidden"
          onClick={e => e.stopPropagation()}
        >
          {/* Header */}
          <div className="px-6 py-4 border-b bg-gradient-to-r from-violet-50 to-purple-50">
            <div className="flex items-start justify-between">
              <div className="flex items-start gap-3">
                <div className="p-2 bg-violet-100 rounded-lg">
                  <FileText className="w-5 h-5 text-violet-600" />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800">
                    {status?.name || filename.replace(/\.md$/, '').replace(/_/g, ' ')}
                  </h2>
                  <p className="text-xs text-gray-500 font-mono mt-0.5">{filename}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`px-3 py-1.5 text-xs font-semibold rounded-full flex items-center gap-1.5 ${statusConfig.color}`}>
                  {statusConfig.icon}
                  {statusConfig.label}
                </span>
                <button onClick={onClose} className="p-1.5 hover:bg-gray-200 rounded-lg transition-colors">
                  <X className="w-5 h-5 text-gray-500" />
                </button>
              </div>
            </div>
          </div>

          {/* Content */}
          <div className="overflow-y-auto max-h-[calc(85vh-140px)]">
            {isLoading ? (
              <div className="flex items-center justify-center h-40">
                <Loader2 className="w-6 h-6 animate-spin text-violet-500" />
              </div>
            ) : !status ? (
              <div className="flex flex-col items-center justify-center h-40 text-gray-500">
                <XCircle className="w-8 h-8 mb-2" />
                <p className="text-sm">Failed to load design status</p>
              </div>
            ) : (
              <div className="p-6 space-y-6">
                {/* Branches */}
                {status?.branches?.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-2">
                      <GitBranch className="w-4 h-4" />
                      Branches
                    </h3>
                    <div className="flex flex-wrap gap-2">
                      {status.branches.map((branch: string) => (
                        <span key={branch} className="px-3 py-1 bg-gray-100 text-gray-700 text-xs font-mono rounded-lg">
                          {branch}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Workflows with Tasks */}
                {status?.workflows?.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-2">
                      <RotateCcw className="w-4 h-4" />
                      Workflow Runs ({status.workflows.length})
                    </h3>
                    <div className="space-y-3">
                      {status.workflows.map((wf: any) => {
                        const wfStatus = STATUS_CONFIG[wf.status] || STATUS_CONFIG.pending;
                        const wfTasks = (status.tasks || []).filter((t: any) => t.workflow_id === wf.id);
                        return (
                          <div key={wf.id} className="border border-gray-200 rounded-lg overflow-hidden">
                            <div className="flex items-center gap-3 p-3 bg-gray-50">
                              <span className={`px-2 py-1 text-xs font-semibold rounded-full flex items-center gap-1 ${wfStatus.color}`}>
                                {wfStatus.icon}
                                {wfStatus.label}
                              </span>
                              <span className="text-xs font-mono text-gray-500">{wf.id.substring(0, 8)}</span>
                              <span className="text-xs text-gray-400 ml-auto">
                                {wfTasks.length} task{wfTasks.length !== 1 ? 's' : ''}
                              </span>
                              {wf.created_at && (
                                <span className="text-xs text-gray-400">
                                  {formatDistanceToNow(new Date(wf.created_at), { addSuffix: true })}
                                </span>
                              )}
                            </div>
                            {/* Tasks under this workflow */}
                            {wfTasks.length > 0 && (
                              <div className="divide-y border-t">
                                {wfTasks.map((task: any) => {
                                  const taskColor = TASK_STATUS_COLORS[task.status] || 'bg-gray-100 text-gray-600';
                                  const isExpanded = expandedTasks.has(task.id);
                                  const phaseName = task.phase_name || 'unknown';
                                  return (
                                    <div key={task.id}>
                                      <button
                                        onClick={() => toggleTask(task.id)}
                                        className="w-full flex items-center gap-3 p-3 hover:bg-gray-50 transition-colors text-left"
                                      >
                                        {isExpanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
                                        <span className={`px-2 py-0.5 text-xs font-semibold rounded-full ${taskColor}`}>
                                          {task.status}
                                        </span>
                                        <span className="px-2 py-0.5 text-xs font-medium bg-violet-100 text-violet-700 rounded">
                                          {phaseName}
                                        </span>
                                        <span className="text-sm text-gray-700 flex-1 truncate">{task.description}</span>
                                        {/* Agent badge in collapsed view */}
                                        {task.agent_id && (
                                          <a
                                            href={`/agents/${task.agent_id}`}
                                            className="flex items-center gap-1 px-2 py-1 text-xs bg-violet-100 text-violet-700 rounded hover:bg-violet-200 transition-colors"
                                            onClick={(e) => e.stopPropagation()}
                                          >
                                            <span className={task.agent_status === 'working' ? 'w-1.5 h-1.5 rounded-full bg-green-500' : 'w-1.5 h-1.5 rounded-full bg-gray-400'}></span>
                                            {task.agent_id.substring(0, 6)}
                                            <ExternalLink className="w-3 h-3" />
                                          </a>
                                        )}
                                      </button>
                                      {isExpanded && (
                                        <div className="px-3 pb-3 pt-0 border-t border-gray-100">
                                          <p className="text-sm text-gray-600 mt-2 whitespace-pre-wrap">{task.description}</p>
                                          <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                                            {task.priority && <span>Priority: {task.priority}</span>}
                                          </div>
                                          {/* Agent info with deep link */}
                                          {task.agent_id && (
                                            <div className="flex items-center gap-2 mt-2 p-2 bg-gray-50 rounded-lg">
                                              <span className={`w-2 h-2 rounded-full ${task.agent_status === 'working' ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`}></span>
                                              <span className="text-xs font-mono text-gray-700">{task.agent_id.substring(0, 8)}</span>
                                              <span className="text-xs text-gray-500">({task.agent_status || 'unknown'})</span>
                                              <a
                                                href={`/agents/${task.agent_id}`}
                                                className="ml-auto px-2 py-1 text-xs bg-violet-100 text-violet-700 rounded hover:bg-violet-200 transition-colors"
                                                onClick={(e) => e.stopPropagation()}
                                              >
                                                <ExternalLink className="w-3 h-3 inline mr-1" />
                                                View
                                              </a>
                                            </div>
                                          )}
                                        </div>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Feature Folder */}
                {status?.feature_folder && (
                  <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-2">Feature Folder</h3>
                    <div className="flex items-center gap-2 p-3 bg-gray-50 rounded-lg">
                      <ExternalLink className="w-4 h-4 text-gray-400" />
                      <span className="text-xs font-mono text-gray-600 break-all">{status.feature_folder}</span>
                    </div>
                  </div>
                )}

                {/* Design Content (collapsible) */}
                {status?.content && (
                  <div>
                    <button
                      onClick={() => setShowContent(!showContent)}
                      className="flex items-center gap-2 text-sm font-semibold text-gray-700 hover:text-violet-600 transition-colors"
                    >
                      {showContent ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                      Design Document
                    </button>
                    {showContent && (
                      <div className="mt-2 p-4 bg-gray-50 rounded-lg max-h-64 overflow-y-auto">
                        <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">
                          {status.content}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t bg-gray-50 flex items-center justify-between">
            <span className="text-xs text-gray-400">
              {status?.tasks?.length || 0} tasks • {status?.workflows?.length || 0} runs
            </span>
            <div className="flex items-center gap-2">
              {onRerun && (
                <>
                  <Button
                    onClick={() => requeueMutation.mutate()}
                    disabled={requeueMutation.isPending}
                    variant="outline"
                    className="text-amber-600 border-amber-200 hover:bg-amber-50"
                  >
                    {requeueMutation.isPending ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <RotateCcw className="w-4 h-4 mr-1" />
                    )}
                    Requeue
                  </Button>
                  <Button
                    onClick={() => rerunMutation.mutate()}
                    disabled={rerunMutation.isPending}
                    variant="outline"
                    className="text-violet-600 border-violet-200 hover:bg-violet-50"
                  >
                    {rerunMutation.isPending ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <RotateCcw className="w-4 h-4 mr-1" />
                    )}
                    Rerun
                  </Button>
                </>
              )}
              <Button onClick={onClose} variant="outline">
                Close
              </Button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
};

export default DesignDetailModal;

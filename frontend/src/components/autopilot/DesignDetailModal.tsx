import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X, FileText, GitBranch, Clock, CheckCircle2, XCircle, AlertTriangle,
  Loader2, RotateCcw, ChevronDown, ChevronRight, ExternalLink, Play, Pause, Square
} from 'lucide-react';
import { MarkdownRenderer } from '@/utils/markdown';
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

const DesignDetailModal: React.FC<DesignDetailModalProps> = ({ projectId, filename, onClose, onRerun }) => {
  const queryClient = useQueryClient();
  const [showContent, setShowContent] = useState(false);

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

  // Pause: terminate the running agents (WIP auto-committed) and mark the run paused
  // so it can be Resumed later. Targets the design's active workflow.
  const pauseMutation = useMutation({
    mutationFn: () => {
      const wfs = (status?.workflows || []) as any[];
      const wf = wfs.find((w) => w.status === 'active') || wfs[0];
      if (!wf?.id) throw new Error('No active run to pause');
      return apiService.pauseWorkflow(wf.id);
    },
    onSuccess: () => {
      toast.success('Run paused — Resume to continue from the last committed phase');
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, filename] });
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || e?.message || 'Failed to pause'),
  });

  // Stop: terminate agents and end the run (marked failed). Use Rerun to start over.
  const stopMutation = useMutation({
    mutationFn: () => {
      const wfs = (status?.workflows || []) as any[];
      const wf = wfs.find((w) => ['active', 'paused'].includes(w.status)) || wfs[0];
      if (!wf?.id) throw new Error('No running run to stop');
      return apiService.cancelWorkflow(wf.id);
    },
    onSuccess: () => {
      toast.success('Run stopped');
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, filename] });
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || e?.message || 'Failed to stop'),
  });

  // Resume: non-destructive recovery of an interrupted run. Restarts the orphaned
  // phase agent on its existing worktree (prior commits + context intact) so the
  // run continues from the last committed phase — unlike Rerun, which starts the
  // design over from scratch. Targets this design's most recent interrupted workflow.
  const recoverMutation = useMutation({
    mutationFn: () => {
      const wfs = (status?.workflows || []) as any[];
      const wf = wfs.find((w) => ['active', 'paused', 'failed'].includes(w.status)) || wfs[0];
      return apiService.recoverWorkflow(wf?.id);
    },
    onSuccess: (data) => {
      toast.success(
        data.resumed_agents > 0
          ? `Resumed ${data.resumed_agents} agent(s) from last checkpoint`
          : 'Run reactivated — continuing from last committed phase'
      );
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, filename] });
    },
    onError: (e: any) => {
      toast.error(e?.response?.data?.detail || 'Failed to resume');
    },
  });

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
                {/* Failure reason */}
                {status?.error && (
                  <div className="rounded-lg border border-red-200 bg-red-50 p-3">
                    <h3 className="text-sm font-semibold text-red-800 mb-1 flex items-center gap-2">
                      <XCircle className="w-4 h-4" />
                      Why this failed
                    </h3>
                    <p className="text-sm text-red-700 whitespace-pre-wrap font-mono">
                      {status.error}
                    </p>
                  </div>
                )}

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

                {/* Workflows */}
                {status?.workflows?.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-2">
                      <RotateCcw className="w-4 h-4" />
                      Workflow Runs ({status.workflows.length})
                    </h3>
                    <div className="space-y-2">
                      {status.workflows.map((wf: any) => {
                        const wfStatus = STATUS_CONFIG[wf.status] || STATUS_CONFIG.pending;
                        return (
                          <div key={wf.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg">
                            <span className={`px-2 py-1 text-xs font-semibold rounded-full flex items-center gap-1 ${wfStatus.color}`}>
                              {wfStatus.icon}
                              {wfStatus.label}
                            </span>
                            <span className="text-xs font-mono text-gray-500">{wf.id.substring(0, 8)}</span>
                            {wf.created_at && (
                              <span className="text-xs text-gray-400 ml-auto">
                                {formatDistanceToNow(new Date(wf.created_at), { addSuffix: true })}
                              </span>
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
                      <div className="mt-2 p-4 bg-gray-50 rounded-lg max-h-96 overflow-y-auto prose prose-sm prose-violet max-w-none">
                        <MarkdownRenderer content={status.content} />
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
              {status?.workflows?.length || 0} runs
            </span>
            <div className="flex items-center gap-2">
              {onRerun && (
                <>
                  <Button
                    onClick={() => pauseMutation.mutate()}
                    disabled={pauseMutation.isPending}
                    variant="outline"
                    className="text-yellow-600 border-yellow-200 hover:bg-yellow-50"
                    title="Pause this run — agents stop (work is committed), Resume later"
                  >
                    {pauseMutation.isPending ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <Pause className="w-4 h-4 mr-1" />
                    )}
                    Pause
                  </Button>
                  <Button
                    onClick={() => stopMutation.mutate()}
                    disabled={stopMutation.isPending}
                    variant="outline"
                    className="text-red-600 border-red-200 hover:bg-red-50"
                    title="Stop this run and terminate its agents"
                  >
                    {stopMutation.isPending ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <Square className="w-4 h-4 mr-1" />
                    )}
                    Stop
                  </Button>
                  <Button
                    onClick={() => recoverMutation.mutate()}
                    disabled={recoverMutation.isPending}
                    variant="outline"
                    className="text-emerald-600 border-emerald-200 hover:bg-emerald-50"
                    title="Continue this run from the last committed phase (non-destructive)"
                  >
                    {recoverMutation.isPending ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <Play className="w-4 h-4 mr-1" />
                    )}
                    Resume
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

import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X, FileText, GitBranch, Clock, CheckCircle2, XCircle, AlertTriangle,
  Loader2, RotateCcw, ExternalLink, Play, Pause, Square
} from 'lucide-react';
import { MarkdownRenderer } from '@/utils/markdown';
import { apiService, api } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';
import { useProject } from '@/context/ProjectContext';

interface DesignDetailModalProps {
  projectId: string;
  designId: string;
  // Only for display and rerun (which posts a filename): status is fetched
  // by id, and a directory-sourced design has no filename at all.
  filename: string | null;
  // Shown in place of the filename when there is none (AutopilotDesign.spec_key).
  specKey: string;
  onClose: () => void;
  onRerun?: (filename: string | null) => void;
}

const STATUS_CONFIG: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300', icon: <Clock className="w-3.5 h-3.5" />, label: 'Pending' },
  active: { color: 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300', icon: <Loader2 className="w-3.5 h-3.5 animate-spin" />, label: 'Active' },
  completed: { color: 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300', icon: <CheckCircle2 className="w-3.5 h-3.5" />, label: 'Completed' },
  failed: { color: 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300', icon: <XCircle className="w-3.5 h-3.5" />, label: 'Failed' },
  paused: { color: 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-700 dark:text-yellow-300', icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Paused' },
};

type DetailTab = 'overview' | 'docs';

const DesignDetailModal: React.FC<DesignDetailModalProps> = ({ projectId, designId, filename, specKey, onClose, onRerun }) => {
  const queryClient = useQueryClient();
  const { projects } = useProject();
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');

  const { data: status, isLoading } = useQuery({
    queryKey: ['design-status', projectId, designId],
    queryFn: () => apiService.getAutopilotProjectDesignStatus(projectId, designId),
    refetchInterval: 5000,
  });

  const rerunMutation = useMutation({
    mutationFn: async () => {
      // This modal is always about a specific project (projectId prop) --
      // resolve THAT project's base_dir, not whichever one happens to be
      // globally active (which may not even be this one now that more
      // than one project can be active at once).
      const project = projects.find((p) => p.id === projectId);
      if (!project) throw new Error('Project not found');
      // /autopilot/queue/rerun addresses a design by filename, which a
      // directory-sourced design does not have -- the button below is
      // disabled for one, so this only guards a programmatic call.
      if (!filename) throw new Error('This design has no source file to rerun');
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
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, designId] });
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
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, designId] });
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
      queryClient.invalidateQueries({ queryKey: ['design-status', projectId, designId] });
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
          className="bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] overflow-hidden"
          onClick={e => e.stopPropagation()}
        >
          {/* Header */}
          <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 bg-gradient-to-r from-violet-50 to-purple-50 dark:from-violet-900/30 dark:to-purple-900/30">
            <div className="flex items-start justify-between">
              <div className="flex items-start gap-3">
                <div className="p-2 bg-violet-100 dark:bg-violet-800/50 rounded-lg">
                  <FileText className="w-5 h-5 text-violet-600 dark:text-violet-400" />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800 dark:text-gray-100">
                    {status?.name || filename?.replace(/\.md$/, '').replace(/_/g, ' ')}
                  </h2>
                  <p className="text-xs text-gray-500 font-mono mt-0.5">{filename ?? specKey}</p>
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

          {/* Tab Nav */}
          <div className="px-6 border-b flex gap-1">
            {([
              { id: 'overview', label: 'Overview' },
              { id: 'docs', label: 'Docs' },
            ] as { id: DetailTab; label: string }[]).map(({ id, label }) => (
              <button
                key={id}
                onClick={() => setActiveTab(id)}
                className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === id
                    ? 'border-violet-500 text-violet-600 dark:text-violet-400'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Content */}
          <div className="overflow-y-auto max-h-[calc(85vh-180px)]">
            {isLoading ? (
              <div className="flex items-center justify-center h-40">
                <Loader2 className="w-6 h-6 animate-spin text-violet-500" />
              </div>
            ) : !status ? (
              <div className="flex flex-col items-center justify-center h-40 text-gray-500">
                <XCircle className="w-8 h-8 mb-2" />
                <p className="text-sm">Failed to load design status</p>
              </div>
            ) : activeTab === 'overview' ? (
              <div className="p-6 space-y-6">
                {/* Warning — completed but with issues */}
                {status?.warning && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
                    <h3 className="text-sm font-semibold text-amber-800 mb-1 flex items-center gap-2">
                      <AlertTriangle className="w-4 h-4" />
                      Heads up
                    </h3>
                    <p className="text-sm text-amber-700 whitespace-pre-wrap">
                      {status.warning}
                    </p>
                  </div>
                )}

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
                    <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2 flex items-center gap-2">
                      <GitBranch className="w-4 h-4" />
                      Branches
                    </h3>
                    <div className="flex flex-wrap gap-2">
                      {status.branches.map((branch: string) => (
                        <span key={branch} className="px-3 py-1 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 text-xs font-mono rounded-lg">
                          {branch}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Workflows */}
                {status?.workflows?.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2 flex items-center gap-2">
                      <RotateCcw className="w-4 h-4" />
                      Workflow Runs ({status.workflows.length})
                    </h3>
                    <div className="space-y-2">
                      {status.workflows.map((wf: any) => {
                        const wfStatus = STATUS_CONFIG[wf.status] || STATUS_CONFIG.pending;
                        return (
                          <div key={wf.id} className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                            <span className={`px-2 py-1 text-xs font-semibold rounded-full flex items-center gap-1 ${wfStatus.color}`}>
                              {wfStatus.icon}
                              {wfStatus.label}
                            </span>
                            <span className="text-xs font-mono text-gray-500 dark:text-gray-400">{wf.id.substring(0, 8)}</span>
                            {wf.error && (
                              <span className="text-xs text-red-600 truncate max-w-[200px]" title={wf.error}>
                                {wf.error}
                              </span>
                            )}
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
                    <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Feature Folder</h3>
                    <div className="flex items-center gap-2 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                      <ExternalLink className="w-4 h-4 text-gray-400" />
                      <span className="text-xs font-mono text-gray-600 dark:text-gray-400 break-all">{status.feature_folder}</span>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="p-6">
                {status?.content ? (
                  <div className="prose prose-sm prose-violet dark:prose-invert max-w-none bg-gray-50 dark:bg-gray-700/50 rounded-xl p-4 border border-gray-200 dark:border-gray-600">
                    <MarkdownRenderer content={status.content} />
                  </div>
                ) : (
                  <div className="flex items-center justify-center h-40 text-gray-400 text-sm">
                    No design document content available
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 flex items-center justify-between">
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
                    className="text-yellow-600 dark:text-yellow-400 border-yellow-200 dark:border-yellow-600 hover:bg-yellow-50 dark:hover:bg-yellow-900/30"
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
                    className="text-red-600 dark:text-red-400 border-red-200 dark:border-red-600 hover:bg-red-50 dark:hover:bg-red-900/30"
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
                    className="text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-600 hover:bg-emerald-50 dark:hover:bg-emerald-900/30"
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
                    onClick={() => {
                      if (confirm(`Rerun "${filename}"? This restarts its pipeline from scratch, deletes its existing worktree (any uncommitted work in it is lost), and will also pause every other currently running pipeline.`)) {
                        rerunMutation.mutate();
                      }
                    }}
                    disabled={rerunMutation.isPending || !filename}
                    variant="outline"
                    className="text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/30"
                    title="Restart this design's pipeline from scratch (deletes its worktree, discarding uncommitted work) and pause every other running pipeline"
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

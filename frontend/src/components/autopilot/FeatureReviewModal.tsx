import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, CheckCircle2, RotateCcw, Clock, DollarSign, Layers, Eye, FileText, ListChecks, GitPullRequest } from 'lucide-react';
import { apiService } from '@/services/api';
import { StatusBadge, StatusIcon, formatTime } from '@/pages/Autopilot';
import { MarkdownRenderer } from '@/utils/markdown';
import toast from 'react-hot-toast';

interface FeatureReviewModalProps {
  featureId: string | null;
  /** Full feature object from design-status response — avoids hitting the
   *  legacy FEATURES_DIR scan endpoint which doesn't accept DB feat- ids. */
  feature?: any;
  projectId: string | null;
  onClose: () => void;
}

const FeatureReviewModal: React.FC<FeatureReviewModalProps> = ({ featureId, feature, projectId, onClose }) => {
  const queryClient = useQueryClient();
  const [feedback, setFeedback] = useState('');
  const [activeTab, setActiveTab] = useState<'report' | 'requirements'>('report');

  // This modal never unmounts (see the `if (!featureId) return null` guard
  // below, after all hooks) -- it just renders nothing while closed. So
  // switching from one feature straight to another leaves the previous
  // feature's typed feedback (and active tab) sitting in state, unseen by
  // the user until they'd already submitted it against the wrong feature.
  // Reset both whenever the feature being reviewed actually changes.
  useEffect(() => {
    setFeedback('');
    setActiveTab('report');
  }, [featureId]);

  // Phase 0 (Feature Architect) pseudo-features have no Feature DB row --
  // the feature-records docs endpoints below 404 for them, so their report
  // and review content is fetched by workflow id instead.
  const isPhase0 = !!featureId?.startsWith('phase0-');

  // Only load docs (to detect report presence); metadata comes from `feature` prop.
  const { data: featureDocs } = useQuery({
    queryKey: ['autopilot-feature-docs', featureId],
    queryFn: () => apiService.getFeatureRecordDocs(featureId!),
    enabled: !!featureId && !isPhase0,
  });
  const reportDoc = featureDocs?.docs.find((d: any) => d.name === 'feature_report.html');

  // Fetch requirements document (real features) or the decomposition's
  // adversarial review.md (Phase 0) -- same tab, different source.
  const { data: requirementsDoc } = useQuery({
    queryKey: ['autopilot-feature-requirements', featureId],
    queryFn: async () => {
      try {
        if (isPhase0) {
          const result = await apiService.getWorkflowDecompositionReview(feature?.workflow_id ?? featureId!.slice('phase0-'.length));
          return result.content;
        }
        const docs = await apiService.getFeatureRecordDocs(featureId!);
        const reqDoc = docs.docs.find((d: any) => d.name === 'requirements.md' || d.name === 'requirements_analysis.md');
        if (reqDoc) {
          const result = await apiService.getFeatureRecordDoc(featureId!, reqDoc.name);
          return result.content;
        }
      } catch {}
      return null;
    },
    // Phase 0 also eagerly fetches on the report tab -- feature_review.md
    // and feature_report.html are always written together by the same
    // task (02_feature_review.yaml steps 4+6), so this doubles as the
    // Phase 0 report-existence signal below (featureDocs, the real-feature
    // equivalent, is disabled for phase0- ids and feature.has_report is
    // never populated for pseudo-features -- without this, "Report not
    // yet available" showed even when the report genuinely existed).
    enabled: !!featureId && (isPhase0 || activeTab === 'requirements'),
  });

  // Also accept has_report from the feature prop as a fallback while docs load
  const hasReport = !!reportDoc || !!feature?.has_report || (isPhase0 && requirementsDoc != null);

  const reviewMutation = useMutation({
    mutationFn: ({ action, fb }: { action: 'approve' | 'request_changes'; fb?: string }) =>
      apiService.postFeatureReview(featureId!, action, fb),
    onSuccess: (data, vars) => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-design-statuses', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
      if (vars.action === 'approve') {
        // merged: false, auto_merge_queued: false means the approval
        // itself went through but landing the work on main did not (gh pr
        // merge and the local fallback both failed, usually a real
        // conflict) -- surfacing that here is the whole point: a blind
        // "approved" toast regardless of outcome is exactly how 4
        // conflicted PRs sat open with no one noticing. auto_merge_queued
        // is a DIFFERENT, non-error state -- GitHub armed --auto and will
        // complete the merge itself once required checks pass -- so it
        // must not show as a failure toast alongside genuine failures.
        if (data?.merged === false && !data?.auto_merge_queued) {
          toast.error(data.message || 'Feature approved, but merging into main failed — needs manual merge');
        } else {
          toast.success(data?.message || 'Feature approved — pipeline advancing');
        }
      } else {
        toast.success('Changes requested — feature queued for revision');
      }
      onClose();
    },
    onError: () => toast.error('Failed to submit review decision'),
  });

  if (!featureId) return null;

  const name = feature?.name ?? featureId;
  const status = feature?.status ?? 'paused';
  const tasks: any[] = feature?.tasks ?? [];
  const totalTime = tasks.reduce((s: number, t: any) => {
    if (t.created_at && t.completed_at) {
      return s + (new Date(t.completed_at).getTime() - new Date(t.created_at).getTime()) / 1000;
    }
    return s;
  }, 0);
  const costTotal: number = feature?.cost_total_usd ?? 0;
  const iterations = tasks.filter((t: any) => t.status === 'done').length;

  return (
    <AnimatePresence>
      <motion.div
        key="feature-review-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
        onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          className="bg-white rounded-2xl shadow-2xl w-full max-w-6xl h-[90vh] overflow-hidden flex flex-col"
        >
          {/* Header */}
          <div className="flex-none px-6 py-4 border-b flex items-center justify-between bg-gradient-to-r from-amber-50 to-white">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-amber-100">
                <Eye className="w-5 h-5 text-amber-600" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <StatusIcon status={status} />
                  <h2 className="text-lg font-bold text-gray-800">{name}</h2>
                  <StatusBadge status={status} />
                </div>
                <span className="text-xs font-semibold text-amber-600 tracking-wide uppercase mt-0.5 block">
                  Awaiting Your Review
                </span>
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Body — split pane */}
          <div className="flex-1 flex overflow-hidden min-h-0">
            {/* Left: content area with tabs */}
            <div className="flex-1 min-w-0 border-r border-gray-100 bg-gray-950 flex flex-col">
              {/* Tab bar */}
              <div className="flex-none flex border-b border-gray-800">
                <button
                  onClick={() => setActiveTab('report')}
                  className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                    activeTab === 'report'
                      ? 'text-white border-b-2 border-amber-500 bg-gray-900'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  <Eye className="w-4 h-4" />
                  Report
                </button>
                <button
                  onClick={() => setActiveTab('requirements')}
                  className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                    activeTab === 'requirements'
                      ? 'text-white border-b-2 border-amber-500 bg-gray-900'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  <ListChecks className="w-4 h-4" />
                  {isPhase0 ? 'Review Findings' : 'Requirements'}
                </button>
              </div>

              {/* Content */}
              <div className="flex-1 min-h-0">
                {activeTab === 'report' ? (
                  hasReport ? (
                    <iframe
                      src={isPhase0
                        ? `/api/autopilot/workflows/${encodeURIComponent(feature?.workflow_id ?? featureId.slice('phase0-'.length))}/feature_report`
                        : `/api/autopilot/feature-records/${encodeURIComponent(featureId)}/report`}
                      className="w-full h-full border-0"
                      title="Feature Report"
                    />
                  ) : (
                    <div className="flex flex-col items-center justify-center h-full text-gray-500 gap-3">
                      <Eye className="w-10 h-10 text-gray-600" />
                      <p className="text-sm">Report not yet available</p>
                    </div>
                  )
                ) : (
                  /* Requirements / Review Findings tab */
                  <div className="h-full overflow-y-auto p-6">
                    {requirementsDoc ? (
                      <MarkdownRenderer content={requirementsDoc} className="text-sm prose prose-sm prose-invert max-w-none text-gray-300" />
                    ) : (
                      <div className="flex flex-col items-center justify-center h-full text-gray-500 gap-3">
                        <FileText className="w-10 h-10 text-gray-600" />
                        <p className="text-sm">{isPhase0 ? 'Review findings not yet available' : 'Requirements not yet available'}</p>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Right: review panel */}
            <div className="w-80 flex-none flex flex-col overflow-y-auto">
              {/* Metrics */}
              <div className="p-5 border-b border-gray-100 space-y-3">
                <h3 className="text-sm font-semibold text-gray-700">Feature Summary</h3>
                {iterations > 0 && (
                  <div className="flex items-center gap-2 text-sm text-gray-600">
                    <Layers className="w-4 h-4 text-gray-400 flex-shrink-0" />
                    {iterations} task{iterations !== 1 ? 's' : ''} completed
                  </div>
                )}
                {totalTime > 0 && (
                  <div className="flex items-center gap-2 text-sm text-gray-600">
                    <Clock className="w-4 h-4 text-gray-400 flex-shrink-0" />
                    {formatTime(Math.round(totalTime))}
                  </div>
                )}
                {costTotal > 0 && (
                  <div className="flex items-center gap-2 text-sm text-gray-600">
                    <DollarSign className="w-4 h-4 text-gray-400 flex-shrink-0" />
                    ${costTotal.toFixed(4)}
                  </div>
                )}
                {feature?.scope && (
                  <p className="text-xs text-gray-500 leading-relaxed line-clamp-4">{feature.scope}</p>
                )}
                {feature?.pr_url && (
                  <a
                    href={feature.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800 hover:underline mt-2"
                  >
                    <GitPullRequest className="w-4 h-4" />
                    View Pull Request
                  </a>
                )}
              </div>

              {/* Feedback */}
              <div className="p-5 flex-1 flex flex-col gap-3">
                <label className="text-sm font-semibold text-gray-700">
                  Feedback
                  <span className="ml-1 text-xs font-normal text-gray-400">(required for changes)</span>
                </label>
                <textarea
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  placeholder="Describe what needs to change, or leave blank to approve…"
                  maxLength={2000}
                  rows={8}
                  className="w-full border border-gray-200 rounded-xl px-3 py-2.5 text-sm text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-amber-400 resize-y"
                />
                <p className="text-xs text-gray-400 text-right">{feedback.length}/2000</p>
              </div>

              {/* Action buttons */}
              <div className="px-5 pb-5 flex flex-col gap-3">
                <button
                  onClick={() => reviewMutation.mutate({ action: 'approve' })}
                  disabled={reviewMutation.isPending}
                  className="flex items-center justify-center gap-2 w-full px-4 py-3 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-sm transition-colors disabled:opacity-50"
                >
                  <CheckCircle2 className="w-4 h-4" />
                  Approve &amp; Continue
                </button>
                <button
                  onClick={() => reviewMutation.mutate({ action: 'request_changes', fb: feedback })}
                  disabled={reviewMutation.isPending || !feedback.trim()}
                  className="flex items-center justify-center gap-2 w-full px-4 py-3 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-semibold text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  title={!feedback.trim() ? 'Enter feedback before requesting changes' : undefined}
                >
                  <RotateCcw className="w-4 h-4" />
                  Request Changes
                </button>
              </div>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
};

export default FeatureReviewModal;

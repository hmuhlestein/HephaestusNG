import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, FileText, Copy, Clock, CheckCircle2, XCircle, Loader2, Layers, Trash2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { MarkdownRenderer } from '@/utils/markdown';
import { apiService } from '@/services/api';
import { FeatureStatusBadge } from './DesignQueuePanel';

interface FeatureRecordDetailModalProps {
  feature: any;
  onClose: () => void;
}

type DetailTab = 'overview' | 'docs';

// Icon per Feature Model status (pending/active/completed/failed/skipped) --
// distinct from pages/Autopilot's StatusIcon, which only recognizes the
// legacy feature-report vocabulary (validated/needs_review) and would
// silently render a generic clock for every Feature Model status here.
const FeatureRecordStatusIcon: React.FC<{ status: string }> = ({ status }) => {
  if (status === 'completed') return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
  if (status === 'failed') return <XCircle className="w-5 h-5 text-red-500" />;
  if (status === 'active') return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />;
  return <Clock className="w-5 h-5 text-gray-400" />;
};

const FeatureRecordDetailModal: React.FC<FeatureRecordDetailModalProps> = ({ feature, onClose }) => {
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');
  const [selectedDoc, setSelectedDoc] = useState<string | null>(null);

  const featureId = feature?.id;
  // Phase 0 and placeholder entries are synthetic (built from task/agent
  // data, not a real Feature DB row -- see get_project_design_status in
  // autopilot_api.py) -- /feature-records/{id}/docs only ever matches a
  // real Feature row, so skip the doomed request for these instead of
  // firing a request that will always 404.
  const isRealFeature = !!featureId && !featureId.startsWith('phase0-') && !featureId.startsWith('placeholder-');

  const { data: docsData } = useQuery({
    queryKey: ['feature-record-docs', featureId],
    queryFn: () => apiService.getFeatureRecordDocs(featureId),
    enabled: isRealFeature && activeTab === 'docs',
  });

  const { data: docContent } = useQuery({
    queryKey: ['feature-record-doc', featureId, selectedDoc],
    queryFn: () => apiService.getFeatureRecordDoc(featureId, selectedDoc!),
    enabled: isRealFeature && !!selectedDoc,
  });

  // `feature` is a point-in-time snapshot passed in by the parent (not
  // itself live/refetched) -- track a local copy so a task deleted here
  // disappears immediately instead of waiting for the modal to be
  // reopened against fresh data.
  const [tasks, setTasks] = useState<any[]>(feature?.tasks || []);
  useEffect(() => {
    setTasks(feature?.tasks || []);
  }, [feature]);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);

  const handleDeleteTask = async (task: any) => {
    if (!confirm(`Permanently delete this task${task.phase_name ? ` (${task.phase_name})` : ''}?`)) {
      return;
    }
    setDeletingTaskId(task.id);
    try {
      await apiService.deleteTask(task.id);
      setTasks((prev) => prev.filter((t) => t.id !== task.id));
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to delete task');
    } finally {
      setDeletingTaskId(null);
    }
  };

  if (!feature) return null;

  const doneCount = tasks.filter((t: any) => t.status === 'done').length;

  return (
    <AnimatePresence>
      {feature && (
        <motion.div
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
            className="bg-white rounded-2xl shadow-2xl w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b flex items-center justify-between bg-gradient-to-r from-gray-50 to-white">
              <div className="flex items-center gap-3">
                <FeatureRecordStatusIcon status={feature.status} />
                <div>
                  <h2 className="text-lg font-bold text-gray-800">{feature.name}</h2>
                  <div className="flex items-center gap-2 mt-0.5">
                    <FeatureStatusBadge status={feature.status} />
                    <span className="text-xs text-gray-400">·</span>
                    <span className="text-xs font-mono text-gray-500">{feature.feature_key}</span>
                  </div>
                </div>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
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
                      ? 'border-violet-500 text-violet-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto">
              {activeTab === 'overview' ? (
                <div className="p-6 space-y-6">
                  {/* Dependencies */}
                  {feature.depends_on?.length > 0 && (
                    <div>
                      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-2">Depends On</h3>
                      <div className="flex flex-wrap gap-2">
                        {feature.depends_on.map((dep: string) => (
                          <span
                            key={dep}
                            className="text-xs font-mono px-2.5 py-1 rounded-full bg-gray-100 text-gray-600 border border-gray-200"
                          >
                            {dep}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Stats Row */}
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
                    <div className="rounded-xl border border-gray-100 p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <div className="p-1.5 rounded-lg text-blue-600 bg-blue-50">
                          <Layers className="w-3.5 h-3.5" />
                        </div>
                        <span className="text-xs text-gray-500 uppercase tracking-wider">Tasks</span>
                      </div>
                      <p className="text-xl font-bold text-gray-800">{doneCount}/{tasks.length} done</p>
                    </div>
                    <div className="rounded-xl border border-gray-100 p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <div className="p-1.5 rounded-lg text-purple-600 bg-purple-50">
                          <Clock className="w-3.5 h-3.5" />
                        </div>
                        <span className="text-xs text-gray-500 uppercase tracking-wider">Created</span>
                      </div>
                      <p className="text-sm font-medium text-gray-800">
                        {feature.created_at ? new Date(feature.created_at).toLocaleString() : 'Unknown'}
                      </p>
                    </div>
                    <div className="rounded-xl border border-gray-100 p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <div className="p-1.5 rounded-lg text-emerald-600 bg-emerald-50">
                          <CheckCircle2 className="w-3.5 h-3.5" />
                        </div>
                        <span className="text-xs text-gray-500 uppercase tracking-wider">Completed</span>
                      </div>
                      <p className="text-sm font-medium text-gray-800">
                        {feature.completed_at ? new Date(feature.completed_at).toLocaleString() : '—'}
                      </p>
                    </div>
                  </div>

                  {/* Scope */}
                  {feature.scope && (
                    <div>
                      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-2">Scope</h3>
                      <p className="text-sm text-gray-600 leading-relaxed rounded-xl border border-gray-100 p-4 bg-gray-50">
                        {feature.scope}
                      </p>
                    </div>
                  )}

                  {/* Tasks */}
                  {tasks.length > 0 && (
                    <div>
                      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-2">Tasks</h3>
                      <div className="space-y-1.5">
                        {tasks.map((t: any) => (
                          <div
                            key={t.id}
                            className="flex items-center justify-between text-xs rounded-lg border border-gray-100 px-3 py-2"
                          >
                            <span className="text-gray-600 truncate flex-1">{t.description || t.id}</span>
                            <span className="text-gray-400 ml-2 flex-shrink-0">{t.status}</span>
                            <button
                              onClick={() => handleDeleteTask(t)}
                              disabled={deletingTaskId === t.id}
                              className="ml-2 p-1 rounded text-gray-300 hover:bg-red-50 hover:text-red-600 transition-colors disabled:opacity-30"
                              title="Delete task"
                            >
                              {deletingTaskId === t.id ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              ) : (
                                <Trash2 className="w-3 h-3" />
                              )}
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex h-[600px]">
                  {/* File list */}
                  <div className="w-64 border-r overflow-y-auto bg-gray-50">
                    <div className="p-3">
                      <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Docs</h4>
                      {(docsData?.docs || []).map((d: any) => (
                        <button
                          key={d.name}
                          onClick={() => setSelectedDoc(d.name)}
                          className={`w-full text-left px-3 py-2 rounded-lg text-sm mb-1 transition-colors flex items-center gap-2 ${
                            selectedDoc === d.name
                              ? 'bg-violet-100 text-violet-700'
                              : 'text-gray-600 hover:bg-gray-100'
                          }`}
                        >
                          <FileText className="w-3.5 h-3.5 flex-shrink-0" />
                          <span className="truncate">{d.name}</span>
                        </button>
                      ))}
                      {!isRealFeature ? (
                        <p className="text-xs text-gray-400 italic px-3">Docs aren't tracked for this entry</p>
                      ) : (
                        docsData && docsData.docs.length === 0 && (
                          <p className="text-xs text-gray-400 italic px-3">No docs generated yet</p>
                        )
                      )}
                    </div>
                  </div>

                  {/* Content */}
                  <div className="flex-1 overflow-y-auto p-5">
                    {selectedDoc && docContent ? (
                      <div>
                        <div className="flex items-center justify-between mb-3">
                          <h4 className="text-sm font-semibold text-gray-700">{docContent.name}</h4>
                          <button
                            onClick={() => navigator.clipboard.writeText(docContent.content)}
                            className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
                          >
                            <Copy className="w-3 h-3" /> Copy
                          </button>
                        </div>
                        <div className="text-sm text-gray-700 prose prose-sm prose-violet max-w-none bg-gray-50 rounded-xl p-4 border">
                          <MarkdownRenderer content={docContent.content} />
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-center justify-center h-full text-gray-400 text-sm">
                        Select a document to view
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

export default FeatureRecordDetailModal;

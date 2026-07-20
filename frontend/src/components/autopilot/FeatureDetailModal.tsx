import React, { useState, useRef, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X, Download, FileText, FileBarChart2, CheckCircle2, XCircle, AlertTriangle,
  Clock, DollarSign, Layers, Shield, Beaker, BookOpen, Code, Microscope, Copy,
  Terminal
} from 'lucide-react';
import { MarkdownRenderer } from '@/utils/markdown';
import { apiService } from '@/services/api';
import { StatusBadge, StatusIcon, formatTime } from '@/pages/Autopilot';

interface FeatureDetailModalProps {
  featureId: string | null;
  onClose: () => void;
}

type DetailTab = 'overview' | 'report' | 'docs' | 'logs';

const FeatureDetailModal: React.FC<FeatureDetailModalProps> = ({ featureId, onClose }) => {
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');
  const [selectedDoc, setSelectedDoc] = useState<string | null>(null);
  const [selectedLog, setSelectedLog] = useState<string | null>(null);

  const { data: detail, isLoading } = useQuery({
    queryKey: ['autopilot-feature', featureId],
    queryFn: () => apiService.getAutopilotFeatureDetail(featureId!),
    enabled: !!featureId,
  });

  // feature_report.html and the rest of a feature's generated docs live
  // under its own workflow's working_directory, not the legacy
  // FEATURES_DIR archival scan the old /features/{id}/report and
  // /features/{id}/docs/{name} endpoints read from -- that scan only has
  // an entry once the full 12-phase pipeline finishes and PhaseManager.
  // _populate_feature_folder archives it, so both tabs were empty for
  // every feature until then. feature-records reads the live worktree
  // instead, same as the report icon on the feature row.
  // Not gated to the docs/report tabs -- the header's "Download Report"
  // link (visible from any tab, including the default Overview one) also
  // needs to know whether feature_report.html exists.
  const { data: featureDocs } = useQuery({
    queryKey: ['autopilot-feature-docs', featureId],
    queryFn: () => apiService.getFeatureRecordDocs(featureId!),
    enabled: !!featureId,
  });

  const reportDoc = featureDocs?.docs.find((d) => d.name === 'feature_report.html');

  const { data: reportHtml } = useQuery({
    queryKey: ['autopilot-feature-report', featureId],
    queryFn: () => apiService.getFeatureRecordDoc(featureId!, 'feature_report.html'),
    enabled: !!featureId && activeTab === 'report' && !!reportDoc,
    select: (data) => data.content,
  });

  const { data: doc } = useQuery({
    queryKey: ['autopilot-doc', featureId, selectedDoc],
    queryFn: () => apiService.getFeatureRecordDoc(featureId!, selectedDoc!),
    enabled: !!featureId && !!selectedDoc,
  });

  const { data: logsIndex } = useQuery({
    queryKey: ['autopilot-feature-logs', featureId],
    queryFn: () => apiService.getAutopilotFeatureLogs(featureId!),
    enabled: !!featureId && activeTab === 'logs',
  });

  const { data: logContent } = useQuery({
    queryKey: ['autopilot-log', featureId, selectedLog],
    queryFn: () => apiService.getAutopilotFeatureLog(featureId!, selectedLog!),
    enabled: !!featureId && !!selectedLog,
    refetchInterval: activeTab === 'logs' && !!selectedLog ? 3000 : false,
  });

  if (!featureId) return null;

  const phaseIcons: Record<string, React.ElementType> = {
    requirements_summary: BookOpen,
    architecture_summary: Code,
    security_summary: Shield,
    qa_summary: Beaker,
    product_validation_summary: CheckCircle2,
    forensics_summary: Microscope,
  };

  const phaseLabels: Record<string, string> = {
    requirements_summary: 'Requirements',
    architecture_summary: 'Architecture',
    security_summary: 'Security Review',
    qa_summary: 'QA Validation',
    product_validation_summary: 'Product Validation',
    forensics_summary: 'Forensics Analysis',
  };

  return (
    <AnimatePresence>
      {featureId && (
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
                {detail && <StatusIcon status={detail.status} />}
                <div>
                  <h2 className="text-lg font-bold text-gray-800">
                    {isLoading ? 'Loading...' : detail?.name || featureId}
                  </h2>
                  {detail && (
                    <div className="flex items-center gap-2 mt-0.5">
                      <StatusBadge status={detail.status} />
                      <span className="text-xs text-gray-400">·</span>
                      <span className="text-xs text-gray-500">{detail.stop_reason.replace(/_/g, ' ')}</span>
                    </div>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {reportDoc && (
                  <a
                    href={`/api/autopilot/feature-records/${encodeURIComponent(featureId)}/report`}
                    className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
                    title="Open Report"
                    target="_blank"
                  >
                    <Download className="w-4 h-4" />
                  </a>
                )}
                <button
                  onClick={onClose}
                  className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Tab Nav */}
            <div className="px-6 border-b flex gap-1">
              {([
                { id: 'overview', label: 'Overview' },
                { id: 'report',   label: 'Report' },
                { id: 'docs',     label: 'Docs' },
                { id: 'logs',     label: 'Phase Logs' },
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
              {isLoading ? (
                <div className="flex items-center justify-center h-64">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
                </div>
              ) : activeTab === 'overview' ? (
                <OverviewTab detail={detail} phaseIcons={phaseIcons} phaseLabels={phaseLabels} />
              ) : activeTab === 'report' ? (
                <ReportTab html={reportHtml} />
              ) : activeTab === 'docs' ? (
                <DocsTab
                  docs={featureDocs?.docs}
                  selectedDoc={selectedDoc}
                  docContent={doc}
                  onSelectDoc={setSelectedDoc}
                />
              ) : (
                <LogsTab
                  logs={logsIndex?.logs ?? []}
                  selectedLog={selectedLog}
                  logContent={logContent}
                  onSelectLog={setSelectedLog}
                />
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

// ── Overview Tab ────────────────────────────────────────────

const OverviewTab: React.FC<{
  detail: any;
  phaseIcons: Record<string, React.ElementType>;
  phaseLabels: Record<string, string>;
}> = ({ detail, phaseIcons, phaseLabels }) => {
  if (!detail) return null;
  return (
  <div className="p-6 space-y-6">
    {/* Stats Row */}
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
      {[
        { label: 'Iterations', value: detail.iterations ?? 0, icon: Layers, color: 'text-blue-600 bg-blue-50' },
        { label: 'Duration', value: formatTime(detail.total_time_seconds ?? 0), icon: Clock, color: 'text-purple-600 bg-purple-50' },
        { label: 'QA Passed', value: detail.qa_passed ? 'Yes' : 'No', icon: detail.qa_passed ? CheckCircle2 : XCircle, color: detail.qa_passed ? 'text-emerald-600 bg-emerald-50' : 'text-red-600 bg-red-50' },
        { label: 'Cost', value: detail.cost_total > 0 ? `$${detail.cost_total.toFixed(2)}` : 'N/A', icon: DollarSign, color: 'text-amber-600 bg-amber-50' },
      ].map((s) => (
        <div key={s.label} className="rounded-xl border border-gray-100 p-4">
          <div className="flex items-center gap-2 mb-2">
            <div className={`p-1.5 rounded-lg ${s.color}`}>
              <s.icon className="w-3.5 h-3.5" />
            </div>
            <span className="text-xs text-gray-500 uppercase tracking-wider">{s.label}</span>
          </div>
          <p className="text-xl font-bold text-gray-800">{s.value}</p>
        </div>
      ))}
    </div>

    {/* Phase Summaries */}
    <div>
      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-3">Phase Reports</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {Object.entries(phaseLabels).map(([key, label]) => {
          const Icon = phaseIcons[key] || FileText;
          const summary = detail[key] || '';
          return (
            <div key={key} className="rounded-xl border border-gray-100 p-4 hover:shadow-sm transition-shadow">
              <div className="flex items-center gap-2 mb-2">
                <Icon className="w-4 h-4 text-violet-600" />
                <span className="text-sm font-medium text-gray-700">{label}</span>
              </div>
              {summary ? (
                <p className="text-xs text-gray-500 line-clamp-4 leading-relaxed">{summary}</p>
              ) : (
                <p className="text-xs text-gray-400 italic">No summary available</p>
              )}
            </div>
          );
        })}
      </div>
    </div>

    {/* Issues */}
    {(detail.issues_resolved?.length > 0 || detail.outstanding_issues?.length > 0) && (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {detail.issues_resolved?.length > 0 && (
          <div className="rounded-xl border border-emerald-100 bg-emerald-50/30 p-4">
            <h4 className="text-sm font-semibold text-emerald-700 mb-2 flex items-center gap-1">
              <CheckCircle2 className="w-4 h-4" /> Issues Resolved ({detail.issues_resolved.length})
            </h4>
            <ul className="space-y-1">
              {detail.issues_resolved.map((issue: string, i: number) => (
                <li key={i} className="text-xs text-emerald-800 flex items-start gap-1">
                  <span className="text-emerald-500 mt-0.5">•</span>
                  {issue}
                </li>
              ))}
            </ul>
          </div>
        )}
        {detail.outstanding_issues?.length > 0 && (
          <div className="rounded-xl border border-amber-100 bg-amber-50/30 p-4">
            <h4 className="text-sm font-semibold text-amber-700 mb-2 flex items-center gap-1">
              <AlertTriangle className="w-4 h-4" /> Outstanding Issues ({detail.outstanding_issues.length})
            </h4>
            <ul className="space-y-1">
              {detail.outstanding_issues.map((issue: string, i: number) => (
                <li key={i} className="text-xs text-amber-800 flex items-start gap-1">
                  <span className="text-amber-500 mt-0.5">•</span>
                  {issue}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    )}
  </div>
  );
};

// ── Report Tab ──────────────────────────────────────────────

const ReportTab: React.FC<{ html: string | undefined }> = ({ html }) => (
  <div className="h-full">
    {html ? (
      <iframe
        srcDoc={html}
        className="w-full h-[700px] border-0"
        title="Feature Report"
        sandbox="allow-scripts"
      />
    ) : (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <FileText className="w-12 h-12 text-gray-300 mx-auto mb-3" />
          <p className="text-sm text-gray-500">No report available yet</p>
          <p className="text-xs text-gray-400 mt-1">Generated by the doc_review phase</p>
        </div>
      </div>
    )}
  </div>
);

// ── Docs Tab ───────────────────────────────────────────

const isHtmlDoc = (name: string) => name.toLowerCase().endsWith('.html');

const DocsTab: React.FC<{
  docs: Array<{ name: string; type?: string }> | undefined;
  selectedDoc: string | null;
  docContent: any;
  onSelectDoc: (name: string) => void;
}> = ({ docs, selectedDoc, docContent, onSelectDoc }) => (
  <div className="flex h-[600px]">
    {/* File list */}
    <div className="w-64 border-r overflow-y-auto bg-gray-50">
      <div className="p-3">
        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Docs</h4>
        {(docs || []).map((d) => (
          <button
            key={d.name}
            onClick={() => onSelectDoc(d.name)}
            className={`w-full text-left px-3 py-2 rounded-lg text-sm mb-1 transition-colors flex items-center gap-2 ${
              selectedDoc === d.name
                ? 'bg-violet-100 text-violet-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            {isHtmlDoc(d.name) ? (
              <FileBarChart2 className="w-3.5 h-3.5 flex-shrink-0 text-emerald-500" />
            ) : (
              <FileText className="w-3.5 h-3.5 flex-shrink-0" />
            )}
            <span className="truncate">{d.name}</span>
          </button>
        ))}
        {docs?.length === 0 && (
          <p className="text-xs text-gray-400 px-3 py-2">No docs generated yet</p>
        )}
      </div>
    </div>

    {/* Content */}
    <div className="flex-1 overflow-y-auto p-5">
      {selectedDoc && docContent ? (
        isHtmlDoc(selectedDoc) ? (
          <iframe
            srcDoc={docContent.content}
            className="w-full h-[560px] border rounded-xl"
            title={docContent.name}
            sandbox="allow-scripts"
          />
        ) : (
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
        )
      ) : (
        <div className="flex items-center justify-center h-full text-gray-400 text-sm">
          Select a document to view
        </div>
      )}
    </div>
  </div>
);

// ── Logs Tab ───────────────────────────────────────────────────

function stripAnsi(str: string): string {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1b\[[0-9;]*[mGKHF]/g, '');
}


function phaseLabel(filename: string): string {
  // e.g. "development_a1b2c3d4.log" → "development · a1b2c3d4"
  const base = filename.replace(/\.log$/, '');
  const lastUnderscore = base.lastIndexOf('_');
  if (lastUnderscore === -1) return base;
  return base.slice(0, lastUnderscore).replace(/_/g, ' ') + ' · ' + base.slice(lastUnderscore + 1);
}

const LogsTab: React.FC<{
  logs: Array<{ name: string; size_bytes: number; modified: string }>;
  selectedLog: string | null;
  logContent: { name: string; content: string } | undefined;
  onSelectLog: (name: string) => void;
}> = ({ logs, selectedLog, logContent, onSelectLog }) => {
  const preRef = useRef<HTMLPreElement>(null);
  const [following, setFollowing] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = (msg: string) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 2000);
  };

  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  const handleMouseUp = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const text = sel.toString();
    if (!text.trim()) return;
    navigator.clipboard.writeText(text).then(() => {
      showToast(`Copied ${text.length} chars`);
    });
  };

  // When content updates, scroll to bottom only if already in follow mode.
  useEffect(() => {
    if (following && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [logContent?.content, following]);

  // Reset to top (no follow) when switching to a new log file.
  useEffect(() => {
    setFollowing(false);
    if (preRef.current) preRef.current.scrollTop = 0;
  }, [selectedLog]);

  const handleScroll = () => {
    const el = preRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    setFollowing(atBottom);
  };

  return (
    <div className="flex h-[600px]">
      {/* File list */}
      <div className="w-64 border-r overflow-y-auto bg-gray-900">
        <div className="p-3">
          <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2 px-1">Phase Logs</h4>
          {logs.length === 0 ? (
            <p className="text-xs text-gray-500 px-1 italic">No logs available</p>
          ) : logs.map((log) => (
            <button
              key={log.name}
              onClick={() => onSelectLog(log.name)}
              className={`w-full text-left px-3 py-2 rounded-lg text-xs mb-1 transition-colors flex items-start gap-2 ${
                selectedLog === log.name
                  ? 'bg-violet-800 text-violet-100'
                  : 'text-gray-300 hover:bg-gray-800'
              }`}
            >
              <Terminal className="w-3 h-3 flex-shrink-0 mt-0.5 text-gray-500" />
              <div className="min-w-0">
                <div className="truncate font-mono">{phaseLabel(log.name)}</div>
                <div className="text-gray-500 text-[10px]">
                  {(log.size_bytes / 1024).toFixed(1)} KB
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="relative flex-1 overflow-hidden flex flex-col bg-gray-950">
        {toast && (
          <div className="absolute bottom-4 right-4 z-10 bg-gray-800 text-green-300 text-xs px-3 py-1.5 rounded-lg shadow-lg pointer-events-none select-none">
            {toast}
          </div>
        )}
        {selectedLog && logContent ? (
          <>
            <div className="flex items-center justify-between px-4 py-2 border-b border-gray-800 bg-gray-900">
              <span className="text-xs font-mono text-gray-300">{phaseLabel(logContent.name)}</span>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => {
                    setFollowing(true);
                    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
                  }}
                  className={`text-xs flex items-center gap-1 transition-colors ${
                    following
                      ? 'text-violet-400 cursor-default'
                      : 'text-gray-500 hover:text-violet-300'
                  }`}
                >
                  {following ? '● Following' : '↓ Follow'}
                </button>
                <button
                  onClick={() => navigator.clipboard.writeText(stripAnsi(logContent.content))}
                  className="text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1 transition-colors"
                >
                  <Copy className="w-3 h-3" /> Copy
                </button>
              </div>
            </div>
            <pre
              ref={preRef}
              onScroll={handleScroll}
              onMouseUp={handleMouseUp}
              className="flex-1 overflow-y-auto p-4 text-xs text-green-300 font-mono leading-relaxed whitespace-pre-wrap break-all"
            >
              {stripAnsi(logContent.content)}
            </pre>
          </>
        ) : (
          <div className="flex items-center justify-center h-full">
            <div className="text-center text-gray-600">
              <Terminal className="w-10 h-10 mx-auto mb-2 opacity-30" />
              <p className="text-sm">Select a phase log to view</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default FeatureDetailModal;

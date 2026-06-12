import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X, Download, ExternalLink, FileText, CheckCircle2, XCircle, AlertTriangle,
  Clock, DollarSign, Layers, Shield, Beaker, BookOpen, Code, Microscope, Copy
} from 'lucide-react';
import { apiService } from '@/services/api';
import { StatusBadge, StatusIcon, formatTime } from '@/pages/Autopilot';

interface FeatureDetailModalProps {
  featureId: string | null;
  onClose: () => void;
}

type DetailTab = 'overview' | 'report' | 'artifacts';

const FeatureDetailModal: React.FC<FeatureDetailModalProps> = ({ featureId, onClose }) => {
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);

  const { data: detail, isLoading } = useQuery({
    queryKey: ['autopilot-feature', featureId],
    queryFn: () => apiService.getAutopilotFeatureDetail(featureId!),
    enabled: !!featureId,
  });

  const { data: reportHtml } = useQuery({
    queryKey: ['autopilot-feature-report', featureId],
    queryFn: () => apiService.getAutopilotFeatureReport(featureId!),
    enabled: !!featureId && activeTab === 'report',
  });

  const { data: artifact } = useQuery({
    queryKey: ['autopilot-artifact', featureId, selectedArtifact],
    queryFn: () => apiService.getAutopilotFeatureArtifact(featureId!, selectedArtifact!),
    enabled: !!featureId && !!selectedArtifact,
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
                {detail?.has_report !== false && (
                  <a
                    href={`/api/autopilot/features/${encodeURIComponent(featureId)}/download`}
                    className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
                    title="Download Report"
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
              {(['overview', 'report', 'artifacts'] as DetailTab[]).map((tab) => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors capitalize ${
                    activeTab === tab
                      ? 'border-violet-500 text-violet-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}
                >
                  {tab}
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
                <ReportTab html={reportHtml} featureId={featureId} />
              ) : (
                <ArtifactsTab
                  detail={detail}
                  selectedArtifact={selectedArtifact}
                  artifactContent={artifact}
                  onSelectArtifact={setSelectedArtifact}
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
}> = ({ detail, phaseIcons, phaseLabels }) => (
  <div className="p-6 space-y-6">
    {/* Stats Row */}
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
      {[
        { label: 'Iterations', value: detail.iterations, icon: Layers, color: 'text-blue-600 bg-blue-50' },
        { label: 'Duration', value: formatTime(detail.total_time_seconds), icon: Clock, color: 'text-purple-600 bg-purple-50' },
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

// ── Report Tab ──────────────────────────────────────────────

const ReportTab: React.FC<{ html: string | undefined; featureId: string }> = ({ html, featureId }) => (
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
          <p className="text-sm text-gray-500">No report available</p>
          <a
            href={`/api/autopilot/features/${encodeURIComponent(featureId)}/report`}
            target="_blank"
            className="text-xs text-violet-600 hover:text-violet-700 mt-2 inline-flex items-center gap-1"
          >
            Open in new tab <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      </div>
    )}
  </div>
);

// ── Artifacts Tab ───────────────────────────────────────────

const ArtifactsTab: React.FC<{
  detail: any;
  selectedArtifact: string | null;
  artifactContent: any;
  onSelectArtifact: (name: string) => void;
}> = ({ detail, selectedArtifact, artifactContent, onSelectArtifact }) => (
  <div className="flex h-[600px]">
    {/* File list */}
    <div className="w-64 border-r overflow-y-auto bg-gray-50">
      <div className="p-3">
        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Artifacts</h4>
        {(detail?.artifacts || []).map((a: any) => (
          <button
            key={a.name}
            onClick={() => onSelectArtifact(a.name)}
            className={`w-full text-left px-3 py-2 rounded-lg text-sm mb-1 transition-colors flex items-center gap-2 ${
              selectedArtifact === a.name
                ? 'bg-violet-100 text-violet-700'
                : 'text-gray-600 hover:bg-gray-100'
            }`}
          >
            <FileText className="w-3.5 h-3.5 flex-shrink-0" />
            <span className="truncate">{a.name}</span>
          </button>
        ))}
      </div>
    </div>

    {/* Content */}
    <div className="flex-1 overflow-y-auto p-5">
      {selectedArtifact && artifactContent ? (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-semibold text-gray-700">{artifactContent.name}</h4>
            <button
              onClick={() => navigator.clipboard.writeText(artifactContent.content)}
              className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
            >
              <Copy className="w-3 h-3" /> Copy
            </button>
          </div>
          <pre className="text-sm text-gray-700 whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 rounded-xl p-4 border">
            {artifactContent.content}
          </pre>
        </div>
      ) : (
        <div className="flex items-center justify-center h-full text-gray-400 text-sm">
          Select an artifact to view
        </div>
      )}
    </div>
  </div>
);

export default FeatureDetailModal;

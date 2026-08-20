import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Lightbulb, Check, X, Undo2, ChevronDown, ChevronRight,
  AlertTriangle, GitCommit, ShieldCheck,
} from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

/**
 * Review queue for prompt rewrites proposed by forensics_analysis.
 *
 * Approving one WRITES the phase YAML and commits it, so the diff is the
 * product here — a reviewer must be able to see exactly what changes before
 * agreeing to it, not just a summary of the intent.
 */

type Proposal = {
  id: string;
  phase_name: string;
  field: string;
  proposing_phase?: string | null;
  proposed_value: any;
  current_value?: any;
  previous_value?: any;
  is_stale?: boolean;
  rationale: string;
  evidence?: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'applied' | 'reverted' | 'failed';
  review_note?: string | null;
  applied_commit_sha?: string | null;
  created_at?: string | null;
};

const asText = (value: any): string => {
  if (value == null) return '';
  if (Array.isArray(value)) return value.map((v) => `- ${v}`).join('\n');
  return String(value);
};

/** Cheap line-level diff — enough to see what moved without pulling in a lib. */
const diffLines = (before: string, after: string) => {
  const a = before.split('\n');
  const b = after.split('\n');
  const bSet = new Set(b);
  const aSet = new Set(a);
  return {
    removed: a.map((line) => ({ line, changed: !bSet.has(line) })),
    added: b.map((line) => ({ line, changed: !aSet.has(line) })),
  };
};

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400',
  applied: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400',
  rejected: 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
  reverted: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
  failed: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
};

const ProposalCard: React.FC<{
  proposal: Proposal;
  onApprove: (note?: string) => void;
  onReject: (note?: string) => void;
  onRevert: () => void;
  busy: boolean;
}> = ({ proposal, onApprove, onReject, onRevert, busy }) => {
  const [expanded, setExpanded] = useState(proposal.status === 'pending');
  const [note, setNote] = useState('');

  const before = asText(proposal.current_value ?? proposal.previous_value);
  const after = asText(proposal.proposed_value);
  const { removed, added } = diffLines(before, after);

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-900">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-start gap-3 p-4 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-4 h-4 mt-1 text-gray-400 shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 mt-1 text-gray-400 shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {proposal.phase_name}
            </span>
            <code className="text-xs px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
              {proposal.field}
            </code>
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_STYLES[proposal.status] || ''}`}>
              {proposal.status}
            </span>
            {proposal.is_stale && proposal.status === 'pending' && (
              <span
                className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400"
                title="The file changed after this was proposed — the 'before' shown is the current file, not what the agent saw."
              >
                <AlertTriangle className="w-3 h-3" /> stale
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400 line-clamp-2">
            {proposal.rationale}
          </p>
        </div>
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden border-t border-gray-200 dark:border-gray-700"
          >
            <div className="p-4 space-y-4">
              {proposal.is_stale && proposal.status === 'pending' && (
                <div className="flex gap-2 p-3 rounded-md bg-orange-50 dark:bg-orange-900/20 text-sm text-orange-800 dark:text-orange-300">
                  <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                  <span>
                    This file changed after the proposal was filed. The “before” below is
                    the file as it stands now — approving replaces <em>that</em>, not what
                    the agent originally read.
                  </span>
                </div>
              )}

              <div>
                <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
                  Why
                </div>
                <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                  {proposal.rationale}
                </p>
              </div>

              {proposal.evidence && (
                <div>
                  <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
                    Evidence
                  </div>
                  <pre className="text-xs p-3 rounded bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300 overflow-x-auto whitespace-pre-wrap">
                    {proposal.evidence}
                  </pre>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                <div>
                  <div className="text-xs font-medium text-red-600 dark:text-red-400 mb-1">
                    Current
                  </div>
                  <pre className="text-xs p-3 rounded bg-red-50/50 dark:bg-red-900/10 overflow-x-auto max-h-80 leading-relaxed">
                    {removed.map((r, i) => (
                      <div
                        key={i}
                        className={r.changed
                          ? 'bg-red-100 dark:bg-red-900/30 text-red-900 dark:text-red-200'
                          : 'text-gray-600 dark:text-gray-400'}
                      >
                        {r.line || ' '}
                      </div>
                    ))}
                  </pre>
                </div>
                <div>
                  <div className="text-xs font-medium text-green-600 dark:text-green-400 mb-1">
                    Proposed
                  </div>
                  <pre className="text-xs p-3 rounded bg-green-50/50 dark:bg-green-900/10 overflow-x-auto max-h-80 leading-relaxed">
                    {added.map((r, i) => (
                      <div
                        key={i}
                        className={r.changed
                          ? 'bg-green-100 dark:bg-green-900/30 text-green-900 dark:text-green-200'
                          : 'text-gray-600 dark:text-gray-400'}
                      >
                        {r.line || ' '}
                      </div>
                    ))}
                  </pre>
                </div>
              </div>

              {proposal.applied_commit_sha && (
                <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                  <GitCommit className="w-3.5 h-3.5" />
                  Committed as
                  <code className="px-1 rounded bg-gray-100 dark:bg-gray-800">
                    {proposal.applied_commit_sha.slice(0, 8)}
                  </code>
                </div>
              )}

              {proposal.review_note && (
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  Note: {proposal.review_note}
                </div>
              )}

              {proposal.status === 'pending' && (
                <div className="space-y-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Optional note (required context if rejecting)…"
                    className="w-full px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={() => onApprove(note || undefined)}
                      disabled={busy}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-md bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white transition-colors"
                    >
                      <Check className="w-4 h-4" />
                      Approve &amp; apply
                    </button>
                    <button
                      onClick={() => onReject(note || undefined)}
                      disabled={busy}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 transition-colors"
                    >
                      <X className="w-4 h-4" />
                      Reject
                    </button>
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    Approving writes the change to <code>{proposal.phase_name}.yaml</code> and
                    commits it. You can revert it afterwards.
                  </p>
                </div>
              )}

              {proposal.status === 'applied' && (
                <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
                  <button
                    onClick={onRevert}
                    disabled={busy}
                    className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 transition-colors"
                  >
                    <Undo2 className="w-4 h-4" />
                    Revert
                  </button>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};

const ImprovementsPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const [showHistory, setShowHistory] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ['prompt-proposals'],
    queryFn: () => apiService.getPromptProposals(),
    refetchInterval: 15000,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['prompt-proposals'] });

  const approve = useMutation({
    mutationFn: ({ id, note }: { id: string; note?: string }) =>
      apiService.approvePromptProposal(id, note),
    onSuccess: () => { toast.success('Applied and committed'); invalidate(); },
    onError: (e: any) =>
      toast.error(e?.response?.data?.detail || 'Could not apply the change'),
  });

  const reject = useMutation({
    mutationFn: ({ id, note }: { id: string; note?: string }) =>
      apiService.rejectPromptProposal(id, note),
    onSuccess: () => { toast.success('Rejected'); invalidate(); },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Could not reject'),
  });

  const revert = useMutation({
    mutationFn: (id: string) => apiService.revertPromptProposal(id),
    onSuccess: () => { toast.success('Reverted'); invalidate(); },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Could not revert'),
  });

  const proposals: Proposal[] = data?.proposals ?? [];
  const pending = proposals.filter((p) => p.status === 'pending');
  const resolved = proposals.filter((p) => p.status !== 'pending');
  const busy = approve.isPending || reject.isPending || revert.isPending;

  if (isLoading) {
    return <div className="py-12 text-center text-gray-500 dark:text-gray-400">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start gap-3 p-4 rounded-lg bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-800">
        <ShieldCheck className="w-5 h-5 text-violet-600 dark:text-violet-400 shrink-0 mt-0.5" />
        <div className="text-sm text-violet-900 dark:text-violet-200">
          <p className="font-medium">Prompt changes proposed by forensics</p>
          <p className="mt-1 text-violet-800/80 dark:text-violet-300/80">
            Nothing here is live until you approve it. Only prose fields can be
            proposed — the pipeline’s gate wiring (<code>spec_gate</code>,{' '}
            <code>outputs</code>, thresholds) is out of reach by design, and a phase
            cannot rewrite its own prompt.
          </p>
        </div>
      </div>

      {pending.length === 0 && (
        <div className="py-12 text-center">
          <Lightbulb className="w-10 h-10 mx-auto text-gray-300 dark:text-gray-600" />
          <p className="mt-3 text-gray-500 dark:text-gray-400">
            No prompt changes awaiting review.
          </p>
          <p className="mt-1 text-sm text-gray-400 dark:text-gray-500">
            forensics_analysis files these after a run that hit problems.
          </p>
        </div>
      )}

      {pending.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Awaiting review ({pending.length})
          </h3>
          {pending.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              busy={busy}
              onApprove={(note) => approve.mutate({ id: p.id, note })}
              onReject={(note) => reject.mutate({ id: p.id, note })}
              onRevert={() => revert.mutate(p.id)}
            />
          ))}
        </div>
      )}

      {resolved.length > 0 && (
        <div className="space-y-3">
          <button
            onClick={() => setShowHistory((s) => !s)}
            className="flex items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
          >
            {showHistory ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            History ({resolved.length})
          </button>
          {showHistory &&
            resolved.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                busy={busy}
                onApprove={(note) => approve.mutate({ id: p.id, note })}
                onReject={(note) => reject.mutate({ id: p.id, note })}
                onRevert={() => revert.mutate(p.id)}
              />
            ))}
        </div>
      )}
    </div>
  );
};

export default ImprovementsPanel;

import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, AlertCircle, ArrowRight, Zap, XCircle } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';
import { apiService } from '@/services/api';

interface ArbitrationAttempt {
  at: string | null;
  decision: string | null;
  target_phase: string | null;
  reason: string;
}

interface ArbitrationDecisionContext {
  phase_name: string;
  attempts: ArbitrationAttempt[];
  distinct_options: Array<{ decision: string; target_phase: string | null; reason: string }>;
}

interface ArbitrationDecisionModalProps {
  inputRequest: { id: string; decision_context?: ArbitrationDecisionContext | null } | null;
  onClose: () => void;
}

/**
 * Surfaces the actual disagreement behind an arbitration deadlock --
 * what each attempt concluded, and where they conflicted -- instead of
 * the single flattened "arbitrated N times without converging" sentence,
 * and lets a human resolve it with the same continue/goto/fail semantics
 * an AI arbiter itself would use (see arbitration.py's
 * _resolve_human_arbitration_choice).
 */
const ArbitrationDecisionModal: React.FC<ArbitrationDecisionModalProps> = ({ inputRequest, onClose }) => {
  const queryClient = useQueryClient();
  const [pendingChoice, setPendingChoice] = useState<string | null>(null);

  const submitMutation = useMutation({
    mutationFn: ({ choice, targetPhase }: { choice: string; targetPhase?: string }) =>
      apiService.submitAutopilotInput(inputRequest!.id, choice, undefined, targetPhase),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-input'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-messages'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs'] });
      toast.success('Decision sent to pipeline');
      onClose();
    },
    onError: () => {
      toast.error('Failed to submit decision');
      setPendingChoice(null);
    },
  });

  const submit = (choice: string, targetPhase?: string) => {
    setPendingChoice(targetPhase ? `g:${targetPhase}` : choice);
    submitMutation.mutate({ choice, targetPhase });
  };

  if (!inputRequest) return null;
  const ctx = inputRequest.decision_context;

  // Goto options ARE the extra, specific choices this phase's own
  // arbitration attempts actually proposed -- "continue" and "fail" are
  // always offered below regardless, so only surface the goto ones here
  // to avoid a redundant duplicate "Continue" button.
  const gotoOptions = (ctx?.distinct_options || []).filter((o) => o.decision === 'goto' && o.target_phase);

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
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          onClick={(e) => e.stopPropagation()}
          className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-2xl w-full max-h-[85vh] overflow-hidden flex flex-col"
        >
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-5 h-5 text-amber-500" />
              <h3 className="text-lg font-semibold text-gray-800 dark:text-white">
                Decision needed{ctx ? `: ${ctx.phase_name}` : ''}
              </h3>
            </div>
            <button
              onClick={onClose}
              className="p-1 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="px-6 py-4 overflow-y-auto space-y-4">
            {ctx && ctx.attempts.length > 0 ? (
              <div>
                <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
                  {ctx.attempts.length} attempt{ctx.attempts.length !== 1 ? 's' : ''} to resolve this
                </p>
                <div className="space-y-2">
                  {ctx.attempts.map((a, i) => (
                    <div
                      key={i}
                      className="p-3 rounded-lg bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 text-sm"
                    >
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="font-medium text-gray-700 dark:text-gray-300">
                          {a.decision ? (
                            <>
                              Decision: <span className="font-mono">{a.decision}</span>
                              {a.target_phase && <span className="font-mono"> {a.target_phase}</span>}
                            </>
                          ) : (
                            'Unparsed response'
                          )}
                        </span>
                        {a.at && (
                          <span className="text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">
                            {formatDistanceToNow(new Date(a.at), { addSuffix: true })}
                          </span>
                        )}
                      </div>
                      <p className="text-gray-600 dark:text-gray-400 whitespace-pre-wrap">{a.reason}</p>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No attempt history is available for this decision.
              </p>
            )}
          </div>

          <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 space-y-2">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              What should happen next
            </p>
            <div className="flex flex-col gap-2">
              {gotoOptions.map((o, i) => (
                <button
                  key={i}
                  onClick={() => submit('g', o.target_phase!)}
                  disabled={submitMutation.isPending}
                  className="flex items-start gap-2 px-3 py-2 bg-blue-50 dark:bg-blue-900/20 hover:bg-blue-100 dark:hover:bg-blue-900/40 border border-blue-200 dark:border-blue-800 rounded-lg text-left disabled:opacity-50 transition-colors"
                >
                  <ArrowRight className="w-4 h-4 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                  <span className="text-sm text-blue-800 dark:text-blue-300">
                    <span className="font-semibold">Send back to {o.target_phase}</span>
                    <span className="block text-xs text-blue-600 dark:text-blue-400 mt-0.5 line-clamp-2">
                      {o.reason}
                    </span>
                  </span>
                  {pendingChoice === `g:${o.target_phase}` && submitMutation.isPending && (
                    <span className="text-xs text-blue-500 ml-auto flex-shrink-0">Sending…</span>
                  )}
                </button>
              ))}

              <div className="flex gap-2">
                <button
                  onClick={() => submit('c')}
                  disabled={submitMutation.isPending}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
                >
                  <Zap className="w-3.5 h-3.5" />
                  Force continue
                </button>
                <button
                  onClick={() => submit('s')}
                  disabled={submitMutation.isPending}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 bg-red-600 hover:bg-red-700 text-white text-sm font-semibold rounded-lg disabled:opacity-50 transition-colors"
                >
                  <XCircle className="w-3.5 h-3.5" />
                  Fail workflow
                </button>
              </div>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
};

export default ArbitrationDecisionModal;

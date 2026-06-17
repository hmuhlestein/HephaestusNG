import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { AlertTriangle, Zap, SkipForward, XCircle, MessageSquare } from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

interface HumanInputBannerProps {
  onOpenMessages?: () => void;
}

const HumanInputBanner: React.FC<HumanInputBannerProps> = ({ onOpenMessages }) => {
  const queryClient = useQueryClient();

  const { data: inputRequest } = useQuery({
    queryKey: ['autopilot-input'],
    queryFn: () => apiService.getAutopilotInput(),
    refetchInterval: 5000,
  });

  const submitMutation = useMutation({
    mutationFn: ({ choice }: { choice: string }) =>
      apiService.submitAutopilotInput(inputRequest!.id, choice),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-input'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      toast.success('Response sent to pipeline');
    },
    onError: () => toast.error('Failed to submit response'),
  });

  const dismissMutation = useMutation({
    mutationFn: () => apiService.dismissAutopilotInput(inputRequest!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-input'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
    },
  });

  const options: { key: string; label: string; icon: React.ElementType; color: string; desc: string }[] = [
    { key: 'c', label: 'Continue', icon: Zap, color: 'bg-emerald-600 hover:bg-emerald-700', desc: 'Keep processing the current design' },
    { key: 's', label: 'Skip', icon: SkipForward, color: 'bg-amber-600 hover:bg-amber-700', desc: 'Skip this design, move to next' },
    { key: 'q', label: 'Quit', icon: XCircle, color: 'bg-red-600 hover:bg-red-700', desc: 'Stop the pipeline entirely' },
  ];

  return (
    <AnimatePresence>
      {inputRequest && (
        <motion.div
          key="human-input-banner"
          initial={{ opacity: 0, y: -20, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -20, scale: 0.98 }}
          className="relative overflow-hidden rounded-2xl border-2 border-amber-300 bg-gradient-to-r from-amber-50 via-orange-50 to-red-50 shadow-lg"
        >
          <div className="absolute inset-0 rounded-2xl border-2 border-amber-400 animate-pulse pointer-events-none" />

          <div className="relative px-6 py-5">
            <div className="flex items-start gap-4">
              <div className="p-3 bg-amber-100 rounded-xl flex-shrink-0">
                <motion.div
                  animate={{ scale: [1, 1.1, 1] }}
                  transition={{ duration: 1.5, repeat: Infinity }}
                >
                  <AlertTriangle className="w-7 h-7 text-amber-600" />
                </motion.div>
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <h3 className="text-lg font-bold text-amber-900">Human Input Required</h3>
                  <span className="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-200 text-amber-800 animate-pulse">
                    Waiting
                  </span>
                </div>

                <p className="text-sm text-amber-800 mb-4">{inputRequest.reason}</p>

                <div className="flex items-center gap-3">
                  {options.map((opt) => (
                    <button
                      key={opt.key}
                      onClick={() => submitMutation.mutate({ choice: opt.key })}
                      disabled={submitMutation.isPending}
                      className={`flex items-center gap-2 px-4 py-2.5 rounded-xl text-white text-sm font-semibold shadow-sm transition-all ${opt.color} disabled:opacity-50`}
                      title={opt.desc}
                    >
                      <opt.icon className="w-4 h-4" />
                      {opt.label}
                    </button>
                  ))}

                  <div className="ml-auto flex items-center gap-3">
                    {onOpenMessages && (
                      <button
                        onClick={onOpenMessages}
                        className="flex items-center gap-1.5 px-3 py-2 text-sm text-amber-700 hover:text-amber-900 hover:bg-amber-100 rounded-lg transition-colors"
                      >
                        <MessageSquare className="w-4 h-4" />
                        Send a message
                      </button>
                    )}
                    <button
                      onClick={() => dismissMutation.mutate()}
                      className="text-xs text-amber-600 hover:text-amber-800 underline"
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div className="mt-3 text-xs text-amber-600">
              Requested {new Date(inputRequest.timestamp).toLocaleTimeString()}
              <span className="ml-2 text-amber-400">#{inputRequest.id}</span>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

export default HumanInputBanner;

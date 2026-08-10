import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { Rocket, Eye } from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

interface ReviewModeToggleProps {
  projectId: string | null;
  reviewMode: boolean;
}

const ReviewModeToggle: React.FC<ReviewModeToggleProps> = ({ projectId, reviewMode: reviewModeProp }) => {
  const queryClient = useQueryClient();
  // Local state for instant UI feedback
  const [reviewMode, setReviewMode] = React.useState(reviewModeProp);

  const mutation = useMutation({
    mutationFn: (next: boolean) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.patchProjectReviewMode(projectId, next);
    },
    onMutate: async (next: boolean) => {
      // Cancel in-flight refetches first
      await queryClient.cancelQueries({ queryKey: ['autopilot-status', projectId] });
      // Instant local state update
      setReviewMode(next);
      // Optimistic update in query cache
      const prev = queryClient.getQueryData<any>(['autopilot-status', projectId]);
      queryClient.setQueryData(['autopilot-status', projectId], (old: any) =>
        old ? { ...old, review_mode: next } : old,
      );
      return { prev };
    },
    onSuccess: (_, next) => {
      toast.success(
        next ? 'Review Mode on — pipeline will pause after each feature' : 'Full Autopilot — pipeline runs unattended',
      );
    },
    onError: (_err, next, ctx: any) => {
      // Revert on error
      setReviewMode(!next);
      if (ctx?.prev) queryClient.setQueryData(['autopilot-status', projectId], ctx.prev);
      toast.error('Failed to update review mode');
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
    },
  });

  // Sync from prop only when mutation is NOT pending
  // This prevents refetches from reverting the optimistic update
  React.useEffect(() => {
    if (!mutation.isPending) {
      setReviewMode(reviewModeProp);
    }
  }, [reviewModeProp, mutation.isPending]);

  const disabled = !projectId || mutation.isPending;

  return (
    <div
      className={`flex items-center gap-3 px-4 py-2 rounded-lg border transition-colors duration-150 ${
        reviewMode
          ? 'bg-amber-50 dark:bg-amber-900/30 border-amber-200 dark:border-amber-600'
          : 'bg-violet-50 dark:bg-violet-900/30 border-violet-200 dark:border-violet-600'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      {/* Label */}
      <div className={`flex items-center gap-1.5 text-sm font-medium transition-colors duration-150 ${
        reviewMode ? 'text-amber-700 dark:text-amber-400' : 'text-violet-700 dark:text-violet-400'
      }`}>
        {reviewMode ? (
          <>
            <Eye className="w-4 h-4" />
            Review Mode
          </>
        ) : (
          <>
            <Rocket className="w-4 h-4" />
            Full Autopilot
          </>
        )}
      </div>

      {/* Pill toggle */}
      <button
        disabled={disabled}
        onClick={() => mutation.mutate(!reviewMode)}
        aria-pressed={reviewMode}
        aria-label="Toggle Review Mode"
        className={`relative w-[48px] h-[26px] rounded-full transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 ${
          reviewMode ? 'bg-amber-500' : 'bg-violet-500'
        } ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 500, damping: 35 }}
          className="absolute top-[3px] w-[20px] h-[20px] rounded-full bg-white shadow-md"
          style={{ left: reviewMode ? 3 : 25 }}
        />
      </button>
    </div>
  );
};

export default ReviewModeToggle;

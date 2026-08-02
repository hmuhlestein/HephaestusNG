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

const ReviewModeToggle: React.FC<ReviewModeToggleProps> = ({ projectId, reviewMode }) => {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (next: boolean) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.patchProjectReviewMode(projectId, next);
    },
    onMutate: async (next: boolean) => {
      // Optimistic update
      await queryClient.cancelQueries({ queryKey: ['autopilot-status', projectId] });
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
    onError: (_err, _next, ctx: any) => {
      if (ctx?.prev) queryClient.setQueryData(['autopilot-status', projectId], ctx.prev);
      toast.error('Failed to update review mode');
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
    },
  });

  const disabled = !projectId || mutation.isPending;

  return (
    <div
      className={`flex items-center gap-4 px-5 py-3 rounded-xl border transition-colors ${
        reviewMode
          ? 'bg-amber-50 border-amber-200'
          : 'bg-gray-50 border-gray-200'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      {/* Label left */}
      <div className="flex items-center gap-1.5 text-sm font-medium text-gray-500 min-w-[120px]">
        <Rocket className="w-4 h-4" />
        Full Autopilot
      </div>

      {/* Pill toggle */}
      <button
        disabled={disabled}
        onClick={() => mutation.mutate(!reviewMode)}
        aria-pressed={reviewMode}
        aria-label="Toggle Review Mode"
        className={`relative w-[56px] h-[28px] rounded-full transition-colors duration-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 ${
          reviewMode ? 'bg-amber-500' : 'bg-gray-300'
        } ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 500, damping: 35 }}
          className="absolute top-[3px] w-[22px] h-[22px] rounded-full bg-white shadow-md"
          style={{ left: reviewMode ? 31 : 3 }}
        />
      </button>

      {/* Label right */}
      <div
        className={`flex items-center gap-1.5 text-sm font-medium min-w-[120px] transition-colors ${
          reviewMode ? 'text-amber-700' : 'text-gray-400'
        }`}
      >
        <Eye className="w-4 h-4" />
        Review Mode
      </div>

      {reviewMode && (
        <p className="text-xs text-amber-600 ml-2 hidden sm:block">
          Pipeline pauses after each feature for your sign-off
        </p>
      )}
    </div>
  );
};

export default ReviewModeToggle;

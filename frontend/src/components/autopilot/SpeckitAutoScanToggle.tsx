import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { Search, Ban } from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

interface SpeckitAutoScanToggleProps {
  projectId: string | null;
  enabled: boolean;
}

const SpeckitAutoScanToggle: React.FC<SpeckitAutoScanToggleProps> = ({ projectId, enabled: enabledProp }) => {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = React.useState(enabledProp);

  const mutation = useMutation({
    mutationFn: (next: boolean) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.patchProjectSpeckitAutoScan(projectId, next);
    },
    onMutate: async (next: boolean) => {
      await queryClient.cancelQueries({ queryKey: ['autopilot-status', projectId] });
      setEnabled(next);
      const prev = queryClient.getQueryData<any>(['autopilot-status', projectId]);
      queryClient.setQueryData(['autopilot-status', projectId], (old: any) =>
        old ? { ...old, speckit_auto_scan_enabled: next } : old,
      );
      return { prev };
    },
    onSuccess: (_, next) => {
      toast.success(next ? 'Spec Kit auto-scan on — ready features build automatically' : 'Spec Kit auto-scan off');
    },
    onError: (_err, next, ctx: any) => {
      setEnabled(!next);
      if (ctx?.prev) queryClient.setQueryData(['autopilot-status', projectId], ctx.prev);
      toast.error('Failed to update Spec Kit auto-scan setting');
    },
  });

  React.useEffect(() => {
    if (!mutation.isPending) setEnabled(enabledProp);
  }, [enabledProp, mutation.isPending]);

  const disabled = !projectId || mutation.isPending;

  return (
    <div
      className={`flex items-center gap-3 px-4 py-2 rounded-lg border transition-colors duration-150 ${
        enabled
          ? 'bg-emerald-50 dark:bg-emerald-900/30 border-emerald-200 dark:border-emerald-600'
          : 'bg-slate-50 dark:bg-slate-900/30 border-slate-200 dark:border-slate-600'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <div className={`flex items-center gap-1.5 text-sm font-medium transition-colors duration-150 ${
        enabled ? 'text-emerald-700 dark:text-emerald-400' : 'text-slate-700 dark:text-slate-400'
      }`}>
        {enabled ? <><Search className="w-4 h-4" />Spec Kit Auto-Scan</> : <><Ban className="w-4 h-4" />Spec Kit Auto-Scan Off</>}
      </div>
      <button
        disabled={disabled}
        onClick={() => mutation.mutate(!enabled)}
        aria-pressed={enabled}
        aria-label="Toggle Spec Kit Auto-Scan"
        className={`relative w-[48px] h-[26px] rounded-full transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ${
          enabled ? 'bg-emerald-500' : 'bg-slate-400'
        } ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 500, damping: 35 }}
          className="absolute top-[3px] w-[20px] h-[20px] rounded-full bg-white shadow-md"
          style={{ left: enabled ? 3 : 25 }}
        />
      </button>
    </div>
  );
};

export default SpeckitAutoScanToggle;

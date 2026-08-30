import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { ArchiveRestore, Trash2, FileText, Archive as ArchiveIcon } from 'lucide-react';
import { apiService } from '@/services/api';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';

interface ArchivedDesignsPanelProps {
  projectId: string | null;
}

const ArchivedDesignsPanel: React.FC<ArchivedDesignsPanelProps> = ({ projectId }) => {
  const queryClient = useQueryClient();

  const { data: designs, isLoading } = useQuery({
    queryKey: ['autopilot-project-designs-archived', projectId],
    queryFn: () => (projectId ? apiService.getAutopilotProjectDesigns(projectId, true) : Promise.resolve([])),
    enabled: !!projectId,
    refetchInterval: 10000,
  });

  const invalidateBoth = () => {
    queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs-archived', projectId] });
    queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
  };

  const unarchiveMutation = useMutation({
    mutationFn: (designId: string) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.unarchiveAutopilotProjectDesign(projectId, designId);
    },
    onSuccess: () => {
      invalidateBoth();
      toast.success('Design restored to queue');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to restore design');
    },
  });

  const removeMutation = useMutation({
    mutationFn: (designId: string) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.removeAutopilotProjectDesign(projectId, designId);
    },
    onSuccess: () => {
      invalidateBoth();
      toast.success('Design permanently deleted');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to delete design');
    },
  });

  if (!projectId) {
    return (
      <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
        <ArchiveIcon className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">No project selected</h3>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
      </div>
    );
  }

  if (!designs || designs.length === 0) {
    return (
      <div className="bg-gray-50 dark:bg-gray-800 border-2 border-dashed border-gray-200 dark:border-gray-700 rounded-2xl p-12 text-center">
        <ArchiveIcon className="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 dark:text-gray-300 mb-2">Nothing archived</h3>
        <p className="text-sm text-gray-400 dark:text-gray-500">
          Archived specs disappear from the queue but keep their history here.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {designs.map((item: any, index: number) => (
        <motion.div
          key={item.id}
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: index * 0.03 }}
          className="flex items-center gap-4 px-5 py-4 rounded-xl border bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700"
        >
          <div className="p-2.5 rounded-lg bg-gray-100 dark:bg-gray-700">
            <FileText className="w-5 h-5 text-gray-400 dark:text-gray-500" />
          </div>
          <div className="flex-1 min-w-0">
            <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 truncate">{item.name}</h4>
            <div className="flex items-center gap-3 mt-1">
              {item.filename && (
                <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{item.filename}</span>
              )}
              {item.archived_at && (
                <span className="text-xs text-gray-400 dark:text-gray-500">
                  Archived {formatDistanceToNow(new Date(item.archived_at), { addSuffix: true })}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => unarchiveMutation.mutate(item.id)}
              disabled={unarchiveMutation.isPending}
              className="p-2 rounded-lg hover:bg-violet-50 dark:hover:bg-violet-900/20 transition-colors text-gray-400 dark:text-gray-500 hover:text-violet-600 dark:hover:text-violet-400"
              title="Restore to queue"
            >
              <ArchiveRestore className="w-4 h-4" />
            </button>
            <button
              onClick={() => {
                if (confirm(`Permanently delete "${item.name}"? This cannot be undone.`)) {
                  removeMutation.mutate(item.id);
                }
              }}
              disabled={removeMutation.isPending}
              className="p-2 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400"
              title="Delete permanently"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </motion.div>
      ))}
    </div>
  );
};

export default ArchivedDesignsPanel;

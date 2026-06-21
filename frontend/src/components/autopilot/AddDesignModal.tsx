import React, { useState, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Plus, Upload } from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import toast from 'react-hot-toast';

interface AddDesignModalProps {
  open: boolean;
  projectId: string | null;
  onClose: () => void;
}

const AddDesignModal: React.FC<AddDesignModalProps> = ({ open, projectId, onClose }) => {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [extension, setExtension] = useState('.md');

  useEffect(() => {
    if (!open) {
      setName('');
      setContent('');
      setExtension('.md');
    }
  }, [open]);

  const addMutation = useMutation({
    mutationFn: () => {
      if (!projectId) throw new Error('No project selected');
      return apiService.addAutopilotProjectDesign(projectId, name, content, extension);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
      toast.success(`"${name}" added to queue`);
      setName('');
      setContent('');
      onClose();
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to add design');
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !content.trim()) {
      toast.error('Name and content are required');
      return;
    }
    addMutation.mutate();
  };

  return (
    <AnimatePresence>
      {open && (
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
            className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="p-2 bg-violet-100 rounded-lg">
                  <Plus className="w-5 h-5 text-violet-600" />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800">Add Design to Queue</h2>
                  <p className="text-xs text-gray-500">Paste or type your design document</p>
                </div>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-gray-100 text-gray-500"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Form */}
            <form onSubmit={handleSubmit} className="p-6 space-y-4">
              <div className="grid grid-cols-3 gap-4">
                <div className="col-span-2">
                  <label className="block text-sm font-medium text-gray-700 mb-1">Design Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. User Authentication System"
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Format</label>
                  <select
                    value={extension}
                    onChange={(e) => setExtension(e.target.value)}
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 bg-white"
                  >
                    <option value=".md">Markdown (.md)</option>
                    <option value=".txt">Text (.txt)</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Design Document</label>
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder={`# Design: ${name || 'Your Feature Name'}\n\n## Overview\nDescribe the feature...\n\n## Requirements\n- Requirement 1\n- Requirement 2\n\n## Constraints\n- ...\n\n## Acceptance Criteria\n- [ ] Criteria 1\n- [ ] Criteria 2`}
                  className="w-full px-4 py-3 border border-gray-200 rounded-xl text-sm font-mono leading-relaxed focus:outline-none focus:ring-2 focus:ring-violet-500 resize-none"
                  rows={16}
                />
                <p className="text-xs text-gray-400 mt-1">
                  {content.length} characters · {content.split(/\s+/).filter(Boolean).length} words
                </p>
              </div>

              {/* Actions */}
              <div className="flex items-center justify-end gap-3 pt-2">
                <Button type="button" variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  className="bg-violet-600 hover:bg-violet-700 text-white"
                  disabled={addMutation.isPending || !name.trim() || !content.trim()}
                >
                  {addMutation.isPending ? (
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                  ) : (
                    <Upload className="w-4 h-4 mr-1" />
                  )}
                  Add to Queue
                </Button>
              </div>
            </form>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

export default AddDesignModal;

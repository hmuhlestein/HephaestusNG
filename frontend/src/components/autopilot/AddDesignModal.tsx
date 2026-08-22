import React, { useState, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Upload, Sparkles, Bug } from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import toast from 'react-hot-toast';

interface AddDesignModalProps {
  open: boolean;
  projectId: string | null;
  defaultType: 'feature' | 'bugfix';
  onClose: () => void;
}

const TYPE_COPY = {
  feature: {
    icon: Sparkles,
    title: 'New Feature',
    subtitle: 'Describe what to build — runs the full pipeline (requirements, architecture, review).',
    namePlaceholder: 'e.g. User Authentication System',
    contentPlaceholder: '# Design: {name}\n\n## Overview\nDescribe the feature...\n\n## Requirements\n- Requirement 1\n- Requirement 2\n\n## Constraints\n- ...\n\n## Acceptance Criteria\n- [ ] Criteria 1\n- [ ] Criteria 2',
    accent: 'blue',
  },
  bugfix: {
    icon: Bug,
    title: 'Report Bug',
    subtitle: 'Describe what’s broken — skips straight to a fix + review + validation, no architecture phase.',
    namePlaceholder: 'e.g. Login fails with valid credentials',
    contentPlaceholder: '# Bug: {name}\n\n## Expected Behavior\n...\n\n## Actual Behavior\n...\n\n## Reproduction Steps\n1. ...\n2. ...\n\n## Environment\n- ...',
    accent: 'amber',
  },
} as const;

const ACCENT_CLASSES = {
  blue: {
    iconBg: 'bg-blue-100',
    iconText: 'text-blue-600',
    ring: 'focus:ring-blue-500',
    button: 'bg-blue-600 hover:bg-blue-700 text-white',
  },
  amber: {
    iconBg: 'bg-amber-100',
    iconText: 'text-amber-600',
    ring: 'focus:ring-amber-500',
    button: 'bg-amber-600 hover:bg-amber-700 text-white',
  },
} as const;

const AddDesignModal: React.FC<AddDesignModalProps> = ({ open, projectId, defaultType, onClose }) => {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [extension, setExtension] = useState('.md');

  const copy = TYPE_COPY[defaultType];
  const accent = ACCENT_CLASSES[copy.accent];
  const Icon = copy.icon;

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
      return apiService.addAutopilotProjectDesign(projectId, name, content, extension, 'queue', defaultType);
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
                <div className={`p-2 rounded-lg ${accent.iconBg}`}>
                  <Icon className={`w-5 h-5 ${accent.iconText}`} />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800">{copy.title}</h2>
                  <p className="text-xs text-gray-500">{copy.subtitle}</p>
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
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    {defaultType === 'bugfix' ? 'Bug Title' : 'Design Name'}
                  </label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder={copy.namePlaceholder}
                    className={`w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 ${accent.ring}`}
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Format</label>
                  <select
                    value={extension}
                    onChange={(e) => setExtension(e.target.value)}
                    className={`w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 ${accent.ring} bg-white`}
                  >
                    <option value=".md">Markdown (.md)</option>
                    <option value=".txt">Text (.txt)</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  {defaultType === 'bugfix' ? 'Bug Report' : 'Design Document'}
                </label>
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder={copy.contentPlaceholder.replace('{name}', name || (defaultType === 'bugfix' ? 'Your Bug Title' : 'Your Feature Name'))}
                  className={`w-full px-4 py-3 border border-gray-200 rounded-xl text-sm font-mono leading-relaxed focus:outline-none focus:ring-2 ${accent.ring} resize-none`}
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
                  className={accent.button}
                  disabled={addMutation.isPending || !name.trim() || !content.trim()}
                >
                  {addMutation.isPending ? (
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                  ) : (
                    <Upload className="w-4 h-4 mr-1" />
                  )}
                  {defaultType === 'bugfix' ? 'Report Bug' : 'Add Feature'}
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

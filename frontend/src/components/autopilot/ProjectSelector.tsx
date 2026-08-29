import React, { useState, useRef, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  FolderOpen, ChevronDown, Plus, Check, X, Trash2, RefreshCw, Star
} from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

interface ProjectSelectorProps {
  projectId: string | null;
  onProjectChange: (projectId: string) => void;
}

const ProjectSelector: React.FC<ProjectSelectorProps> = ({ projectId, onProjectChange }) => {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPath, setNewPath] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  const { data: projects, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: () => apiService.getProjects(),
    refetchInterval: 30000,
  });

  const createMutation = useMutation({
    mutationFn: () => apiService.createProject(newName, newPath, !projects?.length),
    onSuccess: (proj: any) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      onProjectChange(proj.id);
      setShowAdd(false);
      setNewName('');
      setNewPath('');
      toast.success(`Project "${proj.name}" created`);
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.detail || 'Failed to create project');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiService.deleteProject(id),
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      if (projectId === deletedId) {
        onProjectChange('');
      }
      toast.success('Project deleted');
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.detail || 'Failed to delete project');
    },
  });

  const syncMutation = useMutation({
    mutationFn: (id: string) => apiService.syncAutopilotProject(id),
    onSuccess: (designs: any[]) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs'] });
      toast.success(`Synced ${designs.length} designs`);
    },
  });

  const selected = projects?.find((p: any) => p.id === projectId);

  useEffect(() => {
    if (!projectId && projects?.length && !isLoading) {
      const def = projects.find((p: any) => p.is_default) || projects[0];
      if (def) onProjectChange(def.id);
    }
  }, [projectId, projects, isLoading, onProjectChange]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-4 py-2.5 bg-white border border-gray-200 rounded-xl text-sm hover:border-violet-300 hover:shadow-sm transition-all min-w-[280px]"
      >
        <FolderOpen className="w-4 h-4 text-violet-500 flex-shrink-0" />
        <span className="flex-1 text-left truncate font-medium text-gray-700">
          {isLoading ? 'Loading...' : selected?.name || 'Select Project'}
        </span>
        {selected && (
          <span className="text-xs text-gray-400 flex-shrink-0">
            {selected.design_count} designs
          </span>
        )}
        <ChevronDown className={`w-4 h-4 text-gray-400 transition-transform flex-shrink-0 ${open ? 'rotate-180' : ''}`} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden"
          >
            <div className="max-h-64 overflow-y-auto">
              {(!projects || projects.length === 0) && (
                <div className="px-4 py-6 text-center text-sm text-gray-400">
                  No projects yet. Create one to get started.
                </div>
              )}
              {(projects || []).map((proj: any) => (
                <div
                  key={proj.id}
                  className={`flex items-center gap-3 px-4 py-3 cursor-pointer transition-colors ${
                    proj.id === projectId ? 'bg-violet-50' : 'hover:bg-gray-50'
                  }`}
                  onClick={() => { onProjectChange(proj.id); setOpen(false); }}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-800 truncate">{proj.name}</span>
                      {proj.is_default && <Star className="w-3 h-3 text-amber-400 fill-amber-400 flex-shrink-0" />}
                    </div>
                    <span className="text-xs text-gray-400 truncate block">{proj.base_dir}</span>
                  </div>
                  <span className="text-xs text-gray-400 flex-shrink-0">{proj.design_count}</span>
                  {proj.id === projectId && <Check className="w-4 h-4 text-violet-600 flex-shrink-0" />}
                </div>
              ))}
            </div>

            <div className="border-t px-2 py-2 flex gap-1">
              <button
                onClick={() => { setShowAdd(true); setOpen(false); }}
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-violet-600 hover:bg-violet-50 rounded-lg transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> Add Project
              </button>
              {projectId && (
                <>
                  <button
                    onClick={() => syncMutation.mutate(projectId)}
                    className="flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 rounded-lg transition-colors"
                    title="Re-scan design directory"
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${syncMutation.isPending ? 'animate-spin' : ''}`} />
                  </button>
                  {projects && projects.length > 1 && (
                    <button
                      onClick={() => {
                        if (confirm(`Delete project "${selected?.name}"?`)) {
                          deleteMutation.mutate(projectId);
                        }
                      }}
                      className="flex items-center justify-center px-3 py-2 text-xs text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                      title="Delete project"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Add Project Dialog */}
      <AnimatePresence>
        {showAdd && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
            onClick={(e) => { if (e.target === e.currentTarget) setShowAdd(false); }}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden"
            >
              <div className="px-6 py-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 bg-violet-100 rounded-lg">
                    <FolderOpen className="w-5 h-5 text-violet-600" />
                  </div>
                  <div>
                    <h2 className="text-lg font-bold text-gray-800">Add Project</h2>
                    <p className="text-xs text-gray-500">Point to a codebase working directory</p>
                  </div>
                </div>
                <button onClick={() => setShowAdd(false)} className="p-2 rounded-lg hover:bg-gray-100 text-gray-500">
                  <X className="w-4 h-4" />
                </button>
              </div>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!newName.trim() || !newPath.trim()) {
                    toast.error('Name and path are required');
                    return;
                  }
                  createMutation.mutate();
                }}
                className="p-6 space-y-4"
              >
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Project Name</label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="e.g. My App"
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Working Directory</label>
                  <input
                    type="text"
                    value={newPath}
                    onChange={(e) => setNewPath(e.target.value)}
                    placeholder="/path/to/your/project"
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-500"
                  />
                  <p className="text-xs text-gray-400 mt-1">
                    Designs will be loaded from <code className="bg-gray-100 px-1 rounded">{newPath || '…'}/docs/specs/</code>
                  </p>
                </div>
                <div className="flex justify-end gap-3 pt-2">
                  <button type="button" onClick={() => setShowAdd(false)} className="px-4 py-2 text-sm border border-gray-200 rounded-xl hover:bg-gray-50">
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={createMutation.isPending || !newName.trim() || !newPath.trim()}
                    className="px-4 py-2 text-sm bg-violet-600 text-white rounded-xl hover:bg-violet-700 disabled:opacity-50"
                  >
                    {createMutation.isPending ? 'Creating...' : 'Create Project'}
                  </button>
                </div>
              </form>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};

export default ProjectSelector;

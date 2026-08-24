import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X,
  Plus,
  Trash2,
  FolderOpen,
  CheckCircle2,
  Settings,
  Loader2,
  DollarSign,
  AlertTriangle,
  Edit3,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { apiService } from '@/services/api';

interface ProjectSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

const ProjectSettingsModal: React.FC<ProjectSettingsModalProps> = ({ isOpen, onClose }) => {
  const queryClient = useQueryClient();
  const [defaultBudgetValue, setDefaultBudgetValue] = useState('');
  const [editingDefaultBudget, setEditingDefaultBudget] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [newProjectPath, setNewProjectPath] = useState('');
  const [showCreateForm, setShowCreateForm] = useState(false);

  // System-wide default spend cap, seeded onto projects created afterwards.
  const { data: defaultBudget } = useQuery({
    queryKey: ['default-budget'],
    queryFn: () => apiService.getDefaultBudget(),
  });

  const updateDefaultBudgetMutation = useMutation({
    mutationFn: (value: number | null) => apiService.setDefaultBudget(value),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['default-budget'] });
      setEditingDefaultBudget(false);
      toast.success(
        data.default_cost_limit_usd == null
          ? 'Default budget cleared — new projects start unlimited'
          : `Default budget set to $${data.default_cost_limit_usd.toFixed(2)}`
      );
    },
    onError: (e: any) =>
      toast.error(e?.response?.data?.detail || 'Failed to update the default budget'),
  });

  const handleSaveDefaultBudget = () => {
    const raw = defaultBudgetValue.trim();
    if (!raw) {
      updateDefaultBudgetMutation.mutate(null);
      return;
    }
    const parsed = parseFloat(raw);
    if (isNaN(parsed) || parsed <= 0) {
      toast.error('Enter an amount greater than 0, or leave blank to clear');
      return;
    }
    updateDefaultBudgetMutation.mutate(parsed);
  };
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [editingBudget, setEditingBudget] = useState<string | null>(null);
  const [budgetValue, setBudgetValue] = useState('');

  const { data: projects, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: () => apiService.getProjects(),
    enabled: isOpen,
  });

  const updateBudgetMutation = useMutation({
    mutationFn: async ({ projectId, costLimit, clearLimit }: { projectId: string; costLimit?: number; clearLimit?: boolean }) => {
      const response = await apiService.updateProject(projectId, {
        cost_limit_usd: costLimit,
        clear_cost_limit: clearLimit || false,
      });
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast.success('Budget updated');
      setEditingBudget(null);
      setBudgetValue('');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to update budget');
    },
  });

  const createMutation = useMutation({
    mutationFn: async ({ name, path }: { name: string; path: string }) => {
      const response = await apiService.createProject(name, path);
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast.success('Project created');
      setNewProjectName('');
      setNewProjectPath('');
      setShowCreateForm(false);
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to create project');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (projectId: string) => {
      await apiService.deleteProject(projectId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast.success('Project deleted');
      setDeleteConfirm(null);
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to delete project');
    },
  });

  const handleUpdateBudget = (projectId: string) => {
    const value = budgetValue.trim();
    if (value === '') {
      updateBudgetMutation.mutate({ projectId, clearLimit: true });
    } else {
      const parsed = parseFloat(value);
      if (isNaN(parsed) || parsed < 0) {
        toast.error('Budget must be a non-negative number');
        return;
      }
      updateBudgetMutation.mutate({ projectId, costLimit: parsed });
    }
  };

  const handleCreate = () => {
    if (!newProjectName.trim() || !newProjectPath.trim()) {
      toast.error('Name and path are required');
      return;
    }
    createMutation.mutate({ name: newProjectName, path: newProjectPath });
  };

  const handleDelete = (projectId: string) => {
    deleteMutation.mutate(projectId);
  };

  const inputClass = "w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent text-sm bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500";

  if (!isOpen) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.9, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.9, opacity: 0 }}
          className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 bg-gradient-to-r from-gray-50 to-slate-50 dark:from-gray-800 dark:to-gray-700">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-3">
                <Settings className="w-6 h-6 text-gray-600 dark:text-gray-400" />
                <div>
                  <h3 className="text-xl font-semibold text-gray-800 dark:text-gray-200">Project Settings</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400">Manage your projects</p>
                </div>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-6">
            {/* Create Project Section */}
            <div className="mb-6">
              {!showCreateForm ? (
                <button
                  onClick={() => setShowCreateForm(true)}
                  className="flex items-center px-4 py-2 bg-violet-600 text-white rounded-lg hover:bg-violet-700 transition-colors"
                >
                  <Plus className="w-4 h-4 mr-2" />
                  New Project
                </button>
              ) : (
                <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4 border border-gray-200 dark:border-gray-600">
                  <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Create New Project</h4>
                  <div className="space-y-3">
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Project Name</label>
                      <input
                        type="text"
                        value={newProjectName}
                        onChange={(e) => setNewProjectName(e.target.value)}
                        placeholder="My Project"
                        className={inputClass}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Project Path (absolute)</label>
                      <input
                        type="text"
                        value={newProjectPath}
                        onChange={(e) => setNewProjectPath(e.target.value)}
                        placeholder="/Users/you/code/my-project"
                        className={`${inputClass} font-mono`}
                      />
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={handleCreate}
                        disabled={createMutation.isPending}
                        className="flex items-center px-4 py-2 bg-violet-600 text-white rounded-lg hover:bg-violet-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm"
                      >
                        {createMutation.isPending ? (
                          <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        ) : (
                          <CheckCircle2 className="w-4 h-4 mr-2" />
                        )}
                        Create
                      </button>
                      <button
                        onClick={() => {
                          setShowCreateForm(false);
                          setNewProjectName('');
                          setNewProjectPath('');
                        }}
                        className="px-4 py-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors text-sm"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* System default budget */}
            <div className="mb-6 p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-1.5">
                    <DollarSign className="w-4 h-4" />
                    Default budget for new projects
                  </h4>
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    Applied when a project is created. Existing projects are not
                    changed — set those individually below.
                  </p>
                </div>

                {editingDefaultBudget ? (
                  <div className="flex items-center gap-2 shrink-0">
                    <div className="relative">
                      <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-500 text-sm">$</span>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        autoFocus
                        value={defaultBudgetValue}
                        onChange={(e) => setDefaultBudgetValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleSaveDefaultBudget();
                          if (e.key === 'Escape') setEditingDefaultBudget(false);
                        }}
                        className="w-32 pl-5 pr-2 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <button
                      onClick={handleSaveDefaultBudget}
                      disabled={updateDefaultBudgetMutation.isPending}
                      className="px-3 py-1.5 text-sm font-medium rounded-md bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white transition-colors"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => setEditingDefaultBudget(false)}
                      className="px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => {
                      setDefaultBudgetValue(defaultBudget?.default_cost_limit_usd?.toString() || '');
                      setEditingDefaultBudget(true);
                    }}
                    className="shrink-0 px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                  >
                    {defaultBudget?.default_cost_limit_usd != null
                      ? `$${defaultBudget.default_cost_limit_usd.toFixed(2)}`
                      : 'No default'}
                  </button>
                )}
              </div>
            </div>

            {/* Projects List */}
            <div>
              <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Existing Projects</h4>
              {isLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-6 h-6 animate-spin text-gray-400 dark:text-gray-500" />
                </div>
              ) : projects && projects.length > 0 ? (
                <div className="space-y-2">
                  {projects.map((project: any) => (
                    <div
                      key={project.id}
                      className={`flex items-center justify-between p-4 rounded-lg border transition-colors ${
                        project.is_active
                          ? 'bg-violet-50 dark:bg-violet-900/30 border-violet-200 dark:border-violet-700'
                          : 'bg-white dark:bg-gray-700/50 border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
                      }`}
                    >
                      <div className="flex items-center space-x-3 min-w-0">
                        <FolderOpen className={`w-5 h-5 flex-shrink-0 ${project.is_active ? 'text-violet-600 dark:text-violet-400' : 'text-gray-400 dark:text-gray-500'}`} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{project.name}</p>
                            {project.is_active && (
                              <span className="px-2 py-0.5 text-xs font-semibold bg-violet-100 dark:bg-violet-800 text-violet-700 dark:text-violet-300 rounded-full">
                                Active
                              </span>
                            )}
                            {project.is_default && (
                              <span className="px-2 py-0.5 text-xs font-semibold bg-gray-100 dark:bg-gray-600 text-gray-600 dark:text-gray-300 rounded-full">
                                Default
                              </span>
                            )}
                          </div>
                          <p className="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">{project.base_dir}</p>
                          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">{project.design_count || 0} designs</p>
                          {/* C10: Show repos for multi-repo projects (REQ-24) */}
                          {project.repos && project.repos.length > 1 && (
                            <div className="mt-1.5 flex flex-wrap gap-1">
                              {project.repos.map((repo: any) => (
                                <span
                                  key={repo.id}
                                  className={`inline-flex items-center px-1.5 py-0.5 text-xs rounded ${
                                    repo.is_primary
                                      ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300'
                                      : 'bg-gray-100 dark:bg-gray-600 text-gray-600 dark:text-gray-300'
                                  }`}
                                >
                                  {repo.label}
                                  {repo.is_primary && ' (primary)'}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-2 flex-shrink-0">
                        {/* Spend is a plain label -- never a button, never editable.
                            Only the limit is editable, via the single control below. */}
                        {project.cost_total_usd > 0 && editingBudget !== project.id && (
                          <div className="text-right mr-2">
                            <div className="flex items-center gap-1">
                              <DollarSign className="w-3 h-3 text-gray-500 dark:text-gray-400" />
                              <span className="text-sm font-mono text-gray-800 dark:text-gray-200">
                                {project.cost_total_usd >= 1000
                                  ? `$${(project.cost_total_usd / 1000).toFixed(1)}k`
                                  : `$${project.cost_total_usd.toFixed(2)}`}
                              </span>
                              {project.cost_limit_usd != null && (
                                <span className="text-xs text-gray-400 dark:text-gray-500">
                                  / ${project.cost_limit_usd.toFixed(0)}
                                </span>
                              )}
                            </div>
                            {project.cost_limit_usd != null && project.cost_total_usd >= project.cost_limit_usd && (
                              <div className="flex items-center gap-1 mt-0.5">
                                <AlertTriangle className="w-3 h-3 text-red-500 dark:text-red-400" />
                                <span className="text-xs text-red-600 dark:text-red-400">Over budget</span>
                              </div>
                            )}
                          </div>
                        )}

                        {/* The ONE limit editor for this project -- previously duplicated
                            as two independent inputs sharing the same editingBudget state,
                            which meant editing put both on screen at once. */}
                        {editingBudget === project.id ? (
                          <div className="flex items-center gap-1 mr-2">
                            <span className="text-xs text-gray-500 dark:text-gray-400">$</span>
                            <input
                              type="number"
                              value={budgetValue}
                              onChange={(e) => setBudgetValue(e.target.value)}
                              placeholder="blank = no limit"
                              min="0"
                              max="100000"
                              step="0.01"
                              className="w-24 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded focus:outline-none focus:ring-1 focus:ring-violet-500 bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200"
                              autoFocus
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') handleUpdateBudget(project.id);
                                if (e.key === 'Escape') { setEditingBudget(null); setBudgetValue(''); }
                              }}
                            />
                            <button
                              onClick={() => handleUpdateBudget(project.id)}
                              disabled={updateBudgetMutation.isPending}
                              className="px-2 py-1 text-xs bg-violet-600 text-white rounded hover:bg-violet-700 disabled:opacity-50"
                            >
                              Save
                            </button>
                            <button
                              onClick={() => { setEditingBudget(null); setBudgetValue(''); }}
                              className="px-2 py-1 text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-600 rounded"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : project.cost_limit_usd != null ? (
                          <button
                            onClick={() => {
                              setEditingBudget(project.id);
                              setBudgetValue(project.cost_limit_usd?.toString() || '');
                            }}
                            className="p-1 text-gray-400 dark:text-gray-500 hover:text-violet-600 dark:hover:text-violet-400 hover:bg-violet-50 dark:hover:bg-violet-900/20 rounded-lg transition-colors"
                            title="Edit budget"
                          >
                            <Edit3 className="w-4 h-4" />
                          </button>
                        ) : (
                          <button
                            onClick={() => {
                              setEditingBudget(project.id);
                              setBudgetValue('');
                            }}
                            className="px-2 py-1 text-xs font-medium text-violet-600 dark:text-violet-400 hover:text-violet-700 dark:hover:text-violet-300 underline underline-offset-2"
                          >
                            Set Budget
                          </button>
                        )}
                        {deleteConfirm === project.id ? (
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-red-600 dark:text-red-400">Delete?</span>
                            <button
                              onClick={() => handleDelete(project.id)}
                              disabled={deleteMutation.isPending}
                              className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 transition-colors text-xs font-medium"
                            >
                              {deleteMutation.isPending ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              ) : (
                                'Yes, delete'
                              )}
                            </button>
                            <button
                              onClick={() => setDeleteConfirm(null)}
                              className="px-3 py-1.5 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-600 rounded-lg transition-colors text-xs"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setDeleteConfirm(project.id)}
                            className="p-2 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors"
                            title="Delete project"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                  <FolderOpen className="w-8 h-8 mx-auto mb-2 text-gray-300 dark:text-gray-600" />
                  <p className="text-sm">No projects yet</p>
                </div>
              )}
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
};

export default ProjectSettingsModal;

import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { apiService } from '@/services/api';
import { ProjectRepoItem } from '@/types';

const ProjectReposSection: React.FC<{ projectId: string }> = ({ projectId }) => {
  const queryClient = useQueryClient();
  const [showRepos, setShowRepos] = useState(false);
  const [newLabel, setNewLabel] = useState('');
  const [newPath, setNewPath] = useState('');
  const [showAddForm, setShowAddForm] = useState(false);

  const { data: repos, isLoading } = useQuery({
    queryKey: ['project-repos', projectId],
    queryFn: () => apiService.listProjectRepos(projectId),
    enabled: showRepos,
  });

  const createRepoMutation = useMutation({
    mutationFn: async ({ label, path }: { label: string; path: string }) => {
      await apiService.createProjectRepo(projectId, { label, path, is_primary: !repos || repos.length === 0 });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project-repos', projectId] });
      setNewLabel('');
      setNewPath('');
      setShowAddForm(false);
      toast.success('Repo added');
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Failed to add repo'),
  });

  const deleteRepoMutation = useMutation({
    mutationFn: async (repoId: string) => {
      await apiService.deleteProjectRepo(projectId, repoId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project-repos', projectId] });
      toast.success('Repo removed');
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Failed to remove repo'),
  });

  const setPrimaryMutation = useMutation({
    mutationFn: async (repo: ProjectRepoItem) => {
      await apiService.updateProjectRepo(projectId, repo.id, { label: repo.label, path: repo.path, is_primary: true });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project-repos', projectId] });
      toast.success('Primary repo updated');
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Failed to update primary'),
  });

  return (
    <div className="mt-2 ml-8">
      <button
        onClick={() => setShowRepos(!showRepos)}
        className="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
      >
        {showRepos ? '▼' : '▶'} Repositories
      </button>
      {showRepos && (
        <div className="mt-2 space-y-2">
          {isLoading ? (
            <div className="text-xs text-gray-400">Loading...</div>
          ) : repos && repos.length > 0 ? (
            repos.map((repo) => (
              <div key={repo.id} className="flex items-center gap-2 text-xs">
                <span className={repo.is_primary ? 'font-bold text-violet-600' : 'text-gray-600'}>
                  {repo.label}
                </span>
                <span className="text-gray-400 truncate flex-1">{repo.path}</span>
                {!repo.is_primary && (
                  <button
                    onClick={() => setPrimaryMutation.mutate(repo)}
                    className="text-violet-500 hover:text-violet-700"
                    title="Set as primary"
                  >
                    ★
                  </button>
                )}
                <button
                  onClick={() => deleteRepoMutation.mutate(repo.id)}
                  className="text-red-400 hover:text-red-600"
                  title="Remove repo"
                >
                  ×
                </button>
              </div>
            ))
          ) : (
            <div className="text-xs text-gray-400">No repos configured</div>
          )}
          {showAddForm ? (
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                placeholder="Label"
                className="w-20 px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700"
              />
              <input
                type="text"
                value={newPath}
                onChange={(e) => setNewPath(e.target.value)}
                placeholder="/path/to/repo"
                className="flex-1 px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700"
              />
              <button
                onClick={() => createRepoMutation.mutate({ label: newLabel, path: newPath })}
                disabled={!newLabel || !newPath}
                className="px-2 py-1 text-xs bg-violet-600 text-white rounded hover:bg-violet-700 disabled:opacity-50"
              >
                Add
              </button>
              <button
                onClick={() => setShowAddForm(false)}
                className="px-2 py-1 text-xs text-gray-500 hover:text-gray-700"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowAddForm(true)}
              className="text-xs text-violet-500 hover:text-violet-700"
            >
              + Add Repo
            </button>
          )}
        </div>
      )}
    </div>
  );
};

export default ProjectReposSection;

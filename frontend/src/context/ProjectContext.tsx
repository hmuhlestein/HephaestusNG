import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiService } from '@/services/api';

interface Project {
  id: string;
  name: string;
  base_dir: string;
  is_default: boolean;
  is_active: boolean;
  design_count: number;
  created_at: string;
  updated_at: string;
}

interface ProjectContextType {
  projects: Project[];
  activeProjects: Project[];
  selectedProject: Project | null;
  loading: boolean;
  error: Error | null;
  selectProject: (projectId: string) => void;
  activateProject: (projectId: string) => void;
  deactivateProject: (projectId: string) => void;
  createProject: (name: string, baseDir: string, isDefault?: boolean) => Promise<Project>;
  deleteProject: (projectId: string) => Promise<void>;
  refetch: () => void;
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined);

const SELECTED_PROJECT_KEY = 'selectedProjectId';

export const ProjectProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const queryClient = useQueryClient();

  const {
    data: projects = [],
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['projects'],
    queryFn: () => apiService.getProjects(),
    refetchInterval: 30000,
  });

  const activeProjects = projects.filter((p: Project) => p.is_active);

  // Which project's data the dashboard is currently showing -- separate
  // from is_active (which projects have pipelines running). Sticky: once
  // set, it doesn't move just because another project also becomes
  // active. Without this separation, selecting project B while project A
  // stays active (both is_active=true is expected now, not an eviction)
  // used to make the UI silently snap back to project A a moment later --
  // `projects.find(p => p.is_active)` picked whichever one came first in
  // the list, not whichever one was just clicked.
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(() => {
    return localStorage.getItem(SELECTED_PROJECT_KEY);
  });

  const selectProject = useCallback((projectId: string) => {
    setSelectedProjectId(projectId);
    localStorage.setItem(SELECTED_PROJECT_KEY, projectId);
  }, []);

  // Fall back when there's no valid selection yet (first load) or the
  // previously-selected project is gone (deleted): prefer an active
  // project, then any project, so the dashboard never sits on "no
  // project" while real projects exist.
  useEffect(() => {
    if (projects.length === 0) return;
    const stillExists = projects.some((p: Project) => p.id === selectedProjectId);
    if (stillExists) return;
    const fallback = activeProjects[0] || projects[0];
    if (fallback) selectProject(fallback.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects]);

  const selectedProject = projects.find((p: Project) => p.id === selectedProjectId) || null;

  const activateMutation = useMutation({
    mutationFn: (projectId: string) => apiService.activateProject(projectId),
    onMutate: async (projectId) => {
      // Cancel any outgoing refetches so they don't overwrite our optimistic update
      await queryClient.cancelQueries({ queryKey: ['projects'] });

      // Snapshot the previous value
      const previousProjects = queryClient.getQueryData<Project[]>(['projects']);

      // Optimistically mark just the target project active -- activating
      // one project no longer deactivates any other (the backend caps
      // concurrent active projects instead of enforcing exclusivity), so
      // this must not touch every other project's is_active like it used to.
      queryClient.setQueryData(['projects'], (old: Project[] | undefined) => {
        if (!old) return old;
        return old.map(p => (p.id === projectId ? { ...p, is_active: true } : p));
      });

      return { previousProjects };
    },
    onError: (_err, _projectId, context) => {
      // If the mutation fails, roll back to the previous value
      if (context?.previousProjects) {
        queryClient.setQueryData(['projects'], context.previousProjects);
      }
    },
    onSuccess: (_data, projectId) => {
      selectProject(projectId);
      // Invalidate project-scoped queries in background (don't block UI)
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-queue'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-features'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-messages'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-logs'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-input'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-archived-messages'] });
        queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs'] });
        queryClient.invalidateQueries({ queryKey: ['workflow-definitions'] });
        queryClient.invalidateQueries({ queryKey: ['workflow-executions'] });
        queryClient.invalidateQueries({ queryKey: ['tasks'] });
        queryClient.invalidateQueries({ queryKey: ['agents'] });
      }, 0);
    },
    onSettled: () => {
      // Refetch projects in background
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['projects'] });
      }, 0);
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: (projectId: string) => apiService.deactivateProject(projectId),
    onMutate: async (projectId) => {
      await queryClient.cancelQueries({ queryKey: ['projects'] });
      const previousProjects = queryClient.getQueryData<Project[]>(['projects']);
      queryClient.setQueryData(['projects'], (old: Project[] | undefined) => {
        if (!old) return old;
        return old.map(p => (p.id === projectId ? { ...p, is_active: false } : p));
      });
      return { previousProjects };
    },
    onError: (_err, _projectId, context) => {
      if (context?.previousProjects) {
        queryClient.setQueryData(['projects'], context.previousProjects);
      }
    },
    onSettled: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['projects'] });
      }, 0);
    },
  });

  const createMutation = useMutation({
    mutationFn: ({ name, baseDir, isDefault }: { name: string; baseDir: string; isDefault?: boolean }) =>
      apiService.createProject(name, baseDir, isDefault),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (projectId: string) => apiService.deleteProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  const activateProject = useCallback((projectId: string) => {
    activateMutation.mutate(projectId);
  }, [activateMutation]);

  const deactivateProject = useCallback((projectId: string) => {
    deactivateMutation.mutate(projectId);
  }, [deactivateMutation]);

  const createProject = useCallback(async (name: string, baseDir: string, isDefault?: boolean) => {
    return createMutation.mutateAsync({ name, baseDir, isDefault });
  }, [createMutation]);

  const deleteProject = useCallback(async (projectId: string) => {
    return deleteMutation.mutateAsync(projectId);
  }, [deleteMutation]);

  return (
    <ProjectContext.Provider
      value={{
        projects,
        activeProjects,
        selectedProject,
        loading: isLoading,
        error: error as Error | null,
        selectProject,
        activateProject,
        deactivateProject,
        createProject,
        deleteProject,
        refetch,
      }}
    >
      {children}
    </ProjectContext.Provider>
  );
};

export const useProject = () => {
  const context = useContext(ProjectContext);
  if (!context) {
    throw new Error('useProject must be used within a ProjectProvider');
  }
  return context;
};

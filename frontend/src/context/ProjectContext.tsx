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
  activeProject: Project | null;
  loading: boolean;
  error: Error | null;
  activateProject: (projectId: string) => void;
  createProject: (name: string, baseDir: string, isDefault?: boolean) => Promise<Project>;
  deleteProject: (projectId: string) => Promise<void>;
  refetch: () => void;
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined);

export const ProjectProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const queryClient = useQueryClient();
  const [, setCachedActiveId] = useState<string | null>(() => {
    return localStorage.getItem('activeProjectId');
  });

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

  const activeProject = projects.find((p: Project) => p.is_active) || null;

  // Sync localStorage with server state
  useEffect(() => {
    if (activeProject) {
      setCachedActiveId(activeProject.id);
      localStorage.setItem('activeProjectId', activeProject.id);
    }
  }, [activeProject]);

  const activateMutation = useMutation({
    mutationFn: (projectId: string) => apiService.activateProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-projects'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-queue'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-features'] });
      queryClient.invalidateQueries({ queryKey: ['workflow-definitions'] });
      queryClient.invalidateQueries({ queryKey: ['workflow-executions'] });
      queryClient.invalidateQueries({ queryKey: ['tasks'] });
      queryClient.invalidateQueries({ queryKey: ['agents'] });
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
        activeProject,
        loading: isLoading,
        error: error as Error | null,
        activateProject,
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

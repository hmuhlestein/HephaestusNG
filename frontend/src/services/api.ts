import axios from 'axios';
import {
  Agent,
  Task,
  Memory,
  DashboardStats,
  GraphData,
  ResultSummary,
  ResultContentResponse,
  ResultValidationDetail,
  ExtraFileContentResponse,
  TicketDetail,
  TicketComment,
  TicketHistory,
  TicketCommit,
  TicketStats,
  CommitDiff,
  TicketSearchResult,
  BlockedTask,
  WorkflowDefinition,
  WorkflowExecution,
} from '@/types';

interface ResultQueryParams {
  scope?: 'all' | 'workflow' | 'task';
  status?: string;
  workflow_id?: string;
  agent_id?: string;
  search?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

export const apiService = {
  // Workflow Definitions and Executions
  listWorkflowDefinitions: async (): Promise<WorkflowDefinition[]> => {
    const { data } = await api.get('/workflow-definitions');
    return data.definitions || [];
  },

  getWorkflowDefinitionPhases: async (definitionId: string): Promise<any[]> => {
    const { data } = await api.get(`/workflow-definitions/${definitionId}/phases`);
    return data.phases || [];
  },

  listWorkflowExecutions: async (status: string = 'all'): Promise<WorkflowExecution[]> => {
    const { data } = await api.get(`/workflow-executions?status=${status}`);
    return data.executions || [];
  },

  startWorkflowExecution: async (
    definitionId: string,
    description: string,
    workingDirectory?: string,
    launchParams?: Record<string, any>
  ): Promise<{ workflow_id: string }> => {
    const { data } = await api.post('/workflow-executions', {
      definition_id: definitionId,
      description,
      working_directory: workingDirectory,
      launch_params: launchParams,
    });
    return data;
  },

  getWorkflowExecution: async (workflowId: string): Promise<WorkflowExecution & { phases: any[] }> => {
    const { data } = await api.get(`/workflow-executions/${workflowId}`);
    return data;
  },

  stopWorkflow: async (workflowId: string): Promise<{ status: string; agents_terminated: number }> => {
    const { data } = await api.post(`/workflow-executions/${workflowId}/stop`);
    return data;
  },

  resumeWorkflow: async (workflowId: string): Promise<{ status: string }> => {
    const { data } = await api.post(`/workflow-executions/${workflowId}/resume`);
    return data;
  },

  // Recover an interrupted run (crash / sleep / restart): restarts orphaned phase
  // agents on their existing worktree so the run continues from the committed state.
  // Omit workflowId to recover all interrupted runs.
  recoverWorkflow: async (
    workflowId?: string
  ): Promise<{ recovered: boolean; resumed_agents: number; workflows: string[] }> => {
    const { data } = await api.post('/autopilot/recover', null, {
      params: workflowId ? { workflow_id: workflowId } : {},
    });
    return data;
  },

  pauseWorkflow: async (workflowId: string): Promise<{ status: string }> => {
    const { data } = await api.post(`/workflow-executions/${workflowId}/stop`);
    return data;
  },

  cancelWorkflow: async (workflowId: string): Promise<{ cancelled: string; agents_terminated: number }> => {
    const { data } = await api.post(`/workflow-executions/${workflowId}/cancel`);
    return data;
  },

  // Dashboard
  getDashboardStats: async (workflowId?: string, projectId?: string): Promise<DashboardStats> => {
    const params = new URLSearchParams();
    if (workflowId) params.append('workflow_id', workflowId);
    if (projectId) params.append('project_id', projectId);
    const { data } = await api.get(`/dashboard/stats?${params}`);
    return data;
  },

  // Tasks
  getTasks: async (skip = 0, limit = 50, status?: string, workflowId?: string, projectId?: string): Promise<Task[]> => {
    const params = new URLSearchParams();
    params.append('skip', skip.toString());
    params.append('limit', limit.toString());
    if (status) params.append('status', status);
    if (workflowId) params.append('workflow_id', workflowId);
    if (projectId) params.append('project_id', projectId);

    const { data } = await api.get(`/tasks?${params}`);
    return data;
  },

  // Agents
  getAgents: async (status: string = 'active', page: number = 1, projectId?: string): Promise<{ agents: Agent[]; total: number; page: number; per_page: number; pages: number }> => {
    const params = new URLSearchParams();
    params.append('status', status);
    params.append('page', page.toString());
    params.append('per_page', '20');
    if (projectId) params.append('project_id', projectId);
    const { data } = await api.get(`/agents?${params}`);
    return data;
  },

  getAgent: async (agentId: string): Promise<Agent | null> => {
    try {
      // Try fetching from all agents (most agents fit in one page)
      const { data } = await api.get(`/agents?status=all&page=1&per_page=200`);
      const agents = data?.agents || [];
      return agents.find((a: Agent) => a.id === agentId) || null;
    } catch {
      return null;
    }
  },

  getAgentOutput: async (agentId: string, lines = 2000): Promise<{ output: string; timestamp: string }> => {
    const { data } = await api.get(`/agents/${agentId}/output?lines=${lines}`);
    return data;
  },

  // Memories
  getMemories: async (skip = 0, limit = 50, memoryType?: string, search?: string): Promise<{ memories: Memory[]; total: number; type_counts: Record<string, number> }> => {
    const params = new URLSearchParams();
    params.append('skip', skip.toString());
    params.append('limit', limit.toString());
    if (memoryType) params.append('memory_type', memoryType);
    if (search) params.append('search', search);

    const { data } = await api.get(`/memories?${params}`);
    return data;
  },

  // Graph
  getGraphData: async (workflowId?: string): Promise<GraphData> => {
    const params = workflowId ? `?workflow_id=${workflowId}` : '';
    const { data } = await api.get(`/graph${params}`);
    return data;
  },

  // Task Full Details
  getTaskFullDetails: async (taskId: string): Promise<any> => {
    const { data } = await api.get(`/tasks/${taskId}/full-details`);
    return data;
  },

  // Get single task by ID
  getTaskById: async (taskId: string): Promise<Task> => {
    const { data } = await api.get(`/tasks/${taskId}`);
    return data;
  },

  // Guardian Analyses
  getGuardianAnalyses: async (agentId: string, limit = 50): Promise<any[]> => {
    const { data } = await api.get(`/guardian-analyses/${agentId}?limit=${limit}`);
    return data;
  },

  // Conductor Analyses
  getConductorAnalyses: async (limit = 20): Promise<any[]> => {
    const { data } = await api.get(`/conductor-analyses?limit=${limit}`);
    return data;
  },

  getLatestConductorAnalysis: async (): Promise<any | null> => {
    const { data } = await api.get('/conductor-analyses/latest');
    return data;
  },

  // Steering Interventions
  getSteeringInterventions: async (agentId?: string, limit = 50): Promise<any[]> => {
    const params = new URLSearchParams();
    params.append('limit', limit.toString());
    if (agentId) params.append('agent_id', agentId);

    const { data } = await api.get(`/steering-interventions?${params}`);
    return data;
  },

  // System Overview
  getSystemOverview: async (workflowId?: string | null): Promise<any> => {
    const { data } = await api.get('/system-overview', {
      params: workflowId ? { workflow_id: workflowId } : undefined,
    });
    return data;
  },

  // Results
  getResults: async (params: ResultQueryParams = {}): Promise<ResultSummary[]> => {
    try {
      const searchParams = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
          searchParams.append(key, String(value));
        }
      });
      const query = searchParams.toString();
      const { data } = await api.get(`/results${query ? `?${query}` : ''}`);
      return data;
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return [];
      }
      throw error;
    }
  },

  getResultContent: async (resultId: string): Promise<ResultContentResponse | null> => {
    try {
      const { data } = await api.get(`/results/${resultId}/content`);
      return data;
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return null;
      }
      throw error;
    }
  },

  getResultValidation: async (resultId: string): Promise<ResultValidationDetail | null> => {
    try {
      const { data } = await api.get(`/results/${resultId}/validation`);
      return data;
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return null;
      }
      throw error;
    }
  },

  getExtraFileContent: async (resultId: string, fileIndex: number): Promise<ExtraFileContentResponse | null> => {
    try {
      const { data } = await api.get(`/results/${resultId}/extra-files/${fileIndex}`);
      return data;
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return null;
      }
      throw error;
    }
  },

  // Agent Communication
  broadcastMessage: async (message: string, senderAgentId: string = 'ui-user'): Promise<{ success: boolean; recipient_count: number; message: string }> => {
    const { data } = await api.post(
      '/broadcast_message',
      { message },
      {
        headers: {
          'X-Agent-ID': senderAgentId,
        },
      }
    );
    return data;
  },

  sendMessage: async (
    message: string,
    recipientAgentId: string,
    senderAgentId: string = 'ui-user'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post(
      '/send_message',
      {
        recipient_agent_id: recipientAgentId,
        message,
      },
      {
        headers: {
          'X-Agent-ID': senderAgentId,
        },
      }
    );
    return data;
  },

  // Queue management endpoints
  getQueueStatus: async (workflowId?: string): Promise<{
    active_agents: number;
    max_concurrent_agents: number;
    queued_tasks_count: number;
    queued_tasks: Array<{
      task_id: string;
      description: string;
      priority: string;
      priority_boosted: boolean;
      queue_position: number;
      queued_at: string | null;
      phase_id: string | null;
    }>;
    slots_available: number;
    at_capacity: boolean;
  }> => {
    const params = workflowId ? `?workflow_id=${workflowId}` : '';
    const { data } = await api.get(`/queue_status${params}`);
    return data;
  },

  terminateAgent: async (
    agentId: string,
    reason: string = 'Manual termination from UI'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post('/terminate_agent', {
      agent_id: agentId,
      reason,
    });
    return data;
  },

  bumpTaskPriority: async (
    taskId: string
  ): Promise<{ success: boolean; message: string; agent_id: string }> => {
    const { data } = await api.post('/bump_task_priority', {
      task_id: taskId,
    });
    return data;
  },

  cancelQueuedTask: async (
    taskId: string
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post('/cancel_queued_task', {
      task_id: taskId,
    });
    return data;
  },

  restartTask: async (
    taskId: string
  ): Promise<{ success: boolean; message: string; agent_id?: string; status: string }> => {
    const { data } = await api.post('/restart_task', {
      task_id: taskId,
    });
    return data;
  },

  pauseTask: async (taskId: string): Promise<{ success: boolean; task_id: string; status: string }> => {
    const { data } = await api.post(`/tasks/${encodeURIComponent(taskId)}/pause`);
    return data;
  },

  cancelTask: async (taskId: string): Promise<{ success: boolean; task_id: string }> => {
    const { data } = await api.post(`/tasks/${encodeURIComponent(taskId)}/cancel`);
    return data;
  },

  // Ticket Tracking System Endpoints

  createTicket: async (
    ticketData: {
      workflow_id: string;
      title: string;
      description: string;
      ticket_type?: string;
      priority?: string;
      assigned_agent_id?: string;
      parent_ticket_id?: string;
      tags?: string[];
    },
    agentId: string = 'ui-user'
  ): Promise<{ ticket_id: string; status: string }> => {
    const { data } = await api.post('/tickets/create', ticketData, {
      headers: { 'X-Agent-ID': agentId },
    });
    return data;
  },

  updateTicket: async (
    ticketId: string,
    updates: {
      title?: string;
      description?: string;
      priority?: string;
      assigned_agent_id?: string;
      tags?: string[];
    },
    agentId: string = 'ui-user'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post('/tickets/update', { ticket_id: ticketId, ...updates }, {
      headers: { 'X-Agent-ID': agentId },
    });
    return data;
  },

  changeTicketStatus: async (
    ticketId: string,
    newStatus: string,
    comment?: string,
    agentId: string = 'ui-user'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post('/tickets/change-status', {
      ticket_id: ticketId,
      new_status: newStatus,
      comment,
    }, {
      headers: { 'X-Agent-ID': agentId },
    });
    return data;
  },

  addTicketComment: async (
    ticketId: string,
    commentText: string,
    commentType: string = 'general',
    agentId: string = 'ui-user'
  ): Promise<{ comment_id: string }> => {
    const { data } = await api.post('/tickets/comment', {
      ticket_id: ticketId,
      comment_text: commentText,
      comment_type: commentType,
    }, {
      headers: { 'X-Agent-ID': agentId },
    });
    return data;
  },

  getTicket: async (ticketId: string): Promise<{
    ticket: TicketDetail;
    comments: TicketComment[];
    history: TicketHistory[];
    commits: TicketCommit[];
  }> => {
    const { data } = await api.get(`/tickets/${ticketId}`, {
      headers: { 'X-Agent-ID': 'ui-user' },
    });
    return data;
  },

  getTickets: async (params?: {
    workflow_id?: string;
    project_id?: string;
    status?: string;
    assigned_agent_id?: string;
    is_blocked?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<TicketDetail[]> => {
    const searchParams = new URLSearchParams();
    if (params) {
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null) {
          searchParams.append(key, String(value));
        }
      });
    }
    const query = searchParams.toString();
    const { data } = await api.get(`/tickets${query ? `?${query}` : ''}`, {
      headers: { 'X-Agent-ID': 'ui-user' },
    });
    return data.tickets || [];
  },

  searchTickets: async (params: {
    workflow_id?: string;
    query: string;
    search_type?: 'semantic' | 'keyword' | 'hybrid';
    filters?: {
      status?: string[];
      ticket_type?: string[];
      priority?: string[];
      assigned_agent_id?: string[];
      is_blocked?: boolean;
      is_resolved?: boolean;
      tags?: string[];
    };
    limit?: number;
  }): Promise<TicketSearchResult[]> => {
    const { data } = await api.post('/tickets/search', params, {
      headers: { 'X-Agent-ID': 'ui-user' },
    });
    // Backend returns {success, query, results, total_found, search_time_ms}
    // Frontend expects just the results array
    return data.results || [];
  },

  getTicketStats: async (workflowOrProjectId: string): Promise<TicketStats> => {
    const isProjectId = workflowOrProjectId.startsWith('proj-');
    const params = isProjectId ? `?project_id=${workflowOrProjectId}` : '';
    const path = isProjectId ? '/tickets/stats' : `/tickets/stats/${workflowOrProjectId}`;
    const { data } = await api.get(`${path}${params}`, {
      headers: { 'X-Agent-ID': 'ui-user' },
    });
    // Backend returns {success, workflow_id, stats, board_config}
    // Frontend expects stats merged with board_config
    return {
      ...data.stats,
      board_config: data.board_config,
    };
  },

  resolveTicket: async (
    ticketId: string,
    resolution: string,
    agentId: string = 'ui-user'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post('/tickets/resolve', {
      ticket_id: ticketId,
      resolution,
    }, {
      headers: { 'X-Agent-ID': agentId },
    });
    return data;
  },

  approveTicket: async (
    ticketId: string,
    agentId: string = 'ui-user'
  ): Promise<{ success: boolean; ticket_id: string; message: string }> => {
    const { data } = await api.post('/tickets/approve',
      { ticket_id: ticketId },
      { headers: { 'X-Agent-ID': agentId } }
    );
    return data;
  },

  rejectTicket: async (
    ticketId: string,
    rejectionReason: string,
    agentId: string = 'ui-user'
  ): Promise<{ success: boolean; ticket_id: string; message: string }> => {
    const { data } = await api.post('/tickets/reject',
      { ticket_id: ticketId, rejection_reason: rejectionReason },
      { headers: { 'X-Agent-ID': agentId } }
    );
    return data;
  },

  getPendingReviewCount: async (): Promise<{ count: number; ticket_ids: string[] }> => {
    const { data } = await api.get('/tickets/pending-review-count');
    return data;
  },

  getCommitDiff: async (commitSha: string): Promise<CommitDiff> => {
    const { data } = await api.get(`/tickets/commit-diff/${commitSha}`, {
      headers: { 'X-Agent-ID': 'ui-user' },
    });
    return data;
  },

  // Blocked Tasks
  getBlockedTasks: async (workflowId?: string, projectId?: string): Promise<BlockedTask[]> => {
    const params = new URLSearchParams();
    if (workflowId) params.append('workflow_id', workflowId);
    if (projectId) params.append('project_id', projectId);
    const { data } = await api.get(`/blocked-tasks?${params}`);
    return data;
  },

  getTaskBlockerDetails: async (taskId: string): Promise<{
    task_id: string;
    is_blocked: boolean;
    blocker_count: number;
    blockers: Array<{
      ticket_id: string;
      title: string;
      status: string;
      priority: string;
      is_resolved: boolean;
    }>;
  }> => {
    const { data } = await api.get(`/blocked-tasks/${taskId}/blockers`);
    return data;
  },

  syncBlockingStatus: async (): Promise<{
    success: boolean;
    tasks_blocked: number;
    tasks_unblocked: number;
    total_checked: number;
    errors: Array<{ task_id: string; error: string }>;
  }> => {
    const { data } = await api.post('/sync-blocking-status');
    return data;
  },

  // Autopilot
  getAutopilotStatus: async (projectId?: string, projectPath?: string): Promise<any> => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (projectPath) params.set('project_path', projectPath);
    const qs = params.toString();
    const { data } = await api.get(`/autopilot/status${qs ? '?' + qs : ''}`);
    return data;
  },

  startAutopilot: async (projectPath: string, designQueue: string = '', maxIterations: number = 3): Promise<any> => {
    const params = new URLSearchParams({ project_path: projectPath });
    if (designQueue) params.set('design_queue', designQueue);
    params.set('max_iterations', String(maxIterations));
    const { data } = await api.post(`/autopilot/start?${params.toString()}`);
    return data;
  },

  stopAutopilot: async (projectId?: string): Promise<any> => {
    const params = projectId ? `?project_id=${projectId}` : '';
    const { data } = await api.post(`/autopilot/stop${params}`);
    return data;
  },

  getAutopilotQueue: async (): Promise<any[]> => {
    const { data } = await api.get('/autopilot/queue');
    return data;
  },

  addToAutopilotQueue: async (name: string, content: string, extension: string = '.md'): Promise<any> => {
    const { data } = await api.post('/autopilot/queue', { name, content, extension });
    return data;
  },

  removeFromAutopilotQueue: async (filename: string): Promise<void> => {
    await api.delete(`/autopilot/queue/${encodeURIComponent(filename)}`);
  },

  reorderAutopilotQueue: async (filenames: string[]): Promise<void> => {
    await api.post('/autopilot/queue/reorder', { filenames });
  },

  getAutopilotQueueContent: async (filename: string): Promise<{ filename: string; content: string }> => {
    const { data } = await api.get(`/autopilot/queue/${encodeURIComponent(filename)}/content`);
    return data;
  },

  getAutopilotFeatures: async (): Promise<any[]> => {
    const { data } = await api.get('/autopilot/features');
    return data;
  },

  getAutopilotFeatureDetail: async (featureId: string): Promise<any> => {
    const { data } = await api.get(`/autopilot/features/${encodeURIComponent(featureId)}`);
    return data;
  },

  getAutopilotFeatureReport: async (featureId: string): Promise<string> => {
    const { data } = await api.get(`/autopilot/features/${encodeURIComponent(featureId)}/report`, {
      responseType: 'text',
    });
    return data;
  },

  getAutopilotFeatureDoc: async (featureId: string, docName: string): Promise<{ name: string; content: string }> => {
    const { data } = await api.get(`/autopilot/features/${encodeURIComponent(featureId)}/docs/${encodeURIComponent(docName)}`);
    return data;
  },

  getAutopilotFeatureLogs: async (featureId: string): Promise<{ logs: Array<{ name: string; size_bytes: number; modified: string }> }> => {
    const { data } = await api.get(`/autopilot/features/${encodeURIComponent(featureId)}/logs`);
    return data;
  },

  getAutopilotFeatureLog: async (featureId: string, logName: string): Promise<{ name: string; content: string }> => {
    const { data } = await api.get(`/autopilot/features/${encodeURIComponent(featureId)}/logs/${encodeURIComponent(logName)}`);
    return data;
  },

  // Feature Model (DB Feature rows) docs -- distinct from the legacy
  // FEATURES_DIR-scanned endpoints above. Reads generated docs from the
  // feature's own workflow working_directory.
  getFeatureRecordDocs: async (featureId: string): Promise<{ docs: Array<{ name: string; size_bytes: number; modified: string; type: string }> }> => {
    const { data } = await api.get(`/autopilot/feature-records/${encodeURIComponent(featureId)}/docs`);
    return data;
  },

  getFeatureRecordDoc: async (featureId: string, docName: string): Promise<{ name: string; content: string }> => {
    const { data } = await api.get(`/autopilot/feature-records/${encodeURIComponent(featureId)}/docs/${encodeURIComponent(docName)}`);
    return data;
  },

  getAutopilotMessages: async (limit: number = 50): Promise<any[]> => {
    const { data } = await api.get(`/autopilot/messages?limit=${limit}`);
    return data;
  },

  getAutopilotArchivedMessages: async (): Promise<{ archived_ids: string[] }> => {
    const { data } = await api.get('/autopilot/messages/archived');
    return data;
  },

  archiveAutopilotMessage: async (messageId: string, messageType: string, timestamp: string): Promise<void> => {
    await api.post('/autopilot/messages/archive', {
      message_id: messageId,
      message_type: messageType,
      timestamp: timestamp,
    });
  },

  unarchiveAutopilotMessage: async (messageId: string): Promise<void> => {
    await api.post('/autopilot/messages/unarchive', { message_id: messageId });
  },

  unarchiveAllAutopilotMessages: async (): Promise<void> => {
    await api.post('/autopilot/messages/unarchive-all');
  },

  getAutopilotLogs: async (lines: number = 100): Promise<{ lines: string[] }> => {
    const { data } = await api.get(`/autopilot/logs?lines=${lines}`);
    return data;
  },

  getAutopilotInput: async (): Promise<any> => {
    const { data } = await api.get('/autopilot/input');
    return data;
  },

  submitAutopilotInput: async (requestId: string, choice: string, message?: string): Promise<void> => {
    await api.post('/autopilot/input', { request_id: requestId, choice, message });
  },

  dismissAutopilotInput: async (requestId: string): Promise<void> => {
    await api.delete(`/autopilot/input/${encodeURIComponent(requestId)}`);
  },

  // Autopilot Projects
  getAutopilotProjects: async (): Promise<any[]> => {
    const { data } = await api.get('/autopilot/projects');
    return data;
  },

  createAutopilotProject: async (name: string, baseDir: string, isDefault: boolean = false): Promise<any> => {
    const { data } = await api.post('/autopilot/projects', { name, base_dir: baseDir, is_default: isDefault });
    return data;
  },

  updateAutopilotProject: async (projectId: string, updates: { name?: string; base_dir?: string; is_default?: boolean }): Promise<any> => {
    const { data } = await api.put(`/autopilot/projects/${encodeURIComponent(projectId)}`, updates);
    return data;
  },

  deleteAutopilotProject: async (projectId: string): Promise<void> => {
    await api.delete(`/autopilot/projects/${encodeURIComponent(projectId)}`);
  },

  syncAutopilotProject: async (projectId: string): Promise<any[]> => {
    const { data } = await api.post(`/autopilot/projects/${encodeURIComponent(projectId)}/sync`);
    return data;
  },

  getAutopilotProjectDesigns: async (projectId: string): Promise<any[]> => {
    const { data } = await api.get(`/autopilot/projects/${encodeURIComponent(projectId)}/designs`);
    return data;
  },
  reloadAutopilotProjectDesigns: async (projectId: string): Promise<any[]> => {
    const { data } = await api.post(`/autopilot/projects/${encodeURIComponent(projectId)}/designs/reload`);
    return data;
  },

  addAutopilotProjectDesign: async (projectId: string, name: string, content: string, extension: string = '.md'): Promise<any> => {
    const { data } = await api.post(`/autopilot/projects/${encodeURIComponent(projectId)}/designs`, { name, content, extension });
    return data;
  },

  reorderAutopilotProjectDesigns: async (projectId: string, designIds: string[]): Promise<void> => {
    await api.put(`/autopilot/projects/${encodeURIComponent(projectId)}/designs/reorder`, { design_ids: designIds });
  },

  requeueAutopilotDesign: async (filename: string): Promise<{ requeued: boolean; paused_workflows: number }> => {
    const { data } = await api.post('/autopilot/queue/requeue', { filename });
    return data;
  },

  removeAutopilotProjectDesign: async (projectId: string, filename: string): Promise<void> => {
    await api.delete(`/autopilot/projects/${encodeURIComponent(projectId)}/designs/${encodeURIComponent(filename)}`);
  },

  getAutopilotProjectDesignContent: async (projectId: string, filename: string): Promise<{ filename: string; content: string }> => {
    const { data } = await api.get(`/autopilot/projects/${encodeURIComponent(projectId)}/designs/${encodeURIComponent(filename)}/content`);
    return data;
  },

  getAutopilotProjectDesignStatus: async (projectId: string, filename: string): Promise<any> => {
    const { data } = await api.get(`/autopilot/projects/${encodeURIComponent(projectId)}/designs/${encodeURIComponent(filename)}/status`);
    return data;
  },

  pauseFeature: async (featureId: string): Promise<any> => {
    const { data } = await api.post(`/autopilot/features/${encodeURIComponent(featureId)}/pause`);
    return data;
  },

  resumeFeature: async (featureId: string): Promise<any> => {
    const { data } = await api.post(`/autopilot/features/${encodeURIComponent(featureId)}/resume`);
    return data;
  },

  // Unified Projects
  getProjects: async (): Promise<any[]> => {
    const { data } = await api.get('/projects');
    return data;
  },

  getActiveProject: async (): Promise<any | null> => {
    const { data } = await api.get('/projects/active');
    return data;
  },

  createProject: async (name: string, baseDir: string, isDefault: boolean = false): Promise<any> => {
    const { data } = await api.post('/projects', { name, base_dir: baseDir, is_default: isDefault });
    return data;
  },

  activateProject: async (projectId: string): Promise<any> => {
    const { data } = await api.post(`/projects/${encodeURIComponent(projectId)}/activate`);
    return data;
  },

  deleteProject: async (projectId: string): Promise<void> => {
    await api.delete(`/projects/${encodeURIComponent(projectId)}`);
  },

  // ── Phase Prompt Editor ──────────────────────────────────────────────

  updatePhase: async (
    phaseId: string,
    updates: {
      description?: string;
      done_definitions?: string[];
      additional_notes?: string | null;
      outputs?: string | null;
      next_steps?: string | null;
      working_directory?: string | null;
      cli_tool?: string | null;
      cli_model?: string | null;
    }
  ): Promise<{ success: boolean; phase: any }> => {
    const { data } = await api.patch(`/phases/${phaseId}`, updates);
    return data;
  },

  resetPhase: async (
    phaseId: string,
    targetStatus: string,
    force: boolean = false
  ): Promise<{
    success: boolean;
    terminated_agents?: number;
    reset_tasks?: number;
    message: string;
    requires_confirmation?: boolean;
    active_agents?: number;
  }> => {
    const { data } = await api.post(`/phases/${phaseId}/reset`, {
      target_status: targetStatus,
      force,
    });
    return data;
  },

  getPhaseAgents: async (phaseId: string): Promise<{ agents: any[] }> => {
    const { data } = await api.get(`/phases/${phaseId}/agents`);
    return data;
  },

  // ── Phase Prompt Versions ────────────────────────────────────────────

  getPhaseYaml: async (phaseId: string): Promise<any> => {
    const { data } = await api.get(`/phases/${phaseId}/yaml`);
    return data;
  },

  getPhasePromptVersions: async (
    phaseId: string
  ): Promise<{ versions: import('@/types').PhasePromptVersion[] }> => {
    const { data } = await api.get(`/phases/${phaseId}/prompt/versions`);
    return data;
  },

  getPhasePromptVersion: async (
    phaseId: string,
    version: number
  ): Promise<import('@/types').PhasePromptVersionDetail> => {
    const { data } = await api.get(`/phases/${phaseId}/prompt/versions/${version}`);
    return data;
  },

  createPhasePromptVersion: async (
    phaseId: string,
    payload: import('@/types').PromptSavePayload
  ): Promise<import('@/types').PromptSaveResponse> => {
    const { data } = await api.post(`/phases/${phaseId}/prompt/versions`, payload);
    return data;
  },

  publishPhasePromptVersion: async (
    phaseId: string,
    version: number
  ): Promise<{ success: boolean; version: number; status: string }> => {
    const { data } = await api.post(
      `/phases/${phaseId}/prompt/versions/${version}/publish`
    );
    return data;
  },

  restorePhasePromptVersion: async (
    phaseId: string,
    version: number
  ): Promise<{ success: boolean; version: number; restored_from: number }> => {
    const { data } = await api.post(
      `/phases/${phaseId}/prompt/versions/${version}/restore`
    );
    return data;
  },

  getPhasePromptPreview: async (
    phaseId: string,
    variables?: Record<string, string>
  ): Promise<import('@/types').PhasePromptPreview> => {
    const params = variables ? `?variables=${encodeURIComponent(JSON.stringify(variables))}` : '';
    const { data } = await api.get(`/phases/${phaseId}/prompt/preview${params}`);
    return data;
  },

  getPhasePromptPreviewDraft: async (
    phaseId: string,
    draft: {
      description?: string;
      done_definitions?: string[];
      additional_notes?: string | null;
      outputs?: string | null;
      next_steps?: string | null;
      variables?: Record<string, string>;
    }
  ): Promise<import('@/types').PhasePromptPreview> => {
    const { data } = await api.post(`/phases/${phaseId}/prompt/preview`, draft);
    return data;
  },

  getPhasePromptDiff: async (
    phaseId: string,
    v1: number,
    v2: number
  ): Promise<import('@/types').PhasePromptDiff> => {
    const { data } = await api.get(
      `/phases/${phaseId}/prompt/diff?v1=${v1}&v2=${v2}`
    );
    return data;
  },

  // ── Task Prompt Overrides ────────────────────────────────────────────

  getTaskPromptOverrides: async (
    taskId: string
  ): Promise<import('@/types').TaskPromptOverrides> => {
    const { data } = await api.get(`/tasks/${taskId}/prompt/overrides`);
    return data;
  },

  setTaskPromptOverrides: async (
    taskId: string,
    overrides: { system_prompt?: string; user_prompt?: string }
  ): Promise<{
    success: boolean;
    overrides: import('@/types').TaskPromptOverrides;
    effective_prompt: import('@/types').TaskPrompt;
  }> => {
    const { data } = await api.put(`/tasks/${taskId}/prompt/overrides`, overrides);
    return data;
  },

  clearTaskPromptOverrides: async (taskId: string): Promise<{ success: boolean }> => {
    const { data } = await api.delete(`/tasks/${taskId}/prompt/overrides`);
    return data;
  },
};

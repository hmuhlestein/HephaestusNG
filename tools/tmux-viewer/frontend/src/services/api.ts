import axios from 'axios';
import { TmuxSession, SessionOutput, Agent } from './types';

const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
});

export const tmuxApi = {
  listSessions: async (prefix?: string): Promise<TmuxSession[]> => {
    const params = prefix ? `?prefix=${prefix}` : '';
    const { data } = await api.get(`/sessions${params}`);
    return data;
  },

  createSession: async (
    sessionName: string,
    workingDirectory?: string,
    envVars?: Record<string, string>
  ): Promise<{ session_name: string; status: string }> => {
    const { data } = await api.post('/sessions', {
      session_name: sessionName,
      working_directory: workingDirectory,
      env_vars: envVars,
    });
    return data;
  },

  killSession: async (sessionName: string): Promise<{ session_name: string; status: string }> => {
    const { data } = await api.delete(`/sessions/${sessionName}`);
    return data;
  },

  getOutput: async (sessionName: string, lines = 2000): Promise<SessionOutput> => {
    const { data } = await api.get(`/sessions/${sessionName}/output?lines=${lines}`);
    return data;
  },

  sendMessage: async (
    sessionName: string,
    message: string,
    enter = true
  ): Promise<{ session_name: string; status: string }> => {
    const { data } = await api.post(`/sessions/${sessionName}/send`, { message, enter });
    return data;
  },

  sessionExists: async (sessionName: string): Promise<boolean> => {
    const { data } = await api.get(`/sessions/${sessionName}/exists`);
    return data.exists;
  },

  getAgentOutput: async (agentId: string, lines = 2000): Promise<{ output: string; timestamp: string }> => {
    const { data } = await api.get(`/agents/${agentId}/output?lines=${lines}`);
    return data;
  },

  sendMessageToAgent: async (
    message: string,
    recipientAgentId: string,
    senderAgentId = 'ui-user'
  ): Promise<{ success: boolean; message: string }> => {
    const { data } = await api.post(
      '/send_message',
      { recipient_agent_id: recipientAgentId, message },
      { headers: { 'X-Agent-ID': senderAgentId } }
    );
    return data;
  },
};

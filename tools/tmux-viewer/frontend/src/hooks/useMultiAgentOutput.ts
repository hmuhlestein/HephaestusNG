import { useState, useEffect, useRef, useCallback } from 'react';
import { tmuxApi } from '../services/api';
import { AgentOutputData } from '../types';

interface UseMultiAgentOutputOptions {
  updateInterval?: number;
  maxRetries?: number;
  enabled?: boolean;
  staggerInterval?: number;
}

export const useMultiAgentOutput = (
  agentIds: string[],
  options: UseMultiAgentOutputOptions = {}
) => {
  const {
    updateInterval = 1000,
    maxRetries = 3,
    enabled = true,
    staggerInterval = 100,
  } = options;

  const [outputs, setOutputs] = useState<Record<string, AgentOutputData>>({});

  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const retryCountsRef = useRef<Record<string, number>>({});
  const lastOutputsRef = useRef<Record<string, string>>({});
  const mountedRef = useRef(true);
  const fetchQueuesRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    setOutputs(current => {
      const newOutputs = { ...current };

      agentIds.forEach(id => {
        if (!newOutputs[id]) {
          newOutputs[id] = {
            output: '',
            timestamp: '',
            isLoading: false,
            error: null,
            isConnected: false,
            lastUpdateTime: null,
          };
          retryCountsRef.current[id] = 0;
          lastOutputsRef.current[id] = '';
        }
      });

      Object.keys(newOutputs).forEach(id => {
        if (!agentIds.includes(id)) {
          delete newOutputs[id];
          delete retryCountsRef.current[id];
          delete lastOutputsRef.current[id];
        }
      });

      return newOutputs;
    });
  }, [agentIds]);

  const fetchAgentOutput = useCallback(async (agentId: string) => {
    if (!mountedRef.current || fetchQueuesRef.current.has(agentId)) return;

    fetchQueuesRef.current.add(agentId);

    try {
      setOutputs(prev => ({
        ...prev,
        [agentId]: { ...prev[agentId], isLoading: true, error: null },
      }));

      const result = await tmuxApi.getAgentOutput(agentId);

      if (!mountedRef.current) return;

      const hasChanged = result.output !== lastOutputsRef.current[agentId];
      lastOutputsRef.current[agentId] = result.output;

      setOutputs(prev => ({
        ...prev,
        [agentId]: {
          output: result.output,
          timestamp: result.timestamp,
          isLoading: false,
          isConnected: true,
          lastUpdateTime: hasChanged ? new Date() : prev[agentId]?.lastUpdateTime,
          error: null,
        },
      }));

      retryCountsRef.current[agentId] = 0;
    } catch (error) {
      if (!mountedRef.current) return;

      const retryCount = (retryCountsRef.current[agentId] || 0) + 1;
      retryCountsRef.current[agentId] = retryCount;

      setOutputs(prev => ({
        ...prev,
        [agentId]: {
          ...prev[agentId],
          isLoading: false,
          isConnected: retryCount < maxRetries,
          error: retryCount >= maxRetries
            ? `Failed to connect to agent ${agentId.substring(0, 8)}`
            : null,
        },
      }));
    } finally {
      fetchQueuesRef.current.delete(agentId);
    }
  }, [maxRetries]);

  const fetchAllAgents = useCallback(async () => {
    if (!enabled || agentIds.length === 0) return;

    for (let i = 0; i < agentIds.length; i++) {
      if (!mountedRef.current) break;
      if ((retryCountsRef.current[agentIds[i]] || 0) >= maxRetries) continue;

      fetchAgentOutput(agentIds[i]);

      if (i < agentIds.length - 1) {
        await new Promise(resolve => setTimeout(resolve, staggerInterval));
      }
    }
  }, [agentIds, enabled, fetchAgentOutput, maxRetries, staggerInterval]);

  const startPolling = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    fetchAllAgents();
    intervalRef.current = setInterval(fetchAllAgents, updateInterval);
  }, [fetchAllAgents, updateInterval]);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setOutputs(current => {
      const newOutputs = { ...current };
      Object.keys(newOutputs).forEach(id => {
        newOutputs[id] = { ...newOutputs[id], isConnected: false };
      });
      return newOutputs;
    });
  }, []);

  const retryAgent = useCallback((agentId: string) => {
    retryCountsRef.current[agentId] = 0;
    fetchAgentOutput(agentId);
  }, [fetchAgentOutput]);

  useEffect(() => {
    if (enabled && agentIds.length > 0) {
      startPolling();
    } else {
      stopPolling();
    }
    return () => stopPolling();
  }, [enabled, agentIds.length, startPolling, stopPolling]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  const stats = {
    total: agentIds.length,
    connected: Object.values(outputs).filter(o => o.isConnected).length,
    failed: Object.values(outputs).filter(o => o.error).length,
    loading: Object.values(outputs).filter(o => o.isLoading).length,
  };

  return { outputs, stats, retryAgent, startPolling, stopPolling };
};

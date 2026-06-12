import { useState, useEffect, useRef, useCallback } from 'react';
import { tmuxApi } from '../services/api';
import { AgentOutputData } from '../types';

interface UseRealTimeAgentOutputOptions {
  updateInterval?: number;
  maxRetries?: number;
  enabled?: boolean;
}

export const useRealTimeAgentOutput = (
  agentId: string | null,
  options: UseRealTimeAgentOutputOptions = {}
) => {
  const {
    updateInterval = 1000,
    maxRetries = 3,
    enabled = true,
  } = options;

  const [data, setData] = useState<AgentOutputData>({
    output: '',
    timestamp: '',
    isLoading: false,
    error: null,
    isConnected: false,
    lastUpdateTime: null,
  });

  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const retryCountRef = useRef(0);
  const lastOutputRef = useRef('');
  const mountedRef = useRef(true);

  const fetchAgentOutput = useCallback(async () => {
    if (!agentId || !enabled || !mountedRef.current) return;

    try {
      setData(prev => ({ ...prev, isLoading: true, error: null }));

      const result = await tmuxApi.getAgentOutput(agentId);

      if (!mountedRef.current) return;

      const hasChanged = result.output !== lastOutputRef.current;
      lastOutputRef.current = result.output;

      setData(prev => ({
        ...prev,
        output: result.output,
        timestamp: result.timestamp,
        isLoading: false,
        isConnected: true,
        lastUpdateTime: hasChanged ? new Date() : prev.lastUpdateTime,
        error: null,
      }));

      retryCountRef.current = 0;
    } catch (error) {
      if (!mountedRef.current) return;

      retryCountRef.current++;

      setData(prev => ({
        ...prev,
        isLoading: false,
        isConnected: retryCountRef.current < maxRetries,
        error: retryCountRef.current >= maxRetries
          ? 'Failed to connect to agent output'
          : null,
      }));

      if (retryCountRef.current >= maxRetries && intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    }
  }, [agentId, enabled, maxRetries]);

  const startPolling = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    fetchAgentOutput();
    intervalRef.current = setInterval(fetchAgentOutput, updateInterval);
  }, [fetchAgentOutput, updateInterval]);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setData(prev => ({ ...prev, isConnected: false }));
  }, []);

  const retry = useCallback(() => {
    retryCountRef.current = 0;
    startPolling();
  }, [startPolling]);

  useEffect(() => {
    if (agentId && enabled) {
      startPolling();
    } else {
      stopPolling();
    }
    return () => stopPolling();
  }, [agentId, enabled, startPolling, stopPolling]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  return { ...data, retry, startPolling, stopPolling };
};

import React, { createContext, useContext, useEffect, useState, useCallback, useRef } from 'react';
import { WebSocketMessage } from '@/types';
import toast from 'react-hot-toast';

interface WebSocketContextType {
  isConnected: boolean;
  lastMessage: WebSocketMessage | null;
  lastUpdate: Date;
  subscribe: (event: string, callback: (data: any) => void) => () => void;
}

const WebSocketContext = createContext<WebSocketContextType | null>(null);

export const useWebSocket = () => {
  const context = useContext(WebSocketContext);
  if (!context) {
    throw new Error('useWebSocket must be used within WebSocketProvider');
  }
  return context;
};

interface WebSocketProviderProps {
  children: React.ReactNode;
}

export const WebSocketProvider: React.FC<WebSocketProviderProps> = ({ children }) => {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const [lastUpdate, setLastUpdate] = useState(new Date());
  const [_ws, setWs] = useState<WebSocket | null>(null);
  const subscribersRef = useRef<Map<string, Set<(data: any) => void>>>(new Map());
  const retryCountRef = useRef(0);
  const mountedRef = useRef(true);

  const subscribe = useCallback((event: string, callback: (data: any) => void) => {
    if (!subscribersRef.current.has(event)) {
      subscribersRef.current.set(event, new Set());
    }
    subscribersRef.current.get(event)!.add(callback);

    // Return unsubscribe function
    return () => {
      subscribersRef.current.get(event)?.delete(callback);
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;

    const connectWebSocket = () => {
      if (!mountedRef.current) return null;

      const websocket = new WebSocket('ws://localhost:8300/ws');

      websocket.onopen = () => {
        if (!mountedRef.current) return;
        retryCountRef.current = 0;
        setIsConnected(true);
        toast.success('Connected to server', { duration: 2000 });
      };

      websocket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as WebSocketMessage;
          setLastMessage(data);
          setLastUpdate(new Date());

          // Notify subscribers
          const callbacks = subscribersRef.current.get(data.type);
          if (callbacks) {
            callbacks.forEach(callback => callback(data));
          }

          // Show notifications for important events
          switch (data.type) {
            case 'task_created':
              toast('New task created', { icon: '📋' });
              break;
            case 'task_completed':
              toast.success('Task completed!', { icon: '✅' });
              break;
            case 'agent_created':
              toast('New agent spawned', { icon: '🤖' });
              break;
            case 'guardian_analysis':
              // Silent update - no toast for frequent guardian analyses
              break;
            case 'conductor_analysis':
              // Silent update - no toast for frequent conductor analyses
              break;
            case 'steering_intervention':
              toast('Agent steered back on track', { icon: '🎯' });
              break;
            case 'duplicate_detected':
              toast.error('Duplicate work detected', { icon: '⚠️' });
              break;
            case 'results_reported':
              toast('New result submitted', { icon: '📝' });
              break;
            case 'result_validation_completed':
              toast.success('Result validation updated', { icon: '🔍' });
              break;
            case 'ticket_created':
              toast('New ticket created', { icon: '🎫' });
              break;
            case 'ticket_updated':
              // Silent update - too frequent
              break;
            case 'status_changed':
              toast('Ticket status changed', { icon: '🔄' });
              break;
            case 'comment_added':
              toast('New comment added', { icon: '💬' });
              break;
            case 'commit_linked':
              toast('Commit linked to ticket', { icon: '🔗' });
              break;
            case 'ticket_resolved':
              toast.success('Ticket resolved!', { icon: '✅' });
              break;
            case 'ticket_approved':
              toast.success('Ticket approved!', { icon: '✅' });
              break;
            case 'ticket_rejected':
              toast.error('Ticket rejected', { icon: '❌' });
              break;
            case 'ticket_deleted':
              toast('Ticket deleted', { icon: '🗑️' });
              break;
          }
        } catch (error) {
          console.error('Failed to parse WebSocket message:', error);
        }
      };

      websocket.onerror = () => {
        // Suppress error toasts during initial retry backoff —
        // the backend is often not ready on first page load.
        // Only show error after several failed attempts.
        if (!mountedRef.current) return;
        retryCountRef.current += 1;
        if (retryCountRef.current > 3) {
          toast.error('Connection error');
        }
      };

      websocket.onclose = () => {
        if (!mountedRef.current) return;
        setIsConnected(false);

        if (retryCountRef.current <= 3) {
          // Silent retry with increasing backoff (1s, 2s, 4s)
          const delay = Math.min(1000 * Math.pow(2, retryCountRef.current), 8000);
          retryCountRef.current += 1;
          setTimeout(connectWebSocket, delay);
        } else {
          toast.error('Disconnected from server', { duration: 2000 });
          // Continue retrying with longer interval
          setTimeout(connectWebSocket, 5000);
        }
      };

      setWs(websocket);

      return websocket;
    };

    const websocket = connectWebSocket();

    return () => {
      mountedRef.current = false;
      websocket?.close();
    };
  }, []);

  return (
    <WebSocketContext.Provider value={{ isConnected, lastMessage, lastUpdate, subscribe }}>
      {children}
    </WebSocketContext.Provider>
  );
};

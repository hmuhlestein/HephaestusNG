import { useWebSocket } from '@/context/WebSocketContext';

export function useSocket() {
  const { subscribe, isConnected, lastMessage } = useWebSocket();
  return { subscribe, isConnected, lastMessage };
}
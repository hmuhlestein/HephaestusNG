/** Types for the Tmux Viewer frontend. */

export interface TmuxSession {
  name: string;
  session_id: string;
  windows: number;
  attached: boolean;
}

export interface SessionOutput {
  session_name: string;
  output: string;
  line_count: number;
}

export interface AgentOutputData {
  output: string;
  timestamp: string;
  isLoading: boolean;
  error: string | null;
  isConnected: boolean;
  lastUpdateTime: Date | null;
}

export interface Agent {
  id: string;
  status: 'idle' | 'working' | 'stuck' | 'terminated';
  cli_type: string;
  current_task_id: string | null;
  tmux_session_name: string | null;
  health_check_failures: number;
  created_at: string;
  last_activity: string | null;
  current_task?: {
    id: string;
    description: string;
    status: string;
    priority: string;
  } | null;
}

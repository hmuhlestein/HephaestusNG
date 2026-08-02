import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { Bot, FileText, Database, AlertCircle, TrendingUp, Clock, Ban, AlertTriangle } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { apiService } from '@/services/api';
import { DashboardStats } from '@/types';
import { useWebSocket } from '@/context/WebSocketContext';
import { useWorkflow } from '@/context/WorkflowContext';
import { formatDistanceToNow } from 'date-fns';
import QueueStatusWidget from '@/components/QueueStatusWidget';
import BlockedTasksView from '@/components/BlockedTasksView';
import { useProject } from '@/context/ProjectContext';
import { ProjectCostSummary } from '@/components/cost';
import ProjectSettingsModal from '@/components/ProjectSettingsModal';

const StatCard: React.FC<{
  title: string;
  value: number;
  icon: React.ElementType;
  color: string;
  trend?: number;
  href?: string;
}> = ({ title, value, icon: Icon, color, trend, href }) => {
  const navigate = useNavigate();
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      onClick={href ? () => navigate(href) : undefined}
      className={`bg-white rounded-lg shadow-md p-6${href ? ' cursor-pointer hover:shadow-lg transition-shadow' : ''}`}
    >
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-600">{title}</p>
          <motion.p
            key={value}
            initial={{ scale: 0.8 }}
            animate={{ scale: 1 }}
            className="text-3xl font-bold text-gray-800 mt-2"
          >
            {value}
          </motion.p>
          {trend !== undefined && (
            <div className="flex items-center mt-2">
              <TrendingUp className={`w-4 h-4 ${trend > 0 ? 'text-green-500' : 'text-red-500'}`} />
              <span className={`text-sm ml-1 ${trend > 0 ? 'text-green-500' : 'text-red-500'}`}>
                {trend > 0 ? '+' : ''}{trend}
              </span>
            </div>
          )}
        </div>
        <div className={`p-3 rounded-full ${color}`}>
          <Icon className="w-6 h-6 text-white" />
        </div>
      </div>
    </motion.div>
  );
};

const ActivityItem: React.FC<{ activity: any; isNew?: boolean }> = ({ activity, isNew }) => {
  return (
    <motion.div
      initial={isNew ? { opacity: 0, x: -20 } : false}
      animate={{ opacity: 1, x: 0 }}
      className={`flex items-center p-3 ${isNew ? 'bg-blue-50' : ''} hover:bg-gray-50 transition-colors`}
    >
      <div className="flex-1">
        <p className="text-sm text-gray-800">{activity.message}</p>
        <p className="text-xs text-gray-500 mt-1">
          {formatDistanceToNow(new Date(activity.timestamp), { addSuffix: true })}
        </p>
      </div>
    </motion.div>
  );
};

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [recentActivities, setRecentActivities] = useState<any[]>([]);
  const [showProjectSettings, setShowProjectSettings] = useState(false);
  const { subscribe } = useWebSocket();
  const { selectedExecutionId, selectedExecution } = useWorkflow();
  const navigate = useNavigate();
  const { selectedProject } = useProject();
  const projectId = selectedProject?.id || null;

  const { data, isLoading, error } = useQuery({
    queryKey: ['dashboard-stats', projectId],
    queryFn: () => apiService.getDashboardStats(undefined, projectId || undefined),
    refetchInterval: 5000, // Refresh every 5 seconds
    enabled: !!projectId,
  });

  const { data: autopilotInput } = useQuery({
    queryKey: ['autopilot-input', projectId],
    queryFn: () => apiService.getAutopilotInput(),
    refetchInterval: 5000,
    enabled: !!projectId,
  });

  const { data: blockedTasks } = useQuery({
    queryKey: ['blocked-tasks', projectId],
    queryFn: () => apiService.getBlockedTasks(undefined, projectId || undefined),
    refetchInterval: 5000, // Refresh every 5 seconds
    enabled: !!projectId,
  });

  const { data: projectCosts } = useQuery({
    queryKey: ['project-costs', projectId],
    queryFn: () => apiService.getProjectCosts(projectId!),
    refetchInterval: 30000, // Refresh every 30 seconds
    enabled: !!projectId,
  });

  useEffect(() => {
    if (data) {
      setStats(data);
      setRecentActivities(data.recent_activity);
    }
  }, [data]);

  // Subscribe to WebSocket updates
  useEffect(() => {
    const unsubscribe = subscribe('stats_update', (message) => {
      if (stats) {
        setStats({
          ...stats,
          active_agents: message.active_agents ?? stats.active_agents,
          running_tasks: message.running_tasks ?? stats.running_tasks,
          total_memories: message.total_memories ?? stats.total_memories,
        });
      }
    });

    return unsubscribe;
  }, [subscribe, stats]);

  useEffect(() => {
    const unsubscribeTask = subscribe('task_created', (message) => {
      const newActivity = {
        id: Date.now(),
        type: 'task_created',
        message: `New task created: ${message.description?.substring(0, 50)}...`,
        timestamp: new Date().toISOString(),
        agent_id: message.agent_id,
      };
      setRecentActivities(prev => [newActivity, ...prev.slice(0, 9)]);
    });

    const unsubscribeAgent = subscribe('agent_created', (message) => {
      const newActivity = {
        id: Date.now(),
        type: 'agent_created',
        message: `Agent ${message.agent_id?.substring(0, 8)} spawned`,
        timestamp: new Date().toISOString(),
        agent_id: message.agent_id,
      };
      setRecentActivities(prev => [newActivity, ...prev.slice(0, 9)]);
    });

    return () => {
      unsubscribeTask();
      unsubscribeAgent();
    };
  }, [subscribe]);

  if (!selectedExecutionId) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-800">Dashboard</h1>
            <p className="text-gray-600 mt-1">Real-time system overview</p>
          </div>
        </div>
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-12 text-center">
          <p className="text-gray-500 text-lg">Select a workflow to view dashboard statistics</p>
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-800">Dashboard</h1>
            <p className="text-gray-600 mt-1">Real-time system overview</p>
          </div>
        </div>
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-800">Dashboard</h1>
            <p className="text-gray-600 mt-1">Real-time system overview</p>
          </div>
        </div>
        <div className="bg-red-50 border border-red-200 rounded-lg p-6">
          <p className="text-red-600">Failed to load dashboard stats</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-800">Dashboard</h1>
          <p className="text-gray-600 mt-1">
            {selectedExecution
              ? `Workflow: ${selectedExecution.definition_name}`
              : 'Real-time system overview'}
          </p>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-6">
        <StatCard
          title="Active Agents"
          value={stats?.active_agents || 0}
          icon={Bot}
          color="bg-blue-500"
          href="/agents"
        />
        <StatCard
          title="Running Tasks"
          value={stats?.running_tasks || 0}
          icon={FileText}
          color="bg-green-500"
          href="/tasks"
        />
        <StatCard
          title="Queued Tasks"
          value={stats?.queued_tasks || 0}
          icon={Clock}
          color="bg-orange-500"
          href="/tasks"
        />
        <StatCard
          title="Blocked Tasks"
          value={blockedTasks?.length || 0}
          icon={Ban}
          color="bg-red-500"
          href="/tasks"
        />
        <StatCard
          title="Stuck Agents"
          value={stats?.stuck_agents || 0}
          icon={AlertCircle}
          color="bg-yellow-500"
          href="/agents"
        />
        <StatCard
          title="Total Memories"
          value={stats?.total_memories || 0}
          icon={Database}
          color="bg-purple-500"
          href="/memories"
        />
      </div>

      {/* Queue Status */}
      <QueueStatusWidget />

      {/* Project Cost Summary */}
      {projectCosts && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
        >
          <ProjectCostSummary
            projectId={projectCosts.project_id}
            projectName={projectCosts.project_name}
            costTotal={projectCosts.cost_total_usd}
            costLimit={projectCosts.cost_limit_usd}
            isOverBudget={projectCosts.is_over_budget}
            onConfigureBudget={() => setShowProjectSettings(true)}
          />
        </motion.div>
      )}
      <ProjectSettingsModal
        isOpen={showProjectSettings}
        onClose={() => setShowProjectSettings(false)}
      />

      {/* Blocked Tasks */}
      {blockedTasks && blockedTasks.length > 0 && (
        <div>
          <BlockedTasksView />
        </div>
      )}

      {/* Autopilot Human Input Alert */}
      {autopilotInput && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          onClick={() => navigate('/autopilot')}
          className="bg-gradient-to-r from-amber-50 to-orange-50 border-2 border-amber-300 rounded-xl p-4 cursor-pointer hover:shadow-md transition-shadow"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 bg-amber-100 rounded-lg animate-pulse">
              <AlertTriangle className="w-5 h-5 text-amber-600" />
            </div>
            <div className="flex-1 min-w-0">
              <h4 className="text-sm font-bold text-amber-900">Autopilot needs your input</h4>
              <p className="text-xs text-amber-700 truncate">{autopilotInput.reason}</p>
            </div>
            <span className="text-xs font-medium text-amber-700 bg-amber-100 px-3 py-1.5 rounded-lg">
              Take Action →
            </span>
          </div>
        </motion.div>
      )}

      {/* Recent Activity */}
      <div className="bg-white rounded-lg shadow-md">
        <div className="px-6 py-4 border-b flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-800">Recent Activity</h2>
          <div className="flex items-center text-sm text-gray-500">
            <Clock className="w-4 h-4 mr-1" />
            Live Updates
          </div>
        </div>
        <div className="divide-y">
          {recentActivities.length > 0 ? (
            recentActivities.map((activity, index) => (
              <ActivityItem
                key={activity.id}
                activity={activity}
                isNew={index === 0}
              />
            ))
          ) : (
            <div className="p-6 text-center text-gray-500">
              No recent activity
            </div>
          )}
        </div>
      </div>

    </div>
  );
};

export default Dashboard;
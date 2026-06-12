import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  Rocket, CheckCircle2, XCircle, AlertTriangle, FileText,
  Layers, Play, Pause, Zap, ArrowRight, MessageSquare
} from 'lucide-react';
import { apiService } from '@/services/api';
import { formatDistanceToNow } from 'date-fns';

const eventTypeConfig: Record<string, { icon: React.ElementType; color: string; bg: string }> = {
  design_queued: { icon: FileText, color: 'text-blue-600', bg: 'bg-blue-100' },
  design_started: { icon: Play, color: 'text-violet-600', bg: 'bg-violet-100' },
  design_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  design_failed: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-100' },
  phase_started: { icon: ArrowRight, color: 'text-indigo-600', bg: 'bg-indigo-100' },
  phase_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  workflow_started: { icon: Rocket, color: 'text-violet-600', bg: 'bg-violet-100' },
  workflow_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  iteration_started: { icon: Layers, color: 'text-blue-600', bg: 'bg-blue-100' },
  iteration_completed: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100' },
  pipeline_started: { icon: Zap, color: 'text-amber-600', bg: 'bg-amber-100' },
  pipeline_stopped: { icon: Pause, color: 'text-gray-600', bg: 'bg-gray-100' },
  warning: { icon: AlertTriangle, color: 'text-amber-600', bg: 'bg-amber-100' },
  error: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-100' },
  stuck_agent: { icon: AlertTriangle, color: 'text-orange-600', bg: 'bg-orange-100' },
  credit_exhausted: { icon: AlertTriangle, color: 'text-red-600', bg: 'bg-red-100' },
};

const MessageCenter: React.FC = () => {
  const { data: messages, isLoading } = useQuery({
    queryKey: ['autopilot-messages'],
    queryFn: () => apiService.getAutopilotMessages(100),
    refetchInterval: 15000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
      </div>
    );
  }

  if (!messages || messages.length === 0) {
    return (
      <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
        <MessageSquare className="w-12 h-12 text-gray-300 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 mb-2">No messages yet</h3>
        <p className="text-sm text-gray-400">Pipeline events will appear here as they happen</p>
      </div>
    );
  }

  // Group messages by date
  const grouped: Record<string, any[]> = {};
  for (const msg of messages) {
    const date = new Date(msg.timestamp).toLocaleDateString();
    if (!grouped[date]) grouped[date] = [];
    grouped[date].push(msg);
  }

  return (
    <div className="space-y-6">
      {Object.entries(grouped).map(([date, msgs]) => (
        <div key={date}>
          <div className="flex items-center gap-3 mb-4">
            <div className="h-px flex-1 bg-gray-200" />
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">{date}</span>
            <div className="h-px flex-1 bg-gray-200" />
          </div>

          <div className="space-y-2">
            {msgs.map((msg, index) => {
              const config = eventTypeConfig[msg.type] || {
                icon: MessageSquare,
                color: 'text-gray-600',
                bg: 'bg-gray-100',
              };
              const Icon = config.icon;

              return (
                <motion.div
                  key={`${msg.timestamp}-${index}`}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: index * 0.02 }}
                  className="flex items-start gap-3 px-4 py-3 bg-white rounded-xl border border-gray-100 hover:shadow-sm transition-shadow"
                >
                  <div className={`p-2 rounded-lg ${config.bg} flex-shrink-0`}>
                    <Icon className={`w-4 h-4 ${config.color}`} />
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-800">
                        {formatEventType(msg.type)}
                      </span>
                      <span className="text-xs text-gray-400 font-mono">{msg.type}</span>
                    </div>
                    {msg.data && Object.keys(msg.data).length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-2">
                        {Object.entries(msg.data).map(([key, value]) => (
                          <span
                            key={key}
                            className="inline-flex items-center gap-1 text-xs text-gray-500 bg-gray-50 px-2 py-0.5 rounded"
                          >
                            <span className="text-gray-400">{key}:</span>
                            <span className="font-medium text-gray-600 truncate max-w-[200px]">
                              {String(value)}
                            </span>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  <span className="text-xs text-gray-400 flex-shrink-0">
                    {formatDistanceToNow(new Date(msg.timestamp), { addSuffix: true })}
                  </span>
                </motion.div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
};

const formatEventType = (type: string): string => {
  return type
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
};

export default MessageCenter;

import React from 'react';
import { motion } from 'framer-motion';
import { CheckCircle2, XCircle, Clock, Play, Pause, Activity, Users } from 'lucide-react';
import { formatTime } from '@/pages/Autopilot';

interface PipelineStatusCardProps {
  status: any;
  onToggle?: () => void;
  loading?: boolean;
}

const PipelineStatusCard: React.FC<PipelineStatusCardProps> = ({ status, onToggle, loading }) => {
  const running = status?.running ?? false;
  const currentDesign = status?.current_design;
  const designsProcessed = status?.designs_processed ?? 0;
  const queueDepth = status?.queue_depth ?? 0;
  const totalDesigns = designsProcessed + queueDepth;
  // Add minimum progress of 2% when running so bar shows activity
  const baseProgress = totalDesigns > 0 ? (designsProcessed / totalDesigns) * 100 : 0;
  const progressPercent = running && baseProgress < 2 ? 2 : baseProgress;

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`relative overflow-hidden rounded-2xl shadow-lg ${
        running
          ? 'bg-gradient-to-r from-violet-600 via-purple-600 to-indigo-600'
          : 'bg-gradient-to-r from-gray-600 via-gray-700 to-gray-800'
      }`}
    >
      {/* Animated background pattern */}
      {running && (
        <div className="absolute inset-0 opacity-10">
          <div className="absolute inset-0" style={{
            backgroundImage: `repeating-linear-gradient(
              45deg,
              transparent,
              transparent 10px,
              rgba(255,255,255,0.05) 10px,
              rgba(255,255,255,0.05) 20px
            )`,
            animation: 'slide 2s linear infinite',
          }} />
        </div>
      )}

      <div className="relative px-8 py-6">
        <div className="flex items-center justify-between">
          {/* Left: Status */}
          <div className="flex items-center gap-4">
            <button
              onClick={onToggle}
              disabled={loading}
              className={`p-3 rounded-2xl transition-all ${
                onToggle
                  ? 'cursor-pointer hover:scale-110 active:scale-95'
                  : 'cursor-default'
              } ${running ? 'bg-white/20' : 'bg-white/10'}`}
              title={running ? 'Pause pipeline' : 'Start pipeline'}
            >
              {running ? (
                <Pause className="w-8 h-8 text-white" />
              ) : (
                <Play className="w-8 h-8 text-white/70" />
              )}
            </button>
            <div>
              <h2 className="text-2xl font-bold text-white">
                {running ? 'Pipeline Running' : 'Pipeline Idle'}
              </h2>
              {currentDesign ? (
                <div className="text-white/80 text-sm mt-1">
                  <span className="font-semibold">{currentDesign}</span>
                  {status?.current_workflow_id && (
                    <span className="text-white/50 ml-2">• {status.current_workflow_id.substring(0, 8)}</span>
                  )}
                </div>
              ) : (
                <p className="text-white/60 text-sm mt-1">
                  {running ? 'Waiting for designs...' : 'Add a design to the queue to start'}
                </p>
              )}
            </div>
          </div>

          {/* Right: Metrics */}
          <div className="flex items-center gap-6">
            {[
              { label: 'Agents', value: status?.active_agents || 0, icon: Users },
              { label: 'Processed', value: status?.designs_processed || 0, icon: Activity },
              { label: 'Succeeded', value: status?.designs_succeeded || 0, icon: CheckCircle2 },
              { label: 'Failed', value: status?.designs_failed || 0, icon: XCircle },
            ].map((metric) => (
              <div key={metric.label} className="text-center">
                <div className="flex items-center justify-center gap-1 mb-1">
                  <metric.icon className="w-3.5 h-3.5 text-white/60" />
                  <span className="text-xs text-white/60 uppercase tracking-wider">{metric.label}</span>
                </div>
                <p className="text-2xl font-bold text-white">{metric.value}</p>
              </div>
            ))}

            {status?.total_elapsed > 0 && (
              <div className="text-center pl-4 border-l border-white/20">
                <div className="flex items-center justify-center gap-1 mb-1">
                  <Clock className="w-3.5 h-3.5 text-white/60" />
                  <span className="text-xs text-white/60 uppercase tracking-wider">Runtime</span>
                </div>
                <p className="text-2xl font-bold text-white">{formatTime(status.total_elapsed)}</p>
              </div>
            )}
          </div>
        </div>

        {/* Progress bar (if running) */}
        {running && (
          <div className="mt-4">
            <div className="flex items-center justify-between text-xs text-white/60 mb-1.5">
              <span>{Math.min(designsProcessed + 1, totalDesigns)} of {totalDesigns} designs processing</span>
              <span>{progressPercent.toFixed(1)}%</span>
            </div>
            <motion.div 
              className="h-2 bg-white/20 rounded-full overflow-hidden relative"
              animate={{ opacity: [0.8, 1, 0.8] }}
              transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
            >
              <motion.div
                className="h-full bg-white/80 rounded-full"
                initial={{ width: '0%' }}
                animate={{ width: `${progressPercent}%` }}
                transition={{ duration: 0.5, ease: 'easeOut' }}
              />
              {/* Pulse effect at the end of progress bar */}
              {progressPercent > 0 && progressPercent < 100 && (
                <motion.div
                  className="absolute top-0 h-2 w-4 bg-white rounded-full"
                  style={{ left: `calc(${progressPercent}% - 8px)` }}
                  animate={{ opacity: [0.4, 1, 0.4] }}
                  transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
                />
              )}
            </motion.div>
          </div>
        )}
      </div>

      <style>{`
        @keyframes slide {
          from { transform: translateX(0); }
          to { transform: translateX(28px); }
        }
      `}</style>
    </motion.div>
  );
};

export default PipelineStatusCard;

import React from 'react';
import { motion } from 'framer-motion';
import { CheckCircle2, XCircle, Clock, Play, Pause, Activity, Users, AlertTriangle, DollarSign } from 'lucide-react';
import { formatTime } from '@/pages/Autopilot';
import { CostDisplay } from '@/components/cost';

interface PipelineStatusCardProps {
  status: any;
  pendingAgents?: number;
  projectName?: string;
  onToggle?: () => void;
  onMetricClick?: (metric: string) => void;
  loading?: boolean;
  costTotal?: number;
  costLimit?: number | null;
  onBudgetClick?: () => void;
}

const PipelineStatusCard: React.FC<PipelineStatusCardProps> = ({ status, pendingAgents, projectName, onToggle, onMetricClick, loading, costTotal, costLimit, onBudgetClick }) => {
  // Use a stable running state that doesn't flicker during mutations.
  // When loading (mutation in progress), keep the previous visual state
  // until the API confirms the change.
  const [stableRunning, setStableRunning] = React.useState(status?.running ?? false);
  const prevLoadingRef = React.useRef(loading);
  
  React.useEffect(() => {
    // When loading finishes (mutation settled), update stable state
    if (prevLoadingRef.current && !loading) {
      setStableRunning(status?.running ?? false);
    }
    // When not loading, sync with status
    if (!loading) {
      setStableRunning(status?.running ?? false);
    }
    prevLoadingRef.current = loading;
  }, [loading, status?.running]);
  
  const running = loading ? stableRunning : (status?.running ?? false);
  const currentDesign = status?.current_design;
  const queueDepth = status?.queue_depth ?? 0;
  // queueDepth is a live count of designs still needing work right now;
  // designs_processed is a lifetime counter that persists across
  // restarts and unrelated past runs (loaded from PersistentPipelineState).
  // Summing the two used to conflate history with the live queue -- a
  // project that already finished one design in the past, with exactly
  // one still queued, showed "2 of 2" instead of the true "1 remaining".
  // There's no per-run "processed in this batch" counter to build a real
  // done/total fraction from, so this only tracks what's left to do.
  const designsRemaining = queueDepth;

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
                {projectName || 'Pipeline'} {running ? 'Running' : 'Idle'}
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
              {/* Error/reason when pipeline stopped */}
              {!running && status?.last_error && (
                <div className="flex items-center gap-1.5 mt-1.5">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-300" />
                  <p className="text-amber-200 text-xs">{status.last_error}</p>
                </div>
              )}
            </div>
          </div>

          {/* Right: Metrics */}
          <div className="flex items-start gap-6">
            {[
              { label: 'Agents', value: status?.active_agents || 0, icon: Users, key: 'agents' },
              { label: 'Pending', value: pendingAgents || 0, icon: Clock, key: 'pending_agents' },
              { label: 'Processed', value: status?.designs_processed || 0, icon: Activity, key: 'processed' },
              { label: 'Succeeded', value: status?.designs_succeeded || 0, icon: CheckCircle2, key: 'succeeded' },
              { label: 'Failed', value: status?.designs_failed || 0, icon: XCircle, key: 'failed' },
            ].map((metric) => (
              <button
                key={metric.label}
                onClick={() => onMetricClick?.(metric.key)}
                className={`text-center px-3 py-2 rounded-lg transition-all border border-transparent ${
                  onMetricClick
                    ? 'cursor-pointer hover:bg-white/15 hover:border-white/20 hover:underline active:scale-95'
                    : 'cursor-default'
                }`}
                title={`View ${metric.label.toLowerCase()}`}
              >
                <div className="flex items-center justify-center gap-1 mb-1">
                  <metric.icon className="w-3.5 h-3.5 text-white/60" />
                  <span className="text-xs text-white/60 uppercase tracking-wider">{metric.label}</span>
                </div>
                <p className="text-2xl font-bold text-white">{metric.value}</p>
              </button>
            ))}

            {costTotal !== undefined && (
              <button
                onClick={onBudgetClick}
                className={`text-center px-3 py-2 rounded-lg transition-all border border-transparent ${
                  onBudgetClick
                    ? 'cursor-pointer hover:bg-white/15 hover:border-white/20 active:scale-95'
                    : 'cursor-default'
                }`}
                title={costLimit != null ? 'View or change the spend limit' : 'No spend limit set — click to set one'}
              >
                <div className="flex items-center justify-center gap-1 mb-1">
                  <DollarSign className="w-3 h-3 text-white/60" />
                  <span className="text-xs text-white/60 uppercase tracking-wider">Cost</span>
                </div>
                <CostDisplay currentCost={costTotal} costLimit={costLimit} showProgress={false} variant="large" />
                {/* Without this the tile shows spend only, with nothing to
                    indicate a limit CAN be set -- the feature was reachable
                    but invisible unless you already knew to click. A plain
                    link reads as an action; the earlier "no limit · set one"
                    hint read as a status caption, not something to click. */}
                {costLimit == null && onBudgetClick && (
                  <span className="mt-0.5 block text-xs font-medium text-violet-300 hover:text-violet-200 underline underline-offset-2">
                    Set Budget
                  </span>
                )}
              </button>
            )}

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

        {/* Queue activity indicator (if running with work left) -- no
            real done/total fraction is available (see designsRemaining
            above), so this shows an indeterminate "in progress" bar
            rather than a fabricated percentage. */}
        {running && designsRemaining > 0 && (
          <div className="mt-4">
            <div className="flex items-center justify-between text-xs text-white/60 mb-1.5">
              <span>{designsRemaining} design{designsRemaining === 1 ? '' : 's'} remaining in queue</span>
            </div>
            <motion.div
              className="h-2 bg-white/20 rounded-full overflow-hidden relative"
              animate={{ opacity: [0.8, 1, 0.8] }}
              transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
            >
              <div className="h-full w-full bg-white/80 rounded-full" />
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

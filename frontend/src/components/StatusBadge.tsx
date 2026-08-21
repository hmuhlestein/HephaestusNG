import React from 'react';
import clsx from 'clsx';

interface StatusBadgeProps {
  status: string;
  size?: 'sm' | 'md' | 'lg';
  // Optional icon rendered before the label -- lets config-driven callers
  // (e.g. DesignQueuePanel's design/feature rows) keep their icon without
  // a separate badge component.
  icon?: React.ReactNode;
  // Override the computed label -- e.g. DesignQueuePanel's
  // "Paused: budget limit reached" variant on the plain "Paused" status.
  label?: string;
  // Override the computed color/background classes -- lets callers with
  // their own status vocabulary and palette (DesignQueuePanel's
  // DESIGN_FEATURE_STATUS_CONFIG) reuse this component's layout/sizing
  // without adopting this component's color choices for every status.
  colorClassName?: string;
}

// Normalized display labels for all status values
const STATUS_LABELS: Record<string, string> = {
  // Task statuses
  pending: 'Pending',
  queued: 'Queued',
  assigned: 'Assigned',
  in_progress: 'In Progress',
  under_review: 'Under Review',
  validation_in_progress: 'Validating',
  needs_work: 'Needs Work',
  done: 'Done',
  failed: 'Failed',
  blocked: 'Blocked',
  duplicated: 'Duplicate',

  // Workflow/design statuses
  active: 'Active',
  completed: 'Completed',
  paused: 'Paused',
  cancelled: 'Cancelled',
  skipped: 'Skipped',

  // Agent statuses
  working: 'Working',
  idle: 'Idle',
  terminated: 'Not Running',
  starting: 'Starting',
  stuck: 'Stuck',

  // Feature-review statuses
  needs_review: 'Needs Review',

  // Other
  healthy: 'Healthy',
  validated: 'Validated',
  verified: 'Verified',
  unverified: 'Unverified',
  error: 'Error',
  rejected: 'Rejected',
  disputed: 'Disputed',
  pending_validation: 'Pending Validation',
  warning: 'Warning',
  attention: 'Attention',
};

const StatusBadge: React.FC<StatusBadgeProps> = ({ status, size = 'md', icon, label: labelOverride, colorClassName }) => {
  const normalized = status.toLowerCase();
  const label = labelOverride ?? (STATUS_LABELS[normalized] || status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()));

  const getStatusColor = () => {

    if (
      [
        'completed',
        'healthy',
        'validated',
        'verified',
      ].includes(normalized)
    ) {
      return 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-400';
    }

    if (
      [
        'done',
        'in_progress',
        'working',
        'assigned',
        'running',
      ].includes(normalized)
    ) {
      return 'bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-400';
    }

    if (
      [
        'pending',
        'idle',
        'pending_validation',
        'unverified',
      ].includes(normalized)
    ) {
      return 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-400';
    }

    if (
      [
        'failed',
        'error',
        'rejected',
        'disputed',
      ].includes(normalized)
    ) {
      return 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-400';
    }

    if (normalized === 'terminated') {
      return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-400';
    }

    if (
      ['stuck', 'warning', 'attention', 'needs_review'].includes(normalized)
    ) {
      return 'bg-orange-100 dark:bg-orange-900/30 text-orange-800 dark:text-orange-400';
    }

    if (normalized === 'blocked') {
      return 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-400 border border-red-300 dark:border-red-800';
    }

    if (normalized === 'duplicated') {
      return 'bg-purple-100 dark:bg-purple-900/30 text-purple-800 dark:text-purple-400';
    }

    if (normalized === 'queued') {
      return 'bg-cyan-100 dark:bg-cyan-900/30 text-cyan-800 dark:text-cyan-400';
    }

    return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-400';
  };

  const sizeClasses = {
    sm: 'px-2 py-0.5 text-xs',
    md: 'px-2.5 py-1 text-sm',
    lg: 'px-3 py-1.5 text-base',
  };

  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full font-medium',
        icon && 'gap-1',
        colorClassName ?? getStatusColor(),
        sizeClasses[size]
      )}
    >
      {icon}
      {label}
    </span>
  );
};

export default StatusBadge;

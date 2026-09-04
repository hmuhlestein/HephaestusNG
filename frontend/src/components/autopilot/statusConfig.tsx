import React from 'react';
import { Clock, Loader2, Pause, CheckCircle2, XCircle, PauseCircle } from 'lucide-react';

// SOLID review 5.2: SpecQueuePanel.tsx used to define STATUS_CONFIG
// (design-level) and FEATURE_STATUS_CONFIG separately, even though they
// were identical {color, icon, label} maps for every key they shared --
// FEATURE_STATUS_CONFIG just added 'skipped'. A new status required
// updating both, and missing one silently rendered no badge. Merged into
// one DESIGN_FEATURE_STATUS_CONFIG below, used by both StatusBadge and
// FeatureStatusBadge.
//
// TASK_STATUS_CONFIG is kept separate rather than force-merged into the
// same map: it's a genuinely different shape (no `label` -- task rows show
// just an icon) covering a different vocabulary (task lifecycle states
// like `queued`/`under_review`/`duplicated`, not design/feature lifecycle
// states). Co-located here anyway so all the status vocabularies this
// panel renders live in one findable place.

export interface StatusConfigEntry {
  color: string;
  icon: React.ReactNode;
  label: string;
}

export const DESIGN_FEATURE_STATUS_CONFIG: Record<string, StatusConfigEntry> = {
  pending: { color: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400', icon: <Clock className="w-3 h-3" />, label: 'Pending' },
  active: { color: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400', icon: <Loader2 className="w-3 h-3 animate-spin" />, label: 'Active' },
  paused: { color: 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400', icon: <Pause className="w-3 h-3" />, label: 'Paused' },
  completed: { color: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400', icon: <CheckCircle2 className="w-3 h-3" />, label: 'Done' },
  failed: { color: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400', icon: <XCircle className="w-3 h-3" />, label: 'Failed' },
  skipped: { color: 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400', icon: <Clock className="w-3 h-3" />, label: 'Skipped' },
};

export interface TaskStatusConfigEntry {
  color: string;
  icon: React.ReactNode;
}

export const TASK_STATUS_CONFIG: Record<string, TaskStatusConfigEntry> = {
  pending: { color: 'text-gray-400', icon: <Clock className="w-4 h-4" /> },
  queued: { color: 'text-blue-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  assigned: { color: 'text-blue-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  in_progress: { color: 'text-violet-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  under_review: { color: 'text-violet-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  validation_in_progress: { color: 'text-violet-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  needs_work: { color: 'text-violet-500', icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  done: { color: 'text-blue-500', icon: <CheckCircle2 className="w-4 h-4" /> },
  failed: { color: 'text-red-500', icon: <XCircle className="w-4 h-4" /> },
  blocked: { color: 'text-amber-500', icon: <PauseCircle className="w-4 h-4" /> },
  // A duplicate never ran -- it was superseded by a sibling task that
  // already owns the phase (see task_similarity_service.py / the
  // orchestrator's "Superseded by task X" bailout) -- but it isn't
  // pending or in-flight either, so the fallback Clock below misleadingly
  // suggested it was still waiting to run. Checkmark (purple, matching
  // StatusBadge's own "duplicated" color) reads as resolved without
  // claiming it did the same real work "done" represents.
  duplicated: { color: 'text-purple-500', icon: <CheckCircle2 className="w-4 h-4" /> },
};

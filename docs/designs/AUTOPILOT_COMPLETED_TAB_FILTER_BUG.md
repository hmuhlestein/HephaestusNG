# Autopilot Dashboard: "Completed" Tab Shows Pending Features

## Problem

The Autopilot page's "Completed" tab (`frontend/src/pages/Autopilot.tsx:290`,
rendered via `FeatureGallery` at `frontend/src/pages/Autopilot.tsx:416-422`)
lists every feature regardless of its actual status — including features
that are still `pending` (never started) or `active` (currently running).

Root cause, confirmed in code:

- `FeatureGallery` (`frontend/src/components/autopilot/FeatureGallery.tsx`)
  fetches features via `apiService.getAutopilotFeatures()` (line 31) with no
  server-side status filter — the API returns every feature for the design,
  regardless of status.
- The component's own client-side filter (line 35-36):
  ```ts
  const filtered = (features || []).filter((f: any) => {
    if (statusFilter !== 'all' && f.status !== statusFilter) return false;
    ...
  ```
  only excludes anything when `statusFilter` is NOT `'all'`. The tab's
  default filter state, set in the parent (`Autopilot.tsx:37`), is:
  ```ts
  const [featureStatusFilter, setFeatureStatusFilter] = useState<'all' | 'validated' | 'needs_review' | 'failed'>('all');
  ```
  so on first load — and any time the user hasn't manually picked a
  narrower filter — every feature shows, `pending`/`active` included.

The tab is labeled "Completed" (`Autopilot.tsx:290`), so a user reasonably
expects it to show only features that have actually finished, not ones
still queued or in progress.

## Fix

Exclude `pending` and `active` features from this tab's list by default.
Two reasonable approaches (pick one during implementation, whichever fits
the existing filter-option pattern better):

1. Change the tab's default `featureStatusFilter` so `'all'` here means
   "all *completed* statuses" (validated/needs_review/failed/skipped/
   whatever terminal statuses this project already uses — check
   `FeatureStatus` in `src/core/database.py` and `derive_feature_status` in
   `src/core/status_derivation.py` for the authoritative status set), not
   literally every status including in-flight ones.
2. Add an explicit exclusion in `FeatureGallery`'s `filtered` computation
   (`FeatureGallery.tsx:35-36`) so `pending`/`active` features are always
   excluded from this view regardless of `statusFilter`, since this
   component is only ever mounted for the "Completed" tab
   (`Autopilot.tsx:416`).

Either way: a feature that is `pending` or `active` must never appear in
the Completed tab's list or its badge count
(`Autopilot.tsx:290`'s `badge: featuresList?.length`, which also needs the
same exclusion so the tab's own count matches what's actually shown).

## Out of scope

- The Queue tab (`activeTab === 'queue'`, `Autopilot.tsx:407-414`) is the
  correct place for pending/active features to be visible — this fix does
  not change that tab.
- No backend/API change is required; `getAutopilotFeatures()` returning
  every feature is fine, since other consumers (e.g. the Queue tab) may
  legitimately need the full, unfiltered list.

## Verification

- Load a project with at least one `pending` feature, one `active`
  feature, and one `completed`/`validated` feature.
- Open the Completed tab with its default filter (no manual selection) —
  only the completed/validated feature should appear, and the tab's badge
  count should match.
- Confirm the existing `validated` / `needs_review` / `failed` filter
  buttons still work as narrower views within the (now pending/active-free)
  Completed tab.

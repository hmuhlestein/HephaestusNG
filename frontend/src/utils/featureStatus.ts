// Single source of truth for "has this feature finished?" -- used by both
// the Completed tab's feature list (FeatureGallery) and its badge count
// (Autopilot.tsx) so the two can't silently diverge if the definition of
// "completed" ever changes.
export function isCompletedFeatureStatus(status: string): boolean {
  return status !== 'pending' && status !== 'active';
}

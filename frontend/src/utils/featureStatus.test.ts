import { describe, it, expect } from 'vitest';
import { isCompletedFeatureStatus } from './featureStatus';

describe('isCompletedFeatureStatus', () => {
  it('excludes pending and active (still queued or in-flight)', () => {
    expect(isCompletedFeatureStatus('pending')).toBe(false);
    expect(isCompletedFeatureStatus('active')).toBe(false);
  });

  it('includes terminal statuses', () => {
    expect(isCompletedFeatureStatus('validated')).toBe(true);
    expect(isCompletedFeatureStatus('needs_review')).toBe(true);
    expect(isCompletedFeatureStatus('failed')).toBe(true);
  });
});

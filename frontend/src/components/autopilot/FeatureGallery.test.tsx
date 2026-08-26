import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
import FeatureGallery from './FeatureGallery';

const FEATURES = [
  { id: '1', name: 'Pending Feature', status: 'pending', iterations: 0, total_time_seconds: 0, cost_total: 0, created_at: '2026-01-01T00:00:00Z', stop_reason: 'n/a', has_report: false },
  { id: '2', name: 'Active Feature', status: 'active', iterations: 1, total_time_seconds: 10, cost_total: 0, created_at: '2026-01-01T00:00:00Z', stop_reason: 'n/a', has_report: false },
  { id: '3', name: 'Validated Feature', status: 'validated', iterations: 3, total_time_seconds: 100, cost_total: 1.5, created_at: '2026-01-01T00:00:00Z', stop_reason: 'complete', has_report: true },
];

vi.mock('@/services/api', () => ({
  apiService: {
    getAutopilotFeatures: vi.fn(() => Promise.resolve(FEATURES)),
  },
}));

function renderGallery() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FeatureGallery onSelectFeature={() => {}} projectId="proj-1" />
    </QueryClientProvider>
  );
}

describe('FeatureGallery', () => {
  it('excludes pending and active features from the Completed tab list by default', async () => {
    renderGallery();

    expect(await screen.findByText('Validated Feature')).toBeInTheDocument();
    expect(screen.queryByText('Pending Feature')).not.toBeInTheDocument();
    expect(screen.queryByText('Active Feature')).not.toBeInTheDocument();
  });
});

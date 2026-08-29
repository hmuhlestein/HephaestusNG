import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import FeatureGallery from './FeatureGallery';

const FEATURES = [
  { id: '1', name: 'Pending Feature', status: 'pending', iterations: 0, total_time_seconds: 0, cost_total: 0, created_at: '2026-01-01T00:00:00Z', stop_reason: 'n/a', has_report: false, design_id: 'des-a', design_name: 'Spec A' },
  { id: '2', name: 'Active Feature', status: 'active', iterations: 1, total_time_seconds: 10, cost_total: 0, created_at: '2026-01-01T00:00:00Z', stop_reason: 'n/a', has_report: false, design_id: 'des-a', design_name: 'Spec A' },
  { id: '3', name: 'Validated Feature', status: 'validated', iterations: 3, total_time_seconds: 100, cost_total: 1.5, created_at: '2026-01-01T00:00:00Z', stop_reason: 'complete', has_report: true, design_id: 'des-a', design_name: 'Spec A' },
  { id: '4', name: 'Failed Feature', status: 'failed', iterations: 2, total_time_seconds: 50, cost_total: 0.5, created_at: '2026-01-01T00:00:00Z', stop_reason: 'error', has_report: true, design_id: 'des-b', design_name: 'Spec B' },
];

vi.mock('@/services/api', () => ({
  apiService: {
    getAutopilotFeatures: vi.fn(() => Promise.resolve(FEATURES)),
  },
}));

function renderGallery(statusFilter?: 'all' | 'validated' | 'needs_review' | 'failed') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FeatureGallery onSelectFeature={() => {}} projectId="proj-1" statusFilter={statusFilter} />
    </QueryClientProvider>
  );
}

describe('FeatureGallery', () => {
  it('excludes pending and active features from the Completed tab list by default', async () => {
    renderGallery();

    expect(await screen.findByText('Validated Feature')).toBeInTheDocument();
    expect(await screen.findByText('Failed Feature')).toBeInTheDocument();
    expect(screen.queryByText('Pending Feature')).not.toBeInTheDocument();
    expect(screen.queryByText('Active Feature')).not.toBeInTheDocument();
  });

  it('groups features under their own spec, and only shows a spec whose features survive the filter', async () => {
    renderGallery();

    // Spec A contributes only "Validated Feature" here -- its pending/active
    // features are filtered out, so the header must not imply otherwise.
    const specA = await screen.findByText('Spec A');
    expect(specA).toBeInTheDocument();
    expect(specA.parentElement?.textContent).toContain('1');
    expect(screen.getByText('Spec B')).toBeInTheDocument();
  });

  it('drops a spec header entirely when the filter removes all of its features', async () => {
    renderGallery('failed');

    expect(await screen.findByText('Spec B')).toBeInTheDocument();
    expect(screen.queryByText('Spec A')).not.toBeInTheDocument();
  });

  it('still excludes pending/active when a narrower status filter pill is selected', async () => {
    renderGallery('failed');

    expect(await screen.findByText('Failed Feature')).toBeInTheDocument();
    expect(screen.queryByText('Validated Feature')).not.toBeInTheDocument();
    expect(screen.queryByText('Pending Feature')).not.toBeInTheDocument();
    expect(screen.queryByText('Active Feature')).not.toBeInTheDocument();
  });
});

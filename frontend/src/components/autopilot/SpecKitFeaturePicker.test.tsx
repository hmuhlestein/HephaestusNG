import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import SpecKitFeaturePicker from './SpecKitFeaturePicker';

const SINGLE_REPO_FEATURES = [
  { number: '001', slug: 'checkout-flow', repoLabel: null, hasPlan: true, hasTasks: true },
  { number: '002', slug: 'login-page', repoLabel: null, hasPlan: false, hasTasks: false },
];

const MULTI_REPO_FEATURES = [
  { number: '001', slug: 'api-change', repoLabel: 'backend', hasPlan: true, hasTasks: true },
  { number: '002', slug: 'ui-change', repoLabel: 'frontend', hasPlan: true, hasTasks: false },
];

const mockGetFeatures = vi.fn();
const mockGetReadiness = vi.fn();

vi.mock('@/services/api', () => ({
  apiService: {
    getAutopilotProjectSpeckitFeatures: (...args: unknown[]) => mockGetFeatures(...args),
    getAutopilotProjectSpeckitReadiness: (...args: unknown[]) => mockGetReadiness(...args),
  },
}));

function renderPicker(onSelect = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <SpecKitFeaturePicker projectId="proj-1" onSelect={onSelect} />
    </QueryClientProvider>
  );
  return { ...utils, onSelect };
}

describe('SpecKitFeaturePicker', () => {
  it('renders a flat list when the project has one repo (or repo labels are all null)', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    expect(await screen.findByText('001-checkout-flow')).toBeInTheDocument();
    expect(screen.getByText('002-login-page')).toBeInTheDocument();
    // No repo-group headers in the flat case.
    expect(screen.queryByText('backend')).not.toBeInTheDocument();
  });

  it('renders a repo-grouped list when the project has more than one repo', async () => {
    mockGetFeatures.mockResolvedValue(MULTI_REPO_FEATURES);
    renderPicker();

    expect(await screen.findByText('backend')).toBeInTheDocument();
    expect(screen.getByText('frontend')).toBeInTheDocument();
    expect(screen.getByText('001-api-change')).toBeInTheDocument();
    expect(screen.getByText('002-ui-change')).toBeInTheDocument();
  });

  it('calls onSelect with the clicked feature', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    const { onSelect } = renderPicker();

    const row = await screen.findByText('001-checkout-flow');
    fireEvent.click(row.closest('[role="button"]')!);

    expect(onSelect).toHaveBeenCalledWith(SINGLE_REPO_FEATURES[0]);
  });

  it('marks the selected feature aria-pressed after a click', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    const row = (await screen.findByText('001-checkout-flow')).closest('[role="button"]')!;
    expect(row).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(row);

    expect(row).toHaveAttribute('aria-pressed', 'true');
  });

  it('flags a feature missing plan.md', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    expect(await screen.findByText('no plan.md')).toBeInTheDocument();
  });

  it('fetches and renders readiness only after clicking "Check readiness"', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    mockGetReadiness.mockResolvedValue({
      features: [
        {
          number: '001',
          slug: 'checkout-flow',
          repoLabel: null,
          needsClarification: ['What auth scheme?'],
          missingFiles: ['tasks.md'],
        },
      ],
    });
    renderPicker();

    await screen.findByText('001-checkout-flow');
    expect(mockGetReadiness).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByText('Check readiness')[0]);

    expect(await screen.findByText('Missing: tasks.md')).toBeInTheDocument();
    expect(screen.getByText('NEEDS CLARIFICATION: What auth scheme?')).toBeInTheDocument();
  });

  it('disables "Check readiness" while its own request is pending, re-enables after', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    let resolveReadiness: (v: unknown) => void;
    mockGetReadiness.mockReturnValue(
      new Promise(resolve => {
        resolveReadiness = resolve;
      })
    );
    renderPicker();

    await screen.findByText('001-checkout-flow');
    const button = screen.getAllByText('Check readiness')[0].closest('button')!;
    fireEvent.click(button);

    expect(await screen.findByText('Checking…')).toBeInTheDocument();
    expect(button).toBeDisabled();

    resolveReadiness!({ features: [{ number: '001', slug: 'checkout-flow', repoLabel: null, needsClarification: [], missingFiles: [] }] });

    const reenabled = await screen.findByText('Check readiness');
    expect(reenabled.closest('button')!).not.toBeDisabled();
  });

  it('renders an error state on a failed readiness fetch', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    mockGetReadiness.mockRejectedValue(new Error('boom'));
    renderPicker();

    await screen.findByText('001-checkout-flow');
    fireEvent.click(screen.getAllByText('Check readiness')[0]);

    expect(await screen.findByText('Failed to check readiness.')).toBeInTheDocument();
  });

  it('clicking "Check readiness" never calls onSelect', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    mockGetReadiness.mockResolvedValue({ features: [{ number: '001', slug: 'checkout-flow', repoLabel: null, needsClarification: [], missingFiles: [] }] });
    const { onSelect } = renderPicker();

    await screen.findByText('001-checkout-flow');
    fireEvent.click(screen.getAllByText('Check readiness')[0]);

    await screen.findByText('Check readiness');
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('renders nothing when the project has no Spec Kit features', async () => {
    mockGetFeatures.mockResolvedValue([]);
    const { container } = renderPicker();

    await new Promise(resolve => setTimeout(resolve, 0));
    expect(container.querySelector('[data-testid="speckit-feature-picker"]')).not.toBeInTheDocument();
  });
});

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

async function findSelect() {
  return (await screen.findByRole('combobox')) as HTMLSelectElement;
}

describe('SpecKitFeaturePicker', () => {
  it('renders a flat option list when the project has one repo (or repo labels are all null)', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    const select = await findSelect();
    expect(screen.getByRole('option', { name: '001-checkout-flow' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '002-login-page (no plan.md)' })).toBeInTheDocument();
    // No repo optgroups in the flat case.
    expect(select.querySelector('optgroup')).not.toBeInTheDocument();
  });

  it('renders repo optgroups when the project has more than one repo', async () => {
    mockGetFeatures.mockResolvedValue(MULTI_REPO_FEATURES);
    renderPicker();

    const select = await findSelect();
    const groups = Array.from(select.querySelectorAll('optgroup')).map(g => g.label);
    expect(groups).toEqual(['backend', 'frontend']);
    expect(screen.getByRole('option', { name: '001-api-change' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '002-ui-change' })).toBeInTheDocument();
  });

  it('calls onSelect with the chosen feature', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    const { onSelect } = renderPicker();

    const select = await findSelect();
    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });

    expect(onSelect).toHaveBeenCalledWith(SINGLE_REPO_FEATURES[0]);
  });

  it('shows "Check readiness" only after a feature is chosen', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    const select = await findSelect();
    expect(screen.queryByText('Check readiness')).not.toBeInTheDocument();

    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });

    expect(screen.getByText('Check readiness')).toBeInTheDocument();
  });

  it('flags a feature missing plan.md in its option label', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    renderPicker();

    await findSelect();
    expect(screen.getByRole('option', { name: '002-login-page (no plan.md)' })).toBeInTheDocument();
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

    const select = await findSelect();
    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });
    expect(mockGetReadiness).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText('Check readiness'));

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

    const select = await findSelect();
    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });
    const button = screen.getByText('Check readiness').closest('button')!;
    fireEvent.click(button);

    expect(await screen.findByText('Checking…')).toBeInTheDocument();
    expect(button).toBeDisabled();

    resolveReadiness!({ features: [{ number: '001', slug: 'checkout-flow', repoLabel: null, needsClarification: [], missingFiles: [] }] });

    const reenabled = await screen.findByText('Check readiness');
    expect(reenabled.closest('button')!).not.toBeDisabled();
  });

  it("passes the feature's real (non-null) repoLabel through to the readiness call for a multi-repo feature", async () => {
    mockGetFeatures.mockResolvedValue(MULTI_REPO_FEATURES);
    mockGetReadiness.mockResolvedValue({ features: [{ number: '001', slug: 'api-change', repoLabel: 'backend', needsClarification: [], missingFiles: [] }] });
    renderPicker();

    const select = await findSelect();
    fireEvent.change(select, { target: { value: 'backend/001-api-change' } });
    fireEvent.click(screen.getByText('Check readiness'));

    await screen.findByText('Check readiness');
    expect(mockGetReadiness).toHaveBeenCalledWith('proj-1', { number: '001', repoLabel: 'backend' });
  });

  it('renders an error state on a failed readiness fetch', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    mockGetReadiness.mockRejectedValue(new Error('boom'));
    renderPicker();

    const select = await findSelect();
    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });
    fireEvent.click(screen.getByText('Check readiness'));

    expect(await screen.findByText('Failed to check readiness.')).toBeInTheDocument();
  });

  it('choosing a feature never calls onSelect a second time from "Check readiness"', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    mockGetReadiness.mockResolvedValue({ features: [{ number: '001', slug: 'checkout-flow', repoLabel: null, needsClarification: [], missingFiles: [] }] });
    const { onSelect } = renderPicker();

    const select = await findSelect();
    fireEvent.change(select, { target: { value: '/001-checkout-flow' } });
    expect(onSelect).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('Check readiness'));
    await screen.findByText('Check readiness');

    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it('fires onOpen as soon as the dropdown is clicked, before a choice is made', async () => {
    mockGetFeatures.mockResolvedValue(SINGLE_REPO_FEATURES);
    const onOpen = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <SpecKitFeaturePicker projectId="proj-1" onSelect={vi.fn()} onOpen={onOpen} />
      </QueryClientProvider>
    );

    const select = await findSelect();
    fireEvent.mouseDown(select);

    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('renders nothing when the project has no Spec Kit features', async () => {
    mockGetFeatures.mockResolvedValue([]);
    const { container } = renderPicker();

    await new Promise(resolve => setTimeout(resolve, 0));
    expect(container.querySelector('[data-testid="speckit-feature-picker"]')).not.toBeInTheDocument();
  });
});

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import RealTimeAgentOutput from './RealTimeAgentOutput';
import type { Agent } from '@/types';

// Regression: some CLIs (observed live: Claude Code's own "Task ID: ..."
// banner chip) set a background via \x1b[100m and only reset the
// FOREGROUND afterward (\x1b[39m, never \x1b[49m/\x1b[0m). On a real
// terminal that's invisible -- the badge's screen region gets redrawn
// moments later -- but RealTimeAgentOutput concatenates the WHOLE
// transcript into one string and runs ansi-to-html over it ONCE, which
// correctly (per real terminal semantics) carries SGR state across the
// embedded '\n's. An unclosed background from one historical line then
// bled into every later line rendered in that same pass -- e.g. a plain
// white-ish status line landing on the earlier line's dark-gray
// background instead of the container's own background, reading as
// washed-out/illegible.
const BUGGY_BADGE_LINE = '\x1b[37m\x1b[100m▌ \x1b[97mTask ID: abc123\x1b[39m';
const LATER_STATUS_LINE = '\x1b[91m✶\x1b[39m \x1b[91mIdeating… \x1b[37m(6m \xb7 tokens)\x1b[39m';

const mockUseRealTimeAgentOutput = vi.fn();

vi.mock('@/hooks/useRealTimeAgentOutput', () => ({
  useRealTimeAgentOutput: (...args: unknown[]) => mockUseRealTimeAgentOutput(...args),
}));

vi.mock('@/services/api', () => ({
  apiService: {
    getAgent: vi.fn().mockResolvedValue(null),
    restartTask: vi.fn(),
    terminateAgent: vi.fn(),
    sendAgentKey: vi.fn(),
    sendMessage: vi.fn(),
  },
}));

function makeAgent(): Agent {
  return {
    id: 'agent-1',
    status: 'working',
    agent_type: 'phase',
    cli_type: 'claude',
    cli_model: null,
    current_task_id: 'task-1',
    tmux_session_name: 'agent_agent-1',
    health_check_failures: 0,
    created_at: new Date().toISOString(),
    terminated_at: null,
    last_activity: null,
  };
}

describe('RealTimeAgentOutput', () => {
  it('does not let an unclosed background from one line bleed into a later, unrelated line', () => {
    mockUseRealTimeAgentOutput.mockReturnValue({
      output: `${BUGGY_BADGE_LINE}\n${LATER_STATUS_LINE}`,
      isLoading: false,
      error: null,
      isConnected: true,
      lastUpdateTime: new Date(),
      retry: vi.fn(),
      setPauseUpdates: vi.fn(),
    });

    const { container } = render(
      <RealTimeAgentOutput agent={makeAgent()} onClose={vi.fn()} />
    );

    const pane = container.querySelector('.ansi-output');
    expect(pane).not.toBeNull();

    // The badge's dark-gray background span must not be an ancestor of
    // the later "Ideating..." status text -- each line's own span tree
    // must be self-contained.
    const badgeBgSpan = Array.from(pane!.querySelectorAll('span')).find(
      (el) => el.getAttribute('style')?.includes('background-color')
    );
    expect(badgeBgSpan).toBeDefined();
    expect(badgeBgSpan!.textContent).not.toContain('Ideating');
  });
});

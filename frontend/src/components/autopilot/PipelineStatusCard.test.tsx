import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import PipelineStatusCard from './PipelineStatusCard';

// designs_processed is a lifetime counter loaded from
// PersistentPipelineState -- it persists across restarts and unrelated
// past runs, so it must never be summed with the live queue_depth to
// derive a "total designs" figure (see PipelineStatusCard.tsx).

describe('PipelineStatusCard', () => {
  it('does not inflate the remaining-work count with the lifetime designs_processed counter', () => {
    // A project that already finished one design in the past (recorded
    // forever in designs_processed) with exactly one design still queued
    // must show "1 design remaining", not "2 of 2".
    render(
      <PipelineStatusCard
        status={{
          running: true,
          current_design: null,
          designs_processed: 1,
          queue_depth: 1,
        }}
      />
    );

    expect(screen.getByText('1 design remaining in queue')).toBeInTheDocument();
    expect(screen.queryByText(/of 2/)).not.toBeInTheDocument();
  });

  it('hides the queue indicator when nothing is left to process', () => {
    render(
      <PipelineStatusCard
        status={{
          running: true,
          current_design: null,
          designs_processed: 5,
          queue_depth: 0,
        }}
      />
    );

    expect(screen.queryByText(/remaining in queue/)).not.toBeInTheDocument();
    expect(screen.getByText('Waiting for designs...')).toBeInTheDocument();
  });

  it('pluralizes correctly for more than one remaining design', () => {
    render(
      <PipelineStatusCard
        status={{
          running: true,
          current_design: 'some-design',
          designs_processed: 3,
          queue_depth: 2,
        }}
      />
    );

    expect(screen.getByText('2 designs remaining in queue')).toBeInTheDocument();
  });
});

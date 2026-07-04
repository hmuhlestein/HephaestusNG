"""Tests for src/monitoring/signals.py — the monitor -> orchestrator
feedback channel (Enhancement 4, docs/LOOP_ENGINEERING_REVIEW.md).
"""

from src.monitoring.signals import MonitorSignal, SignalQueue, SignalType


def make_signal(
    workflow_id="wf-1",
    signal_type=SignalType.STUCK_PATTERN,
    confidence=0.7,
    agent_id=None,
    evidence="",
):
    return MonitorSignal(
        type=signal_type,
        workflow_id=workflow_id,
        agent_id=agent_id,
        confidence=confidence,
        evidence=evidence,
    )


class TestEmitAndGet:
    def test_emit_then_get_returns_signal(self):
        queue = SignalQueue()
        sig = make_signal()
        queue.emit(sig)

        result = queue.get_signals("wf-1")
        assert result == [sig]

    def test_get_signals_for_unknown_workflow_returns_empty(self):
        queue = SignalQueue()
        assert queue.get_signals("nonexistent") == []

    def test_get_signals_filters_by_workflow(self):
        queue = SignalQueue()
        sig1 = make_signal(workflow_id="wf-1")
        sig2 = make_signal(workflow_id="wf-2")
        queue.emit(sig1)
        queue.emit(sig2)

        assert queue.get_signals("wf-1") == [sig1]
        assert queue.get_signals("wf-2") == [sig2]

    def test_get_signals_filters_by_type(self):
        queue = SignalQueue()
        stuck = make_signal(signal_type=SignalType.STUCK_PATTERN)
        drift = make_signal(signal_type=SignalType.TRAJECTORY_DEVIATION)
        queue.emit(stuck)
        queue.emit(drift)

        result = queue.get_signals(
            "wf-1", signal_type=SignalType.STUCK_PATTERN, consume=False
        )
        assert result == [stuck]

    def test_get_signals_filters_by_min_confidence(self):
        queue = SignalQueue()
        low = make_signal(confidence=0.3)
        high = make_signal(confidence=0.9)
        queue.emit(low)
        queue.emit(high)

        result = queue.get_signals("wf-1", min_confidence=0.7, consume=False)
        assert result == [high]


class TestConsume:
    def test_consume_true_removes_matching_signals(self):
        queue = SignalQueue()
        sig = make_signal()
        queue.emit(sig)

        first = queue.get_signals("wf-1", consume=True)
        second = queue.get_signals("wf-1", consume=True)

        assert first == [sig]
        assert second == []

    def test_consume_false_leaves_signals_in_queue(self):
        queue = SignalQueue()
        sig = make_signal()
        queue.emit(sig)

        first = queue.get_signals("wf-1", consume=False)
        second = queue.get_signals("wf-1", consume=False)

        assert first == [sig]
        assert second == [sig]

    def test_consume_only_removes_matching_signals_not_others(self):
        """Regression test: get_signals(consume=True) with a type/confidence
        filter must only remove the signals that matched the filter, leaving
        non-matching signals in the queue for a later call."""
        queue = SignalQueue()
        stuck = make_signal(signal_type=SignalType.STUCK_PATTERN, confidence=0.9)
        drift = make_signal(signal_type=SignalType.TRAJECTORY_DEVIATION, confidence=0.9)
        queue.emit(stuck)
        queue.emit(drift)

        # Consume only STUCK_PATTERN signals
        result = queue.get_signals(
            "wf-1", signal_type=SignalType.STUCK_PATTERN, consume=True
        )
        assert result == [stuck]

        # The drift signal must still be there
        remaining = queue.get_signals("wf-1", consume=False)
        assert remaining == [drift]

    def test_consume_with_structurally_identical_signals(self):
        """Two signals with identical field values (a plain dataclass's
        auto-generated __eq__ would consider them equal) must still both be
        handled correctly by count, not silently merged/dropped."""
        sig_a = MonitorSignal(
            type=SignalType.STUCK_PATTERN,
            workflow_id="wf-1",
            confidence=0.7,
            evidence="same evidence",
            timestamp=make_signal().timestamp,  # force identical timestamp
        )
        sig_b = MonitorSignal(
            type=SignalType.STUCK_PATTERN,
            workflow_id="wf-1",
            confidence=0.7,
            evidence="same evidence",
            timestamp=sig_a.timestamp,
        )
        assert sig_a == sig_b  # confirms they really are value-equal

        queue = SignalQueue()
        queue.emit(sig_a)
        queue.emit(sig_b)

        result = queue.get_signals("wf-1", consume=True)
        assert len(result) == 2

        remaining = queue.get_signals("wf-1", consume=False)
        assert remaining == []


class TestCountSignals:
    def test_count_signals_does_not_consume(self):
        queue = SignalQueue()
        queue.emit(make_signal())

        count = queue.count_signals("wf-1")
        assert count == 1
        # Still there after counting
        assert queue.count_signals("wf-1") == 1

    def test_count_signals_respects_filters(self):
        queue = SignalQueue()
        queue.emit(make_signal(signal_type=SignalType.STUCK_PATTERN))
        queue.emit(make_signal(signal_type=SignalType.TRAJECTORY_DEVIATION))

        assert queue.count_signals("wf-1", signal_type=SignalType.STUCK_PATTERN) == 1
        assert queue.count_signals("wf-1") == 2


class TestMaxSignalsPerWorkflow:
    def test_evicts_oldest_when_over_limit(self):
        queue = SignalQueue(max_signals_per_workflow=3)
        sigs = [make_signal(evidence=str(i)) for i in range(5)]
        for s in sigs:
            queue.emit(s)

        result = queue.get_signals("wf-1", consume=False)
        assert len(result) == 3
        # Should have kept the most recent 3
        assert [s.evidence for s in result] == ["2", "3", "4"]


class TestClear:
    def test_clear_specific_workflow(self):
        queue = SignalQueue()
        queue.emit(make_signal(workflow_id="wf-1"))
        queue.emit(make_signal(workflow_id="wf-2"))

        queue.clear("wf-1")

        assert queue.get_signals("wf-1", consume=False) == []
        assert len(queue.get_signals("wf-2", consume=False)) == 1

    def test_clear_all_workflows(self):
        queue = SignalQueue()
        queue.emit(make_signal(workflow_id="wf-1"))
        queue.emit(make_signal(workflow_id="wf-2"))

        queue.clear()

        assert queue.get_signals("wf-1", consume=False) == []
        assert queue.get_signals("wf-2", consume=False) == []

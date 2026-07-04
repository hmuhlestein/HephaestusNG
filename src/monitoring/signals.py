"""Signal types for monitor → orchestrator feedback.

Enhancement 4 (from docs/LOOP_ENGINEERING_EVALUATION.md):
Connects the Guardian/Conductor's findings to the orchestrator's loop
control decisions. Previously, the monitoring loop ran asynchronously
but its findings sat in DB records and logs — they never influenced the
orchestrator's impasse detection or loop termination.

This module defines the signal types and a thread-safe queue that the
monitoring loop writes to and the orchestrator's poll loop reads from.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Types of signals the monitoring loop can emit."""

    # Agent is making no net progress (rewriting same files, stuck pattern)
    STUCK_PATTERN = "stuck_pattern"

    # Same error class seen N times from same agent
    REPEATED_FAILURE = "repeated_failure"

    # Agent consuming excessive resources (tokens, time)
    RESOURCE_EXHAUSTION = "resource_exhaustion"

    # Agent trajectory deviating from plan
    TRAJECTORY_DEVIATION = "trajectory_deviation"

    # Phase-level concern (e.g., too many retries on same phase)
    PHASE_STUCK = "phase_stuck"


@dataclass
class MonitorSignal:
    """A signal from the monitoring loop to the orchestrator."""

    type: SignalType
    workflow_id: str
    agent_id: Optional[str] = None
    phase_id: Optional[str] = None
    confidence: float = 0.5  # 0-1, how confident the monitor is
    evidence: str = ""  # Human-readable description
    metadata: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        agent_part = f" agent={self.agent_id[:8]}" if self.agent_id else ""
        return (
            f"MonitorSignal({self.type.value}, wf={self.workflow_id[:8]}{agent_part}, "
            f"conf={self.confidence:.2f}): {self.evidence[:80]}"
        )


class SignalQueue:
    """Thread-safe queue for monitor → orchestrator signals.

    The monitoring loop calls emit() to add signals.
    The orchestrator's poll loop calls get_signals() to consume them.
    """

    def __init__(self, max_signals_per_workflow: int = 100):
        self._lock = threading.Lock()
        self._signals: Dict[str, List[MonitorSignal]] = {}  # workflow_id -> signals
        self._max_per_workflow = max_signals_per_workflow

    def emit(self, signal: MonitorSignal) -> None:
        """Add a signal to the queue (called by monitoring loop)."""
        with self._lock:
            wf_id = signal.workflow_id
            if wf_id not in self._signals:
                self._signals[wf_id] = []
            self._signals[wf_id].append(signal)

            # Evict oldest if over limit
            if len(self._signals[wf_id]) > self._max_per_workflow:
                self._signals[wf_id] = self._signals[wf_id][-self._max_per_workflow:]

            logger.info(f"[SIGNAL] Emitted: {signal}")

    def get_signals(
        self,
        workflow_id: str,
        signal_type: Optional[SignalType] = None,
        min_confidence: float = 0.0,
        consume: bool = True,
    ) -> List[MonitorSignal]:
        """Get signals for a workflow (called by orchestrator).

        Args:
            workflow_id: Filter by workflow
            signal_type: Filter by signal type (None = all types)
            min_confidence: Minimum confidence threshold
            consume: If True, remove returned signals from queue

        Returns:
            List of matching signals
        """
        def _matches(s: MonitorSignal) -> bool:
            return s.confidence >= min_confidence and (
                signal_type is None or s.type == signal_type
            )

        with self._lock:
            signals = self._signals.get(workflow_id, [])

            # Partition by the predicate directly rather than filtering
            # "remaining" via `s not in filtered` — MonitorSignal is a plain
            # dataclass with value-based __eq__, so two structurally
            # identical signals (possible if emitted in the same
            # microsecond with the same evidence/confidence) would
            # incorrectly be treated as the same object under `in`.
            filtered = [s for s in signals if _matches(s)]

            if consume:
                remaining = [s for s in signals if not _matches(s)]
                if remaining:
                    self._signals[workflow_id] = remaining
                else:
                    self._signals.pop(workflow_id, None)

            return filtered

    def count_signals(
        self,
        workflow_id: str,
        signal_type: Optional[SignalType] = None,
        min_confidence: float = 0.0,
    ) -> int:
        """Count matching signals without consuming them."""
        with self._lock:
            signals = self._signals.get(workflow_id, [])
            return sum(
                1
                for s in signals
                if s.confidence >= min_confidence
                and (signal_type is None or s.type == signal_type)
            )

    def clear(self, workflow_id: Optional[str] = None) -> None:
        """Clear signals for a workflow (or all workflows)."""
        with self._lock:
            if workflow_id:
                self._signals.pop(workflow_id, None)
            else:
                self._signals.clear()


# Global signal queue instance
_signal_queue: Optional[SignalQueue] = None
_queue_lock = threading.Lock()


def get_signal_queue() -> SignalQueue:
    """Get the global signal queue (creates if needed)."""
    global _signal_queue
    if _signal_queue is None:
        with _queue_lock:
            if _signal_queue is None:
                _signal_queue = SignalQueue()
    return _signal_queue

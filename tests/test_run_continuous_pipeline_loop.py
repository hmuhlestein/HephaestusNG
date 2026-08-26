"""Characterization tests for run_continuous_pipeline's main loop.

Companion to test_run_single_workflow_loop.py, same purpose (SOLID review
2.3): pin the loop's existing behavior before decomposing it. Prior coverage
(tests/sdk/test_client_start.py) deliberately stops at sdk.start, so nothing
exercised the loop itself.

The invariants here are mostly *protective* -- this loop decides when it is
safe to dispatch a new design, and dispatching wrongly is destructive:
run_single_workflow's default pause_existing=True terminates every other
active workflow's agents project-wide. Several of the branches below exist
because that damage was previously observed live (see the comments in
run_continuous_pipeline itself), which is exactly the kind of hard-won
behavior a refactor can silently undo.
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from src.autopilot import orchestrator
from src.autopilot.orchestrator.state import DesignStatus, PipelineState


@pytest.fixture
def args(tmp_path):
    return argparse.Namespace(
        project_path=str(tmp_path / "proj"),
        design_queue=str(tmp_path / "proj" / ".hephaestus" / "specs"),
        max_iterations=3,
        in_process=True,
        project_id="proj-1",
    )


def _design(name="D1", content_hash="h1"):
    d = MagicMock()
    d.name = name
    d.content_hash = content_hash
    d.feature_folder = None
    d.path.name = f"{name}.md"
    d.status = DesignStatus.PENDING
    return d


class _PipelineHarness:
    """Patches run_continuous_pipeline's collaborators.

    Defaults: nothing active, nothing resumable, empty queue -- so the loop
    idles. The stop signal fires after `stop_after_cycles` scan cycles so
    every test terminates.
    """

    def __init__(self, stop_after_cycles=2):
        self.stop_after_cycles = stop_after_cycles
        self.cycles = 0
        self.active_workflows = []
        self.still_blocking = []
        self.resumable_elsewhere = False
        self.next_designs = []
        self.run_single_design = MagicMock(
            return_value=(DesignStatus.COMPLETED, MagicMock(iterations=1, qa_passed=True, product_validated=True, total_time_seconds=1))
        )
        self.gating_error = None
        self.state = PipelineState()

    def _should_stop(self, project_id=None):
        self.cycles += 1
        return self.cycles > self.stop_after_cycles

    def _get_active_workflows(self, *a, **k):
        if self.gating_error:
            raise self.gating_error
        return list(self.active_workflows)

    def _pick_next_design(self, *a, **k):
        return self.next_designs.pop(0) if self.next_designs else None

    def __enter__(self):
        fake_sdk = MagicMock()
        self.sdk = fake_sdk
        persistent = MagicMock()
        persistent.load.return_value = (self.state, set())
        persistent.has_incomplete_work.return_value = False
        self.persistent = persistent

        self._stack = [
            patch("src.sdk.HephaestusSDK", return_value=fake_sdk),
            patch("src.autopilot.orchestrator.pipeline.get_config"),
            patch("src.autopilot.orchestrator.pipeline.PersistentPipelineState", return_value=persistent),
            patch("src.workflow_registry.get_all_workflow_definitions", return_value=[]),
            patch("src.autopilot.orchestrator.pipeline._register_orchestrator_agent", return_value="orch-1"),
            patch("src.autopilot.orchestrator.pipeline._should_stop", side_effect=self._should_stop),
            patch("src.autopilot.orchestrator.pipeline.get_active_workflows", side_effect=self._get_active_workflows),
            patch("src.autopilot.orchestrator.pipeline._escalate_stale_active_workflows", side_effect=lambda *a, **k: list(self.still_blocking)),
            patch("src.autopilot.orchestrator.pipeline._has_resumable_active_design", side_effect=lambda *a, **k: self.resumable_elsewhere),
            patch("src.autopilot.orchestrator.pipeline.pick_next_design", side_effect=self._pick_next_design),
            patch("src.autopilot.orchestrator.pipeline.run_single_design", self.run_single_design),
            patch("src.autopilot.orchestrator.pipeline._update_orchestrator_status"),
            patch("src.autopilot.orchestrator.pipeline._interruptible_sleep"),
            patch("src.autopilot.orchestrator.pipeline.pause_workflow_direct"),
            patch("src.autopilot.orchestrator.pipeline.get_workflow_status", return_value={"status": "active", "project_id": "proj-1"}),
            patch("src.autopilot.orchestrator.pipeline.is_design_fully_complete", return_value=(True, "done")),
            patch("src.autopilot.orchestrator.pipeline._clean_stale_assigned_tasks"),
            patch("src.autopilot.orchestrator.pipeline.attempt_recovery", return_value=(True, "recovered")),
            patch("src.autopilot.orchestrator.pipeline.DESIGN_QUEUE_SCAN_INTERVAL", 0),
            patch("src.autopilot.orchestrator.pipeline.POLL_INTERVAL", 0),
            patch("src.autopilot.orchestrator.pipeline.time.sleep"),
        ]
        for p in self._stack:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._stack):
            p.stop()
        return False


class TestProtectiveGating:
    """The loop must not dispatch a new design unless it has positively
    verified nothing else is running."""

    def test_active_workflow_blocks_new_design_dispatch(self, args):
        harness = _PipelineHarness()
        harness.still_blocking = ["wf-active"]
        harness.resumable_elsewhere = False
        harness.next_designs = [_design()]

        with harness:
            orchestrator.run_continuous_pipeline(args)

        harness.run_single_design.assert_not_called()

    def test_gating_failure_skips_the_cycle_instead_of_dispatching(self, args):
        """A transient error anywhere in the gating section must be treated
        as "not safe to proceed", not logged-and-ignored. Falling through
        here would dispatch on an UNVERIFIED "nothing else is active", and
        run_single_workflow would then terminate a genuinely in-progress
        design's agents mid-work.
        """
        harness = _PipelineHarness()
        harness.gating_error = RuntimeError("transient DB failure")
        harness.next_designs = [_design()]

        with harness:
            orchestrator.run_continuous_pipeline(args)

        harness.run_single_design.assert_not_called()

    def test_resumable_design_elsewhere_bypasses_the_active_block(self, args):
        """An active workflow does NOT block when another design already has
        resumable ready features -- otherwise a design left tracked from
        before a restart blocks the queue forever."""
        harness = _PipelineHarness()
        harness.still_blocking = ["wf-active"]
        harness.resumable_elsewhere = True
        harness.next_designs = [_design()]

        with harness:
            orchestrator.run_continuous_pipeline(args)

        harness.run_single_design.assert_called_once()


class TestDesignProcessing:
    def test_empty_queue_does_not_dispatch(self, args):
        harness = _PipelineHarness()

        with harness:
            orchestrator.run_continuous_pipeline(args)

        harness.run_single_design.assert_not_called()

    def test_design_is_dispatched_and_marked_processed(self, args):
        harness = _PipelineHarness()
        design = _design()
        harness.next_designs = [design]

        with harness:
            orchestrator.run_continuous_pipeline(args)

        harness.run_single_design.assert_called_once()
        assert design.status is DesignStatus.COMPLETED
        assert harness.state.designs_processed == 1
        assert harness.state.designs_succeeded == 1

    def test_run_single_design_raising_is_recorded_as_a_failed_design(self, args):
        """An unexpected raise must not kill the pipeline -- the design is
        marked failed and the loop continues to the next one."""
        harness = _PipelineHarness()
        harness.run_single_design.side_effect = RuntimeError("design exploded")
        design = _design()
        harness.next_designs = [design]

        with harness:
            orchestrator.run_continuous_pipeline(args)

        assert design.status is DesignStatus.FAILED
        assert harness.state.designs_failed == 1
        assert harness.state.designs_processed == 1


class TestShutdown:
    def test_stop_pauses_this_projects_active_workflows_and_shuts_down_sdk(self, args):
        harness = _PipelineHarness()
        harness.active_workflows = [{"id": "wf-1"}, {"id": "wf-2"}]
        # Nothing blocks, so the loop idles until the stop signal.
        harness.still_blocking = []

        with harness:
            with patch("src.autopilot.orchestrator.pipeline.pause_workflow_direct") as pause:
                orchestrator.run_continuous_pipeline(args)

        assert {c.args[0] for c in pause.call_args_list} == {"wf-1", "wf-2"}
        harness.sdk.shutdown.assert_called_once()

    def test_active_workflow_scan_at_shutdown_is_scoped_to_this_project(self, args):
        """Unscoped, shutdown would pause an unrelated project's active
        workflow just because this project's pipeline stopped."""
        harness = _PipelineHarness()

        with harness:
            with patch(
                "src.autopilot.orchestrator.pipeline.get_active_workflows",
                side_effect=harness._get_active_workflows,
            ) as gaw:
                orchestrator.run_continuous_pipeline(args)

        assert gaw.call_args_list, "expected at least the shutdown scan"
        for call in gaw.call_args_list:
            assert call.kwargs.get("project_id") == "proj-1"

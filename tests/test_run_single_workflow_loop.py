"""Characterization tests for run_single_workflow's monitoring loop.

Written to pin the loop's existing exit-verdict behavior BEFORE decomposing
it (SOLID review 2.3 -- the function had grown from 465 to 591 lines and had
no test coverage at all; tests/sdk/test_client_start.py exercises only the
pre-loop SDK-init path, deliberately short-circuiting before the loop runs).

These are characterization tests, not specifications: they assert what the
loop does today so a refactor that changes it fails loudly. The loop body is
dense with fixes for specific observed-live incidents (see the comments in
run_single_workflow itself), several of which are subtle enough that a
plausible-looking extraction could silently undo one -- notably the
no_tasks_streak two-poll confirmation and the post-_try_advance_phases count
refresh, both of which exist precisely because a single stale/failed poll
previously killed healthy workflows.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.autopilot.orchestrator import run_single_workflow
from src.autopilot.orchestrator.state import FeatureRunStatus


@pytest.fixture
def mock_logger():
    return MagicMock()


def _task(task_id="t1"):
    return {"id": task_id}


def _agent(agent_id="a1", status="working", agent_type="developer"):
    return {"id": agent_id, "status": status, "agent_type": agent_type}


class _LoopHarness:
    """Patches every collaborator run_single_workflow's loop calls.

    Defaults describe a workflow that is active, has no agents and no tasks,
    and reports no credit/hard-error/impasse trouble -- i.e. the loop spins
    without reaching any verdict. Each test overrides just the collaborators
    its scenario needs, so the assertion is about one exit path at a time.
    """

    def __init__(self):
        self.tasks_by_status = {}
        self.agents = []
        self.workflow_status = {"status": "active"}
        self.should_stop = False
        self.credits = (False, "")
        self.hard_error = (False, "")
        self.impasse = (False, "")
        self.prompt_response = "c"
        self.elapsed_values = None

    def get_tasks(self, status=None, workflow_id=None):
        return list(self.tasks_by_status.get(status, []))

    def get_agents(self, workflow_id=None):
        return list(self.agents)

    def __enter__(self):
        self._stack = [
            # POLL_INTERVAL 0 keeps the loop's time.sleep calls instant.
            patch("src.autopilot.orchestrator.pipeline.POLL_INTERVAL", 0),
            patch("src.autopilot.orchestrator.pipeline._get_workflow_timeout", return_value=3600),
            patch("src.autopilot.orchestrator.pipeline._should_stop", side_effect=lambda pid: self.should_stop),
            patch("src.autopilot.orchestrator.pipeline.get_active_workflows", return_value=[]),
            patch("src.autopilot.orchestrator.pipeline.get_workflow_status", side_effect=lambda wid: dict(self.workflow_status)),
            patch("src.autopilot.orchestrator.pipeline.get_agents", side_effect=self.get_agents),
            patch("src.autopilot.orchestrator.pipeline.get_tasks", side_effect=self.get_tasks),
            patch("src.autopilot.orchestrator.pipeline._try_advance_phases", return_value=False),
            patch("src.autopilot.orchestrator.pipeline.check_api_credits", side_effect=lambda: self.credits),
            patch("src.autopilot.orchestrator.pipeline.detect_hard_error", side_effect=lambda *a, **k: self.hard_error),
            patch("src.autopilot.orchestrator.pipeline.detect_impasse", side_effect=lambda *a, **k: self.impasse),
            patch("src.autopilot.orchestrator.pipeline.prompt_human", side_effect=lambda *a, **k: self.prompt_response),
            patch("src.autopilot.orchestrator.pipeline.peek_agent_output", return_value=""),
            patch("src.autopilot.orchestrator.pipeline._register_monitored_workflow"),
            patch("src.autopilot.orchestrator.pipeline._unregister_monitored_workflow"),
            patch("src.autopilot.orchestrator.pipeline.terminate_agent_direct"),
            patch("src.autopilot.orchestrator.pipeline.pause_workflow_direct"),
        ]
        for p in self._stack:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._stack):
            p.stop()
        return False


def _run(harness_setup=None, sdk=None, **kwargs):
    harness = _LoopHarness()
    if harness_setup:
        harness_setup(harness)
    sdk = sdk or MagicMock()
    sdk.start_workflow.return_value = "exec-1"
    with harness:
        return run_single_workflow(
            sdk=sdk,
            workflow_id="autopilot",
            project_path="/tmp/does-not-exist-project",
            description="d",
            logger=MagicMock(),
            **kwargs,
        )


class TestExitVerdicts:
    def test_stop_request_returns_interrupted(self):
        def setup(h):
            h.should_stop = True

        assert _run(setup) is FeatureRunStatus.INTERRUPTED

    def test_timeout_returns_timeout(self):
        """Checked before the workflow-status read, so a workflow that is
        still genuinely active still times out."""
        assert _run(timeout_seconds=-1) is FeatureRunStatus.TIMEOUT

    @pytest.mark.parametrize("terminal", ["completed", "failed", "paused"])
    def test_terminal_workflow_status_is_returned_verbatim(self, terminal):
        def setup(h):
            h.workflow_status = {"status": terminal}

        assert _run(setup) is FeatureRunStatus(terminal)

    def test_hard_error_detection_returns_hard_error(self):
        def setup(h):
            # An active agent keeps the "no work left" completion check from
            # firing first, isolating detect_hard_error's own verdict.
            h.agents = [_agent()]
            h.hard_error = (True, "agent crashed")

        assert _run(setup) is FeatureRunStatus.HARD_ERROR

    def test_done_tasks_with_no_remaining_work_completes(self):
        def setup(h):
            h.tasks_by_status = {"done": [_task()]}

        assert _run(setup) is FeatureRunStatus.COMPLETED


class TestNoTasksStreakRequiresTwoConsecutivePolls:
    """The HARD_ERROR "no tasks exist" verdict must never fire on a single
    poll. get_tasks() swallows its own exceptions and returns [] on failure,
    making a transient DB error indistinguishable from "genuinely no tasks"
    -- one bad poll previously killed healthy, actively-progressing
    workflows outright.
    """

    def test_single_empty_poll_does_not_hard_error(self):
        """Real activity on the second poll resets the streak, so the loop
        keeps running rather than reaching a verdict -- forced to stop here
        via should_stop so the test terminates."""
        state = {"polls": 0}

        def setup(h):
            def tasks(status=None, workflow_id=None):
                state["polls"] += 1
                # First cycle: entirely empty (would start the streak).
                # Then real pending work appears, which must reset it.
                if state["polls"] > 8:
                    h.should_stop = True
                    return []
                if state["polls"] > 4 and status == "pending":
                    return [_task()]
                return []

            h.get_tasks = tasks

        # Elapsed must exceed 300s for the streak logic to engage at all.
        with patch("src.autopilot.orchestrator.pipeline.time") as mock_time:
            mock_time.time.side_effect = [0] + [400] * 200
            mock_time.sleep.return_value = None
            result = _run(setup)

        assert result is FeatureRunStatus.INTERRUPTED

    def test_two_consecutive_empty_polls_hard_errors(self):
        with patch("src.autopilot.orchestrator.pipeline.time") as mock_time:
            mock_time.time.side_effect = [0] + [400] * 200
            mock_time.sleep.return_value = None
            result = _run()

        assert result is FeatureRunStatus.HARD_ERROR

    def test_work_created_by_phase_advancement_resets_the_streak(self):
        """The counts are re-read AFTER _try_advance_phases, because that
        call may itself have created the next phase's task. Reading only the
        pre-advance snapshot makes a workflow that IS progressing look
        empty, and two such polls reach the HARD_ERROR verdict -- so the
        refresh is what keeps a healthy workflow alive here, not just a
        tidiness measure.
        """
        state = {"advanced": False, "cycles": 0}

        harness = _LoopHarness()

        def workflow_status(wid):
            # Runs once per cycle, before the pre-advance task reads.
            state["advanced"] = False
            state["cycles"] += 1
            if state["cycles"] > 4:
                harness.should_stop = True
            return {"status": "active"}

        def advance(*a, **k):
            state["advanced"] = True
            return True

        def tasks(status=None, workflow_id=None):
            # The task _try_advance_phases just created is visible only to
            # the post-advance read.
            if state["advanced"] and status == "pending":
                return [_task()]
            return []

        harness.get_tasks = tasks

        sdk = MagicMock()
        sdk.start_workflow.return_value = "exec-1"

        with harness:
            with (
                patch("src.autopilot.orchestrator.pipeline.get_workflow_status", side_effect=workflow_status),
                patch("src.autopilot.orchestrator.pipeline.get_tasks", side_effect=tasks),
                patch("src.autopilot.orchestrator.pipeline._try_advance_phases", side_effect=advance),
                patch("src.autopilot.orchestrator.pipeline.time") as mock_time,
            ):
                mock_time.time.side_effect = [0] + [400] * 200
                mock_time.sleep.return_value = None
                result = run_single_workflow(
                    sdk=sdk,
                    workflow_id="autopilot",
                    project_path="/tmp/does-not-exist-project",
                    description="d",
                    logger=MagicMock(),
                )

        assert result is FeatureRunStatus.INTERRUPTED


class TestImpasseEscalation:
    """Impasse only escalates to a human prompt after STUCK_THRESHOLD
    consecutive impasse polls, and the human's answer decides the verdict."""

    def test_quit_response_returns_interrupted(self):
        def setup(h):
            h.agents = [_agent()]
            h.impasse = (True, "no progress")
            h.prompt_response = "q"

        assert _run(setup) is FeatureRunStatus.INTERRUPTED

    def test_skip_response_returns_skipped_and_terminates_agents(self):
        def setup(h):
            h.agents = [_agent()]
            h.impasse = (True, "no progress")
            h.prompt_response = "s"

        assert _run(setup) is FeatureRunStatus.SKIPPED

    def test_prompt_only_fires_after_threshold_consecutive_impasses(self):
        """A single impasse poll must not prompt -- one non-impasse poll in
        between resets the counter to zero."""
        from src.autopilot.orchestrator import STUCK_THRESHOLD

        calls = {"n": 0, "prompts": 0}

        def setup(h):
            def impasse(*a, **k):
                calls["n"] += 1
                if calls["n"] > STUCK_THRESHOLD * 3:
                    h.should_stop = True
                # Alternate: impasse, clear, impasse, clear ... so the
                # counter never reaches the threshold.
                return (calls["n"] % 2 == 1, "flapping")

            h.agents = [_agent()]
            h.impasse = None
            h._impasse_fn = impasse

        harness = _LoopHarness()
        setup(harness)
        sdk = MagicMock()
        sdk.start_workflow.return_value = "exec-1"

        def counting_prompt(*a, **k):
            calls["prompts"] += 1
            return "c"

        with harness:
            with (
                patch("src.autopilot.orchestrator.pipeline.detect_impasse", side_effect=harness._impasse_fn),
                patch("src.autopilot.orchestrator.pipeline.prompt_human", side_effect=counting_prompt),
            ):
                result = run_single_workflow(
                    sdk=sdk,
                    workflow_id="autopilot",
                    project_path="/tmp/does-not-exist-project",
                    description="d",
                    logger=MagicMock(),
                )

        assert result is FeatureRunStatus.INTERRUPTED
        assert calls["prompts"] == 0


class TestCredits:
    def test_out_of_credits_quit_returns_interrupted(self):
        def setup(h):
            h.agents = [_agent()]
            h.credits = (True, "out of credits")
            h.prompt_response = "q"

        assert _run(setup) is FeatureRunStatus.INTERRUPTED

    def test_credit_check_short_circuits_before_impasse_detection(self):
        """The credit branch ends in `continue`, so detect_impasse is never
        consulted while credits are exhausted. Without that short-circuit an
        out-of-credits workflow -- which necessarily looks stalled, since
        nothing can run -- would also accumulate impasse strikes and
        escalate to a second, spurious human prompt for a problem the
        operator has already been told about.

        Uses the "continue watching" answer specifically: a "q"/"s" answer
        returns from the credit branch outright, which would mask a missing
        short-circuit.
        """
        seen = {"impasse_calls": 0, "cycles": 0}

        harness = _LoopHarness()
        harness.agents = [_agent()]
        harness.credits = (True, "out of credits")
        harness.prompt_response = "c"

        def counting_impasse(*a, **k):
            seen["impasse_calls"] += 1
            return (True, "would escalate")

        def workflow_status(wid):
            seen["cycles"] += 1
            if seen["cycles"] > 4:
                harness.should_stop = True
            return {"status": "active"}

        sdk = MagicMock()
        sdk.start_workflow.return_value = "exec-1"

        with harness:
            with (
                patch("src.autopilot.orchestrator.pipeline.detect_impasse", side_effect=counting_impasse),
                patch("src.autopilot.orchestrator.pipeline.get_workflow_status", side_effect=workflow_status),
            ):
                result = run_single_workflow(
                    sdk=sdk,
                    workflow_id="autopilot",
                    project_path="/tmp/does-not-exist-project",
                    description="d",
                    logger=MagicMock(),
                )

        assert result is FeatureRunStatus.INTERRUPTED
        assert seen["impasse_calls"] == 0


class TestCleanupAlwaysRuns:
    def test_agents_terminated_and_workflow_paused_on_exit(self):
        """The finally block must terminate this workflow's still-active
        agents and pause a still-active workflow, on every exit path."""
        harness = _LoopHarness()
        harness.agents = [_agent("agent-live", status="working")]
        harness.should_stop = True
        sdk = MagicMock()
        sdk.start_workflow.return_value = "exec-1"

        with harness:
            with (
                patch("src.autopilot.orchestrator.pipeline.terminate_agent_direct") as term,
                patch("src.autopilot.orchestrator.pipeline.pause_workflow_direct") as pause,
                patch("src.autopilot.orchestrator.pipeline._unregister_monitored_workflow") as unreg,
            ):
                result = run_single_workflow(
                    sdk=sdk,
                    workflow_id="autopilot",
                    project_path="/tmp/does-not-exist-project",
                    description="d",
                    logger=MagicMock(),
                )

        assert result is FeatureRunStatus.INTERRUPTED
        term.assert_called_once_with("agent-live")
        pause.assert_called_once_with("exec-1")
        unreg.assert_called_once_with("exec-1")

    def test_launch_failure_returns_failed_without_entering_loop(self):
        """A failed sdk.start_workflow returns FAILED directly -- the loop,
        and therefore the monitored-workflow registration, is never reached."""
        sdk = MagicMock()
        sdk.start_workflow.side_effect = RuntimeError("launch exploded")

        harness = _LoopHarness()
        with harness:
            with patch("src.autopilot.orchestrator.pipeline._register_monitored_workflow") as reg:
                result = run_single_workflow(
                    sdk=sdk,
                    workflow_id="autopilot",
                    project_path="/tmp/does-not-exist-project",
                    description="d",
                    logger=MagicMock(),
                )

        assert result is FeatureRunStatus.FAILED
        reg.assert_not_called()

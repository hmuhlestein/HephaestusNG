"""Task.depends_on/parallel_group were written on create_task and never read
again. A task with an unmet dependency dispatched an agent immediately, the
same as one with none -- the ordering architecture_design.yaml's prompt
teaches agents to build with these fields (a "handlers" group waiting for a
"types" group to finish) was purely advisory, resting entirely on the
creating agent choosing WHEN to call create_task rather than on any system
guarantee.

Two halves, tested separately:
  - _has_unmet_dependencies: the gate, checked at creation time.
  - _dispatch_ready_dependents: the promotion, fired whenever a task reaches
    "done" (from any of its three transition sites), which re-checks every
    pending sibling and dispatches the ones whose dependencies just cleared.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.core.database import Task, Workflow


@pytest.fixture
def _wired_server_state(db_manager, monkeypatch):
    import src.mcp.server._create_task_steps as steps

    monkeypatch.setattr(steps.server_state, "db_manager", db_manager)
    return steps.server_state


def _seed_workflow(db_manager, workflow_id):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(id=workflow_id, name="w", phases_folder_path="/tmp", status="active")
        )


def _seed_task(db_manager, task_id, workflow_id, status="pending", depends_on=None, **extra):
    with db_manager.session_scope() as session:
        session.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description=extra.pop("raw_description", "x"),
                enriched_description=extra.pop("enriched_description", "x enriched"),
                done_definition=extra.pop("done_definition", "x"),
                status=status,
                depends_on=depends_on,
                **extra,
            )
        )


class TestHasUnmetDependencies:
    def test_none_or_empty_has_no_unmet_dependencies(self, db_manager, _wired_server_state):
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        assert _has_unmet_dependencies(None) is False
        assert _has_unmet_dependencies([]) is False

    def test_a_done_dependency_is_satisfied(self, db_manager, _wired_server_state):
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        wf = str(uuid.uuid4())
        dep_id = str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_id, wf, status="done")

        assert _has_unmet_dependencies([dep_id]) is False

    @pytest.mark.parametrize(
        "status", ["pending", "assigned", "in_progress", "queued", "blocked", "needs_work"]
    )
    def test_a_not_yet_done_dependency_is_unmet(self, db_manager, _wired_server_state, status):
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        wf = str(uuid.uuid4())
        dep_id = str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_id, wf, status=status)

        assert _has_unmet_dependencies([dep_id]) is True

    def test_a_failed_dependency_stays_unmet_forever(self, db_manager, _wired_server_state):
        """Deliberate: a failed prerequisite must never be treated as
        satisfying anything downstream. Proceeding on top of a known-broken
        dependency is the less safe default, not an oversight."""
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        wf = str(uuid.uuid4())
        dep_id = str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_id, wf, status="failed")

        assert _has_unmet_dependencies([dep_id]) is True

    def test_an_unknown_dependency_id_fails_closed(self, db_manager, _wired_server_state):
        """A vanished/typo'd dependency id is not the same as a satisfied
        one -- must not silently let the task through."""
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        assert _has_unmet_dependencies(["no-such-task-id"]) is True

    def test_all_of_several_dependencies_must_be_done(self, db_manager, _wired_server_state):
        from src.mcp.server._create_task_steps import _has_unmet_dependencies

        wf = str(uuid.uuid4())
        done_id, pending_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, done_id, wf, status="done")
        _seed_task(db_manager, pending_id, wf, status="pending")

        assert _has_unmet_dependencies([done_id, pending_id]) is True
        assert _has_unmet_dependencies([done_id]) is False


class TestCreationTimeGateIsWired:
    """process_task_async (agent_task_routes.py) must check
    _has_unmet_dependencies(request.depends_on) AFTER the duplicate check
    (a duplicate must still be deduped regardless of its dependency state)
    and BEFORE both the capacity queue and dispatch (an unready task must
    not be capacity-queued either -- that queue means "next in line once a
    slot frees", which is the wrong promise for a task that should not run
    even with a free slot). Checked by source inspection, matching this
    suite's own established convention for these endpoint-level guards
    (test_create_task_guards.py does not drive the real LLM/RAG pipeline
    either) rather than a heavier end-to-end dispatch test.

    Full behavioral coverage of the gate ITSELF (what counts as unmet, fail
    -closed on an unknown id, a failed dependency never satisfying anything)
    lives in TestHasUnmetDependencies above; this class only verifies
    process_task_async actually calls it, in the right order."""

    @staticmethod
    def _process_task_async_source() -> str:
        import inspect

        import src.mcp.server.agent_task_routes as routes

        return inspect.getsource(routes.create_task)

    def test_gate_is_called_with_the_requests_own_depends_on(self):
        src_text = self._process_task_async_source()
        assert "_has_unmet_dependencies(request.depends_on)" in src_text

    def test_gate_runs_after_the_duplicate_check(self):
        src_text = self._process_task_async_source()
        dup_at = src_text.index("_check_for_duplicate_task(")
        gate_at = src_text.index("_has_unmet_dependencies(request.depends_on)")
        assert dup_at < gate_at

    def test_gate_runs_before_the_capacity_queue_and_dispatch(self):
        src_text = self._process_task_async_source()
        gate_at = src_text.index("_has_unmet_dependencies(request.depends_on)")
        queue_at = src_text.index("_maybe_queue_task_at_capacity(")
        dispatch_at = src_text.index("_dispatch_agent_for_task(")
        assert gate_at < queue_at < dispatch_at

    def test_gate_true_branch_returns_without_dispatching(self):
        """The branch body must be a bare early return -- no call to the
        capacity queue or dispatch inside it."""
        src_text = self._process_task_async_source()
        gate_at = src_text.index("if _has_unmet_dependencies(request.depends_on):")
        next_stmt_at = src_text.index("_maybe_queue_task_at_capacity(", gate_at)
        branch_body = src_text[gate_at:next_stmt_at]
        assert "return" in branch_body
        assert "_dispatch_agent_for_task(" not in branch_body


class TestDispatchReadyDependents:
    """The promotion half: when a task reaches "done", its dependents get
    re-checked and dispatched if now ready."""

    @pytest.mark.asyncio
    async def test_dispatches_a_dependent_whose_only_dependency_just_finished(
        self, db_manager, _wired_server_state
    ):
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        wf = str(uuid.uuid4())
        dep_id, dependent_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_id, wf, status="done")
        _seed_task(db_manager, dependent_id, wf, status="pending", depends_on=[dep_id])

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            await _dispatch_ready_dependents(dep_id, wf)

        dispatch_mock.assert_called_once()
        assert dispatch_mock.call_args[0][0]["id"] == dependent_id

    @pytest.mark.asyncio
    async def test_does_not_dispatch_while_a_sibling_dependency_is_still_pending(
        self, db_manager, _wired_server_state
    ):
        """A task depending on TWO tasks must wait for both, not just the one
        that happened to trigger this sweep."""
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        wf = str(uuid.uuid4())
        dep_a, dep_b, dependent_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_a, wf, status="done")
        _seed_task(db_manager, dep_b, wf, status="in_progress")
        _seed_task(db_manager, dependent_id, wf, status="pending", depends_on=[dep_a, dep_b])

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            await _dispatch_ready_dependents(dep_a, wf)

        dispatch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_pending_tasks_with_no_relation_to_the_completed_task(
        self, db_manager, _wired_server_state
    ):
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        wf = str(uuid.uuid4())
        completed_id, unrelated_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, completed_id, wf, status="done")
        _seed_task(db_manager, unrelated_id, wf, status="pending", depends_on=None)

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            await _dispatch_ready_dependents(completed_id, wf)

        dispatch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_a_dependent_belonging_to_a_different_workflow(
        self, db_manager, _wired_server_state
    ):
        """depends_on ids are only ever meaningful within one workflow's own
        subtasks -- scoping the candidate query by workflow_id is a real
        correctness boundary, not just an optimization."""
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        wf_a, wf_b = str(uuid.uuid4()), str(uuid.uuid4())
        dep_id, other_workflow_task = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf_a)
        _seed_workflow(db_manager, wf_b)
        _seed_task(db_manager, dep_id, wf_a, status="done")
        _seed_task(db_manager, other_workflow_task, wf_b, status="pending", depends_on=[dep_id])

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            await _dispatch_ready_dependents(dep_id, wf_a)

        dispatch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dispatch_failure_for_one_sibling_does_not_stop_the_others(
        self, db_manager, _wired_server_state
    ):
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        wf = str(uuid.uuid4())
        dep_id, ok_id, broken_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        _seed_workflow(db_manager, wf)
        _seed_task(db_manager, dep_id, wf, status="done")
        _seed_task(db_manager, ok_id, wf, status="pending", depends_on=[dep_id])
        _seed_task(db_manager, broken_id, wf, status="pending", depends_on=[dep_id])

        calls = []

        async def _dispatch_side_effect(task_data):
            calls.append(task_data["id"])
            if task_data["id"] == broken_id:
                raise RuntimeError("dispatch blew up")

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(side_effect=_dispatch_side_effect),
        ):
            await _dispatch_ready_dependents(dep_id, wf)

        assert set(calls) == {ok_id, broken_id}

    @pytest.mark.asyncio
    async def test_no_workflow_id_is_a_safe_no_op(self, db_manager, _wired_server_state):
        from src.mcp.server._create_task_steps import _dispatch_ready_dependents

        with patch(
            "src.mcp.server._create_task_steps._dispatch_or_queue_promoted_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            await _dispatch_ready_dependents("some-task-id", None)

        dispatch_mock.assert_not_called()


class TestCompletionSitesFireOnTheActualOutcome:
    """All three places a task can reach "done" must trigger promotion --
    and only when the task genuinely ended up done, not merely requested.

    verify_output_survived_commit can flip task.status to "failed" in place
    (same ORM object, same session) after a "done" request, if the declared
    output vanished after commit. request.status still reads "done" when
    that happens (it is the caller's original, never-mutated request), so
    the trigger must key off task.status -- the actual outcome -- not
    request.status, or it would promote dependents of a task that was just
    rejected.
    """

    def test_complete_task_normally_keys_off_task_status_not_request_status(self):
        import inspect

        import src.mcp.server._update_task_status_steps as steps

        src_text = inspect.getsource(steps._complete_task_normally)
        assert '_dispatch_ready_dependents(task.id, task.workflow_id)' in src_text
        # The exact bug: this must NOT be gated on request.status, which
        # stays "done" even after verify_output_survived_commit rejects it.
        assert 'if task.status == "done":' in src_text
        assert (
            'if request.status == "done":\n        # Fires the dependency-promotion'
            not in src_text
        )

    def test_human_completion_endpoint_fires_promotion(self):
        import inspect

        import src.mcp.server.task_admin_routes as routes

        src_text = inspect.getsource(routes)
        assert "_dispatch_ready_dependents(task_id, workflow_id)" in src_text

    def test_orphan_recovery_path_fires_promotion(self):
        import inspect

        import src.mcp.server.lifecycle as lifecycle

        src_text = inspect.getsource(lifecycle)
        assert "_dispatch_ready_dependents(task.id, task.workflow_id)" in src_text

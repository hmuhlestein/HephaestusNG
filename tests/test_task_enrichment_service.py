"""Tests for TaskEnrichmentService — specifically the phase resolution
behavior that changed during the SOLID refactor (finding #1 in
docs/SOLID_REFACTOR_ADVERSARIAL_REVIEW.md).

The key behavior change: process_queue now passes workflow_id to
resolve_phase_id, scoping phase resolution to the task's own workflow
instead of the phase_manager singleton's active workflow. This test
verifies that behavior is correct.
"""

import uuid
from unittest.mock import MagicMock, patch


class TestResolvePhaseId:
    """Tests for TaskEnrichmentService.resolve_phase_id."""

    def _make_mock_server_state(self, phase_manager=None):
        """Create a mock server_state with a phase_manager."""
        state = MagicMock()
        state.phase_manager = phase_manager or MagicMock()
        return state

    @patch("src.core.app_context.get_app_state")
    def test_digit_phase_id_resolves_with_workflow_id(self, mock_get_state):
        """When phase_id_raw is a digit string, resolve_phase_id should
        pass workflow_id to get_phase_for_task (the behavior change from #1)."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        expected_phase_uuid = str(uuid.uuid4())
        pm.get_phase_for_task.return_value = expected_phase_uuid

        mock_get_state.return_value = self._make_mock_server_state(pm)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="2",
            phase_order=None,
            workflow_id="wf-abc-123",
            requesting_agent_id="system",
        )

        assert result == expected_phase_uuid
        pm.get_phase_for_task.assert_called_once_with(
            phase_id=None,
            order=2,
            requesting_agent_id="system",
            workflow_id="wf-abc-123",
        )

    @patch("src.core.app_context.get_app_state")
    def test_uuid_phase_id_returned_as_is(self, mock_get_state):
        """When phase_id_raw is already a UUID, it should be returned directly."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        mock_get_state.return_value = self._make_mock_server_state(pm)

        uuid_str = str(uuid.uuid4())
        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw=uuid_str,
            phase_order=None,
            workflow_id="wf-abc-123",
            requesting_agent_id="system",
        )

        assert result == uuid_str
        pm.get_phase_for_task.assert_not_called()

    @patch("src.core.app_context.get_app_state")
    def test_no_phase_id_uses_phase_order(self, mock_get_state):
        """When phase_id_raw is None, phase_order is used as fallback."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        expected_phase_uuid = str(uuid.uuid4())
        pm.get_phase_for_task.return_value = expected_phase_uuid

        mock_get_state.return_value = self._make_mock_server_state(pm)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw=None,
            phase_order=3,
            workflow_id="wf-abc-123",
            requesting_agent_id="system",
        )

        assert result == expected_phase_uuid
        pm.get_phase_for_task.assert_called_once_with(
            phase_id=None,
            order=3,
            requesting_agent_id="system",
            workflow_id="wf-abc-123",
        )

    @patch("src.core.app_context.get_app_state")
    def test_different_workflow_ids_resolve_differently(self, mock_get_state):
        """FIX #1: Verify that passing different workflow_ids produces
        different phase resolutions — this is the multi-workflow scenario
        that the refactor fixed."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        phase_wf1 = str(uuid.uuid4())
        phase_wf2 = str(uuid.uuid4())

        def mock_get_phase(phase_id, order, requesting_agent_id, workflow_id):
            if workflow_id == "wf-1":
                return phase_wf1
            elif workflow_id == "wf-2":
                return phase_wf2
            return None

        pm.get_phase_for_task.side_effect = mock_get_phase
        mock_get_state.return_value = self._make_mock_server_state(pm)

        # Resolve for workflow 1
        result1 = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="1",
            phase_order=None,
            workflow_id="wf-1",
            requesting_agent_id="system",
        )

        # Resolve for workflow 2
        result2 = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="1",
            phase_order=None,
            workflow_id="wf-2",
            requesting_agent_id="system",
        )

        # They should resolve to different phase UUIDs
        assert result1 == phase_wf1
        assert result2 == phase_wf2
        assert result1 != result2

    @patch("src.core.app_context.get_app_state")
    def test_none_workflow_id_passed_through(self, mock_get_state):
        """When workflow_id is None, it should be passed through to
        get_phase_for_task (letting the phase manager use its default)."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        pm.get_phase_for_task.return_value = str(uuid.uuid4())
        mock_get_state.return_value = self._make_mock_server_state(pm)

        TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="1",
            phase_order=None,
            workflow_id=None,
            requesting_agent_id="system",
        )

        pm.get_phase_for_task.assert_called_once_with(
            phase_id=None,
            order=1,
            requesting_agent_id="system",
            workflow_id=None,
        )


class TestGetPhaseContextStr:
    """Tests for TaskEnrichmentService.get_phase_context_str."""

    @patch("src.core.app_context.get_app_state")
    def test_returns_empty_for_none_phase_id(self, mock_get_state):
        from src.services.task_enrichment_service import TaskEnrichmentService

        result = TaskEnrichmentService.get_phase_context_str(None)
        assert result == ("", None)

    @patch("src.core.app_context.get_app_state")
    def test_returns_context_and_workflow_id(self, mock_get_state):
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        phase_ctx = MagicMock()
        phase_ctx.to_prompt_context.return_value = "Phase context text"
        phase_ctx.workflow_id = "wf-456"
        pm.get_phase_context.return_value = phase_ctx

        mock_state = MagicMock()
        mock_state.phase_manager = pm
        mock_get_state.return_value = mock_state

        context_str, wf_id = TaskEnrichmentService.get_phase_context_str("phase-uuid-123")

        assert context_str == "Phase context text"
        assert wf_id == "wf-456"

    @patch("src.core.app_context.get_app_state")
    def test_returns_empty_when_no_context_found(self, mock_get_state):
        from src.services.task_enrichment_service import TaskEnrichmentService

        pm = MagicMock()
        pm.get_phase_context.return_value = None

        mock_state = MagicMock()
        mock_state.phase_manager = pm
        mock_get_state.return_value = mock_state

        context_str, wf_id = TaskEnrichmentService.get_phase_context_str("nonexistent-phase")

        assert context_str == ""
        assert wf_id is None


class TestResolvePhaseIdNameLookup:
    """Regression tests for the name-lookup branch added 2026-08-19.

    Before this, resolve_phase_id's non-digit branch returned phase_id_raw
    completely unvalidated -- any non-digit string was assumed to already
    be a real Phase.id UUID. An agent passing a phase NAME (e.g.
    "adversarial_review", the natural, common way to write a follow-up
    task) sailed through unresolved, and the later `UPDATE tasks SET
    phase_id=...` failed with a live FOREIGN KEY constraint violation --
    silently, since that write happens inside a fire-and-forget
    background task. Confirmed live: three tasks in the production DB
    stuck permanently at status="pending" with phase_id empty, from
    exactly this path.

    These use a real db_manager (not a mocked session) because the fix's
    entire job is a real DB query -- a mocked session's auto-truthy
    MagicMock would make "phase exists" and "phase doesn't exist" return
    the same thing, which is exactly how the old passthrough behavior
    hid behind test_uuid_phase_id_returned_as_is without anyone noticing
    it wasn't really validating anything.
    """

    def _make_workflow_and_phase(self, db_manager, *, workflow_id, phase_name, phase_id=None):
        from src.core.database import Phase, Workflow

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id=workflow_id, name="w", phases_folder_path="/tmp", status="active",
                )
            )
            phase_id = phase_id or str(uuid.uuid4())
            session.add(
                Phase(
                    id=phase_id, workflow_id=workflow_id, order=1, name=phase_name,
                    description="d", done_definitions=[],
                )
            )
        return phase_id

    def _mock_state(self, db_manager, *, phase_manager_workflow_id=None):
        state = MagicMock()
        state.db_manager = db_manager
        state.phase_manager = MagicMock()
        state.phase_manager.workflow_id = phase_manager_workflow_id
        return state

    @patch("src.core.app_context.get_app_state")
    def test_a_real_phase_uuid_is_returned_without_a_name_lookup(self, mock_get_state, db_manager):
        from src.services.task_enrichment_service import TaskEnrichmentService

        phase_id = self._make_workflow_and_phase(
            db_manager, workflow_id="wf-1", phase_name="development"
        )
        mock_get_state.return_value = self._mock_state(db_manager)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw=phase_id, phase_order=None, workflow_id="wf-1",
            requesting_agent_id="system",
        )

        assert result == phase_id

    @patch("src.core.app_context.get_app_state")
    def test_a_phase_name_resolves_to_its_uuid(self, mock_get_state, db_manager):
        """The exact live bug: an agent passes "adversarial_review" (a
        name), not a UUID."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        phase_id = self._make_workflow_and_phase(
            db_manager, workflow_id="wf-1", phase_name="adversarial_review"
        )
        mock_get_state.return_value = self._mock_state(db_manager)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="adversarial_review", phase_order=None, workflow_id="wf-1",
            requesting_agent_id="system",
        )

        assert result == phase_id

    @patch("src.core.app_context.get_app_state")
    def test_a_phase_name_is_scoped_to_the_given_workflow_id(self, mock_get_state, db_manager):
        """Two different workflows both have a "development" phase --
        must resolve to the one in the workflow actually passed in, not
        whichever row a plain name query happens to find first."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        self._make_workflow_and_phase(db_manager, workflow_id="wf-a", phase_name="development")
        phase_id_b = self._make_workflow_and_phase(
            db_manager, workflow_id="wf-b", phase_name="development"
        )
        mock_get_state.return_value = self._mock_state(db_manager)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="development", phase_order=None, workflow_id="wf-b",
            requesting_agent_id="system",
        )

        assert result == phase_id_b

    @patch("src.core.app_context.get_app_state")
    def test_a_phase_name_falls_back_to_the_phase_managers_workflow_when_none_given(
        self, mock_get_state, db_manager
    ):
        from src.services.task_enrichment_service import TaskEnrichmentService

        phase_id = self._make_workflow_and_phase(
            db_manager, workflow_id="wf-current", phase_name="qa_validation"
        )
        mock_get_state.return_value = self._mock_state(
            db_manager, phase_manager_workflow_id="wf-current"
        )

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="qa_validation", phase_order=None, workflow_id=None,
            requesting_agent_id="system",
        )

        assert result == phase_id

    @patch("src.core.app_context.get_app_state")
    def test_an_unresolvable_name_returns_none_instead_of_the_raw_string(
        self, mock_get_state, db_manager
    ):
        """The critical regression this whole fix is about: the old code
        returned the raw string here, which then failed the FK constraint
        downstream. Returning None makes the caller's own "no phase_id
        determined" warning path handle it instead of a silent DB
        failure three steps later."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        mock_get_state.return_value = self._mock_state(db_manager)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="totally-not-a-real-phase", phase_order=None, workflow_id="wf-1",
            requesting_agent_id="system",
        )

        assert result is None

    @patch("src.core.app_context.get_app_state")
    def test_an_ambiguous_name_with_no_workflow_scope_returns_none(self, mock_get_state, db_manager):
        """Same name in two different workflows, no workflow_id to
        narrow it -- refuse to guess rather than silently picking one."""
        from src.services.task_enrichment_service import TaskEnrichmentService

        self._make_workflow_and_phase(db_manager, workflow_id="wf-a", phase_name="development")
        self._make_workflow_and_phase(db_manager, workflow_id="wf-b", phase_name="development")
        mock_get_state.return_value = self._mock_state(db_manager)

        result = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw="development", phase_order=None, workflow_id=None,
            requesting_agent_id="system",
        )

        assert result is None


class TestNormalizeEnrichedDescription:
    """Tests for TaskEnrichmentService._normalize_enriched_description."""

    def test_none_becomes_fallback(self):
        from src.services.task_enrichment_service import TaskEnrichmentService

        task = {"enriched_description": None}
        TaskEnrichmentService._normalize_enriched_description(task, "fallback text")
        assert task["enriched_description"] == "fallback text"

    def test_dict_becomes_json_string(self):
        from src.services.task_enrichment_service import TaskEnrichmentService

        task = {"enriched_description": {"key": "value"}}
        TaskEnrichmentService._normalize_enriched_description(task, "fallback")
        assert task["enriched_description"] == '{\n  "key": "value"\n}'

    def test_string_unchanged(self):
        from src.services.task_enrichment_service import TaskEnrichmentService

        task = {"enriched_description": "already a string"}
        TaskEnrichmentService._normalize_enriched_description(task, "fallback")
        assert task["enriched_description"] == "already a string"

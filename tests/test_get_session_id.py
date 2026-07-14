"""Tests for autopilot.phases.get_session_id.

Regression: pi's --session-id resume permanently pins whatever model the
session was FIRST created with -- a --model flag passed on a later resume
is silently ignored (confirmed live: a session's own file had exactly one
modelId entry, recorded at creation, unchanged across 343 subsequent
turns). Before this fix, get_session_id's hash didn't include the model,
so switching the configured model (e.g. off one whose output-token
ceiling turned out too small) had zero effect on any EXISTING session for
a role that had already been used -- every goto back to that role kept
resuming the stale session on the old model forever.
"""

from src.autopilot.phases import get_session_id


class TestGetSessionId:
    def test_same_inputs_produce_same_id(self):
        a = get_session_id("proj-1", "my-design", "development", model="mimo-v2.5-pro")
        b = get_session_id("proj-1", "my-design", "development", model="mimo-v2.5-pro")
        assert a == b

    def test_different_model_produces_different_id(self):
        """The core fix: a model change must break session continuity so
        pi starts fresh instead of resuming a session pinned to the old
        model."""
        old = get_session_id("proj-1", "my-design", "development", model="mimo-v2.5")
        new = get_session_id("proj-1", "my-design", "development", model="mimo-v2.5-pro")
        assert old != new

    def test_different_role_produces_different_id(self):
        a = get_session_id("proj-1", "my-design", "development", model="m")
        b = get_session_id("proj-1", "my-design", "qa_validation", model="m")
        assert a != b

    def test_different_project_produces_different_id(self):
        a = get_session_id("proj-1", "my-design", "development", model="m")
        b = get_session_id("proj-2", "my-design", "development", model="m")
        assert a != b

    def test_default_empty_model_is_stable(self):
        """Backward compatible: omitting model still produces a consistent,
        deterministic id (relevant for any caller that doesn't yet know
        its model at call time)."""
        a = get_session_id("proj-1", "my-design", "development")
        b = get_session_id("proj-1", "my-design", "development")
        assert a == b

    def test_shared_session_role_reuses_same_id_across_phases(self):
        """architecture_design and architectural_review share the
        "architect" session role -- same model must still produce the
        same session_id across both phase names, preserving the whole
        point of session continuity."""
        a = get_session_id(
            "proj-1", "my-design", "architecture_design", model="mimo-v2.5-pro"
        )
        b = get_session_id(
            "proj-1", "my-design", "architectural_review", model="mimo-v2.5-pro"
        )
        assert a == b

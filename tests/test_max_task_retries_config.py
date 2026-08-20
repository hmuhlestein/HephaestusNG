"""Regression: workflow.yaml's orchestrator.max_task_retries must be honored.

The retry paths in phase_transitions.py were written to read this setting via
`spec.load_workflow_definition(...)` -- a function that has never existed in
spec.py. Each of the four call sites sat inside `except Exception: max_retry
= 5`, so the ImportError was swallowed every time and the configured value
was never consulted.

It went unnoticed because the shipped config sets max_task_retries: 5, the
same number as the hardcoded fallback -- so the setting appeared to work
while actually being inert. Changing it in workflow.yaml did nothing. Found
by mypy ("Module src.autopilot.spec has no attribute load_workflow_definition"
x4) once c38f143 unblocked it.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.autopilot.spec import (
    _MAX_TASK_RETRIES_CACHE,
    DEFAULT_MAX_TASK_RETRIES,
    get_max_task_retries,
)


@pytest.fixture(autouse=True)
def clear_cache():
    _MAX_TASK_RETRIES_CACHE.clear()
    yield
    _MAX_TASK_RETRIES_CACHE.clear()


def _db_with_definition(definition_id):
    workflow = MagicMock()
    workflow.definition_id = definition_id
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = workflow
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


def _write_workflow_yaml(root, definition_id, body):
    wf_dir = root / definition_id
    wf_dir.mkdir(parents=True)
    (wf_dir / "workflow.yaml").write_text(body)
    return root


def test_configured_value_is_used_not_the_fallback(tmp_path):
    """The load-bearing assertion: a workflow.yaml value that differs from
    the hardcoded default must actually take effect."""
    root = _write_workflow_yaml(
        tmp_path, "custom", "orchestrator:\n  max_task_retries: 11\n"
    )

    with (
        patch("src.core.database.get_db", return_value=_db_with_definition("custom")),
        patch("src.workflow_registry._WORKFLOWS_DIR", root),
    ):
        assert get_max_task_retries("wf-1") == 11


def test_the_real_autopilot_config_is_readable():
    """Guards the actual shipped config rather than a synthetic one -- if the
    key is renamed or moved, this fails instead of silently reverting to the
    default the old code always used."""
    import yaml

    from src.workflow_registry import _WORKFLOWS_DIR

    workflow_yaml = _WORKFLOWS_DIR / "autopilot" / "workflow.yaml"
    assert workflow_yaml.exists()
    config = yaml.safe_load(workflow_yaml.read_text())
    assert "max_task_retries" in (config or {}).get("orchestrator", {})


def test_missing_key_falls_back_to_the_default(tmp_path):
    root = _write_workflow_yaml(tmp_path, "bare", "orchestrator:\n  other: 1\n")

    with (
        patch("src.core.database.get_db", return_value=_db_with_definition("bare")),
        patch("src.workflow_registry._WORKFLOWS_DIR", root),
    ):
        assert get_max_task_retries("wf-1") == DEFAULT_MAX_TASK_RETRIES


def test_no_workflow_id_falls_back_to_the_default():
    assert get_max_task_retries(None) == DEFAULT_MAX_TASK_RETRIES


def test_a_db_failure_falls_back_rather_than_raising(tmp_path):
    """A retry decision must not blow up over a config read."""
    with patch("src.core.database.get_db", side_effect=RuntimeError("db down")):
        assert get_max_task_retries("wf-1") == DEFAULT_MAX_TASK_RETRIES


def test_non_integer_config_is_ignored(tmp_path):
    root = _write_workflow_yaml(
        tmp_path, "bad", "orchestrator:\n  max_task_retries: 'lots'\n"
    )

    with (
        patch("src.core.database.get_db", return_value=_db_with_definition("bad")),
        patch("src.workflow_registry._WORKFLOWS_DIR", root),
    ):
        assert get_max_task_retries("wf-1") == DEFAULT_MAX_TASK_RETRIES

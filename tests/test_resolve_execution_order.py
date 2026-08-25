"""Tests for _resolve_execution_order function."""

from unittest.mock import MagicMock

import pytest

from src.autopilot.orchestrator.features import _resolve_execution_order


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    return logger


class TestResolveExecutionOrder:
    """Test cases for _resolve_execution_order."""

    def test_single_feature(self, mock_logger):
        """Test with a single feature."""
        features = [
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "parallel"}
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        assert len(result) == 1
        assert len(result[0]) == 1
        assert result[0][0]["id"] == "auth"
        assert layers == [0]

    def test_parallel_features(self, mock_logger):
        """Test parallel features at same depth."""
        features = [
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "parallel"},
            {"id": "api", "name": "API", "depends_on": [], "execution": "parallel"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        assert len(result) == 1
        assert len(result[0]) == 2
        ids = {f["id"] for f in result[0]}
        assert ids == {"auth", "api"}
        assert layers == [0]

    def test_sequential_features(self, mock_logger):
        """Test sequential features."""
        features = [
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "sequential"},
            {"id": "api", "name": "API", "depends_on": [], "execution": "sequential"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        assert len(result) == 2
        assert result[0][0]["id"] == "auth"
        assert result[1][0]["id"] == "api"
        # Same depth, no dependency relationship -- both groups share a
        # layer even though the grouping logic still splits sequential
        # features into their own groups. See test_group_layers_* below
        # for the behavior this enables.
        assert layers == [0, 0]

    def test_dependency_order(self, mock_logger):
        """Test features with dependencies."""
        features = [
            {
                "id": "api",
                "name": "API",
                "depends_on": ["auth"],
                "execution": "parallel",
            },
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "parallel"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        # auth should come first
        assert result[0][0]["id"] == "auth"
        assert result[1][0]["id"] == "api"
        assert layers == [0, 1]

    def test_complex_dependency_graph(self, mock_logger):
        """Test complex dependency graph."""
        features = [
            {"id": "c", "name": "C", "depends_on": ["a", "b"], "execution": "parallel"},
            {"id": "a", "name": "A", "depends_on": [], "execution": "parallel"},
            {"id": "b", "name": "B", "depends_on": ["a"], "execution": "parallel"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        # Should have 3 groups: a, b, c
        assert len(result) == 3
        assert result[0][0]["id"] == "a"
        assert result[1][0]["id"] == "b"
        assert result[2][0]["id"] == "c"
        assert layers == [0, 1, 2]

    def test_cycle_detection(self, mock_logger):
        """Test cycle detection falls back to sequential."""
        features = [
            {"id": "a", "name": "A", "depends_on": ["b"], "execution": "parallel"},
            {"id": "b", "name": "B", "depends_on": ["a"], "execution": "parallel"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        # Should fall back to sequential
        assert len(result) == 2
        # Each cyclic remainder gets its own fresh layer -- no
        # dependency-safety guarantee exists for them, unlike a real
        # Kahn layer, so they must not be merged for concurrent execution.
        assert layers == [0, 1]

    def test_empty_features(self, mock_logger):
        """Test with empty features list."""
        features = []
        result, layers = _resolve_execution_order(features, mock_logger)
        assert len(result) == 0
        assert layers == []

    def test_mixed_parallel_sequential(self, mock_logger):
        """Test mix of parallel and sequential features.

        Ordering within a dependency layer follows the architect's original
        features.json order -- a "sequential" feature gets its own group in
        place rather than every "parallel" feature in the layer running
        first regardless of list position. Here b (sequential) sits between
        a and c (both parallel), so it splits them into separate groups:
        [a], [b], [c] -- not [a, c] batched together with [b] pushed last.
        """
        features = [
            {"id": "a", "name": "A", "depends_on": [], "execution": "parallel"},
            {"id": "b", "name": "B", "depends_on": [], "execution": "sequential"},
            {"id": "c", "name": "C", "depends_on": [], "execution": "parallel"},
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        assert len(result) == 3
        assert result[0][0]["id"] == "a"
        assert result[1][0]["id"] == "b"
        assert result[2][0]["id"] == "c"
        # All three groups share a layer -- none of a/b/c depends on
        # either of the others, despite the split into separate groups.
        assert layers == [0, 0, 0]

    def test_group_layers_distinguishes_same_layer_from_dependent_groups(
        self, mock_logger
    ):
        """Regression, observed live: feature frontend-multi-repo
        (sequential, depends on commit-resolution + project-repo-api, both
        long since completed) sat untouched for hours behind an unrelated,
        still-running feature -- same dependency layer, zero actual
        dependency relationship, but listed earlier in features.json, so
        the grouping split them into separate groups and the old
        strictly-sequential consumer loop waited on the unrelated one
        anyway. group_layers is what lets a caller (run_feature_pipelines)
        tell "these two groups can run concurrently" (same layer) apart
        from "this group genuinely must wait" (a later layer, depending on
        something in an earlier one)."""
        features = [
            {"id": "root", "name": "Root", "depends_on": [], "execution": "parallel"},
            {
                "id": "unrelated-long-runner",
                "name": "Unrelated",
                "depends_on": ["root"],
                "execution": "parallel",
            },
            {
                "id": "frontend-multi-repo",
                "name": "Frontend",
                "depends_on": ["root"],
                "execution": "sequential",
            },
        ]
        result, layers = _resolve_execution_order(features, mock_logger)
        # unrelated-long-runner and frontend-multi-repo both depend only
        # on root (layer 0) -- they belong in layer 1 together, in
        # separate groups (one parallel, one sequential), but sharing a
        # layer index that tells a caller they have no dependency on each
        # other and are safe to run at the same time.
        group_ids = [[f["id"] for f in group] for group in result]
        assert group_ids == [["root"], ["unrelated-long-runner"], ["frontend-multi-repo"]]
        assert layers == [0, 1, 1]

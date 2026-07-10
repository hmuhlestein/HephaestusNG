"""Tests for _resolve_execution_order function."""

from unittest.mock import MagicMock

import pytest

from src.autopilot.orchestrator import _resolve_execution_order


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
        result = _resolve_execution_order(features, mock_logger)
        assert len(result) == 1
        assert len(result[0]) == 1
        assert result[0][0]["id"] == "auth"

    def test_parallel_features(self, mock_logger):
        """Test parallel features at same depth."""
        features = [
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "parallel"},
            {"id": "api", "name": "API", "depends_on": [], "execution": "parallel"},
        ]
        result = _resolve_execution_order(features, mock_logger)
        assert len(result) == 1
        assert len(result[0]) == 2
        ids = {f["id"] for f in result[0]}
        assert ids == {"auth", "api"}

    def test_sequential_features(self, mock_logger):
        """Test sequential features."""
        features = [
            {"id": "auth", "name": "Auth", "depends_on": [], "execution": "sequential"},
            {"id": "api", "name": "API", "depends_on": [], "execution": "sequential"},
        ]
        result = _resolve_execution_order(features, mock_logger)
        assert len(result) == 2
        assert result[0][0]["id"] == "auth"
        assert result[1][0]["id"] == "api"

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
        result = _resolve_execution_order(features, mock_logger)
        # auth should come first
        assert result[0][0]["id"] == "auth"
        assert result[1][0]["id"] == "api"

    def test_complex_dependency_graph(self, mock_logger):
        """Test complex dependency graph."""
        features = [
            {"id": "c", "name": "C", "depends_on": ["a", "b"], "execution": "parallel"},
            {"id": "a", "name": "A", "depends_on": [], "execution": "parallel"},
            {"id": "b", "name": "B", "depends_on": ["a"], "execution": "parallel"},
        ]
        result = _resolve_execution_order(features, mock_logger)
        # Should have 3 groups: a, b, c
        assert len(result) == 3
        assert result[0][0]["id"] == "a"
        assert result[1][0]["id"] == "b"
        assert result[2][0]["id"] == "c"

    def test_cycle_detection(self, mock_logger):
        """Test cycle detection falls back to sequential."""
        features = [
            {"id": "a", "name": "A", "depends_on": ["b"], "execution": "parallel"},
            {"id": "b", "name": "B", "depends_on": ["a"], "execution": "parallel"},
        ]
        result = _resolve_execution_order(features, mock_logger)
        # Should fall back to sequential
        assert len(result) == 2

    def test_empty_features(self, mock_logger):
        """Test with empty features list."""
        features = []
        result = _resolve_execution_order(features, mock_logger)
        assert len(result) == 0

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
        result = _resolve_execution_order(features, mock_logger)
        assert len(result) == 3
        assert result[0][0]["id"] == "a"
        assert result[1][0]["id"] == "b"
        assert result[2][0]["id"] == "c"

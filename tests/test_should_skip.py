"""Tests for _should_skip function."""

from src.autopilot.orchestrator import _should_skip


class TestShouldSkip:
    """Test cases for _should_skip."""

    def test_no_dependencies(self):
        """Test feature with no dependencies."""
        feature = {
            "id": "auth",
            "depends_on": [],
        }
        feature_results = {}
        assert _should_skip(feature, feature_results) is False

    def test_dependency_completed(self):
        """Test feature with completed dependency."""
        feature = {
            "id": "api",
            "depends_on": ["auth"],
        }
        feature_results = {"auth": "completed"}
        assert _should_skip(feature, feature_results) is False

    def test_dependency_failed(self):
        """Test feature with failed dependency."""
        feature = {
            "id": "api",
            "depends_on": ["auth"],
        }
        feature_results = {"auth": "failed"}
        assert _should_skip(feature, feature_results) is True

    def test_dependency_skipped(self):
        """Test feature with skipped dependency."""
        feature = {
            "id": "api",
            "depends_on": ["auth"],
        }
        feature_results = {"auth": "skipped"}
        assert _should_skip(feature, feature_results) is False

    def test_dependency_pending(self):
        """Test feature with pending dependency."""
        feature = {
            "id": "api",
            "depends_on": ["auth"],
        }
        feature_results = {"auth": "pending"}
        assert _should_skip(feature, feature_results) is False

    def test_multiple_dependencies_one_failed(self):
        """Test feature with multiple dependencies, one failed."""
        feature = {
            "id": "api",
            "depends_on": ["auth", "database"],
        }
        feature_results = {"auth": "completed", "database": "failed"}
        assert _should_skip(feature, feature_results) is True

    def test_multiple_dependencies_all_completed(self):
        """Test feature with multiple dependencies, all completed."""
        feature = {
            "id": "api",
            "depends_on": ["auth", "database"],
        }
        feature_results = {"auth": "completed", "database": "completed"}
        assert _should_skip(feature, feature_results) is False

    def test_dependency_not_in_results(self):
        """Test feature with dependency not in results (still pending)."""
        feature = {
            "id": "api",
            "depends_on": ["auth"],
        }
        feature_results = {}
        assert _should_skip(feature, feature_results) is False

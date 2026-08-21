"""Tests for _validate_features_json function."""

import pytest

from src.autopilot.orchestrator.features import _validate_features_json


class TestValidateFeaturesJson:
    """Test cases for _validate_features_json."""

    def test_valid_single_feature(self):
        """Test valid features.json with single feature."""
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Authentication",
                    "scope": "User authentication system",
                    "files": ["src/auth/"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        _validate_features_json(features_json)  # Should not raise

    def test_valid_multiple_features(self):
        """Test valid features.json with multiple features."""
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Authentication",
                    "scope": "User authentication system",
                    "files": ["src/auth/"],
                    "depends_on": [],
                    "execution": "parallel",
                },
                {
                    "id": "api",
                    "name": "API Gateway",
                    "scope": "API gateway",
                    "files": ["src/api/"],
                    "depends_on": ["auth"],
                    "execution": "parallel",
                },
            ],
        }
        _validate_features_json(features_json)  # Should not raise

    def test_missing_design_name(self):
        """Test missing design_name field."""
        features_json = {
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ]
        }
        with pytest.raises(ValueError, match="missing 'design_name'"):
            _validate_features_json(features_json)

    def test_missing_features(self):
        """Test missing features array."""
        features_json = {"design_name": "Test"}
        with pytest.raises(ValueError, match="missing 'features'"):
            _validate_features_json(features_json)

    def test_empty_features(self):
        """Test empty features array."""
        features_json = {"design_name": "Test", "features": []}
        with pytest.raises(ValueError, match="at least 1 entry"):
            _validate_features_json(features_json)

    def test_more_than_five_features_is_allowed(self):
        """Regression: the count is a rough prompt target (~5), not a hard
        cap. A well-formed 6+ feature decomposition for a genuinely
        multi-concern design must not be rejected -- observed live: a valid
        6-feature decomposition got its entire Phase 0 output discarded by
        a strict 1-5 check, throwing away real analysis work."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": f"f{i}",
                    "name": f"Feature {i}",
                    "scope": f"Scope {i}",
                    "files": [f"src/f{i}/"],
                    "depends_on": [],
                    "execution": "parallel",
                }
                for i in range(10)
            ],
        }
        _validate_features_json(features_json)  # Should not raise

    def test_absurdly_many_features_still_rejected(self):
        """Sanity ceiling: not a product cap, just a guard against garbage
        like one 'feature' per file."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": f"f{i}",
                    "name": f"Feature {i}",
                    "scope": f"Scope {i}",
                    "files": [f"src/f{i}.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
                for i in range(51)
            ],
        }
        with pytest.raises(ValueError, match="one feature per file"):
            _validate_features_json(features_json)

    def test_duplicate_ids(self):
        """Test duplicate feature IDs."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth 1",
                    "scope": "Scope 1",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                },
                {
                    "id": "auth",
                    "name": "Auth 2",
                    "scope": "Scope 2",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                },
            ],
        }
        with pytest.raises(ValueError, match="Duplicate feature id"):
            _validate_features_json(features_json)

    def test_invalid_depends_on(self):
        """Test depends_on references non-existent feature."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "api",
                    "name": "API",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": ["nonexistent"],
                    "execution": "parallel",
                }
            ],
        }
        with pytest.raises(ValueError, match="depends on unknown feature"):
            _validate_features_json(features_json)

    def test_cycle_in_depends_on(self):
        """Test cycle in depends_on."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "a",
                    "name": "A",
                    "scope": "Scope A",
                    "files": [],
                    "depends_on": ["b"],
                    "execution": "parallel",
                },
                {
                    "id": "b",
                    "name": "B",
                    "scope": "Scope B",
                    "files": [],
                    "depends_on": ["a"],
                    "execution": "parallel",
                },
            ],
        }
        with pytest.raises(ValueError, match="Dependency cycle"):
            _validate_features_json(features_json)

    def test_invalid_execution(self):
        """Test invalid execution value."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "invalid",
                }
            ],
        }
        with pytest.raises(ValueError, match="invalid execution"):
            _validate_features_json(features_json)

    def test_file_overlap(self):
        """Test overlapping file paths."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "Scope 1",
                    "files": ["src/auth/"],
                    "depends_on": [],
                    "execution": "parallel",
                },
                {
                    "id": "api",
                    "name": "API",
                    "scope": "Scope 2",
                    "files": ["src/auth/login.py"],
                    "depends_on": [],
                    "execution": "parallel",
                },
            ],
        }
        with pytest.raises(ValueError, match="File overlap"):
            _validate_features_json(features_json)

    def test_missing_id_field(self):
        """Test missing id field in feature."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "name": "Auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        with pytest.raises(ValueError, match="missing 'id'"):
            _validate_features_json(features_json)

    def test_missing_name_field(self):
        """Test missing name field in feature."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        with pytest.raises(ValueError, match="missing 'name'"):
            _validate_features_json(features_json)

    def test_missing_scope_field(self):
        """Test missing scope field in feature."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        with pytest.raises(ValueError, match="missing 'scope'"):
            _validate_features_json(features_json)

    def test_repo_field_omitted_is_valid(self):
        """REQ-19: 'repo' is optional -- single-repo projects never set it."""
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth", "name": "Auth", "scope": "Scope",
                    "files": [], "depends_on": [], "execution": "parallel",
                }
            ],
        }
        _validate_features_json(features_json)  # Should not raise

    def test_repo_field_string_is_valid(self):
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth", "name": "Auth", "scope": "Scope",
                    "files": [], "depends_on": [], "execution": "parallel",
                    "repo": "backend",
                }
            ],
        }
        _validate_features_json(features_json)  # Should not raise

    def test_repo_field_non_string_rejected(self):
        features_json = {
            "design_name": "Test",
            "features": [
                {
                    "id": "auth", "name": "Auth", "scope": "Scope",
                    "files": [], "depends_on": [], "execution": "parallel",
                    "repo": ["backend"],
                }
            ],
        }
        with pytest.raises(ValueError, match="'repo' must be a string"):
            _validate_features_json(features_json)

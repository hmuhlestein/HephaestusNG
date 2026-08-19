"""Additional tests to improve coverage for validation helpers."""

from pathlib import Path
from unittest.mock import patch

from src.services.validation_helpers import validate_file_path


class TestValidationHelpersAdditionalCoverage:
    """Additional tests to reach 90% coverage for validation helpers."""

    def test_validate_file_path_relative_path(self):
        """Test validation of relative paths."""
        # Test that relative paths are converted to absolute
        with patch("src.services.validation_helpers.Path.cwd") as mock_cwd:
            mock_cwd.return_value = Path("/home/user")

            # This should not raise
            validate_file_path("relative/path/file.md")


class TestValidateFilePathAllowedRoot:
    """Phase 3 Tier 2 item 15 (docs/AUTOPILOT_REFACTOR_PLAN.md):
    validate_file_path's traversal check only rejected paths containing a
    literal ".." -- an absolute path needing no ".." at all to point
    somewhere unsafe ("/etc/passwd") passed through untouched. The
    resolve() + allowed_root containment check closes that gap for any
    caller that has a real root to check against (none currently do --
    see the function's own docstring for why -- so allowed_root is opt-in,
    not the default)."""

    def test_absolute_path_outside_allowed_root_is_rejected(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "secret.md"
        outside.parent.mkdir()
        outside.write_text("x")

        import pytest

        with pytest.raises(ValueError, match="outside allowed directories"):
            validate_file_path(str(outside), allowed_root=root)

    def test_absolute_path_inside_allowed_root_is_accepted(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        inside = root / "result.md"
        inside.write_text("x")

        # Should not raise.
        validate_file_path(str(inside), allowed_root=root)

    def test_traversal_segment_still_rejected_without_allowed_root(self):
        import pytest

        with pytest.raises(ValueError, match="directory traversal detected"):
            validate_file_path("../../etc/passwd")

    def test_filename_merely_containing_dotdot_is_not_a_false_positive(self, tmp_path):
        # "notes..final.md" contains ".." as a raw substring but is not a
        # path-traversal segment -- the old substring check would have
        # rejected this outright.
        f = tmp_path / "notes..final.md"
        f.write_text("x")

        # Should not raise.
        validate_file_path(str(f))

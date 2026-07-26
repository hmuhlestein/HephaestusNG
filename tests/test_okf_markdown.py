"""Unit tests for src/autopilot/okf_markdown.py's OKF frontmatter read/write helpers."""

from src.autopilot.okf_markdown import read_okf, write_okf


class TestReadOkf:
    def test_reads_valid_frontmatter_and_body(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("---\ntype: qa_validation_result\nfailed_tests: 0\n---\n\n# QA Report\nAll good.")

        result = read_okf(path)

        assert result is not None
        frontmatter, body = result
        assert frontmatter == {"type": "qa_validation_result", "failed_tests": 0}
        assert body == "# QA Report\nAll good."

    def test_missing_file_returns_none(self, tmp_path):
        assert read_okf(tmp_path / "does_not_exist.md") is None

    def test_no_opening_delimiter_returns_none(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("# Just a plain report\nNo frontmatter here.")

        assert read_okf(path) is None

    def test_no_closing_delimiter_returns_none(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("---\ntype: qa_validation_result\n\n# QA Report\n")

        assert read_okf(path) is None

    def test_malformed_yaml_returns_none(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("---\ntype: [unclosed\n---\n\nbody")

        assert read_okf(path) is None

    def test_non_dict_frontmatter_returns_none(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("---\n- one\n- two\n---\n\nbody")

        assert read_okf(path) is None

    def test_body_containing_its_own_horizontal_rule_is_not_mistaken_for_closing_delimiter(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text(
            "---\ntype: qa_validation_result\n---\n\n# Report\n\nSection one\n\n---\n\nSection two"
        )

        result = read_okf(path)

        assert result is not None
        frontmatter, body = result
        assert frontmatter == {"type": "qa_validation_result"}
        assert body == "# Report\n\nSection one\n\n---\n\nSection two"


class TestWriteOkf:
    def test_round_trips_through_read_okf(self, tmp_path):
        path = tmp_path / "report.md"
        frontmatter = {"verdict": "PASS", "type": "qa_validation_result", "failed_tests": 0}

        write_okf(path, frontmatter, "# QA Report\nAll good.")
        result = read_okf(path)

        assert result is not None
        read_frontmatter, body = result
        assert read_frontmatter == frontmatter
        assert body == "# QA Report\nAll good."

    def test_type_is_written_first(self, tmp_path):
        path = tmp_path / "report.md"

        write_okf(path, {"verdict": "PASS", "type": "qa_validation_result"}, "body")

        text = path.read_text()
        frontmatter_block = text.split("\n---\n", 1)[0]
        assert frontmatter_block.splitlines()[1] == "type: qa_validation_result"

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "docs" / "qa_validation" / "qa_report.md"

        write_okf(path, {"type": "qa_validation_result"}, "body")

        assert path.exists()

"""Tests for detect_workflow_type's keyword heuristic (see
docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md section 4)."""

from src.services.workflow_type_detection import detect_workflow_type


class TestDetectWorkflowType:
    def test_bugfix_title_wins(self):
        assert detect_workflow_type("Fix login crash", "Users see a blank screen.") == "bugfix"

    def test_feature_title_wins(self):
        assert detect_workflow_type("Add dark mode support", "Users want a dark theme.") == "feature"

    def test_no_keywords_defaults_to_feature(self):
        assert detect_workflow_type("Q3 Roadmap Item 12", "See attached spec.") == "feature"

    def test_body_keywords_alone_can_tip_the_balance(self):
        text = "This is broken, it crashes, and returns the wrong error every single time it fails."
        assert detect_workflow_type("Untitled", text) == "bugfix"

    def test_title_weighted_over_body(self):
        # Title says "Add" (feature); body has one incidental "bug" mention
        # that shouldn't be enough to flip a clearly feature-titled design.
        assert detect_workflow_type("Add export to CSV", "Note: unrelated to the old export bug.") == "feature"

    def test_case_insensitive(self):
        assert detect_workflow_type("FIX BROKEN LOGIN", "") == "bugfix"

    def test_empty_inputs_default_to_feature(self):
        assert detect_workflow_type("", "") == "feature"

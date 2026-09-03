"""Tests for get_pr_status -- the §3.3 CI/review-status primitive both
verify_git_expert_merged_and_pushed (completion floor) and
_resolve_pending_pr_status (periodic sweep) call into.

JSON shapes below are taken from real `gh pr view --json url,state,
statusCheckRollup,reviewDecision` output (verified live against real
GitHub PRs in cli/cli during implementation) -- not guessed.
"""

from unittest.mock import MagicMock, patch

from src.services.github_pr_status import get_pr_status


def _gh_result(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestGetPRStatus:
    def test_all_checks_passing_no_review_decision(self):
        stdout = """{"reviewDecision": "", "state": "MERGED", "statusCheckRollup": [], "url": "https://github.com/o/r/pull/1"}"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)):
            status = get_pr_status("https://github.com/o/r/pull/1")

        assert status.state == "MERGED"
        assert status.ci_conclusion == "passing"
        assert status.review_decision is None
        assert status.failing_checks == []
        assert status.needs_work is False
        assert status.is_pending is False

    def test_a_failing_check_is_detected(self):
        stdout = """{
            "reviewDecision": "REVIEW_REQUIRED", "state": "OPEN", "url": "https://github.com/o/r/pull/2",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"},
                {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "label-external"}
            ]
        }"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)):
            status = get_pr_status("https://github.com/o/r/pull/2")

        assert status.ci_conclusion == "failing"
        assert status.failing_checks == ["lint"]
        assert status.needs_work is True
        assert status.is_pending is False
        assert "lint" in status.summary

    def test_a_still_running_check_is_pending_not_failing(self):
        stdout = """{
            "reviewDecision": null, "state": "OPEN", "url": "https://github.com/o/r/pull/3",
            "statusCheckRollup": [
                {"status": "IN_PROGRESS", "conclusion": null, "name": "build"},
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"}
            ]
        }"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)):
            status = get_pr_status("https://github.com/o/r/pull/3")

        assert status.ci_conclusion == "pending"
        assert status.failing_checks == []
        assert status.needs_work is False
        assert status.is_pending is True

    def test_changes_requested_is_needs_work_even_with_green_ci(self):
        stdout = """{
            "reviewDecision": "CHANGES_REQUESTED", "state": "OPEN", "url": "https://github.com/o/r/pull/4",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"}]
        }"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)):
            status = get_pr_status("https://github.com/o/r/pull/4")

        assert status.ci_conclusion == "passing"
        assert status.review_decision == "CHANGES_REQUESTED"
        assert status.needs_work is True
        assert status.is_pending is False
        assert "changes" in status.summary.lower()

    def test_gh_nonzero_exit_returns_none_not_a_crash(self):
        with patch("subprocess.run", return_value=_gh_result(returncode=1, stderr="no pull requests found")):
            assert get_pr_status("some-branch", cwd="/tmp") is None

    def test_gh_raising_returns_none_not_a_crash(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not installed")):
            assert get_pr_status("https://github.com/o/r/pull/5") is None

    def test_malformed_json_returns_none_not_a_crash(self):
        with patch("subprocess.run", return_value=_gh_result(stdout="not json")):
            assert get_pr_status("https://github.com/o/r/pull/6") is None

    def test_passes_ref_and_cwd_through_to_gh(self):
        stdout = """{"reviewDecision": "", "state": "OPEN", "statusCheckRollup": [], "url": "u"}"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)) as mock_run:
            get_pr_status("my-feature-branch", cwd="/repo/worktree")

        args, kwargs = mock_run.call_args
        assert args[0][:3] == ["gh", "pr", "view"]
        assert args[0][3] == "my-feature-branch"
        assert kwargs["cwd"] == "/repo/worktree"

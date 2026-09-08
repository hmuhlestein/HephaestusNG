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
        # Fixture carries real passing checks. It previously carried an EMPTY
        # rollup while still asserting "passing" -- encoding the very bug
        # this module now fixes (an empty rollup means CI has not attached
        # yet, not that it succeeded). See TestEmptyRollupIsNotAPass.
        stdout = """{
            "reviewDecision": "", "state": "MERGED", "url": "https://github.com/o/r/pull/1",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"},
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"}
            ]
        }"""
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
        stdout = """{
            "reviewDecision": "", "state": "OPEN", "url": "u",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"}]
        }"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)) as mock_run:
            get_pr_status("my-feature-branch", cwd="/repo/worktree")

        args, kwargs = mock_run.call_args
        assert args[0][:3] == ["gh", "pr", "view"]
        assert args[0][3] == "my-feature-branch"
        assert kwargs["cwd"] == "/repo/worktree"


class TestEmptyRollupIsNotAPass:
    """An empty statusCheckRollup must never read as "CI passed".

    GitHub attaches check runs a few seconds after a push, so a `gh pr view`
    issued inside that window returns no checks at all. "No failing checks
    and none pending" then fell through to "passing", and the completion
    floor was told CI had succeeded before CI existed.

    Observed live: an agent pushed, opened PR #1097 and reported done at
    20:39:17; the first CI job started at 20:39:32. Fifteen seconds. The
    git_expert phase completed on that false pass, the workflow parked for
    human review, and the check that later went red was seen by nothing --
    _resolve_pending_pr_status only runs for an ACTIVE workflow with an
    in_progress git_expert task, and completing the phase ends both.

    "Pending" and "no CI configured" are distinguished by asking for the
    head commit's check suites, which exist as soon as a workflow is
    queued. That question is asked ONLY when the rollup is empty, so the
    common path stays a single gh call. IDB-2482.
    """

    PR_JSON = """{
        "reviewDecision": "", "state": "OPEN", "url": "https://github.com/o/r/pull/9",
        "statusCheckRollup": [], "headRefOid": "deadbeefcafe1234"
    }"""

    def test_pending_when_check_suites_exist_but_no_runs_yet(self):
        """The live case: CI is queued, its runs have not surfaced."""
        with patch(
            "subprocess.run",
            side_effect=[_gh_result(stdout=self.PR_JSON), _gh_result(stdout="3\n")],
        ):
            status = get_pr_status("my-branch", cwd="/repo")

        assert status.ci_conclusion == "pending"
        assert status.is_pending is True
        assert status.needs_work is False

    def test_passing_only_when_the_repo_has_no_check_suites_at_all(self):
        """A repository with no CI must not strand git_expert forever
        waiting for checks that will never arrive."""
        with patch(
            "subprocess.run",
            side_effect=[_gh_result(stdout=self.PR_JSON), _gh_result(stdout="0\n")],
        ):
            status = get_pr_status("my-branch", cwd="/repo")

        assert status.ci_conclusion == "passing"
        assert status.is_pending is False

    def test_a_failed_check_suites_lookup_errs_to_pending(self):
        """The cost of a wrong "pending" is one more sweep tick. The cost of
        a wrong "passing" is a red PR nothing is watching."""
        with patch(
            "subprocess.run",
            side_effect=[_gh_result(stdout=self.PR_JSON), _gh_result(returncode=1, stderr="404")],
        ):
            assert get_pr_status("my-branch", cwd="/repo").ci_conclusion == "pending"

    def test_a_raising_check_suites_lookup_errs_to_pending(self):
        with patch(
            "subprocess.run",
            side_effect=[_gh_result(stdout=self.PR_JSON), TimeoutError("gh hung")],
        ):
            assert get_pr_status("my-branch", cwd="/repo").ci_conclusion == "pending"

    def test_a_missing_head_oid_errs_to_pending_without_a_second_call(self):
        no_oid = """{"reviewDecision": "", "state": "OPEN", "url": "u", "statusCheckRollup": []}"""
        with patch("subprocess.run", return_value=_gh_result(stdout=no_oid)) as mock_run:
            assert get_pr_status("my-branch").ci_conclusion == "pending"
        assert mock_run.call_count == 1, "must not ask about a commit it cannot name"

    def test_the_second_call_is_scoped_to_the_head_commit(self):
        with patch(
            "subprocess.run",
            side_effect=[_gh_result(stdout=self.PR_JSON), _gh_result(stdout="1")],
        ) as mock_run:
            get_pr_status("my-branch", cwd="/repo/worktree")

        assert mock_run.call_count == 2
        second = mock_run.call_args_list[1]
        assert second[0][0][:2] == ["gh", "api"]
        assert "deadbeefcafe1234/check-suites" in second[0][0][2]
        assert second[1]["cwd"] == "/repo/worktree"

    def test_a_non_empty_rollup_never_triggers_the_extra_call(self):
        """The common path must stay a single gh call."""
        stdout = """{
            "reviewDecision": "", "state": "OPEN", "url": "u",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"}]
        }"""
        with patch("subprocess.run", return_value=_gh_result(stdout=stdout)) as mock_run:
            assert get_pr_status("my-branch").ci_conclusion == "passing"
        assert mock_run.call_count == 1

"""Independent CI/review-status check for an open PR, via `gh pr view`.

§3.3 of the external evaluation: git_expert previously had no way to know
whether an open PR's CI passed or a reviewer requested changes -- the only
GitHub operations anywhere were `gh pr create`/`gh pr view` (URL recovery)
and `gh pr merge`. This is the shared primitive both
verify_git_expert_merged_and_pushed (the completion floor, checked at the
moment the agent self-reports done) and _resolve_pending_pr_status (the
periodic sweep check that resolves a still-pending PR later, without
spinning up a fresh agent just to poll) call into.
"""

import logging
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

GH_TIMEOUT_SECONDS = 30


@dataclass
class PRStatus:
    url: Optional[str]
    state: str  # OPEN, MERGED, CLOSED
    ci_conclusion: str  # "passing", "failing", "pending"
    review_decision: Optional[str]  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED, or None
    # GitHub's mergeStateStatus: CLEAN, BEHIND, BLOCKED, DIRTY, UNSTABLE,
    # HAS_HOOKS, DRAFT, UNKNOWN. Distinct from the `mergeable` field, which
    # reports merge CONFLICTS ONLY -- a PR can be mergeable=MERGEABLE and
    # still be unmergeable in practice.
    merge_state: Optional[str] = None
    failing_checks: List[str] = field(default_factory=list)
    summary: str = ""

    # mergeStateStatus values that the agent itself can resolve, and the
    # instruction for each. Both are things git_expert.yaml's prompt already
    # mandates ("Merge main into the feature branch and resolve conflicts
    # BEFORE pushing -- mandatory"), so this hands back work it knows how to
    # do rather than a state nobody acts on.
    _AGENT_FIXABLE_MERGE_STATES = {
        "BEHIND": (
            "the branch is behind its base and this repository requires branches "
            "to be up to date before merging -- merge the base branch into this "
            "one and push"
        ),
        "DIRTY": (
            "the branch has merge conflicts with its base -- merge the base branch "
            "into this one, resolve the conflicts and push"
        ),
    }

    @property
    def needs_work(self) -> bool:
        return (
            self.ci_conclusion == "failing"
            or self.review_decision == "CHANGES_REQUESTED"
            or (self.merge_state or "") in self._AGENT_FIXABLE_MERGE_STATES
        )

    @property
    def ready_to_merge(self) -> bool:
        """Whether this PR could actually be merged right now.

        Deliberately NOT the same question as "are the checks green".
        `mergeable` reports merge conflicts only, so a PR reads
        mergeable=MERGEABLE while mergeStateStatus=BEHIND and the merge
        button is disabled -- which is exactly how a PR came to be reported
        as finished while its base branch had moved three commits ahead
        under a strict required-status-checks policy.
        """
        return (
            self.state == "OPEN"
            and self.ci_conclusion == "passing"
            and self.review_decision != "CHANGES_REQUESTED"
            and (self.merge_state or "UNKNOWN") == "CLEAN"
        )

    @property
    def is_pending(self) -> bool:
        return self.ci_conclusion == "pending" and not self.needs_work



def _empty_rollup_conclusion(head_oid: Optional[str], cwd: Optional[str]) -> str:
    """Resolve an empty statusCheckRollup into "pending" or "passing".

    Returns "pending" when GitHub reports check suites for this commit (CI
    is attached and queued, the runs just have not surfaced yet) and
    "passing" only when it reports none at all -- a repository with no CI
    configured, where waiting forever would strand every git_expert task.

    Errs to "pending" on any failure, including a missing head_oid. The
    cost of a wrong "pending" is one more sweep tick; the cost of a wrong
    "passing" is a red PR that nothing is watching.
    """
    if not head_oid:
        return "pending"
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{{owner}}/{{repo}}/commits/{head_oid}/check-suites",
                "--jq", ".total_count",
            ],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS, cwd=cwd,
        )
    except Exception as e:
        logger.warning(f"check-suites lookup failed for {head_oid[:8]}: {e}")
        return "pending"
    if result.returncode != 0:
        logger.warning(
            f"check-suites lookup for {head_oid[:8]} exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:200]}"
        )
        return "pending"
    raw = (result.stdout or "").strip()
    if raw.isdigit() and int(raw) == 0:
        logger.info(
            f"No check suites for {head_oid[:8]} -- treating this repository as "
            "having no CI configured rather than waiting for checks that will "
            "never arrive"
        )
        return "passing"
    return "pending"

def get_pr_status(ref: str, cwd: Optional[str] = None) -> Optional[PRStatus]:
    """Fetch a PR's CI/review status via `gh pr view`.

    ref: a PR URL, branch name, or PR number -- anything `gh pr view`
    itself accepts. A branch name only resolves correctly when `cwd` is
    the repo it belongs to (gh infers owner/repo from the git remote).

    Returns None on any lookup failure (gh unavailable, no PR for this
    ref, network error, malformed JSON) -- callers must treat None the
    same as "unknown/pending", never as a rejection: a transient gh
    failure must not fail a real task.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", ref,
                "--json", "url,state,statusCheckRollup,reviewDecision,headRefOid,mergeStateStatus",
            ],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except Exception as e:
        logger.warning(f"gh pr view failed for {ref!r}: {e}")
        return None

    if result.returncode != 0:
        logger.warning(f"gh pr view {ref!r} exited {result.returncode}: {(result.stderr or '').strip()}")
        return None

    import json

    try:
        data = json.loads(result.stdout)
    except Exception as e:
        logger.warning(f"gh pr view {ref!r} returned unparseable JSON: {e}")
        return None

    url = data.get("url")
    state = data.get("state") or "OPEN"
    review_decision = data.get("reviewDecision") or None
    merge_state = (data.get("mergeStateStatus") or "").upper() or None

    checks = data.get("statusCheckRollup") or []
    failing_checks = [
        c.get("name") or c.get("context") or "unknown check"
        for c in checks
        if (c.get("conclusion") or "").upper() in ("FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE")
    ]
    any_pending = any(
        not c.get("conclusion") or (c.get("status") or "").upper() in ("QUEUED", "IN_PROGRESS", "PENDING")
        for c in checks
    )

    if failing_checks:
        ci_conclusion = "failing"
    elif any_pending:
        ci_conclusion = "pending"
    elif not checks:
        # An EMPTY rollup is not a pass. GitHub attaches check runs a few
        # seconds after a push, so a `gh pr view` issued in that window
        # returns no checks at all -- and "no failing checks and none
        # pending" fell through to "passing" here. Observed live: an agent
        # pushed, opened a PR and reported done at 20:39:17; CI's first job
        # started at 20:39:32. Fifteen seconds. This function told the
        # completion floor CI had passed before CI existed, the phase
        # completed, the workflow parked for human review, and the check
        # that later went red (a repo-local SOLID file-size gate) was never
        # seen by anything -- _resolve_pending_pr_status only ever runs for
        # an ACTIVE workflow with an in_progress git_expert task, and the
        # phase completing is precisely what ends both.
        #
        # This module's own docstring already requires the caller to treat
        # a None return as "unknown/pending, never as a rejection". The
        # empty-list case is the same class of unknown and just slipped
        # past that rule.
        #
        # Distinguishing "CI has not attached yet" from "this repo has no
        # CI" needs one more question, asked ONLY in this branch so the
        # common path stays a single gh call: check-suites exist as soon as
        # a workflow is queued, before any individual run appears.
        ci_conclusion = _empty_rollup_conclusion(data.get("headRefOid"), cwd)
    else:
        ci_conclusion = "passing"

    summary_parts = []
    if failing_checks:
        summary_parts.append(f"CI check(s) failed: {', '.join(failing_checks)}")
    if review_decision == "CHANGES_REQUESTED":
        summary_parts.append("a reviewer requested changes on this PR")
    if merge_state in PRStatus._AGENT_FIXABLE_MERGE_STATES:
        summary_parts.append(PRStatus._AGENT_FIXABLE_MERGE_STATES[merge_state])
    if not summary_parts:
        summary_parts.append(
            "CI is still running" if ci_conclusion == "pending" else "CI passing, no changes requested"
        )

    return PRStatus(
        url=url,
        state=state,
        ci_conclusion=ci_conclusion,
        review_decision=review_decision,
        merge_state=merge_state,
        failing_checks=failing_checks,
        summary="; ".join(summary_parts) + f" (PR {url})" if url else "; ".join(summary_parts),
    )

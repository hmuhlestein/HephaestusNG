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
    failing_checks: List[str] = field(default_factory=list)
    summary: str = ""

    @property
    def needs_work(self) -> bool:
        return self.ci_conclusion == "failing" or self.review_decision == "CHANGES_REQUESTED"

    @property
    def is_pending(self) -> bool:
        return self.ci_conclusion == "pending" and not self.needs_work


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
                "--json", "url,state,statusCheckRollup,reviewDecision",
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
    else:
        ci_conclusion = "passing"

    summary_parts = []
    if failing_checks:
        summary_parts.append(f"CI check(s) failed: {', '.join(failing_checks)}")
    if review_decision == "CHANGES_REQUESTED":
        summary_parts.append("a reviewer requested changes on this PR")
    if not summary_parts:
        summary_parts.append(
            "CI is still running" if ci_conclusion == "pending" else "CI passing, no changes requested"
        )

    return PRStatus(
        url=url,
        state=state,
        ci_conclusion=ci_conclusion,
        review_decision=review_decision,
        failing_checks=failing_checks,
        summary="; ".join(summary_parts) + f" (PR {url})" if url else "; ".join(summary_parts),
    )

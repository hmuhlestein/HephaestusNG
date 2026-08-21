"""Auto-detect a design document's workflow type ("feature" vs "bugfix").

Deterministic keyword heuristic, not an LLM call -- runs synchronously at
design-add time so the queue UI can show a resolved badge immediately
instead of waiting on Phase 0. See docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md
section 4 for why this was chosen over folding detection into Phase 0's
own structured output.

A manual selection in the UI always overrides this -- a wrong guess here
is a one-click fix, not a pipeline restart.
"""

import re

_BUGFIX_KEYWORDS = (
    "bug", "fix", "broken", "regression", "crash", "doesn't work",
    "does not work", "incorrect", "error", "fails", "failing", "failure",
    "not working", "wrong", "issue",
)
_FEATURE_KEYWORDS = (
    "add", "implement", "new feature", "support for", "introduce",
    "build", "create",
)

# Title matches count more than body matches -- a design named "Fix login
# crash" is a much stronger signal than one paragraph in a long spec that
# happens to mention "error handling".
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1


def _score(text: str, keywords: tuple, weight: int) -> int:
    return sum(weight for kw in keywords if re.search(re.escape(kw), text))


def detect_workflow_type(name: str, content: str) -> str:
    """Return "bugfix" or "feature" for the given design name/content.

    Ties (including the common case of no keyword hits at all) resolve to
    "feature" -- the full pipeline is the safer default since it never skips
    work a bugfix-trimmed pipeline would, only ever does more than strictly
    necessary.
    """
    name_l = (name or "").lower()
    content_l = (content or "").lower()

    bugfix_score = _score(name_l, _BUGFIX_KEYWORDS, _TITLE_WEIGHT) + _score(content_l, _BUGFIX_KEYWORDS, _BODY_WEIGHT)
    feature_score = _score(name_l, _FEATURE_KEYWORDS, _TITLE_WEIGHT) + _score(content_l, _FEATURE_KEYWORDS, _BODY_WEIGHT)

    return "bugfix" if bugfix_score > feature_score else "feature"

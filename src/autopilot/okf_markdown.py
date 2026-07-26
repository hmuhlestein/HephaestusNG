"""Read/write helpers for phase agent reports in Google's Open Knowledge
Format (OKF): a markdown file with a YAML frontmatter block delimited by
`---` on its own line (open and close), `type` as the only required key,
followed by a markdown body -- see
https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md

Replaces the previous two-file convention (a `.json` for structured fields
read by src/autopilot/spec.py's gate scorers, plus a separate `.md` for the
narrative report) with one file per phase report.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_DELIMITER = "---"


def read_okf(path: Path) -> Optional[Tuple[Dict[str, Any], str]]:
    """Parse an OKF markdown file.

    Returns (frontmatter, body) on success. Returns None if the file
    doesn't start with a frontmatter block, the block has no closing
    delimiter, or the YAML fails to parse -- never raises, matching the
    prior read_result's "missing/invalid means no result yet" behavior.
    """
    try:
        text = path.read_text()
    except Exception:
        return None

    if not text.startswith(f"{_DELIMITER}\n"):
        return None

    # Only the FIRST "\n---\n" closes the frontmatter -- a body containing
    # its own "---" horizontal rule later on must not be mistaken for the
    # closing delimiter.
    remainder = text[len(_DELIMITER) + 1 :]
    parts = remainder.split(f"\n{_DELIMITER}\n", 1)
    if len(parts) != 2:
        return None
    frontmatter_text, body = parts

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except Exception:
        return None
    if not isinstance(frontmatter, dict):
        return None

    return frontmatter, body.lstrip("\n")


def write_okf(path: Path, frontmatter: Dict[str, Any], body: str) -> None:
    """Write an OKF file: `type` first (frontmatter's declared order is
    otherwise preserved), then the body, unchanged."""
    ordered = {"type": frontmatter.get("type")}
    ordered.update({k: v for k, v in frontmatter.items() if k != "type"})
    frontmatter_text = yaml.safe_dump(ordered, default_flow_style=False, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_DELIMITER}\n{frontmatter_text}{_DELIMITER}\n\n{body}")

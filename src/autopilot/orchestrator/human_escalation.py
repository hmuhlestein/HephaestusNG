"""Human-in-the-loop escalation: prompt for input via a request/response
file pair, auto-continuing after a timeout. Extracted from
orchestrator/__init__.py (SOLID review: that module had grown to 3411
lines mixing the actual pipeline-execution flow with unrelated
config-getter/human-escalation/agent-registration helpers -- see
docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md).
"""

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.constants import AUTOPILOT_STATE_DIR
from src.core.database import utc_now

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger


def prompt_human(reason: str, logger: "OrchestratorLogger", timeout: int = 600, project_id: str = None) -> str:
    """Prompt for human input. Auto-continues after timeout seconds."""
    import sys
    import uuid

    logger.warning(f"DECISION POINT: {reason}")

    input_dir = Path(AUTOPILOT_STATE_DIR)
    input_dir.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())[:8]
    request_file = input_dir / f"input_request_{request_id}.json"
    response_file = input_dir / f"input_response_{request_id}.json"

    # Atomic write via temp+rename
    payload = json.dumps(
        {
            "id": request_id,
            "reason": reason,
            # utcnow: intervention_routes._find_first_non_stale_input_request
            # parses this back, sees tzinfo is None, and ASSUMES UTC
            # (ts.replace(tzinfo=timezone.utc)) before comparing against
            # datetime.now(timezone.utc). A local-time stamp here is
            # therefore misread by the host's UTC offset: west of UTC the
            # request looks hours older than it is and is deleted as stale
            # (threshold 1h) before the human ever sees the prompt; east of
            # UTC the age goes negative and it is never cleaned up.
            "timestamp": utc_now().isoformat(),
            "options": ["c", "s", "q"],
            "labels": {"c": "Continue", "s": "Skip design", "q": "Quit pipeline"},
            "timeout_seconds": timeout,
            "project_id": project_id,
        },
        indent=2,
    )
    tmp = request_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, request_file)

    logger.event("human_input_required", {"reason": reason, "request_id": request_id})

    start = time.time()
    while time.time() - start < timeout:
        # Check if request file was dismissed (deleted by API)
        if not request_file.exists():
            logger.warning("Input request was dismissed (auto-continuing)")
            response_file.unlink(missing_ok=True)
            return "c"  # Auto-continue when dismissed

        # Check file response
        if response_file.exists():
            try:
                data = json.loads(response_file.read_text())
                choice = data.get("choice", "").strip().lower()
                message = data.get("message", "")

                if choice == "m" and message:
                    # Log the message and continue waiting for actual decision
                    logger.info(f"Human message: {message}")
                    logger.event(
                        "human_input",
                        {
                            "choice": "m",
                            "message": message,
                            "reason": reason,
                            "source": "web",
                            "request_id": request_id,
                        },
                    )
                    response_file.unlink(missing_ok=True)  # Delete response, keep waiting
                    continue

                if choice in ("c", "s", "q"):
                    logger.event(
                        "human_input",
                        {
                            "choice": choice,
                            "reason": reason,
                            "source": "web",
                            "request_id": request_id,
                        },
                    )
                    request_file.unlink(missing_ok=True)
                    response_file.unlink(missing_ok=True)
                    return choice
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Error reading response file: {e}")

        # Check terminal input (non-blocking on Unix only)
        try:
            if sys.platform != "win32" and sys.stdin.isatty():
                import select as select_mod

                rlist, _, _ = select_mod.select([sys.stdin], [], [], 1.0)
                if rlist:
                    choice = sys.stdin.readline().strip().lower()
                    if choice in ("c", "s", "q"):
                        logger.event(
                            "human_input",
                            {
                                "choice": choice,
                                "reason": reason,
                                "source": "terminal",
                                "request_id": request_id,
                            },
                        )
                        request_file.unlink(missing_ok=True)
                        response_file.unlink(missing_ok=True)
                        return choice
            else:
                time.sleep(2)
        except (OSError, ValueError):
            time.sleep(2)

    # Timeout - auto-continue
    logger.warning(f"Human input timed out after {timeout}s, auto-continuing")
    logger.event("human_input", {"choice": "timeout", "reason": reason, "request_id": request_id})
    request_file.unlink(missing_ok=True)
    return "c"

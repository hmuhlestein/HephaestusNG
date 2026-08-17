"""Ticket auto-creation from forensics analysis reports.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.
"""

import logging
import re

logger = logging.getLogger(__name__)


def _parse_forensics_recommendations(report_text: str) -> list:
    """Extract actionable recommendations from a forensics.md.

    Expected shape (what agents actually produce — see
    forensics_analysis.yaml's own example):

        ## Recommendations for Future Pipeline Runs

        ### High Priority
        1. **Title** - description

        ### Medium Priority
        ...

    Falls back to "medium" priority for any numbered item found outside
    a recognized High/Medium/Low subheading, so a differently-formatted
    report still yields tickets instead of silently producing none.
    """
    match = re.search(
        r"^##\s*Recommendations.*?$(.*?)(?=^##\s|\Z)",
        report_text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    section = match.group(1)

    item_re = re.compile(r"^\s*\d+\.\s+\*\*(.+?)\*\*\s*-?\s*(.*)$", re.MULTILINE)

    recommendations = []
    current_priority = "medium"
    heading_re = re.compile(r"^###\s*(.+)$", re.MULTILINE)
    # Walk headings and items in document order so each item picks up
    # whichever priority heading most recently preceded it.
    markers = sorted(
        [(m.start(), "heading", m.group(1)) for m in heading_re.finditer(section)] + [(m.start(), "item", m) for m in item_re.finditer(section)],
        key=lambda t: t[0],
    )
    for _, kind, payload in markers:
        if kind == "heading":
            text = payload.lower()
            if "high" in text:
                current_priority = "high"
            elif "medium" in text:
                current_priority = "medium"
            elif "low" in text:
                current_priority = "low"
        else:
            title = payload.group(1).strip()
            description = payload.group(2).strip() or title
            recommendations.append(
                {
                    "title": title,
                    "description": description,
                    "priority": current_priority,
                }
            )
    return recommendations


async def create_tickets_from_forensics_report(session, task) -> int:
    """Auto-create tickets from a completed forensics_analysis report.

    forensics_analysis.yaml's done_definitions mandate "Tickets created
    for actionable findings", but this is exactly the kind of mechanical,
    easy-to-skip step an agent drops once the more interesting analysis
    work is done — observed live: an agent wrote a genuinely thorough
    report with 7 concrete recommendations and saved memory entries, but
    never called hephaestus_create_ticket even once. Same class of gap
    as the ash security scan (src/autopilot/orchestrator.py
    _run_ash_scan) — don't trust the agent to remember a mandated but
    tedious step; do it at the orchestrator/service level instead.

    Best-effort: failures for individual recommendations (or the whole
    thing, e.g. no BoardConfig for this workflow) are logged and
    swallowed rather than blocking the task's "done" status, since
    ticket creation is a side effect, not a correctness gate.

    Returns the number of tickets created.
    """
    from pathlib import Path as _Path

    from src.core.database import Phase, Workflow
    from src.services.ticket_service import TicketService

    phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase or phase.name != "forensics_analysis":
        return 0

    wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
    if not wf or not wf.working_directory:
        return 0

    from src.core.constants import CONTEXT_DIR_NAME

    report_path = _Path(wf.working_directory) / CONTEXT_DIR_NAME / "forensics.md"
    if not report_path.exists():
        return 0

    try:
        report_text = report_path.read_text()
    except OSError as e:
        logger.warning(f"[FORENSICS_TICKETS] Could not read {report_path}: {e}")
        return 0

    recommendations = _parse_forensics_recommendations(report_text)
    if not recommendations:
        return 0

    created = 0
    for rec in recommendations:
        try:
            await TicketService.create_ticket(
                workflow_id=task.workflow_id,
                agent_id=task.assigned_agent_id or "forensics-auto",
                title=rec["title"][:200],
                description=rec["description"],
                ticket_type="improvement",
                priority=rec["priority"],
                task_id=task.id,
                phase_id=task.phase_id,
                tags=["forensics-auto"],
            )
            created += 1
        except Exception as e:
            logger.warning(f"[FORENSICS_TICKETS] Failed to create ticket for '{rec['title']}': {e}")
    logger.info(f"[FORENSICS_TICKETS] Created {created}/{len(recommendations)} tickets from forensics.md")
    return created

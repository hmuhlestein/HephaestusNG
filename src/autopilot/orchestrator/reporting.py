"""Pure report/artifact generation (no DB writes)."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict


from src.core.database import (
    get_db,
)

from src.autopilot.orchestrator.state import (
    DesignEntry,
    FeatureReport,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger

logger = logging.getLogger(__name__)


_REPORT_SUBDIR = ".hephaestus"


def _report_path(project_path: Path, filename: str) -> Path:
    """Locate a report an agent wrote.

    Under worktree isolation agents write reports to ./.hephaestus/ (relative
    to their worktree), which is git-excluded. Fall back to the project root.
    Does NOT iterate worktrees (too slow for per-turn calls).
    """
    in_hephaestus = project_path / _REPORT_SUBDIR / filename
    if in_hephaestus.exists():
        return in_hephaestus
    return project_path / filename


def collect_report_summaries(project_path: Path) -> Dict[str, str]:
    summaries = {}
    report_files = {
        "requirements": "requirements.md",
        "architecture": "architecture.md",
        "review": "review_report.md",
        "doc_review": "docs.md",
        "security": "security.md",
        "qa": "qa.md",
        "product_validation": "validation.md",
        "forensics": "forensics.md",
    }

    for key, filename in report_files.items():
        # First check .hephaestus/ (where agents write), then project root
        filepath = project_path / ".hephaestus" / filename
        if not filepath.exists():
            filepath = project_path / filename
        if filepath.exists():
            try:
                content = filepath.read_text()
                lines = content.strip().split("\n")
                summary_lines = []
                for line in lines[:80]:
                    if line.strip():
                        summary_lines.append(line.strip())
                summaries[key] = "\n".join(summary_lines)
            except Exception:
                summaries[key] = f"[Could not read {filename}]"
        else:
            summaries[key] = f"[{filename} not found]"

    return summaries


def _generate_design_report_html(
    design_entry: DesignEntry,
    feature_results: Dict[str, str],
    designs_folder: Path,
    logger: "OrchestratorLogger",
) -> None:
    """Generate HTML design report using Jinja2 template.

    Args:
        design_entry: Design entry
        feature_results: Feature results mapping
        designs_folder: Path to designs folder
        logger: Orchestrator logger
    """
    from jinja2 import Environment, FileSystemLoader

    templates_dir = Path(__file__).parent.parent / "templates"  # one .parent deeper: now a package module
    if not templates_dir.exists():
        logger.warning(f"Templates directory not found: {templates_dir}")
        return

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)

    try:
        template = env.get_template("design_report.html")
    except Exception as e:
        logger.warning(f"Design report template not found: {e}")
        return

    # Load feature records from DB
    from src.core.database import Feature

    feature_records = []
    with get_db() as db:
        for feat_key in feature_results:
            feat = (
                db.query(Feature)
                .filter_by(
                    design_id=design_entry.db_id,
                    feature_key=feat_key,
                )
                .first()
            )
            if feat:
                feature_records.append(
                    {
                        "name": feat.name,
                        "status": feat.status,
                        "started_at": feat.started_at.isoformat() if feat.started_at else None,
                        "completed_at": feat.completed_at.isoformat() if feat.completed_at else None,
                    }
                )

    context = {
        "design_name": design_entry.name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "features": feature_records,
        "total_features": len(feature_records),
        "completed_features": sum(1 for f in feature_records if f["status"] == "completed"),
        "failed_features": sum(1 for f in feature_records if f["status"] == "failed"),
        "skipped_features": sum(1 for f in feature_records if f["status"] == "skipped"),
    }

    html = template.render(**context)
    html_path = designs_folder / "design_report.html"
    html_path.write_text(html)
    logger.info(f"Design report: {html_path}")


def _empty_report(design_entry: DesignEntry) -> FeatureReport:
    """Create an empty FeatureReport for failed designs."""
    return FeatureReport(
        design_name=design_entry.name,
        project_path="",
        feature_folder="",
        design_document=str(design_entry.path),
        iterations=0,
        total_time_seconds=0,
        qa_passed=False,
        product_validated=False,
        stop_reason="failed",
    )

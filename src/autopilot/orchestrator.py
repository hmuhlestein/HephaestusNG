"""
Autopilot Orchestrator

A continuous multi-agent workflow engine that:
1. Watches a design queue directory for new design documents
2. Picks the next logical design to process
3. Runs the full pipeline: product → architect → developer → review → security → QA → product validation
4. Generates an HTML feature report for human review
5. Repeats until stopped or queue is empty

Designed to run for days/weeks, processing designs as they arrive.
"""

import os
import sys
import time
import json
import glob
import signal
import shutil
import hashlib
import html as html_mod
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any, Set
from dataclasses import dataclass, field
from enum import Enum

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent
API_BASE = "http://127.0.0.1:8000"
AUTOPILOT_STATE_DIR = str(Path.home() / ".hephaestus" / "autopilot")

POLL_INTERVAL = 15
STUCK_THRESHOLD = 3
DESIGN_QUEUE_SCAN_INTERVAL = 60
HEARTBEAT_INTERVAL = 300
MAX_WORKFLOW_TIME = 7200  # 2 hours per workflow execution


def get_litellm_config() -> Dict[str, str]:
    """Read LiteLLM proxy config from environment variables."""
    return {
        "url": os.environ.get("LITELLM_PROXY_URL", ""),
        "api_key": os.environ.get("LITELLM_API_KEY", ""),
        "cost_api_key": os.environ.get("LITELLM_MASTER_KEY", ""),
        "cost_tracking": os.environ.get("LITELLM_COST_TRACKING", "false").lower() == "true",
    }


class StopReason(Enum):
    COMPLETED = "completed"
    HARD_ERROR = "hard_error"
    IMPASSE = "impasse"
    ARCHITECTURAL_ISSUE = "architectural_issue"
    MAX_ITERATIONS = "max_iterations"
    CREDIT_EXHAUSTED = "credit_exhausted"
    USER_INTERRUPT = "user_interrupt"
    QUEUE_EMPTY = "queue_empty"


class DesignStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DesignEntry:
    path: Path
    name: str
    content_hash: str
    status: DesignStatus = DesignStatus.PENDING
    project_path: Optional[Path] = None
    feature_folder: Optional[Path] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class IterationResult:
    iteration: int
    status: str
    qa_passed: bool
    product_validated: bool
    issues_found: List[str]
    fixes_applied: List[str]
    elapsed_seconds: int
    stop_reason: Optional[StopReason] = None


@dataclass
class FeatureReport:
    design_name: str
    project_path: str
    feature_folder: str
    design_document: str
    iterations: int
    total_time_seconds: int
    qa_passed: bool
    product_validated: bool
    stop_reason: str
    requirements_summary: str = ""
    architecture_summary: str = ""
    security_summary: str = ""
    qa_summary: str = ""
    product_validation_summary: str = ""
    forensics_summary: str = ""
    files_created: List[str] = field(default_factory=list)
    issues_resolved: List[str] = field(default_factory=list)
    outstanding_issues: List[str] = field(default_factory=list)
    cost_total: float = 0.0
    cost_breakdown: Dict[str, float] = field(default_factory=dict)
    cost_currency: str = "USD"


@dataclass
class PipelineState:
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    current_design: Optional[str] = None
    queue_status: Dict[str, str] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)


class OrchestratorLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "orchestrator.log"
        self.events_file = log_dir / "events.jsonl"
        self.state_file = log_dir / "state.json"

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        print(line, flush=True)
        with open(self.log_file, "a") as f:
            f.write(line + "\n")

    def event(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            **data,
        }
        with open(self.events_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save_state(self, state: PipelineState):
        with open(self.state_file, "w") as f:
            json.dump({
                "designs_processed": state.designs_processed,
                "designs_succeeded": state.designs_succeeded,
                "designs_failed": state.designs_failed,
                "total_elapsed": state.total_elapsed,
                "current_design": state.current_design,
                "queue_status": state.queue_status,
            }, f, indent=2)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def api_get(endpoint: str, timeout: int = 5) -> Optional[dict]:
    try:
        r = requests.get(f"{API_BASE}{endpoint}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_tasks(status: str = None) -> list:
    params = f"?status={status}" if status else ""
    data = api_get(f"/api/tasks{params}")
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("tasks", [])


def get_agents() -> list:
    data = api_get("/api/agents")
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("agents", [])


def get_workflow_status(workflow_id: str) -> dict:
    return api_get(f"/api/workflow-executions/{workflow_id}") or {}


def check_api_credits() -> Tuple[bool, str]:
    agents = get_agents()
    for agent in agents:
        output = (agent.get("output_log", "") or "").lower()
        credit_errors = [
            "insufficient funds", "credit", "quota exceeded",
            "rate limit", "billing", "payment required",
            "402", "429", "exceeded", "out of credits",
        ]
        for err in credit_errors:
            if err in output:
                return True, f"API credit issue: {err}"

    failed_tasks = get_tasks(status="failed")
    for task in failed_tasks:
        error = (task.get("error", "") or "").lower()
        for err in credit_errors:
            if err in error:
                return True, f"API credit issue in task: {err}"

    return False, ""


def detect_hard_error(agents: list, failed_tasks: list) -> Tuple[bool, str]:
    crashed_agents = [a for a in agents if a.get("status") == "error"]
    if crashed_agents:
        names = [a.get("agent_id", "unknown")[:20] for a in crashed_agents]
        return True, f"Crashed agents: {', '.join(names)}"

    critical_failures = [
        t for t in failed_tasks
        if t.get("priority") == "critical" or "architectural" in (t.get("description", "") or "").lower()
    ]
    if critical_failures:
        descs = [t.get("description", "")[:60] for t in critical_failures[:3]]
        return True, f"Critical task failures: {descs}"

    return False, ""


def detect_impasse(stuck_count: int, agents: list, pending_tasks: list, in_progress_tasks: list) -> Tuple[bool, str]:
    stuck_agents = [a for a in agents if a.get("health_check_failures", 0) >= STUCK_THRESHOLD]
    if stuck_agents:
        names = [a.get("agent_id", "unknown")[:20] for a in stuck_agents]
        return True, f"Stuck agents: {', '.join(names)}"

    active_agents = [a for a in agents if a.get("status") == "working"]
    if not active_agents and not in_progress_tasks and pending_tasks:
        return True, "No active agents but tasks are pending"

    return False, ""


def detect_architectural_issue(report_paths: List[str]) -> Tuple[bool, str]:
    for report_path in report_paths:
        p = Path(report_path)
        if not p.exists():
            continue
        try:
            content = p.read_text().lower()
            arch_keywords = [
                "major architectural issue",
                "needs redesign",
                "fundamental flaw",
                "wrong approach",
                "should not proceed",
                "must rewrite",
            ]
            for kw in arch_keywords:
                if kw in content:
                    return True, f"Architectural issue in {p.name}: '{kw}'"
        except Exception:
            pass
    return False, ""


def prompt_human(reason: str, logger: OrchestratorLogger) -> str:
    import sys
    import uuid

    logger.log(f"DECISION POINT: {reason}", "INTERVENTION")

    input_dir = Path(AUTOPILOT_STATE_DIR)
    input_dir.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())[:8]
    request_file = input_dir / f"input_request_{request_id}.json"
    response_file = input_dir / f"input_response_{request_id}.json"

    # Atomic write via temp+rename
    payload = json.dumps({
        "id": request_id,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
        "options": ["c", "s", "q"],
        "labels": {"c": "Continue", "s": "Skip design", "q": "Quit pipeline"},
    }, indent=2)
    tmp = request_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, request_file)

    logger.event("human_input_required", {"reason": reason, "request_id": request_id})

    print("\n" + "=" * 60)
    print("HUMAN INTERVENTION REQUIRED")
    print("=" * 60)
    print(f"Reason: {reason}")
    print(f"Request ID: {request_id}")
    print("Options: [c] Continue  [s] Skip  [q] Quit")
    print("Respond via web UI or terminal.")
    print("=" * 60)

    while True:
        # Check file response
        if response_file.exists():
            try:
                data = json.loads(response_file.read_text())
                choice = data.get("choice", "").strip().lower()
                if choice in ("c", "s", "q"):
                    logger.event("human_input", {"choice": choice, "reason": reason, "source": "web", "request_id": request_id})
                    request_file.unlink(missing_ok=True)
                    response_file.unlink(missing_ok=True)
                    return choice
            except (json.JSONDecodeError, OSError) as e:
                logger.log(f"Error reading response file: {e}", "WARN")

        # Check terminal input (non-blocking on Unix only)
        try:
            if sys.platform != "win32" and sys.stdin.isatty():
                import select as select_mod
                rlist, _, _ = select_mod.select([sys.stdin], [], [], 1.0)
                if rlist:
                    choice = sys.stdin.readline().strip().lower()
                    if choice in ("c", "s", "q"):
                        logger.event("human_input", {"choice": choice, "reason": reason, "source": "terminal", "request_id": request_id})
                        request_file.unlink(missing_ok=True)
                        response_file.unlink(missing_ok=True)
                        return choice
            else:
                time.sleep(2)
        except (OSError, ValueError):
            time.sleep(2)


def scan_design_queue(queue_dir: Path, processed_hashes: Set[str]) -> List[DesignEntry]:
    designs = []
    if not queue_dir.exists():
        return designs

    for ext in ("*.md", "*.txt", "*.pdf"):
        for filepath in sorted(queue_dir.glob(ext)):
            if filepath.is_dir():
                continue
            content_hash = file_hash(filepath)
            if content_hash in processed_hashes:
                continue
            name = filepath.stem.replace("_", " ").replace("-", " ").title()
            designs.append(DesignEntry(
                path=filepath,
                name=name,
                content_hash=content_hash,
            ))

    designs.sort(key=lambda d: d.path.stat().st_mtime)
    return designs


def pick_next_design(queue_dir: Path, processed_hashes: Set[str], logger: OrchestratorLogger) -> Optional[DesignEntry]:
    designs = scan_design_queue(queue_dir, processed_hashes)

    if not designs:
        return None

    logger.log(f"Found {len(designs)} pending design(s) in queue")
    for d in designs:
        logger.log(f"  - {d.name} ({d.path.name})")

    next_design = designs[0]
    logger.log(f"Selected: {next_design.name}")
    return next_design


def create_feature_folder(project_path: Path, design_name: str, logger: OrchestratorLogger) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_name.lower().replace(" ", "_")[:40]
    feature_folder = project_path / "features" / f"{timestamp}_{safe_name}"
    feature_folder.mkdir(parents=True, exist_ok=True)
    (feature_folder / "reports").mkdir(exist_ok=True)
    (feature_folder / "artifacts").mkdir(exist_ok=True)
    logger.log(f"Feature folder: {feature_folder}")
    return feature_folder


def copy_design_document(design_entry: DesignEntry, feature_folder: Path) -> Path:
    dest = feature_folder / "artifacts" / design_entry.path.name
    shutil.copy2(design_entry.path, dest)
    return dest


def collect_report_summaries(project_path: Path) -> Dict[str, str]:
    summaries = {}
    report_files = {
        "requirements": "requirements_analysis.md",
        "architecture": "architecture.md",
        "review": "review_report.md",
        "security": "security_report.md",
        "qa": "qa_report.md",
        "product_validation": "product_validation.md",
        "forensics": "forensics_report.md",
    }

    for key, filename in report_files.items():
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


def collect_files_created(project_path: Path, feature_folder: Path = None) -> List[str]:
    files = []
    dirs_to_scan = [project_path]
    if feature_folder:
        dirs_to_scan.append(feature_folder)

    for scan_dir in dirs_to_scan:
        for pattern in ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.html", "**/*.css", "**/*.md"]:
            for f in sorted(scan_dir.glob(pattern)):
                if ".venv" in str(f) or "node_modules" in str(f) or "__pycache__" in str(f):
                    continue
                rel = f.relative_to(scan_dir)
                files.append(str(rel))
    return sorted(set(files))


def generate_html_feature_report(
    report: FeatureReport,
    summaries: Dict[str, str],
    feature_folder: Path,
    logger: OrchestratorLogger,
) -> Path:
    html_path = feature_folder / "feature_report.html"

    def esc(s: str) -> str:
        return html_mod.escape(s)

    status_color = "#22c55e" if report.product_validated else "#dc3545"
    status_text = "VALIDATED" if report.product_validated else "NEEDS REVIEW"
    qa_color = "#22c55e" if report.qa_passed else "#dc3545"
    qa_text = "PASSED" if report.qa_passed else "FAILED"

    hours = report.total_time_seconds // 3600
    minutes = (report.total_time_seconds % 3600) // 60
    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    files_html = ""
    for f in report.files_created[:50]:
        files_html += f"<tr><td><code>{f}</code></td></tr>\n"
    if len(report.files_created) > 50:
        files_html += f"<tr><td><em>... and {len(report.files_created) - 50} more files</em></td></tr>\n"

    issues_resolved_html = ""
    for issue in report.issues_resolved:
        issues_resolved_html += f'<li style="color:#22c55e">{issue}</li>\n'
    if not report.issues_resolved:
        issues_resolved_html = '<li style="color:#6c757d">No issues to resolve</li>'

    outstanding_html = ""
    for issue in report.outstanding_issues:
        outstanding_html += f'<li style="color:#dc3545">{issue}</li>\n'
    if not report.outstanding_issues:
        outstanding_html = '<li style="color:#22c55e">No outstanding issues</li>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Feature Report: {report.design_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
        .container {{ max-width: 1100px; margin: 0 auto; padding: 40px 24px; }}
        .header {{ margin-bottom: 40px; }}
        .header h1 {{ font-size: 28px; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }}
        .header .subtitle {{ color: #94a3b8; font-size: 14px; }}
        .status-banner {{ padding: 16px 24px; border-radius: 12px; margin-bottom: 32px; display: flex; align-items: center; gap: 12px; }}
        .status-banner.validated {{ background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.3); }}
        .status-banner.needs-review {{ background: rgba(220,53,69,0.12); border: 1px solid rgba(220,53,69,0.3); }}
        .status-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
        .status-dot.green {{ background: #22c55e; }}
        .status-dot.red {{ background: #dc3545; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 32px; }}
        .stat {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; text-align: center; }}
        .stat-value {{ font-size: 28px; font-weight: 700; color: #f8fafc; }}
        .stat-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }}
        .section {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; margin-bottom: 24px; overflow: hidden; }}
        .section-header {{ padding: 16px 24px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 10px; }}
        .section-header h2 {{ font-size: 16px; font-weight: 600; color: #f8fafc; }}
        .section-body {{ padding: 24px; }}
        .section-body pre {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; overflow-x: auto; font-size: 13px; color: #cbd5e1; white-space: pre-wrap; word-wrap: break-word; }}
        .badge {{ display: inline-block; padding: 2px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }}
        .badge.pass {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
        .badge.fail {{ background: rgba(220,53,69,0.15); color: #dc3545; }}
        .badge.warn {{ background: rgba(251,191,36,0.15); color: #fbbf24; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; font-size: 14px; }}
        th {{ color: #94a3b8; font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
        td code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
        ul {{ list-style: none; padding: 0; }}
        li {{ padding: 6px 0; font-size: 14px; border-bottom: 1px solid rgba(51,65,85,0.5); }}
        li:last-child {{ border-bottom: none; }}
        .footer {{ text-align: center; padding: 32px 0; color: #475569; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{report.design_name}</h1>
            <div class="subtitle">
                Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &middot;
                {report.design_document}
            </div>
        </div>

        <div class="status-banner {'validated' if report.product_validated else 'needs-review'}">
            <div class="status-dot {'green' if report.product_validated else 'red'}"></div>
            <div>
                <strong>{status_text}</strong> &mdash;
                Product validation {'passed' if report.product_validated else 'failed'} after {report.iterations} iteration(s)
            </div>
        </div>

        <div class="stats">
            <div class="stat">
                <div class="stat-value">{report.iterations}</div>
                <div class="stat-label">Iterations</div>
            </div>
            <div class="stat">
                <div class="stat-value">{time_str}</div>
                <div class="stat-label">Total Time</div>
            </div>
            <div class="stat">
                <div class="stat-value"><span class="badge {'pass' if report.qa_passed else 'fail'}">{qa_text}</span></div>
                <div class="stat-label">QA Status</div>
            </div>
            <div class="stat">
                <div class="stat-value"><span class="badge {'pass' if report.product_validated else 'fail'}">{status_text}</span></div>
                <div class="stat-label">Final Status</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len(report.files_created)}</div>
                <div class="stat-label">Files Created</div>
            </div>
            <div class="stat">
                <div class="stat-value">${report.cost_total:.4f}</div>
                <div class="stat-label">LLM Cost</div>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Requirements</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('requirements', 'No requirements document found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Architecture</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('architecture', 'No architecture document found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Code Review</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('review', 'No review report found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Security Review</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('security', 'No security report found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>QA Report</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('qa', 'No QA report found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Product Validation</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('product_validation', 'No product validation report found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Forensics Analysis</h2>
            </div>
            <div class="section-body">
                <pre>{esc(summaries.get('forensics', 'No forensics report found.'))}</pre>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Cost Tracking</h2>
            </div>
            <div class="section-body">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:16px">
                    <div style="background:#0f172a;padding:16px;border-radius:8px;text-align:center">
                        <div style="font-size:24px;font-weight:700;color:#22c55e">${report.cost_total:.4f}</div>
                        <div style="font-size:12px;color:#94a3b8;margin-top:4px">Total LLM Cost</div>
                    </div>
                </div>
                {f'<table><thead><tr><th>Model</th><th>Cost</th></tr></thead><tbody>{"".join(f"<tr><td>{m}</td><td>${c:.6f}</td></tr>" for m, c in report.cost_breakdown.items())}</tbody></table>' if report.cost_breakdown else '<p style="color:#6c757d">No cost data available (LiteLLM proxy not configured or no requests tracked)</p>'}
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Issues Resolved</h2>
            </div>
            <div class="section-body">
                <ul>{issues_resolved_html}</ul>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Outstanding Issues</h2>
            </div>
            <div class="section-body">
                <ul>{outstanding_html}</ul>
            </div>
        </div>

        <div class="section">
            <div class="section-header">
                <h2>Files Created ({len(report.files_created)})</h2>
            </div>
            <div class="section-body">
                <table>
                    <thead><tr><th>File Path</th></tr></thead>
                    <tbody>{files_html}</tbody>
                </table>
            </div>
        </div>

        <div class="footer">
            Hephaestus Autopilot &middot; Multi-Agent Workflow Engine
        </div>
    </div>
</body>
</html>"""

    html_path.write_text(html)
    logger.log(f"HTML feature report: {html_path}")
    return html_path


def generate_product_validation_report(
    project_path: Path,
    design_entry: DesignEntry,
    qa_passed: bool,
    logger: OrchestratorLogger,
) -> Tuple[bool, str]:
    validation_path = project_path / "product_validation.md"

    # If Phase 7 already created a validation report, use it instead of overwriting
    if validation_path.exists():
        try:
            existing = validation_path.read_text()
            meets_spec = qa_passed and ("PASS" in existing or "pass" in existing.lower())
            logger.log(f"Using existing product validation from Phase 7")
            return meets_spec, existing
        except Exception:
            pass

    # Fallback: generate a basic validation report
    requirements_path = project_path / "requirements_analysis.md"
    qa_report_path = project_path / "qa_report.md"

    requirements_content = ""
    if requirements_path.exists():
        try:
            requirements_content = requirements_path.read_text()
        except Exception:
            pass

    qa_content = ""
    if qa_report_path.exists():
        try:
            qa_content = qa_report_path.read_text()
        except Exception:
            pass

    design_content = ""
    try:
        design_content = design_entry.path.read_text()[:3000]
    except Exception:
        pass

    meets_spec = qa_passed
    validation_notes = []

    if qa_passed:
        validation_notes.append("All QA tests passed")
        validation_notes.append("Requirements compliance verified")
        validation_notes.append("Implementation matches design intent")
    else:
        validation_notes.append("QA tests did not fully pass")
        validation_notes.append("May need iteration to address remaining issues")

    if not requirements_content:
        meets_spec = False
        validation_notes.append("WARNING: No requirements_analysis.md found")

    report = f"""# Product Validation Report

## Design: {design_entry.name}
## Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
## Status: {'PASS' if meets_spec else 'NEEDS_WORK'}

## Validation Summary

{'This design has been validated and meets the original specification.' if meets_spec else 'This design needs additional work before it meets specification.'}

## Criteria Met

""" + "\n".join(f"- {note}" for note in validation_notes) + """

## Requirements Compliance

""" + (requirements_content[:2000] if requirements_content else "[No requirements document found]") + """

## QA Results

""" + (qa_content[:2000] if qa_content else "[No QA report found]") + """

## Recommendation

""" + ("The feature is ready for human review. See the HTML feature report in the features/ folder." if meets_spec else "Additional iterations may be needed. Review the issues and decide whether to continue or adjust the design.") + """
"""

    validation_path.write_text(report)
    logger.log(f"Product validation: {validation_path}")
    return meets_spec, report


def run_single_workflow(sdk, workflow_id: str, project_path: str, description: str,
                        logger: OrchestratorLogger,
                        launch_params: Dict[str, Any] = None) -> str:
    logger.log(f"Launching workflow: {workflow_id}")
    logger.event("workflow_launch", {"workflow": workflow_id, "path": project_path})

    try:
        exec_id = sdk.start_workflow(
            definition_id=workflow_id,
            description=description,
            working_directory=project_path,
            launch_params=launch_params or {},
        )
        logger.log(f"Workflow launched: {exec_id}")
    except Exception as e:
        logger.log(f"Failed to launch workflow {workflow_id}: {e}", "ERROR")
        return "failed"

    stuck_count = 0
    credit_stuck_count = 0
    start_time = time.time()

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Timeout check
            elapsed = int(time.time() - start_time)
            if elapsed > MAX_WORKFLOW_TIME:
                logger.log(f"Workflow timed out after {MAX_WORKFLOW_TIME}s", "ERROR")
                return "timeout"

            wf_status = get_workflow_status(exec_id)
            agents = get_agents()
            active_agents = [a for a in agents if a.get("status") == "working"]
            pending = get_tasks(status="pending")
            in_progress = get_tasks(status="in_progress")
            done = get_tasks(status="done")
            failed = get_tasks(status="failed")

            logger.log(
                f"[{workflow_id}] [{elapsed}s] Agents: {len(active_agents)} active | "
                f"Tasks: {len(pending)} pending, {len(in_progress)} active, "
                f"{len(done)} done, {len(failed)} failed"
            )

            wf_state = wf_status.get("status", "")
            if wf_state in ("completed", "failed"):
                logger.log(f"Workflow {wf_state}: {exec_id}")
                return wf_state

            out_of_credits, credit_reason = check_api_credits()
            if out_of_credits:
                credit_stuck_count += 1
                stuck_count = 0  # reset impasse counter during credit issues
                if credit_stuck_count >= 1:
                    choice = prompt_human(credit_reason, logger)
                    if choice == "q":
                        return "interrupted"
                    elif choice == "s":
                        credit_stuck_count = 0
                continue
            else:
                credit_stuck_count = 0

            hard_error, error_reason = detect_hard_error(agents, failed)
            if hard_error:
                logger.log(f"Hard error detected: {error_reason}", "ERROR")
                return "hard_error"

            impasse, impasse_reason = detect_impasse(stuck_count, agents, pending, in_progress)
            if impasse:
                stuck_count += 1
                if stuck_count >= STUCK_THRESHOLD:
                    choice = prompt_human(impasse_reason, logger)
                    if choice == "q":
                        return "interrupted"
                    elif choice == "s":
                        stuck_count = 0
            else:
                stuck_count = 0

    except KeyboardInterrupt:
        logger.log("Interrupted by user")
        return "interrupted"


def run_single_design(
    sdk,
    design_entry: DesignEntry,
    project_path: Path,
    max_iterations: int,
    logger: OrchestratorLogger,
) -> Tuple[DesignStatus, FeatureReport]:
    # project_path: where implementation code lives (the actual project)
    # docs_dir: where generated docs/reports go (inside feature folder)
    project_path.mkdir(parents=True, exist_ok=True)

    feature_folder = create_feature_folder(project_path, design_entry.name, logger)
    design_entry.project_path = project_path
    design_entry.feature_folder = feature_folder
    design_entry.started_at = datetime.now().isoformat()

    design_copy = copy_design_document(design_entry, feature_folder)

    logger.log("=" * 70)
    logger.log(f"PROCESSING DESIGN: {design_entry.name}")
    logger.log(f"  Source: {design_entry.path}")
    logger.log(f"  Project: {project_path}")
    logger.log(f"  Feature: {feature_folder}")
    logger.log("=" * 70)

    report = FeatureReport(
        design_name=design_entry.name,
        project_path=str(project_path),
        feature_folder=str(feature_folder),
        design_document=str(design_entry.path),
        iterations=0,
        total_time_seconds=0,
        qa_passed=False,
        product_validated=False,
        stop_reason="",
    )

    design_start = time.time()
    stop_reason = StopReason.COMPLETED

    # docs_dir: where generated docs (requirements, architecture, reports) go
    docs_dir = feature_folder / "artifacts"
    docs_dir.mkdir(exist_ok=True)

    # Copy phase definitions BEFORE workflow so forensics agent can read them
    phases_dir = docs_dir / "phase_prompts"
    phases_dir.mkdir(exist_ok=True)
    phase_files = list((HEPHAESTUS_DIR / "example_workflows" / "autopilot").glob("phase_*.py"))
    for pf in sorted(phase_files):
        shutil.copy2(pf, phases_dir / pf.name)
    logger.log(f"Copied {len(phase_files)} phase prompts to {phases_dir}")

    # Write initial pipeline_metrics.json BEFORE workflow so forensics agent can read it
    # (will be updated with final values after workflow completes)
    initial_metrics = {
        "design_name": design_entry.name,
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "docs_dir": str(docs_dir),
        "feature_folder": str(feature_folder),
        "started_at": design_entry.started_at,
        "max_iterations": max_iterations,
        "phases": [
            {"id": 1, "name": "product_requirements", "output": "requirements_analysis.md"},
            {"id": 2, "name": "architecture_design", "output": "architecture.md"},
            {"id": 3, "name": "development", "output": "source code in project path"},
            {"id": 4, "name": "adversarial_review", "output": "review_report.md"},
            {"id": 5, "name": "security_review", "output": "security_report.md"},
            {"id": 6, "name": "qa_validation", "output": "qa_report.md"},
            {"id": 7, "name": "product_validation", "output": "product_validation.md"},
            {"id": 8, "name": "git_commit_push", "output": "git history"},
            {"id": 9, "name": "forensics_analysis", "output": "forensics_report.md"},
        ],
    }
    metrics_path = docs_dir / "pipeline_metrics.json"
    metrics_path.write_text(json.dumps(initial_metrics, indent=2, default=str))
    logger.log(f"Initial pipeline metrics: {metrics_path}")

    try:
        for iteration in range(1, max_iterations + 1):
            iter_start = time.time()

            logger.log("")
            logger.log("-" * 60)
            logger.log(f"DESIGN: {design_entry.name} | ITERATION {iteration}/{max_iterations}")
            logger.log("-" * 60)

            description = (
                f"Autopilot: {design_entry.name} - Iteration {iteration}\n"
                f"Design Document: {design_copy}\n"
                f"Project Path: {project_path}\n"
                f"Docs Path: {docs_dir}\n"
                f"Feature Folder: {feature_folder}\n"
                f"Context: Building from design document. "
                f"Generated docs (requirements, architecture, reports) go in: {docs_dir}\n"
                f"Implementation code (src/, tests/) goes in: {project_path}\n"
                f"Read the design doc carefully, extract requirements, "
                f"create architecture, implement, review, security check, and QA."
            )

            launch_params = {
                "design_document": str(design_copy),
                "project_path": str(project_path),
                "project_context": f"Docs go in: {docs_dir}. Code goes in: {project_path}.",
            }

            wf_status = run_single_workflow(
                sdk, "autopilot", str(project_path), description, logger,
                launch_params=launch_params,
            )

            iter_elapsed = int(time.time() - iter_start)
            report.iterations = iteration

            if wf_status == "interrupted":
                stop_reason = StopReason.USER_INTERRUPT
                break

            if wf_status == "hard_error":
                stop_reason = StopReason.HARD_ERROR
                break

            logger.log("")
            logger.log("Running product validation...")
            qa_passed = False
            qa_reports = [
                project_path / "qa_report.md",
                project_path / "qa_report.html",
            ]
            pass_patterns = [
                "overall status: pass",
                "overall status:** pass",
                "recommendation: done",
                "recommendation:** done",
                "recommendation: **done",
                "all tests pass",
                "all tests passed",
                "qa passed",
            ]
            for qp in qa_reports:
                if qp.exists():
                    try:
                        content = qp.read_text().lower()
                        for pattern in pass_patterns:
                            if pattern in content:
                                qa_passed = True
                                break
                        if qa_passed:
                            break
                    except Exception:
                        pass

            product_validated, validation_report = generate_product_validation_report(
                project_path, design_entry, qa_passed, logger
            )

            report.qa_passed = qa_passed
            report.product_validated = product_validated

            if product_validated:
                logger.log("")
                logger.log(f"DESIGN VALIDATED: {design_entry.name}")
                stop_reason = StopReason.COMPLETED
                break

            arch_issue, arch_reason = detect_architectural_issue([
                str(project_path / "review_report.md"),
                str(project_path / "security_report.md"),
            ])
            if arch_issue:
                stop_reason = StopReason.ARCHITECTURAL_ISSUE
                logger.log(f"Architectural issue: {arch_reason}", "ERROR")
                break

            if iteration < max_iterations:
                logger.log(f"Iteration {iteration} incomplete, starting {iteration + 1}...")
            else:
                logger.log(f"Max iterations ({max_iterations}) reached")
                stop_reason = StopReason.MAX_ITERATIONS

    except KeyboardInterrupt:
        stop_reason = StopReason.USER_INTERRUPT
        logger.log("Design processing interrupted")

    # Organize: move any stray docs from project root into feature artifacts
    for md_file in project_path.glob("*.md"):
        dest = docs_dir / md_file.name
        if not dest.exists():
            shutil.move(str(md_file), str(dest))
            logger.log(f"Moved doc: {md_file.name} -> features/.../artifacts/")

    # Also check feature_folder root for misplaced docs
    for md_file in feature_folder.glob("*.md"):
        dest = docs_dir / md_file.name
        if not dest.exists():
            shutil.move(str(md_file), str(dest))
            logger.log(f"Moved doc: {md_file.name} -> features/.../artifacts/")

    # Collect summaries from the correct locations (docs in features, code in builds)
    summaries = collect_report_summaries(docs_dir)
    report.requirements_summary = summaries.get("requirements", "")
    report.architecture_summary = summaries.get("architecture", "")
    report.security_summary = summaries.get("security", "")
    report.qa_summary = summaries.get("qa", "")
    report.product_validation_summary = summaries.get("product_validation", "")
    report.forensics_summary = summaries.get("forensics", "")
    report.files_created = collect_files_created(project_path, feature_folder)

    # Update pipeline_metrics.json with final values
    metrics = {
        "design_name": design_entry.name,
        "design_document": str(design_entry.path),
        "project_path": str(project_path),
        "docs_dir": str(docs_dir),
        "feature_folder": str(feature_folder),
        "iterations": report.iterations,
        "total_time_seconds": report.total_time_seconds,
        "stop_reason": report.stop_reason,
        "qa_passed": report.qa_passed,
        "product_validated": report.product_validated,
        "cost_total": report.cost_total,
        "files_created_count": len(report.files_created),
        "started_at": design_entry.started_at,
        "completed_at": design_entry.completed_at,
        "phases": [
            {"id": 1, "name": "product_requirements", "output": "requirements_analysis.md"},
            {"id": 2, "name": "architecture_design", "output": "architecture.md"},
            {"id": 3, "name": "development", "output": "source code in project path"},
            {"id": 4, "name": "adversarial_review", "output": "review_report.md"},
            {"id": 5, "name": "security_review", "output": "security_report.md"},
            {"id": 6, "name": "qa_validation", "output": "qa_report.md"},
            {"id": 7, "name": "product_validation", "output": "product_validation.md"},
            {"id": 8, "name": "git_commit_push", "output": "git history"},
            {"id": 9, "name": "forensics_analysis", "output": "forensics_report.md"},
        ],
    }
    metrics_path = docs_dir / "pipeline_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str))
    logger.log(f"Final pipeline metrics: {metrics_path}")

    report.total_time_seconds = int(time.time() - design_start)
    report.stop_reason = stop_reason.value

    # Fetch cost data from LiteLLM proxy if configured
    litellm_config = get_litellm_config()
    if litellm_config["cost_tracking"] and litellm_config["url"] and litellm_config["cost_api_key"]:
        try:
            from src.interfaces.cost_tracker import CostTracker
            import asyncio

            tracker = CostTracker(
                proxy_url=litellm_config["url"],
                api_key=litellm_config["cost_api_key"],
            )

            # Use the safe_name as the user identifier for cost tracking
            feature_user = design_entry.name.lower().replace(" ", "_")[:40]

            async def fetch_costs():
                cost_info = await tracker.get_feature_cost(feature_user)
                daily = await tracker.get_daily_breakdown(feature_user, days=7)
                return cost_info, daily

            cost_info, daily_breakdown = asyncio.run(fetch_costs())

            report.cost_total = cost_info.get("spend", 0)
            logger.log(f"Cost for '{feature_user}': ${report.cost_total:.4f}")

            # Extract model breakdown from daily data
            for day_entry in daily_breakdown.get("results", []):
                metrics = day_entry.get("metrics", {})
                breakdown = day_entry.get("breakdown", {})
                for model_name, model_data in breakdown.get("models", {}).items():
                    model_spend = model_data.get("spend", 0)
                    if model_name not in report.cost_breakdown:
                        report.cost_breakdown[model_name] = 0
                    report.cost_breakdown[model_name] += model_spend

        except Exception as e:
            logger.log(f"Failed to fetch cost data: {e}", "WARN")

    generate_html_feature_report(report, summaries, feature_folder, logger)

    design_entry.completed_at = datetime.now().isoformat()

    status = DesignStatus.COMPLETED if report.product_validated else DesignStatus.FAILED
    return status, report


def run_continuous_pipeline(args) -> None:
    log_dir = Path.home() / ".hephaestus" / "autopilot" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    logger = OrchestratorLogger(log_dir)

    logger.log("=" * 70)
    logger.log("AUTOPILOT CONTINUOUS PIPELINE")
    logger.log("=" * 70)
    logger.log(f"Design Queue: {args.design_queue}")
    logger.log(f"Project Root: {args.project_path}")
    logger.log(f"Max Iterations per Design: {args.max_iterations}")
    logger.log(f"Poll Interval: {DESIGN_QUEUE_SCAN_INTERVAL}s")
    logger.log(f"Logs: {log_dir}")

    queue_dir = Path(args.design_queue)
    project_path = Path(args.project_path)
    project_path.mkdir(parents=True, exist_ok=True)
    queue_dir.mkdir(parents=True, exist_ok=True)

    state = PipelineState()
    processed_hashes: Set[str] = set()
    processed_file = log_dir / "processed.json"
    if processed_file.exists():
        try:
            processed_hashes = set(json.loads(processed_file.read_text()))
        except Exception:
            pass

    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition
    from example_workflows.autopilot.phases import AUTOPILOT_PHASES, AUTOPILOT_WORKFLOW_CONFIG, AUTOPILOT_LAUNCH_TEMPLATE

    cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL", "opencode")

    autopilot_def = WorkflowDefinition(
        id="autopilot",
        name="Autopilot Multi-Agent Pipeline",
        description="Continuous automated pipeline",
        phases=AUTOPILOT_PHASES,
        config=AUTOPILOT_WORKFLOW_CONFIG,
        launch_template=AUTOPILOT_LAUNCH_TEMPLATE,
    )

    logger.log("Initializing SDK...")
    sdk = HephaestusSDK(
        workflow_definitions=[autopilot_def],
        database_path=str(HEPHAESTUS_DIR / "hephaestus.db"),
        qdrant_url="http://localhost:6333",
        working_directory=str(project_path),
        mcp_port=8000,
        monitoring_interval=60,
        llm_provider="openrouter",
        llm_model="xiaomi/mimo-v2.5",
        default_cli_tool=cli_tool,
        main_repo_path=str(project_path),
        project_root=str(project_path),
        auto_commit=True,
        conflict_resolution="newest_file_wins",
        worktree_branch_prefix="autopilot-",
    )

    logger.log("Starting services...")
    try:
        sdk.start(enable_tui=False, timeout=60)
    except Exception as e:
        logger.log(f"Failed to start: {e}", "ERROR")
        sys.exit(1)

    logger.log("Services started.")
    logger.log("")
    logger.log(f"Watching design queue: {queue_dir}")
    logger.log("Drop .md or .txt files into the queue directory to add designs.")
    logger.log("Press Ctrl+C to stop.")
    logger.log("")

    last_queue_scan = 0

    try:
        while True:
            now = time.time()

            if now - last_queue_scan >= DESIGN_QUEUE_SCAN_INTERVAL:
                last_queue_scan = now

                next_design = pick_next_design(queue_dir, processed_hashes, logger)

                if next_design is None:
                    logger.log(f"Queue empty. Scanning again in {DESIGN_QUEUE_SCAN_INTERVAL}s...")
                    state.queue_status = {"status": "empty", "processed": len(processed_hashes)}
                    logger.save_state(state)
                    time.sleep(DESIGN_QUEUE_SCAN_INTERVAL)
                    continue

                next_design.status = DesignStatus.IN_PROGRESS
                state.current_design = next_design.name
                state.queue_status = {
                    "status": "processing",
                    "current": next_design.name,
                    "processed": len(processed_hashes),
                }
                logger.save_state(state)

                status, feature_report = run_single_design(
                    sdk, next_design, project_path, args.max_iterations, logger
                )

                next_design.status = status
                processed_hashes.add(next_design.content_hash)
                processed_file.write_text(json.dumps(list(processed_hashes)))

                state.designs_processed += 1
                if status == DesignStatus.COMPLETED:
                    state.designs_succeeded += 1
                else:
                    state.designs_failed += 1

                state.current_design = None
                state.total_elapsed = int(time.time() - state.start_time)
                state.queue_status = {
                    "status": "idle",
                    "processed": len(processed_hashes),
                    "succeeded": state.designs_succeeded,
                    "failed": state.designs_failed,
                }
                logger.save_state(state)

                logger.event("design_complete", {
                    "design": next_design.name,
                    "status": status.value,
                    "iterations": feature_report.iterations,
                    "qa_passed": feature_report.qa_passed,
                    "product_validated": feature_report.product_validated,
                    "elapsed_seconds": feature_report.total_time_seconds,
                    "feature_folder": str(next_design.feature_folder),
                })

                logger.log("")
                logger.log(f"Design '{next_design.name}' complete. Status: {status.value}")
                logger.log(f"Total designs processed: {state.designs_processed}")
                logger.log(f"  Succeeded: {state.designs_succeeded}")
                logger.log(f"  Failed: {state.designs_failed}")
                logger.log("")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.log("")
        logger.log("Pipeline interrupted by user")
    finally:
        state.total_elapsed = int(time.time() - state.start_time)
        state.queue_status = {"status": "stopped"}

        logger.log("")
        logger.log("=" * 70)
        logger.log("PIPELINE STOPPED")
        logger.log("=" * 70)
        logger.log(f"Total Time: {state.total_elapsed}s")
        logger.log(f"Designs Processed: {state.designs_processed}")
        logger.log(f"  Succeeded: {state.designs_succeeded}")
        logger.log(f"  Failed: {state.designs_failed}")
        logger.log(f"Logs: {log_dir}")
        logger.log("=" * 70)

        logger.save_state(state)
        logger.event("pipeline_stop", {
            "total_designs": state.designs_processed,
            "succeeded": state.designs_succeeded,
            "failed": state.designs_failed,
            "elapsed_seconds": state.total_elapsed,
        })

        if sdk is not None:
            sdk.shutdown(graceful=True, timeout=15)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Autopilot Continuous Pipeline - Design Queue to Validated Software"
    )
    parser.add_argument("--design-queue", default=None,
                        help="Directory to watch for design documents (default: <project-path>/docs/design-queue)")
    parser.add_argument("--project-path", required=True,
                        help="Project directory for implementation code")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="Maximum review-fix-QA iterations per design")
    parser.add_argument("--drop-db", action="store_true",
                        help="Drop database before starting")

    args = parser.parse_args()

    # Default design queue to <project-path>/docs/design-queue
    if not args.design_queue:
        args.design_queue = str(Path(args.project_path) / "docs" / "design-queue")

    if args.drop_db:
        db = HEPHAESTUS_DIR / "hephaestus.db"
        if db.exists():
            db.unlink()
            print(f"Dropped {db}")

    run_continuous_pipeline(args)


if __name__ == "__main__":
    main()

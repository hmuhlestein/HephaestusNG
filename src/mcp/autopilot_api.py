"""API endpoints for the Autopilot dashboard."""

import asyncio
import collections
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])

DESIGN_QUEUE_DIR = ""
FEATURES_DIR = ""
AUTOPILOT_STATE_DIR = os.path.expanduser("~/.hephaestus/autopilot")

ALLOWED_EXTENSIONS = {".md", ".txt"}

# ── TTL cache ────────────────────────────────────────────────────

T = TypeVar("T")

_cache: Dict[str, Tuple[Any, float]] = {}
CACHE_TTL = 10.0


def _cached(key: str, ttl: float = CACHE_TTL) -> Optional[Any]:
    entry = _cache.get(key)
    if entry is None:
        return None
    data, ts = entry
    if time.monotonic() - ts >= ttl:
        return None
    return data


def _store(key: str, data: Any) -> Any:
    _cache[key] = (data, time.monotonic())
    return data


def _invalidate(*keys: str):
    for k in keys:
        _cache.pop(k, None)


# ── Path safety ──────────────────────────────────────────────────

def _safe_path(base: str, *parts: str) -> Path:
    if not base:
        raise HTTPException(500, "Directory not configured")
    resolved = (Path(base) / Path(*parts)).resolve()
    base_resolved = Path(base).resolve()
    if not (resolved == base_resolved or str(resolved).startswith(str(base_resolved) + os.sep)):
        raise HTTPException(400, "Invalid path")
    return resolved


def _feature_status(metrics: dict) -> str:
    if metrics.get("product_validated"):
        return "validated"
    if metrics.get("stop_reason") in ("hard_error", "impasse", "architectural_issue"):
        return "failed"
    return "needs_review"


# ── File I/O ─────────────────────────────────────────────────────

def _get_latest_run_dir() -> Optional[Path]:
    base = Path(AUTOPILOT_STATE_DIR)
    if not base.exists():
        return None
    runs = sorted(base.glob("run-*"), reverse=True)
    return runs[0] if runs else None


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_jsonl_tail(path: Path, limit: int = 100) -> List[dict]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            raw_lines = collections.deque(f, maxlen=limit)
        entries = []
        for line in raw_lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
    except Exception:
        return []


async def _is_orchestrator_running() -> bool:
    cached = _cached("orchestrator_running", ttl=5.0)
    if cached is not None:
        return cached

    pid_file = Path(AUTOPILOT_STATE_DIR) / "orchestrator.pid"
    if not pid_file.exists():
        return _store("orchestrator_running", False)
    try:
        pid = int(pid_file.read_text().strip())
        proc = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return _store("orchestrator_running", proc.returncode == 0)
    except Exception:
        return _store("orchestrator_running", False)


# ── Pydantic models ──────────────────────────────────────────────

class DesignQueueItem(BaseModel):
    filename: str
    name: str
    size_bytes: int
    modified: str
    extension: str


class DesignQueueAdd(BaseModel):
    name: str
    content: str
    extension: str = ".md"


class FeatureSummary(BaseModel):
    id: str
    name: str
    status: str
    iterations: int
    total_time_seconds: int
    stop_reason: str
    cost_total: float
    cost_currency: str
    created_at: str
    has_report: bool


class FeatureDetail(BaseModel):
    id: str
    name: str
    status: str
    iterations: int
    total_time_seconds: int
    stop_reason: str
    qa_passed: bool
    product_validated: bool
    has_report: bool
    design_name: str
    project_path: str
    feature_folder: str
    requirements_summary: str
    architecture_summary: str
    security_summary: str
    qa_summary: str
    product_validation_summary: str
    forensics_summary: str
    files_created: List[str]
    issues_resolved: List[str]
    outstanding_issues: List[str]
    cost_total: float
    cost_breakdown: Dict[str, float]
    cost_currency: str
    created_at: str
    artifacts: List[Dict[str, Any]]


class PipelineStatus(BaseModel):
    running: bool
    current_design: Optional[str] = None
    designs_processed: int = 0
    designs_succeeded: int = 0
    designs_failed: int = 0
    total_elapsed: int = 0
    queue_depth: int = 0
    last_event: Optional[Dict[str, Any]] = None


class MessageItem(BaseModel):
    timestamp: str
    type: str
    data: Dict[str, Any]


# ── Pipeline Status ───────────────────────────────────────────────

@router.get("/status", response_model=PipelineStatus)
async def get_pipeline_status():
    cached = _cached("status", ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    running = await _is_orchestrator_running()

    state = _cached("state", ttl=5.0)
    if state is None:
        if run_dir:
            state = _store("state", _read_json(run_dir / "state.json") or {})
        else:
            state = _store("state", {})

    queue_depth = 0
    if DESIGN_QUEUE_DIR and Path(DESIGN_QUEUE_DIR).exists():
        for ext in ALLOWED_EXTENSIONS:
            queue_depth += len(list(Path(DESIGN_QUEUE_DIR).glob(f"*{ext}")))

    last_event = _cached("last_event", ttl=5.0)
    if last_event is None:
        if run_dir:
            events = _read_jsonl_tail(run_dir / "events.jsonl", limit=1)
            last_event = _store("last_event", events[-1] if events else None)
        else:
            last_event = _store("last_event", None)

    result = PipelineStatus(
        running=running,
        current_design=state.get("current_design"),
        designs_processed=state.get("designs_processed", 0),
        designs_succeeded=state.get("designs_succeeded", 0),
        designs_failed=state.get("designs_failed", 0),
        total_elapsed=state.get("total_elapsed", 0),
        queue_depth=queue_depth,
        last_event=last_event,
    )
    return _store("status", result)


# ── Design Queue ─────────────────────────────────────────────────

def _get_queue_order_path() -> Optional[Path]:
    if not DESIGN_QUEUE_DIR:
        return None
    return Path(DESIGN_QUEUE_DIR) / ".queue_order.json"


def _load_queue_order() -> List[str]:
    path = _get_queue_order_path()
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def _save_queue_order(order: List[str]):
    path = _get_queue_order_path()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(order))


@router.get("/queue", response_model=List[DesignQueueItem])
async def list_design_queue():
    cached = _cached("queue")
    if cached is not None:
        return cached

    if not DESIGN_QUEUE_DIR or not Path(DESIGN_QUEUE_DIR).exists():
        return _store("queue", [])

    queue_path = Path(DESIGN_QUEUE_DIR)
    saved_order = _load_queue_order()

    files_by_name: Dict[str, Path] = {}
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            files_by_name[f.name] = f

    ordered_names = [n for n in saved_order if n in files_by_name]
    unordered = [n for n in files_by_name if n not in saved_order]
    all_names = ordered_names + sorted(unordered, key=lambda n: files_by_name[n].stat().st_mtime)

    items = []
    for fname in all_names:
        f = files_by_name[fname]
        stat = f.stat()
        name = f.stem.replace("_", " ").replace("-", " ").title()
        items.append(DesignQueueItem(
            filename=f.name,
            name=name,
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            extension=f.suffix,
        ))

    return _store("queue", items)


class QueueReorderRequest(BaseModel):
    filenames: List[str]


@router.post("/queue/reorder")
async def reorder_queue(req: QueueReorderRequest):
    if not DESIGN_QUEUE_DIR or not Path(DESIGN_QUEUE_DIR).exists():
        raise HTTPException(500, "DESIGN_QUEUE_DIR not configured")

    queue_path = Path(DESIGN_QUEUE_DIR)
    existing = set()
    for ext in ALLOWED_EXTENSIONS:
        for f in queue_path.glob(f"*{ext}"):
            existing.add(f.name)

    for fname in req.filenames:
        if fname not in existing:
            raise HTTPException(400, f"Unknown file: {fname}")

    _save_queue_order(req.filenames)
    _invalidate("queue")
    return {"order": req.filenames}


@router.post("/queue", response_model=DesignQueueItem)
async def add_to_queue(item: DesignQueueAdd):
    if not DESIGN_QUEUE_DIR:
        raise HTTPException(500, "DESIGN_QUEUE_DIR not configured")

    queue_path = Path(DESIGN_QUEUE_DIR)
    queue_path.mkdir(parents=True, exist_ok=True)

    ext = item.extension if item.extension in ALLOWED_EXTENSIONS else ".md"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in item.name)
    safe_name = safe_name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(400, "Invalid design name")
    filename = f"{safe_name}{ext}"
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)

    if filepath.exists():
        raise HTTPException(409, f"Design '{filename}' already exists in queue")

    filepath.write_text(item.content)
    stat = filepath.stat()

    _invalidate("queue", "status")

    return DesignQueueItem(
        filename=filename,
        name=item.name,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        extension=ext,
    )


@router.delete("/queue/{filename}")
async def remove_from_queue(filename: str):
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    filepath.unlink()
    _invalidate("queue", "status")
    return {"removed": filename}


@router.get("/queue/{filename}/content")
async def get_queue_item_content(filename: str):
    filepath = _safe_path(DESIGN_QUEUE_DIR, filename)
    if not filepath.exists():
        raise HTTPException(404, f"Design '{filename}' not found")
    return {"filename": filename, "content": filepath.read_text(errors="replace")}


# ── Features Gallery ─────────────────────────────────────────────

def _scan_features() -> List[Dict[str, Any]]:
    cached = _cached("features", ttl=30.0)
    if cached is not None:
        return cached

    if not FEATURES_DIR or not Path(FEATURES_DIR).exists():
        return _store("features", [])

    features = []
    features_path = Path(FEATURES_DIR)

    for feature_dir in sorted(features_path.iterdir(), reverse=True):
        if not feature_dir.is_dir():
            continue

        metrics_path = feature_dir / "artifacts" / "pipeline_metrics.json"
        metrics = _read_json(metrics_path) or {}

        report_path = feature_dir / "feature_report.html"
        created_at = datetime.fromtimestamp(
            feature_dir.stat().st_mtime, tz=timezone.utc
        ).isoformat()

        dir_name = feature_dir.name
        if "_" in dir_name:
            name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title()
        else:
            name = dir_name

        features.append({
            "id": feature_dir.name,
            "name": name,
            "status": _feature_status(metrics),
            "iterations": metrics.get("iterations", 0),
            "total_time_seconds": metrics.get("total_time_seconds", 0),
            "stop_reason": metrics.get("stop_reason", "unknown"),
            "cost_total": metrics.get("cost_total", 0),
            "cost_currency": metrics.get("cost_currency", "USD"),
            "created_at": created_at,
            "has_report": report_path.exists(),
        })

    return _store("features", features)


@router.get("/features", response_model=List[FeatureSummary])
async def list_features():
    return _scan_features()


@router.get("/features/{feature_id}", response_model=FeatureDetail)
async def get_feature_detail(feature_id: str):
    cache_key = f"feature:{feature_id}"
    cached = _cached(cache_key, ttl=30.0)
    if cached is not None:
        return cached

    feature_dir = _safe_path(FEATURES_DIR, feature_id)
    if not feature_dir.exists() or not feature_dir.is_dir():
        raise HTTPException(404, f"Feature '{feature_id}' not found")

    report_path = feature_dir / "feature_report.html"
    metrics = _read_json(feature_dir / "artifacts" / "pipeline_metrics.json") or {}

    artifacts_dir = feature_dir / "artifacts"
    artifacts = []
    if artifacts_dir.exists():
        for f in sorted(artifacts_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                artifacts.append({
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "type": "markdown" if f.suffix == ".md" else
                            "json" if f.suffix == ".json" else
                            "text" if f.suffix == ".txt" else
                            "other",
                })

    summaries = {}
    summary_files = {
        "requirements_summary": "requirements_analysis.md",
        "architecture_summary": "architecture.md",
        "security_summary": "security_report.md",
        "qa_summary": "qa_report.md",
        "product_validation_summary": "product_validation.md",
        "forensics_summary": "forensics_report.md",
    }
    for key, fname in summary_files.items():
        fpath = artifacts_dir / fname
        if fpath.exists():
            content = fpath.read_text(errors="replace")
            summaries[key] = content[:500] + ("..." if len(content) > 500 else "")

    dir_name = feature_dir.name
    name = dir_name.split("_", 1)[1].replace("_", " ").replace("-", " ").title() if "_" in dir_name else dir_name

    created_at = datetime.fromtimestamp(
        feature_dir.stat().st_mtime, tz=timezone.utc
    ).isoformat()

    result = FeatureDetail(
        id=feature_dir.name,
        name=name,
        status=_feature_status(metrics),
        iterations=metrics.get("iterations", 0),
        total_time_seconds=metrics.get("total_time_seconds", 0),
        stop_reason=metrics.get("stop_reason", "unknown"),
        qa_passed=metrics.get("qa_passed", False),
        product_validated=metrics.get("product_validated", False),
        has_report=report_path.exists(),
        design_name=metrics.get("design_name", name),
        project_path=metrics.get("project_path", ""),
        feature_folder=metrics.get("feature_folder", str(feature_dir)),
        requirements_summary=summaries.get("requirements_summary", ""),
        architecture_summary=summaries.get("architecture_summary", ""),
        security_summary=summaries.get("security_summary", ""),
        qa_summary=summaries.get("qa_summary", ""),
        product_validation_summary=summaries.get("product_validation_summary", ""),
        forensics_summary=summaries.get("forensics_summary", ""),
        files_created=metrics.get("files_created", []),
        issues_resolved=metrics.get("issues_resolved", []),
        outstanding_issues=metrics.get("outstanding_issues", []),
        cost_total=metrics.get("cost_total", 0),
        cost_breakdown=metrics.get("cost_breakdown", {}),
        cost_currency=metrics.get("cost_currency", "USD"),
        created_at=created_at,
        artifacts=artifacts,
    )
    return _store(cache_key, result)


@router.get("/features/{feature_id}/report")
async def get_feature_report(feature_id: str):
    report_path = _safe_path(FEATURES_DIR, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return HTMLResponse(content=report_path.read_text(errors="replace"))


@router.get("/features/{feature_id}/artifacts/{artifact_name}")
async def get_feature_artifact(feature_id: str, artifact_name: str):
    cache_key = f"artifact:{feature_id}:{artifact_name}"
    cached = _cached(cache_key, ttl=60.0)
    if cached is not None:
        return cached

    artifact_path = _safe_path(FEATURES_DIR, feature_id, "artifacts", artifact_name)
    if not artifact_path.exists():
        raise HTTPException(404, f"Artifact '{artifact_name}' not found")
    return _store(cache_key, {"name": artifact_name, "content": artifact_path.read_text(errors="replace")})


@router.get("/features/{feature_id}/download")
async def download_feature_report(feature_id: str):
    report_path = _safe_path(FEATURES_DIR, feature_id, "feature_report.html")
    if not report_path.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/html",
        filename=f"{feature_id}_report.html",
    )


# ── Message Center ───────────────────────────────────────────────

@router.get("/messages", response_model=List[MessageItem])
async def get_messages(limit: int = Query(50, ge=1, le=500)):
    cache_key = f"messages:{limit}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    if not run_dir:
        return _store(cache_key, [])

    events = _read_jsonl_tail(run_dir / "events.jsonl", limit=limit)
    result = [
        MessageItem(
            timestamp=e.get("timestamp", ""),
            type=e.get("type", "unknown"),
            data={k: v for k, v in e.items() if k not in ("timestamp", "type")},
        )
        for e in events
    ]
    return _store(cache_key, result)


@router.get("/logs")
async def get_logs(lines: int = Query(100, ge=1, le=2000)):
    cache_key = f"logs:{lines}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    if not run_dir:
        return _store(cache_key, {"lines": []})

    log_path = run_dir / "orchestrator.log"
    if not log_path.exists():
        return _store(cache_key, {"lines": []})

    try:
        all_lines = log_path.read_text(errors="replace").splitlines()
        return _store(cache_key, {"lines": all_lines[-lines:]})
    except Exception:
        return _store(cache_key, {"lines": []})


# ── Human Input ─────────────────────────────────────────────────

STALE_INPUT_SECONDS = 3600  # 1 hour


class HumanInputRequest(BaseModel):
    id: str
    reason: str
    timestamp: str
    options: List[str]
    labels: Dict[str, str]


class HumanInputResponse(BaseModel):
    request_id: str
    choice: str


def _find_pending_input() -> Optional[Path]:
    """Find the first non-stale input request file."""
    input_dir = Path(AUTOPILOT_STATE_DIR)
    if not input_dir.exists():
        return None
    for f in sorted(input_dir.glob("input_request_*.json")):
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > STALE_INPUT_SECONDS:
                f.unlink(missing_ok=True)
                # Also clean up any orphaned response
                rid = data.get("id", "")
                resp = input_dir / f"input_response_{rid}.json"
                resp.unlink(missing_ok=True)
                continue
            return f
        except Exception:
            continue
    return None


@router.get("/input", response_model=Optional[HumanInputRequest])
async def get_human_input_request():
    """Check if the orchestrator is waiting for human input."""
    request_file = _find_pending_input()
    if not request_file:
        return None
    try:
        data = json.loads(request_file.read_text())
        return HumanInputRequest(**data)
    except Exception:
        return None


@router.post("/input")
async def submit_human_input(resp: HumanInputResponse):
    """Submit a human input response to the orchestrator."""
    if resp.choice not in ("c", "s", "q"):
        raise HTTPException(400, "Invalid choice. Must be 'c', 's', or 'q'.")

    # Verify the request still exists
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{resp.request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Input request not found or already answered.")

    response_file = Path(AUTOPILOT_STATE_DIR) / f"input_response_{resp.request_id}.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp+rename
    payload = json.dumps({
        "request_id": resp.request_id,
        "choice": resp.choice,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    tmp = response_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, response_file)

    _invalidate("status")
    return {"submitted": resp.choice, "request_id": resp.request_id}


@router.delete("/input/{request_id}")
async def dismiss_human_input(request_id: str):
    """Dismiss a pending human input request without responding."""
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Request not found.")
    # Only delete the request — orchestrator will see it's gone and stop polling
    request_file.unlink(missing_ok=True)
    _invalidate("status")
    return {"dismissed": request_id}


# ── Config ───────────────────────────────────────────────────────

def configure_autopilot_api(
    design_queue_dir: str = "",
    features_dir: str = "",
):
    global DESIGN_QUEUE_DIR, FEATURES_DIR
    DESIGN_QUEUE_DIR = design_queue_dir or os.getenv("DESIGN_QUEUE_DIR", "")
    FEATURES_DIR = features_dir or os.getenv("FEATURES_DIR", "")
    _invalidate("queue", "features", "status")
    logger.info(f"Autopilot API configured: queue={DESIGN_QUEUE_DIR}, features={FEATURES_DIR}")

"""Project cost/budget routes: default-budget settings, cost-entry ingestion,
and cost-summary queries (task/workflow/feature/design/project).

Split out of project_routes.py (SOLID review: that file mixed project CRUD,
7 cost-accounting endpoints, and design-file browsing/management -- see
docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md's finding on
src/mcp/autopilot/project_routes.py). Mounted alongside project_routes.router
in src/mcp/autopilot/__init__.py.
"""

import asyncio
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator, model_validator, validator
from sqlalchemy import func as sqlfunc

from src.core.agent_identity import is_known_system_identity
from src.mcp.autopilot._shared import _invalidate

# Import authentication function from server module
from src.mcp.server._shared import verify_agent_authentication
from src.mcp.server.oauth_routes import _check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


class CostEntryCreate(BaseModel):
    """Request model for creating a cost entry."""

    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    source: str
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float
    raw_usage: Optional[dict] = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        """Validate source is a known cost collection source."""
        valid_sources = {"pi", "claude_code", "opencode", "codex", "openrouter_direct"}
        if v not in valid_sources:
            raise ValueError(f"source must be one of {valid_sources}, got '{v}'")
        return v

    @field_validator("cost_usd")
    @classmethod
    def validate_cost_usd(cls, v: float) -> float:
        """Validate cost_usd is a reasonable positive value."""
        if v < 0:
            raise ValueError("cost_usd must be non-negative")
        if v > 1000.0:  # Cap at $1000 per single LLM call
            raise ValueError("cost_usd exceeds maximum allowed value of $1000")
        return v

    @field_validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    @classmethod
    def validate_token_counts(cls, v: int) -> int:
        """Validate token counts are non-negative."""
        if v < 0:
            raise ValueError("token counts must be non-negative")
        if v > 10_000_000:  # 10M tokens max per call
            raise ValueError("token count exceeds maximum allowed value")
        return v

    @validator("raw_usage")
    def validate_raw_usage(cls, v: Optional[dict]) -> Optional[dict]:
        """Validate raw_usage is not excessively large.

        SECURITY: Prevents abuse where a malicious caller could store
        arbitrarily large payloads in the raw_usage JSON column,
        consuming database storage and slowing queries.
        """
        if v is not None:
            import sys as _sys

            size = _sys.getsizeof(json.dumps(v))
            if size > 10_000:  # 10KB limit
                raise ValueError("raw_usage exceeds maximum size of 10KB")
        return v

    @validator("model")
    def validate_model(cls, v: Optional[str]) -> Optional[str]:
        """Validate model string length."""
        if v is not None and len(v) > 200:
            raise ValueError("model name exceeds maximum length of 200 characters")
        return v

    @model_validator(mode="after")
    def validate_entity_link(self) -> "CostEntryCreate":
        """Require at least one of task_id or workflow_id for cost attribution.

        Without an entity link, the cost entry bypasses budget enforcement
        because no derivation rollup occurs (record_cost skips derive_task_cost
        and derive_workflow_cost when both are None).
        """
        if self.task_id is None and self.workflow_id is None:
            raise ValueError("At least one of task_id or workflow_id must be provided for cost attribution and budget enforcement")
        return self


class DefaultBudgetUpdate(BaseModel):
    """None clears the default; a positive number sets it."""

    default_cost_limit_usd: Optional[float] = None


@router.get("/settings/default-budget")
async def get_default_budget():
    """The system-wide default spend cap applied to newly created projects."""
    from src.services.system_settings import get_default_cost_limit

    loop = asyncio.get_running_loop()
    value = await loop.run_in_executor(None, get_default_cost_limit)
    return {"default_cost_limit_usd": value}


@router.put("/settings/default-budget")
async def put_default_budget(req: DefaultBudgetUpdate):
    """Set or clear the default. Existing projects are untouched -- this only
    seeds projects created afterwards, so raising it does not silently widen
    the cap on a project someone deliberately constrained."""
    from src.services.system_settings import set_default_cost_limit

    loop = asyncio.get_running_loop()
    try:
        value = await loop.run_in_executor(
            None, set_default_cost_limit, req.default_cost_limit_usd
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    _invalidate("status")
    return {"default_cost_limit_usd": value}


@router.post("/cost-entries")
async def create_cost_entry(
    req: CostEntryCreate,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Create a cost entry and trigger cost derivation rollup.

    Used by Pi extension (real-time) and external callers.
    Requires valid agent authentication via X-Agent-ID header.
    """
    # SECURITY: Verify agent authentication before allowing cost entry creation
    if not await verify_agent_authentication(agent_id):
        logger.warning(f"Unauthenticated cost entry attempt from agent {agent_id}")
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    # SECURITY: Rate limit by client IP, not X-Agent-ID. The header is
    # caller-supplied and several prefixes (sdk-*, mcp-*) are trusted
    # unconditionally by verify_agent_authentication, so a caller could
    # otherwise reset the rate-limit bucket on every request just by
    # rotating the header value. The server binds 0.0.0.0 (hephaestus_config.yaml),
    # so this endpoint is reachable beyond localhost.
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_entry:{client_host}", max_requests=60):
        logger.warning(f"Rate limit exceeded for cost entries from {client_host} (agent {agent_id})")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost entries per minute.",
        )

    # SECURITY (ticket-5a75167a): verify_agent_authentication only checks
    # that agent_id names a real/trusted caller -- it never binds that
    # identity to the entry being written. A caller authenticated as one
    # real agent could otherwise supply a *different* agent_id in the body
    # and post a cost entry that impersonates another agent's task, which
    # src/services/cost_collection_service.py's real-time-suppression logic
    # (see ticket-9259f) treats as proof that task's own session reported in
    # real time -- permanently hiding its real JSONL-derived cost. System/
    # SDK identities (KNOWN_SYSTEM_AGENTS, sdk-*/mcp-* prefixes) have no
    # single agent to bind to and post cost entries on behalf of whichever
    # agent/task they're servicing, so only a real per-agent UUID identity
    # is bound here.
    if not is_known_system_identity(agent_id):
        if req.agent_id and req.agent_id != agent_id:
            raise HTTPException(
                status_code=403,
                detail="agent_id does not match authenticated X-Agent-ID",
            )
        req.agent_id = agent_id

    from src.core.cost_derivation import record_cost
    from src.core.database import get_db

    with get_db() as db:
        entry = record_cost(
            db=db,
            cost_usd=req.cost_usd,
            source=req.source,
            task_id=req.task_id,
            agent_id=req.agent_id,
            workflow_id=req.workflow_id,
            model=req.model,
            input_tokens=req.input_tokens,
            output_tokens=req.output_tokens,
            cache_read_tokens=req.cache_read_tokens,
            cache_write_tokens=req.cache_write_tokens,
            reasoning_tokens=req.reasoning_tokens,
            raw_usage=req.raw_usage,
        )

        return {"id": entry.id, "cost_usd": entry.cost_usd}


class CostEntrySummary(BaseModel):
    """Summary of a single cost entry."""

    id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    source: str
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float
    recorded_at: Optional[str] = None


class TaskCostSummary(BaseModel):
    """Cost summary for a task."""

    task_id: str
    task_description: str
    cost_total_usd: float
    entries: List[CostEntrySummary]


class WorkflowCostSummary(BaseModel):
    """Cost summary for a workflow."""

    workflow_id: str
    workflow_name: str
    cost_total_usd: float
    tasks: List[TaskCostSummary]


class FeatureCostSummary(BaseModel):
    """Cost summary for a feature."""

    feature_id: str
    feature_name: str
    cost_total_usd: float
    workflows: List[WorkflowCostSummary]


class DesignCostSummary(BaseModel):
    """Cost summary for a design."""

    design_id: str
    design_name: str
    cost_total_usd: float
    features: List[FeatureCostSummary]


class ProjectCostSummary(BaseModel):
    """Cost summary for a project."""

    project_id: str
    project_name: str
    cost_total_usd: float
    cost_limit_usd: Optional[float] = None
    remaining_usd: Optional[float] = None
    is_over_budget: bool = False
    designs: List[DesignCostSummary]


@router.get("/tasks/{task_id}/costs", response_model=TaskCostSummary)
async def get_task_costs(
    task_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a single task.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_task_cost
    from src.core.database import CostEntry, Task, get_db

    with get_db() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            raise HTTPException(404, "Task not found")

        cost = derive_task_cost(db, task_id, write_back=False)
        entries = db.query(CostEntry).filter(CostEntry.task_id == task_id).order_by(CostEntry.recorded_at.desc()).limit(100).all()

        return TaskCostSummary(
            task_id=task.id,
            task_description=(task.raw_description or "")[:200],
            cost_total_usd=cost,
            entries=[
                CostEntrySummary(
                    id=e.id,
                    task_id=e.task_id,
                    agent_id=e.agent_id,
                    workflow_id=e.workflow_id,
                    source=e.source,
                    model=e.model,
                    input_tokens=e.input_tokens or 0,
                    output_tokens=e.output_tokens or 0,
                    cost_usd=e.cost_usd,
                    recorded_at=e.recorded_at.isoformat() if e.recorded_at else None,
                )
                for e in entries
            ],
        )


@router.get("/workflows/{workflow_id}/costs", response_model=WorkflowCostSummary)
async def get_workflow_costs(
    workflow_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a workflow.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_workflow_cost
    from src.core.database import CostEntry, Task, Workflow, get_db

    with get_db() as db:
        workflow = db.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(404, "Workflow not found")

        cost = derive_workflow_cost(db, workflow_id, write_back=False)

        # Get tasks with costs
        tasks = db.query(Task).filter(Task.workflow_id == workflow_id).all()
        task_summaries = []
        for t in tasks:
            task_cost = db.query(sqlfunc.sum(CostEntry.cost_usd)).filter(CostEntry.task_id == t.id).scalar() or 0.0
            if task_cost > 0:
                task_summaries.append(
                    TaskCostSummary(
                        task_id=t.id,
                        task_description=(t.raw_description or "")[:200],
                        cost_total_usd=task_cost,
                        entries=[],
                    )
                )

        return WorkflowCostSummary(
            workflow_id=workflow.id,
            workflow_name=workflow.name or workflow.id[:8],
            cost_total_usd=cost,
            tasks=task_summaries,
        )


@router.get("/features/{feature_id}/costs", response_model=FeatureCostSummary)
async def get_feature_costs(
    feature_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a feature.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_feature_cost, derive_workflow_cost
    from src.core.database import Feature, Workflow, get_db

    with get_db() as db:
        feature = db.query(Feature).filter_by(id=feature_id).first()
        if not feature:
            raise HTTPException(404, "Feature not found")

        cost = derive_feature_cost(db, feature_id, write_back=False)

        # Get workflows for this feature
        workflows = db.query(Workflow).filter(Workflow.feature_id == feature_id).all()
        workflow_summaries = []
        for w in workflows:
            wf_cost = derive_workflow_cost(db, w.id, write_back=False)
            if wf_cost > 0:
                workflow_summaries.append(
                    WorkflowCostSummary(
                        workflow_id=w.id,
                        workflow_name=w.name or w.id[:8],
                        cost_total_usd=wf_cost,
                        tasks=[],
                    )
                )

        return FeatureCostSummary(
            feature_id=feature.id,
            feature_name=feature.name or feature.feature_key,
            cost_total_usd=cost,
            workflows=workflow_summaries,
        )


@router.get("/designs/{design_id}/costs", response_model=DesignCostSummary)
async def get_design_costs(
    design_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a design.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_design_cost, derive_feature_cost
    from src.core.database import AutopilotDesign, Feature, get_db

    with get_db() as db:
        design = db.query(AutopilotDesign).filter_by(id=design_id).first()
        if not design:
            raise HTTPException(404, "Design not found")

        cost = derive_design_cost(db, design_id, write_back=False)

        # Get features for this design
        features = db.query(Feature).filter(Feature.design_id == design_id).all()
        feature_summaries = []
        for feat in features:
            feat_cost = derive_feature_cost(db, feat.id, write_back=False)
            if feat_cost > 0:
                feature_summaries.append(
                    FeatureCostSummary(
                        feature_id=feat.id,
                        feature_name=feat.name or feat.feature_key,
                        cost_total_usd=feat_cost,
                        workflows=[],
                    )
                )

        return DesignCostSummary(
            design_id=design.id,
            design_name=design.name or design.filename,
            cost_total_usd=cost,
            features=feature_summaries,
        )


@router.get("/projects/{project_id}/costs", response_model=ProjectCostSummary)
async def get_project_costs(
    project_id: str,
    request: Request,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get cost breakdown for a project.

    SECURITY: Requires valid agent authentication.
    Cost data is sensitive financial information.
    """
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )
    client_host = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"cost_query:{client_host}", max_requests=60):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 60 cost queries per minute.",
        )
    from src.core.cost_derivation import derive_design_cost, derive_project_cost
    from src.core.database import AutopilotDesign, AutopilotProject, get_db

    with get_db() as db:
        project = db.query(AutopilotProject).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(404, "Project not found")

        cost = derive_project_cost(db, project_id, write_back=False)

        # Get designs for this project
        designs = db.query(AutopilotDesign).filter(AutopilotDesign.project_id == project_id).all()
        design_summaries = []
        for d in designs:
            d_cost = derive_design_cost(db, d.id, write_back=False)
            if d_cost > 0:
                design_summaries.append(
                    DesignCostSummary(
                        design_id=d.id,
                        design_name=d.name or d.filename,
                        cost_total_usd=d_cost,
                        features=[],
                    )
                )

        remaining = None
        is_over = False
        if project.cost_limit_usd is not None:
            remaining = max(0.0, project.cost_limit_usd - cost)
            is_over = cost >= project.cost_limit_usd

        return ProjectCostSummary(
            project_id=project.id,
            project_name=project.name,
            cost_total_usd=cost,
            cost_limit_usd=project.cost_limit_usd,
            remaining_usd=remaining,
            is_over_budget=is_over,
            designs=design_summaries,
        )


"""Phase detail/reset, and phase prompt version + task prompt override
editing.

Split out of FrontendAPI (src/mcp/frontend/_shared.py) -- SOLID review 1.7:
routing was already split into per-domain routers, but the class underneath
stayed one 2673-line, 41-method god object. This is the phase_routes.py
domain's share of that split.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import func

from src.agents.manager import AgentManager
from src.autopilot.orchestrator.engine_client import terminate_agent
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    PhasePromptVersion,
    Task,
    TaskPromptOverride,
    utc_now,
)
from src.core.phase_lookup import resolve_task_phase
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

class PhaseService:
    """API handlers for phase detail/reset and phase prompt version /
    task prompt override editing."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        phase_manager: PhaseManager = None,
    ):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager

    async def get_phase_details(self, phase_id: str) -> Dict[str, Any]:
        """Get detailed phase information from database."""
        session = self.db_manager.get_session()
        try:
            # Get the phase from database
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            # Return phase details directly from database
            return {
                "description": phase.description or "",
                "done_definitions": phase.done_definitions or [],
                "additional_notes": phase.additional_notes or "",
                "outputs": phase.outputs or "",
                "next_steps": phase.next_steps or "",
            }
        finally:
            session.close()

    # ── Phase Prompt Editor ──────────────────────────────────────────────

    async def update_phase(
        self, phase_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Partial update of phase definition fields."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            # Only allow mutable fields with type validation
            mutable_fields = {
                "description",
                "done_definitions",
                "additional_notes",
                "outputs",
                "next_steps",
                "working_directory",
                "cli_tool",
                "cli_model",
                "glm_api_token_env",
            }
            str_fields = {
                "description",
                "additional_notes",
                "outputs",
                "next_steps",
                "working_directory",
                "cli_tool",
                "cli_model",
                "glm_api_token_env",
            }
            list_fields = {"done_definitions"}

            for key, value in updates.items():
                if key not in mutable_fields:
                    raise HTTPException(
                        status_code=400, detail=f"Field '{key}' is not mutable"
                    )
                if (
                    key in str_fields
                    and value is not None
                    and not isinstance(value, str)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Field '{key}' must be a string or null",
                    )
                if (
                    key in list_fields
                    and value is not None
                    and not isinstance(value, list)
                ):
                    raise HTTPException(
                        status_code=400, detail=f"Field '{key}' must be a list or null"
                    )
                setattr(phase, key, value)

            session.commit()
            return {
                "success": True,
                "phase": {
                    "id": phase.id,
                    "order": phase.order,
                    "name": phase.name,
                    "description": phase.description,
                    "done_definitions": phase.done_definitions,
                    "additional_notes": phase.additional_notes,
                    "outputs": phase.outputs,
                    "next_steps": phase.next_steps,
                    "working_directory": phase.working_directory,
                    "cli_tool": phase.cli_tool,
                    "cli_model": phase.cli_model,
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()


    async def reset_phase(
        self, phase_id: str, target_status: str, force: bool = False
    ) -> Dict[str, Any]:
        """Reset phase execution status, handling active agents."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            valid_statuses = {
                "pending",
                "in_progress",
                "completed",
                "failed",
                "skipped",
            }
            if target_status not in valid_statuses:
                raise HTTPException(
                    status_code=400,
                    detail=f"target_status must be one of: {valid_statuses}",
                )

            # Find active agents in this phase
            active_agents = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(Task.phase_id == phase.id)
                .filter(Agent.status == "working")
                .all()
            )

            if active_agents and not force:
                return {
                    "success": False,
                    "active_agents": len(active_agents),
                    "message": f"{len(active_agents)} agents are active. Use force=true to terminate them.",
                    "requires_confirmation": True,
                }

            # Terminate active agents if force
            terminated_count = 0
            if active_agents and force:
                import asyncio
                import functools

                loop = asyncio.get_event_loop()
                for agent in active_agents:
                    try:
                        # Terminate via tmux kill-session (non-blocking)
                        import subprocess

                        _tmux_name = agent.tmux_session_name
                        if _tmux_name:
                            await loop.run_in_executor(
                                None,
                                functools.partial(
                                    subprocess.run,
                                    [
                                        "tmux",
                                        "kill-session",
                                        "-t",
                                        _tmux_name,
                                    ],
                                    timeout=5,
                                    capture_output=True,
                                ),
                            )
                        if terminate_agent(agent.id, session=session):
                            terminated_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to terminate agent {agent.id}: {e}")

            # Fail assigned tasks
            tasks = (
                session.query(Task)
                .filter(Task.phase_id == phase.id)
                .filter(Task.status.in_(["assigned", "in_progress", "pending"]))
                .all()
            )
            for task in tasks:
                task.status = "failed"
                task.failure_reason = f"Phase reset to {target_status}"
                task.completed_at = utc_now()

            # Update phase execution status
            from src.core.database import PhaseExecution

            pe = (
                session.query(PhaseExecution)
                .filter_by(phase_id=phase.id)
                .order_by(PhaseExecution.started_at.desc())
                .first()
            )
            if pe:
                pe.status = target_status
                if target_status in ("completed", "failed"):
                    pe.completed_at = utc_now()

            session.commit()
            return {
                "success": True,
                "terminated_agents": terminated_count,
                "reset_tasks": len(tasks),
                "message": f"Phase reset to {target_status}",
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def get_phase_prompt_versions(self, phase_id: str) -> Dict[str, Any]:
        """List prompt versions for a phase (newest first)."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            versions = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id)
                .order_by(PhasePromptVersion.version.desc())
                .all()
            )

            return {
                "versions": [
                    {
                        "version": v.version,
                        "status": v.status,
                        "created_by": v.created_by,
                        "created_at": v.created_at.isoformat() + "Z"
                        if v.created_at
                        else None,
                        "change_summary": v.change_summary,
                        "parent_version": v.parent_version,
                        "changed_fields": list(
                            {
                                f
                                for f, val in [
                                    ("description", v.description),
                                    ("done_definitions", v.done_definitions),
                                    ("additional_notes", v.additional_notes),
                                    ("outputs", v.outputs),
                                    ("next_steps", v.next_steps),
                                ]
                                if val is not None and val != "" and val != []
                            }
                        ),
                    }
                    for v in versions
                ]
            }
        finally:
            session.close()

    async def get_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Get a specific prompt version's content."""
        session = self.db_manager.get_session()
        try:
            pv = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=version)
                .first()
            )
            if not pv:
                raise HTTPException(
                    status_code=404,
                    detail=f"Version {version} not found for phase {phase_id}",
                )

            return {
                "version": pv.version,
                "status": pv.status,
                "description": pv.description,
                "done_definitions": pv.done_definitions or [],
                "additional_notes": pv.additional_notes,
                "outputs": pv.outputs,
                "next_steps": pv.next_steps,
                "change_summary": pv.change_summary,
                "created_by": pv.created_by,
                "created_at": pv.created_at.isoformat() + "Z" if pv.created_at else None,
                "parent_version": pv.parent_version,
            }
        finally:
            session.close()

    async def create_phase_prompt_version(
        self, phase_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a new prompt version for a phase."""
        import asyncio

        from sqlalchemy.exc import IntegrityError

        last_error = None
        for attempt in range(3):
            session = self.db_manager.get_session()
            try:
                phase = session.query(Phase).filter_by(id=phase_id).first()
                if not phase:
                    raise HTTPException(status_code=404, detail="Phase not found")

                max_version = (
                    session.query(func.max(PhasePromptVersion.version))
                    .filter_by(phase_id=phase_id)
                    .scalar()
                    or 0
                )
                new_version = max_version + 1
                publish = data.get("publish", False)

                new_pv = PhasePromptVersion(
                    id=f"{phase_id}_v{new_version}",
                    phase_id=phase_id,
                    version=new_version,
                    status="active" if publish else "draft",
                    description=data.get("description", phase.description or ""),
                    done_definitions=data.get(
                        "done_definitions", phase.done_definitions or []
                    ),
                    additional_notes=data.get(
                        "additional_notes", phase.additional_notes
                    ),
                    outputs=data.get("outputs", phase.outputs),
                    next_steps=data.get("next_steps", phase.next_steps),
                    change_summary=data.get("change_summary", ""),
                    created_by=data.get("created_by", "ui-user"),
                    parent_version=max_version if max_version > 0 else None,
                )
                session.add(new_pv)

                if publish:
                    existing = (
                        session.query(PhasePromptVersion)
                        .filter_by(phase_id=phase_id, status="active")
                        .all()
                    )
                    for pv in existing:
                        pv.status = "archived"
                    phase.description = data.get("description", phase.description)
                    phase.done_definitions = data.get(
                        "done_definitions", phase.done_definitions
                    )
                    phase.additional_notes = data.get(
                        "additional_notes", phase.additional_notes
                    )
                    phase.outputs = data.get("outputs", phase.outputs)
                    phase.next_steps = data.get("next_steps", phase.next_steps)

                session.commit()

                diff_result = {}
                if max_version > 0 and new_pv.parent_version:
                    parent_pv = (
                        session.query(PhasePromptVersion)
                        .filter_by(phase_id=phase_id, version=max_version)
                        .first()
                    )
                    if parent_pv:
                        from src.prompts.assembler import PromptAssembler

                        old_asm = PromptAssembler(
                            phase_description=parent_pv.description,
                            done_definitions=parent_pv.done_definitions or [],
                            additional_notes=parent_pv.additional_notes,
                            outputs=parent_pv.outputs,
                            next_steps=parent_pv.next_steps,
                        )
                        new_asm = PromptAssembler(
                            phase_description=new_pv.description,
                            done_definitions=new_pv.done_definitions or [],
                            additional_notes=new_pv.additional_notes,
                            outputs=new_pv.outputs,
                            next_steps=new_pv.next_steps,
                        )
                        diff_result = old_asm.diff(new_asm)

                return {
                    "success": True,
                    "version": new_version,
                    "status": new_pv.status,
                    "created_at": new_pv.created_at.isoformat() + "Z"
                    if new_pv.created_at
                    else None,
                    "created_by": new_pv.created_by,
                    "diff": diff_result,
                }
            except IntegrityError:
                session.rollback()
                last_error = "Version conflict"
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            except HTTPException:
                session.close()
                raise
            except Exception as e:
                session.rollback()
                session.close()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    session.close()
                except Exception:
                    pass

        raise HTTPException(
            status_code=500,
            detail=f"Failed to create version after retries: {last_error}",
        )

    async def publish_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Publish a draft version as active."""
        session = self.db_manager.get_session()
        try:
            pv = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=version)
                .first()
            )
            if not pv:
                raise HTTPException(
                    status_code=404, detail=f"Version {version} not found"
                )

            # Demote existing active
            existing_active = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, status="active")
                .all()
            )
            for v in existing_active:
                v.status = "archived"

            pv.status = "active"

            # Update phase definition
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if phase:
                phase.description = pv.description
                phase.done_definitions = pv.done_definitions
                phase.additional_notes = pv.additional_notes
                phase.outputs = pv.outputs
                phase.next_steps = pv.next_steps

            session.commit()
            return {"success": True, "version": version, "status": "active"}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def restore_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Restore an older version as a new active version."""
        import asyncio

        from sqlalchemy.exc import IntegrityError

        last_error = None
        for attempt in range(3):
            session = self.db_manager.get_session()
            try:
                pv = (
                    session.query(PhasePromptVersion)
                    .filter_by(phase_id=phase_id, version=version)
                    .first()
                )
                if not pv:
                    raise HTTPException(
                        status_code=404, detail=f"Version {version} not found"
                    )

                max_version = (
                    session.query(func.max(PhasePromptVersion.version))
                    .filter_by(phase_id=phase_id)
                    .scalar()
                    or 0
                )
                new_version = max_version + 1

                new_pv = PhasePromptVersion(
                    id=f"{phase_id}_v{new_version}",
                    phase_id=phase_id,
                    version=new_version,
                    status="active",
                    description=pv.description,
                    done_definitions=pv.done_definitions,
                    additional_notes=pv.additional_notes,
                    outputs=pv.outputs,
                    next_steps=pv.next_steps,
                    change_summary=f"Restored from version {version}",
                    created_by="ui-user",
                    parent_version=version,
                )
                session.add(new_pv)

                existing_active = (
                    session.query(PhasePromptVersion)
                    .filter_by(phase_id=phase_id, status="active")
                    .all()
                )
                for v in existing_active:
                    if v.id != new_pv.id:
                        v.status = "archived"

                phase = session.query(Phase).filter_by(id=phase_id).first()
                if phase:
                    phase.description = pv.description
                    phase.done_definitions = pv.done_definitions
                    phase.additional_notes = pv.additional_notes
                    phase.outputs = pv.outputs
                    phase.next_steps = pv.next_steps

                session.commit()
                return {
                    "success": True,
                    "version": new_version,
                    "restored_from": version,
                    "status": "active",
                }
            except IntegrityError:
                session.rollback()
                last_error = "Version conflict"
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            except HTTPException:
                session.close()
                raise
            except Exception as e:
                session.rollback()
                session.close()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    session.close()
                except Exception:
                    pass

        raise HTTPException(
            status_code=500,
            detail=f"Failed to restore version after retries: {last_error}",
        )

    async def get_phase_prompt_preview(
        self, phase_id: str, variables: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Render a preview of the assembled prompt."""
        try:
            from src.prompts.assembler import assemble_phase_prompt

            result = assemble_phase_prompt(phase_id, variables=variables)
            return {
                "system_prompt": result.system_prompt,
                "user_prompt": result.user_prompt,
                "variables_used": result.variables_used,
                "variables_missing": result.variables_missing,
                "warnings": result.warnings,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_phase_prompt_diff(
        self, phase_id: str, v1: int, v2: int
    ) -> Dict[str, Any]:
        """Get diff between two versions."""
        session = self.db_manager.get_session()
        try:
            pv1 = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=v1)
                .first()
            )
            pv2 = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=v2)
                .first()
            )
            if not pv1:
                raise HTTPException(status_code=404, detail=f"Version {v1} not found")
            if not pv2:
                raise HTTPException(status_code=404, detail=f"Version {v2} not found")

            from src.prompts.assembler import PromptAssembler

            assembler1 = PromptAssembler(
                phase_description=pv1.description,
                done_definitions=pv1.done_definitions or [],
                additional_notes=pv1.additional_notes,
                outputs=pv1.outputs,
                next_steps=pv1.next_steps,
            )
            assembler2 = PromptAssembler(
                phase_description=pv2.description,
                done_definitions=pv2.done_definitions or [],
                additional_notes=pv2.additional_notes,
                outputs=pv2.outputs,
                next_steps=pv2.next_steps,
            )
            diff = assembler1.diff(assembler2)
            diff["from_version"] = v1
            diff["to_version"] = v2
            return diff
        finally:
            session.close()

    async def get_task_prompt_overrides(self, task_id: str) -> Dict[str, Any]:
        """Get prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if not override:
                return {"system_prompt": None, "user_prompt": None}

            return {
                "system_prompt": override.system_prompt,
                "user_prompt": override.user_prompt,
                "updated_at": override.updated_at.isoformat() + "Z"
                if override.updated_at
                else None,
                "updated_by": override.updated_by,
            }
        finally:
            session.close()

    async def set_task_prompt_overrides(
        self, task_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Set prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            if task.status in ("done", "failed", "duplicated"):
                raise HTTPException(
                    status_code=400, detail="Cannot edit prompts for completed tasks"
                )

            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if override:
                if data.get("system_prompt") is not None:
                    override.system_prompt = data["system_prompt"]
                if data.get("user_prompt") is not None:
                    override.user_prompt = data["user_prompt"]
                override.updated_by = data.get("updated_by", "ui-user")
            else:
                override = TaskPromptOverride(
                    task_id=task_id,
                    system_prompt=data.get("system_prompt"),
                    user_prompt=data.get("user_prompt"),
                    updated_by=data.get("updated_by", "ui-user"),
                )
                session.add(override)

            session.commit()

            # Build effective prompt using already-loaded data (no N+1 query)
            from src.prompts.assembler import PromptAssembler

            phase = None
            if task.phase_id:
                phase = resolve_task_phase(session, task)

            assembler = PromptAssembler(
                phase_description=phase.description if phase else "",
                done_definitions=phase.done_definitions if phase else [],
                additional_notes=phase.additional_notes if phase else None,
                outputs=phase.outputs if phase else None,
                next_steps=phase.next_steps if phase else None,
                phase_order=phase.order if phase else None,
                phase_name=phase.name if phase else None,
            )
            effective = assembler.render(
                task_description=task.enriched_description or task.raw_description,
                task_done_definition=task.done_definition,
                agent_id=task.assigned_agent_id,
                task_id=task.id,
                task_system_prompt=override.system_prompt,
                task_user_prompt=override.user_prompt,
            )

            return {
                "success": True,
                "overrides": {
                    "system_prompt": override.system_prompt,
                    "user_prompt": override.user_prompt,
                },
                "effective_prompt": {
                    "system_prompt": effective.system_prompt[:500] + "..."
                    if len(effective.system_prompt) > 500
                    else effective.system_prompt,
                    "user_prompt": effective.user_prompt[:500] + "..."
                    if len(effective.user_prompt) > 500
                    else effective.user_prompt,
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def clear_task_prompt_overrides(self, task_id: str) -> Dict[str, Any]:
        """Clear prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if override:
                session.delete(override)
                session.commit()
            return {"success": True, "message": "Overrides cleared"}
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

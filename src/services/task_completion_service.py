"""Task-completion side effects: memory persistence, output-artifact
validation, ticket auto-creation, validator spawning, cost collection,
and git commit + ticket linking.

Extracted from src/mcp/server.py's update_task_status, a single 450-line
route handler that fused all of these concerns inline — see
docs/SOLID_OO_REVIEW.md finding 1.2. (Spec-gate firing left this module:
fire_spec_gate_if_ready now lives in
src/autopilot/orchestrator/phase_transitions.py.)

Implementation now lives in src/services/task_completion/ sub-modules
(per design_docs/phase_1b_decomposition.md section 4.4).  This class
retains one delegating @staticmethod per implementation function so that
every existing test @patch("...TaskCompletionService.<method>") target
keeps working.
"""


class TaskCompletionService:
    """Side effects triggered when an agent reports a task's status."""

    @staticmethod
    async def record_learnings(
        session,
        agent_id: str,
        task_id: str,
        key_learnings: list,
        code_changes: list,
    ) -> None:
        from src.services.task_completion.memory import record_learnings
        return await record_learnings(session, agent_id, task_id, key_learnings, code_changes)

    @staticmethod
    def verify_output_artifact(session, task, phase=None):
        from src.services.task_completion.verification import verify_output_artifact
        return verify_output_artifact(session, task, phase)

    @staticmethod
    def verify_gate_result_schema(session, task, phase=None):
        from src.services.task_completion.verification import verify_gate_result_schema
        return verify_gate_result_schema(session, task, phase)

    @staticmethod
    def verify_no_open_tickets(session, task, phase=None):
        from src.services.task_completion.verification import verify_no_open_tickets
        return verify_no_open_tickets(session, task, phase)

    @staticmethod
    def _parse_forensics_recommendations(report_text: str) -> list:
        from src.services.task_completion.tickets import _parse_forensics_recommendations
        return _parse_forensics_recommendations(report_text)

    @staticmethod
    async def create_tickets_from_forensics_report(session, task) -> int:
        from src.services.task_completion.tickets import create_tickets_from_forensics_report
        return await create_tickets_from_forensics_report(session, task)

    @staticmethod
    async def spawn_validation(
        agent_id: str,
        task_id: str,
        task_workflow_id,
        task_validation_iteration: int,
    ) -> None:
        from src.services.task_completion.validation import spawn_validation
        return await spawn_validation(agent_id, task_id, task_workflow_id, task_validation_iteration)

    @staticmethod
    def verify_output_survived_commit(session, task, phase=None):
        from src.services.task_completion.verification import verify_output_survived_commit
        return verify_output_survived_commit(session, task, phase)

    @staticmethod
    def verify_development_produced_a_commit(session, task, phase=None):
        from src.services.task_completion.verification import verify_development_produced_a_commit
        return verify_development_produced_a_commit(session, task, phase)

    @staticmethod
    def verify_git_expert_merged_and_pushed(session, task, phase=None):
        from src.services.task_completion.verification import verify_git_expert_merged_and_pushed
        return verify_git_expert_merged_and_pushed(session, task, phase)

    @staticmethod
    def verify_requirements_cover_scope_cli_flags(session, task, phase=None):
        from src.services.task_completion.verification import verify_requirements_cover_scope_cli_flags
        return verify_requirements_cover_scope_cli_flags(session, task, phase)

    @staticmethod
    def verify_scope_cli_flags_are_implemented(session, task, phase=None):
        from src.services.task_completion.verification import verify_scope_cli_flags_are_implemented
        return verify_scope_cli_flags_are_implemented(session, task, phase)

    @staticmethod
    async def commit_and_link_ticket(session, agent_id: str, task, summary: str):
        from src.services.task_completion.git_link import commit_and_link_ticket
        return await commit_and_link_ticket(session, agent_id, task, summary)

    @staticmethod
    def collect_cost_on_completion(task_id: str) -> None:
        from src.services.task_completion.cost import collect_cost_on_completion
        return collect_cost_on_completion(task_id)

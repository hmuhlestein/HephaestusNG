"""Sub-modules for task-completion side effects.

Extracted from src.services.task_completion_service.TaskCompletionService per
the Phase 1b decomposition plan (design_docs/phase_1b_decomposition.md,
section 4.4).  Each sub-module corresponds to one concern-cluster of the
original monolithic class.  The parent module
(src.services.task_completion_service) retains a thin facade class whose
@staticmethod methods delegate here, preserving every existing test patch path.
"""

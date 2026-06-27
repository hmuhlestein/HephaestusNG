"""Centralized workflow registry.

Workflow definitions are auto-discovered from config/workflows/ subdirectories.
Any directory that contains a workflow.yaml is loaded as a WorkflowDefinition.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.workflow_engine.yaml_loader import load_full_workflow_definition

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(__file__).parent.parent / "config" / "workflows"


def get_all_workflow_definitions() -> list:
    """Return all workflow definitions discovered under config/workflows/.

    Each subdirectory that contains a workflow.yaml is loaded as a WorkflowDefinition.

    Returns:
        List of WorkflowDefinition (sdk) instances, sorted by directory name.
    """
    definitions = []
    if not _WORKFLOWS_DIR.exists():
        logger.warning(f"Workflows directory not found: {_WORKFLOWS_DIR}")
        return definitions

    for wf_dir in sorted(_WORKFLOWS_DIR.iterdir()):
        if not wf_dir.is_dir():
            continue
        if not (wf_dir / "workflow.yaml").exists():
            continue
        try:
            wd = load_full_workflow_definition(wf_dir)
            definitions.append(wd)
            logger.info(f"Loaded workflow: {wd.id} ({len(wd.phases)} phases)")
        except Exception as e:
            logger.warning(f"Skipping workflow {wf_dir.name}: {e}")

    return definitions

"""Centralized workflow registry.

All workflow definitions are registered here so they can be loaded
by both the MCP server startup and run_hephaestus_dev.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sdk.models import WorkflowDefinition

# Example workflows
from example_workflows.prd_to_software.phases import PRD_PHASES, PRD_WORKFLOW_CONFIG, PRD_LAUNCH_TEMPLATE
from example_workflows.bug_fix.phases import BUG_FIX_PHASES, BUG_FIX_WORKFLOW_CONFIG, BUG_FIX_LAUNCH_TEMPLATE
from example_workflows.index_repo.phases import INDEX_REPO_PHASES, INDEX_REPO_CONFIG, INDEX_REPO_LAUNCH_TEMPLATE
from example_workflows.feature_development.phases import FEATURE_DEV_PHASES, FEATURE_DEV_CONFIG, FEATURE_DEV_LAUNCH_TEMPLATE
from example_workflows.documentation_generation.phases import DOC_GEN_PHASES, DOC_GEN_CONFIG, DOC_GEN_LAUNCH_TEMPLATE
from example_workflows.qa.phases import QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE

# Core workflows
from src.autopilot.phases import AUTOPILOT_PHASES, AUTOPILOT_WORKFLOW_CONFIG, AUTOPILOT_LAUNCH_TEMPLATE


def get_all_workflow_definitions() -> list:
    """Return all workflow definitions for registration."""
    return [
        WorkflowDefinition(
            id="index-repo",
            name="Index Repository",
            phases=INDEX_REPO_PHASES,
            config=INDEX_REPO_CONFIG,
            description="Scan and index a repository to build codebase knowledge in memory",
            launch_template=INDEX_REPO_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="bug-fix",
            name="Bug Fix",
            phases=BUG_FIX_PHASES,
            config=BUG_FIX_WORKFLOW_CONFIG,
            description="Analyze, fix, and verify bug fixes",
            launch_template=BUG_FIX_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="feature-dev",
            name="Feature Development",
            phases=FEATURE_DEV_PHASES,
            config=FEATURE_DEV_CONFIG,
            description="Add features to existing codebases following existing patterns",
            launch_template=FEATURE_DEV_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="doc-gen",
            name="Documentation Generation",
            phases=DOC_GEN_PHASES,
            config=DOC_GEN_CONFIG,
            description="Generate comprehensive documentation for existing codebases",
            launch_template=DOC_GEN_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="prd-to-software",
            name="PRD to Software Builder",
            phases=PRD_PHASES,
            config=PRD_WORKFLOW_CONFIG,
            description="Build working software from a Product Requirements Document",
            launch_template=PRD_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="qa",
            name="QA Testing",
            phases=QA_PHASES,
            config=QA_WORKFLOW_CONFIG,
            description="Comprehensive QA with browser automation and log analysis",
            launch_template=QA_LAUNCH_TEMPLATE,
        ),
        WorkflowDefinition(
            id="autopilot",
            name="Autopilot Pipeline",
            phases=AUTOPILOT_PHASES,
            config=AUTOPILOT_WORKFLOW_CONFIG,
            description="9-phase automated pipeline: requirements, architecture, development, review, security, QA, validation, git, forensics",
            launch_template=AUTOPILOT_LAUNCH_TEMPLATE,
        ),
    ]

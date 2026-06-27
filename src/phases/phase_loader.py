"""Phase loader — thin wrapper around workflow_engine.yaml_loader.

Provides backward-compatible API for loading workflow definitions. The heavy
lifting is done by src.workflow_engine.yaml_loader; this module exists so
existing call sites (server.py, phase_manager.py) don't need to change.
"""

import logging
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional

from src.phases.models import PhasesConfig
from src.workflow_engine.yaml_loader import load_full_workflow_definition
from src.sdk.models import WorkflowDefinition

logger = logging.getLogger(__name__)


class PhaseLoader:
    """Loads and parses workflow phases from YAML directories."""

    @staticmethod
    def load_phases_from_folder(folder_path: str) -> WorkflowDefinition:
        """Load a workflow from a config/workflows/-style directory.

        The folder must contain:
          - workflow.yaml  (shared config: model, board, orchestrator, etc.)
          - <name>.yaml    (one file per phase, each with id/name/description/...)

        Args:
            folder_path: Path to directory containing workflow.yaml + phase files.

        Returns:
            WorkflowDefinition (sdk) with populated phases, config, and launch template.

        Raises:
            ValueError: If the directory doesn't exist or contains invalid config.
        """
        logger.info(f"PhaseLoader.load_phases_from_folder called with: '{folder_path}'")

        folder = Path(folder_path)
        logger.info(f"Resolved folder path: {folder.absolute()}")

        if not folder.exists():
            raise ValueError(f"Phases folder not found: {folder_path}")

        if not folder.is_dir():
            raise ValueError(f"Path is not a directory: {folder_path}")

        wd = load_full_workflow_definition(folder)
        logger.info(f"Loaded workflow '{wd.name}' with {len(wd.phases)} phases")
        return wd

    @staticmethod
    def load_phases_config(folder_path: str) -> PhasesConfig:
        """Load phases configuration from phases_config.yaml.

        This is a legacy helper for the HEPHAESTUS_PHASES_FOLDER path in server.py.
        If phases_config.yaml doesn't exist, returns a default PhasesConfig.

        Args:
            folder_path: Path to folder that may contain phases_config.yaml.

        Returns:
            PhasesConfig with loaded configuration or defaults if file missing.

        Raises:
            ValueError: If configuration file is invalid.
        """
        logger.info(f"PhaseLoader.load_phases_config called with: '{folder_path}'")

        folder = Path(folder_path)
        config_file = folder / "phases_config.yaml"

        if not config_file.exists():
            logger.info(f"No phases_config.yaml found in {folder_path}, using defaults")
            return PhasesConfig()

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                content = yaml.safe_load(f)

            if content is None:
                logger.warning(f"Empty phases_config.yaml in {folder_path}, using defaults")
                return PhasesConfig()

            logger.info(f"Loaded phases config from {config_file}")
            return PhasesConfig.from_yaml_content(content)

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse phases_config.yaml: {e}")
            raise ValueError(f"Invalid YAML in phases_config.yaml: {e}")
        except Exception as e:
            logger.error(f"Failed to load phases config: {e}")
            raise ValueError(f"Failed to load phases configuration: {e}")

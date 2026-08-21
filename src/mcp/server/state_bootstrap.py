"""One-time startup steps that run inside ServerState.initialize(), before
any service (agent_manager, phase_manager, etc.) is constructed.

Extracted from ServerState (SOLID review 1.6). Both functions only ever
touched self.db_manager, nothing else on the class, so neither needed to be
a method -- and grouping them here separates "get the DB and config into the
shape managers expect" from "compose the managers", which is what
ServerState.initialize() itself does.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_is_active_column(db_manager) -> None:
    """Add is_active column to autopilot_projects if missing."""
    import sqlalchemy

    try:
        with db_manager.get_session() as session:
            session.execute(sqlalchemy.text("ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0"))
            session.commit()
            logger.info("Migrated: added is_active column to autopilot_projects")
    except Exception:
        pass  # Column already exists


def load_active_project(db_manager, config) -> None:
    """Load active project from DB and apply to config before managers init."""
    from src.core.database import AutopilotProject

    try:
        with db_manager.get_session() as session:
            active = session.query(AutopilotProject).filter_by(is_active=True).first()
            if active:
                config.git.main_repo_path = Path(active.base_dir)
                config.paths.project_root = Path(active.base_dir)
                logger.info(f"Active project loaded: {active.name} ({active.base_dir})")
            else:
                # Auto-activate the default or first project
                proj = session.query(AutopilotProject).filter_by(is_default=True).first()
                if not proj:
                    proj = session.query(AutopilotProject).first()
                if proj:
                    proj.is_active = True
                    session.commit()
                    config.git.main_repo_path = Path(proj.base_dir)
                    config.paths.project_root = Path(proj.base_dir)
                    logger.info(f"Auto-activated project: {proj.name} ({proj.base_dir})")
    except Exception as e:
        logger.warning(f"Could not load active project: {e}")

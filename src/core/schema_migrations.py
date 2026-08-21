"""Ad hoc schema migrations for existing SQLite databases.

Split out of DatabaseManager (SOLID review 4.1): these 18 ALTER-TABLE-or-
skip migrations were ~590 of that class's ~940 lines, wedged between
connection lifecycle, DDL, FTS5 setup, and index creation. They need
exactly one thing from the manager -- its engine -- so they are plain
functions here rather than methods.

Each keeps its own internal resilience unchanged (several independent
sub-steps per function, several already isolating their own failures).
DatabaseManager._run_schema_migration still owns the "have we attempted
this before" bookkeeping in the schema_migrations table.

SCHEMA_MIGRATIONS at the bottom is the ordered registry create_tables()
iterates. **The recorded ids deliberately keep their original
underscore-prefixed method names** (`_migrate_task_dependency_columns`,
not `migrate_task_dependency_columns`): those strings are already stored
as primary keys in the schema_migrations table of every existing
database, and renaming them would make all 18 look unapplied and re-run
on the next startup of every deployed instance.
"""

import logging

from sqlalchemy import exc as sqlalchemy_exc, text

logger = logging.getLogger(__name__)

def migrate_task_dependency_columns(engine):
    """Add dependency columns to tasks table for existing databases."""
    try:
        with engine.connect() as conn:
            # Add depends_on column
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN depends_on TEXT"))
            except Exception:
                pass  # Column already exists

            # Add parallel_group column
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN parallel_group TEXT"))
            except Exception:
                pass  # Column already exists

            # Add max_concurrent column
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN max_concurrent INTEGER DEFAULT 1"))
            except Exception:
                pass  # Column already exists

            conn.commit()
            logger.info("Migrated task dependency columns")
    except Exception as e:
        logger.warning(f"Task dependency migration failed (not just 'already exists' -- check this): {e}")

def migrate_autopilot_designs_columns(engine):
    """Add status/content_hash/feature_folder/completed_at to autopilot_designs for existing databases."""
    try:
        with engine.connect() as conn:
            # Add content_hash column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN content_hash VARCHAR(64)"))
            except Exception:
                pass  # Column already exists

            # Add status column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'pending'"))
            except Exception:
                pass  # Column already exists

            # Add feature_folder column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN feature_folder TEXT"))
            except Exception:
                pass  # Column already exists

            # Add completed_at column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN completed_at DATETIME"))
            except Exception:
                pass  # Column already exists

            conn.commit()
            logger.info("Migrated autopilot_designs columns")
    except Exception as e:
        logger.warning(f"autopilot_designs migration failed (not just 'already exists' -- check this): {e}")

    # Add thinking_level to phases for existing databases
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE phases ADD COLUMN thinking_level VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
    except Exception as e:
        logger.warning(f"phases.thinking_level migration failed (not just 'already exists' -- check this): {e}")

    # Add design_id FK to workflows for existing databases (§9.7)
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN design_id VARCHAR REFERENCES autopilot_designs(id)"))
            except Exception:
                pass  # Column already exists
            conn.commit()
    except Exception as e:
        logger.warning(f"workflows.design_id migration failed (not just 'already exists' -- check this): {e}")

    # Add cli_model to agents table if missing
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE agents ADD COLUMN cli_model TEXT"))
            except sqlalchemy_exc.OperationalError:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated agents.cli_model column")
    except Exception as e:
        logger.warning(f"agents.cli_model migration failed (not just 'already exists' -- check this): {e}")

    # Add launched_at to agents table if missing
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE agents ADD COLUMN launched_at DATETIME"))
            except sqlalchemy_exc.OperationalError:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated agents.launched_at column")
    except Exception as e:
        logger.warning(f"agents.launched_at migration failed (not just 'already exists' -- check this): {e}")

def migrate_feature_model_columns(engine):
    """Add Feature model columns to autopilot_designs and workflows for existing databases.

    Idempotent - safe to call on every startup.
    """
    # Imported here, not at module scope: src.core.database imports
    # this module, so a top-level import back into it would be circular.
    from src.core.database import Base, Feature, PromptProposal

    # Add new columns to autopilot_designs
    try:
        with engine.connect() as conn:
            # Add file_path column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN file_path TEXT"))
            except Exception:
                pass  # Column already exists

            # Add designs_folder column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN designs_folder TEXT"))
            except Exception:
                pass  # Column already exists

            # Add phase0_workflow_id column
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN phase0_workflow_id VARCHAR REFERENCES workflows(id)"))
            except Exception:
                pass  # Column already exists

            conn.commit()
            logger.info("Migrated autopilot_designs feature model columns")
    except Exception as e:
        logger.warning(f"autopilot_designs feature model migration failed (not just 'already exists' -- check this): {e}")

    # Add new columns to workflows
    try:
        with engine.connect() as conn:
            # Add workflow_type column
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN workflow_type VARCHAR DEFAULT NULL"))
            except Exception:
                pass  # Column already exists

            # Add feature_id column.
            # Note: SQLite silently ignores FK declarations in ALTER TABLE, so
            # the REFERENCES clause is documentation only — cascade deletes and
            # constraint checks are enforced by the ORM layer, not the DB engine.
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN feature_id VARCHAR REFERENCES features(id)"))
            except Exception:
                pass  # Column already exists

            conn.commit()
            logger.info("Migrated workflows feature model columns")
    except Exception as e:
        logger.warning(f"workflows feature model migration failed (not just 'already exists' -- check this): {e}")

    # Create features table if it doesn't exist
    try:
        Base.metadata.create_all(engine, tables=[Feature.__table__], checkfirst=True)
        logger.info("Ensured features table exists")
    except Exception as e:
        logger.warning(f"features table creation failed (not just 'already exists' -- check this): {e}")

    # Create prompt_proposals table if it doesn't exist (finding 8).
    try:
        Base.metadata.create_all(
            engine, tables=[PromptProposal.__table__], checkfirst=True
        )
        logger.info("Ensured prompt_proposals table exists")
    except Exception as e:
        logger.warning(f"prompt_proposals table creation failed (not just 'already exists' -- check this): {e}")

    # Add pr_url column to features table for existing databases
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN pr_url TEXT"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated features.pr_url column")
    except Exception as e:
        logger.warning(f"features.pr_url migration failed (not just 'already exists' -- check this): {e}")

def migrate_total_gotos_column(engine):
    """Add workflows.total_gotos for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN total_gotos INTEGER DEFAULT 0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.total_gotos column")
    except Exception as e:
        logger.warning(f"workflows.total_gotos migration failed (not just 'already exists' -- check this): {e}")

def migrate_workflow_gotos_reset_at_column(engine):
    """Add workflows.gotos_reset_at for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN gotos_reset_at DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.gotos_reset_at column")
    except Exception as e:
        logger.warning(f"workflows.gotos_reset_at migration failed (not just 'already exists' -- check this): {e}")

def migrate_task_retry_count_column(engine):
    """Add tasks.retry_count for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated tasks.retry_count column")
    except Exception as e:
        logger.warning(f"tasks.retry_count migration failed (not just 'already exists' -- check this): {e}")

def migrate_phase_retry_count_column(engine):
    """Add phases.retry_count for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE phases ADD COLUMN retry_count INTEGER DEFAULT 0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated phases.retry_count column")
    except Exception as e:
        logger.warning(f"phases.retry_count migration failed (not just 'already exists' -- check this): {e}")

def migrate_self_review_columns(engine):
    """Add tasks.self_review_done and phases.self_review for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN self_review_done BOOLEAN DEFAULT 0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE phases ADD COLUMN self_review JSON"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN self_review_started_at DATETIME"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN self_review_started_commit VARCHAR"))
            except Exception:
                pass  # Column already exists
            # Populate self_review for development phases that were created
            # before the fix that passes self_review from YAML to DB.
            # SECURITY: Use parameterized query to avoid SQL injection pattern
            try:
                # Both spellings of "no value": a true SQL NULL, and the
                # JSON null literal. A column written as the four-byte
                # string 'null' is not SQL NULL, so an IS NULL predicate
                # skips it silently and that phase never gets self-review
                # enabled -- indistinguishable, from the caller's side,
                # from a phase that was deliberately configured off.
                conn.execute(text(
                    "UPDATE phases SET self_review = :value "
                    "WHERE name = 'development' "
                    "AND (self_review IS NULL OR self_review = 'null')"
                ), {"value": '{"enabled": true}'})
            except Exception:
                pass  # Already populated or table empty
            conn.commit()
            logger.info("Migrated tasks.self_review_done / phases.self_review columns")
    except Exception as e:
        logger.warning(f"self_review columns migration failed (not just 'already exists' -- check this): {e}")

def migrate_phase_execution_task_claim_column(engine):
    """Add phase_executions.task_creation_claimed_at for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE phase_executions ADD COLUMN task_creation_claimed_at DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated phase_executions.task_creation_claimed_at column")
    except Exception as e:
        logger.warning(f"phase_executions.task_creation_claimed_at migration failed (not just 'already exists' -- check this): {e}")

def migrate_autopilot_designs_error_column(engine):
    """Add autopilot_designs.error for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN error TEXT"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated autopilot_designs.error column")
    except Exception as e:
        logger.warning(f"autopilot_designs.error migration failed (not just 'already exists' -- check this): {e}")

def migrate_workflow_paused_by_column(engine):
    """Add workflows.paused_by for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN paused_by VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.paused_by column")
    except Exception as e:
        logger.warning(f"workflows.paused_by migration failed (not just 'already exists' -- check this): {e}")

def migrate_workflow_status_reason_column(engine):
    """Add workflows.status_reason for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN status_reason VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.status_reason column")
    except Exception as e:
        logger.warning(f"workflows.status_reason migration failed (not just 'already exists' -- check this): {e}")

def migrate_workflow_paused_at_column(engine):
    """Add workflows.paused_at for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN paused_at DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.paused_at column")
    except Exception as e:
        logger.warning(f"workflows.paused_at migration failed (not just 'already exists' -- check this): {e}")

def migrate_workflow_paused_retry_count_column(engine):
    """Add workflows.paused_retry_count for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN paused_retry_count INTEGER DEFAULT 0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.paused_retry_count column")
    except Exception as e:
        logger.warning(f"workflows.paused_retry_count migration failed (not just 'already exists' -- check this): {e}")

def migrate_task_action_target_phase_column(engine):
    """Add tasks.action_target_phase for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN action_target_phase VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated tasks.action_target_phase column")
    except Exception as e:
        logger.warning(f"tasks.action_target_phase migration failed (not just 'already exists' -- check this): {e}")

def migrate_cost_tracking_columns(engine):
    """Add cost tracking columns and tables for existing databases.

    Adds:
    - cost_total_usd to tasks, features, autopilot_designs, autopilot_projects
    - cost_limit_usd to autopilot_projects
    - cost_entries table (append-only ledger)
    - session_cost_checkpoints table

    Idempotent - safe to call on every startup.
    """
    # Imported here, not at module scope: src.core.database imports
    # this module, so a top-level import back into it would be circular.
    from src.core.database import Base, CostEntry, SessionCostCheckpoint

    # Add cost_total_usd to tasks
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN cost_total_usd REAL DEFAULT 0.0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated tasks.cost_total_usd column")
    except Exception as e:
        logger.warning(f"tasks.cost_total_usd migration failed (not just 'already exists' -- check this): {e}")

    # Add cost_total_usd to features
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN cost_total_usd REAL DEFAULT 0.0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated features.cost_total_usd column")
    except Exception as e:
        logger.warning(f"features.cost_total_usd migration failed (not just 'already exists' -- check this): {e}")

    # Add cost_total_usd to autopilot_designs
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN cost_total_usd REAL DEFAULT 0.0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated autopilot_designs.cost_total_usd column")
    except Exception as e:
        logger.warning(f"autopilot_designs.cost_total_usd migration failed (not just 'already exists' -- check this): {e}")

    # Add cost_total_usd and cost_limit_usd to autopilot_projects
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_projects ADD COLUMN cost_total_usd REAL DEFAULT 0.0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE autopilot_projects ADD COLUMN cost_limit_usd REAL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated autopilot_projects cost tracking columns")
    except Exception as e:
        logger.warning(f"autopilot_projects cost migration failed (not just 'already exists' -- check this): {e}")

    # Add cost_total_usd to workflows
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN cost_total_usd REAL DEFAULT 0.0 NOT NULL"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflows.cost_total_usd column")
    except Exception as e:
        logger.warning(f"workflows.cost_total_usd migration failed (not just 'already exists' -- check this): {e}")

    # Create cost_entries and session_cost_checkpoints tables
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                CostEntry.__table__,
                SessionCostCheckpoint.__table__,
            ],
            checkfirst=True,
        )
        logger.info("Ensured cost_entries and session_cost_checkpoints tables exist")
    except Exception as e:
        logger.warning(f"Cost tracking tables creation failed (not just 'already exists' -- check this): {e}")

    # Create indexes for cost_entries
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS ix_cost_entries_task_id
                ON cost_entries(task_id)
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS ix_cost_entries_workflow_id
                ON cost_entries(workflow_id)
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS ix_cost_entries_recorded_at
                ON cost_entries(recorded_at)
                """
                )
            )
            conn.commit()
            logger.info("Created cost_entries indexes")
    except Exception as e:
        logger.warning(f"Cost entries indexes failed (not just 'already exists' -- check this): {e}")

def migrate_phase_fallback_columns(engine):
    """Add fallback_cli_tool and fallback_cli_model columns to phases table."""
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE phases ADD COLUMN fallback_cli_tool VARCHAR"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE phases ADD COLUMN fallback_cli_model VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated phases fallback_cli_tool/fallback_cli_model columns")

        # Populate fallback values from workflow config for existing phases
        try:
            from src.core.simple_config import get_config
            cfg = get_config()
            if cfg.agents.default_fallback_cli_tool:
                with engine.connect() as conn:
                    result = conn.execute(text(
                        "UPDATE phases SET fallback_cli_tool = :tool, fallback_cli_model = :model "
                        "WHERE fallback_cli_tool IS NULL OR fallback_cli_tool = ''"
                    ), {"tool": cfg.agents.default_fallback_cli_tool, "model": cfg.agents.default_fallback_cli_model})
                    conn.commit()
                    if result.rowcount > 0:
                        logger.info(f"Populated fallback for {result.rowcount} phases from global config")
        except Exception as e:
            logger.warning(f"Could not populate phase fallbacks from global config: {e}")
    except Exception as e:
        logger.warning(f"Phases fallback columns migration failed (not just 'already exists' -- check this): {e}")

def migrate_review_mode_columns(engine):
    """Add review_mode to autopilot_projects and review columns to features."""
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_projects ADD COLUMN review_mode BOOLEAN NOT NULL DEFAULT 0"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN review_status VARCHAR"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN review_feedback TEXT"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN reviewed_at DATETIME"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN reviewed_by VARCHAR(100)"))
            except Exception:
                pass
            conn.commit()
            logger.info("Migrated review_mode columns")
    except Exception as e:
        logger.warning(f"Review mode columns migration failed (not just 'already exists' -- check this): {e}")




def migrate_project_repos_table(engine):
    """REQ-01/03/04/05: create project_repos, backfill from base_dir.

    REQ-02: add nullable repo_id FK to tasks, tickets, ticket_commits,
    agent_worktrees, features. NULLable -- no backfill of historical rows
    (REQ-05); resolve_primary_repo() is the runtime fallback (REQ-06).
    """
    from sqlalchemy import inspect

    # Imported here, not at module scope: src.core.database imports
    # this module, so a top-level import back into it would be circular.
    from src.core.database import AutopilotProject, ProjectRepo

    try:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        # Create project_repos table if it doesn't exist
        if "project_repos" not in existing_tables:
            try:
                ProjectRepo.__table__.create(bind=engine)
                logger.info("Created project_repos table")
            except Exception as e:
                logger.warning(f"project_repos table creation failed: {e}")

        # Backfill one ProjectRepo per existing AutopilotProject
        try:
            import uuid
            from sqlalchemy.orm import Session

            with Session(engine) as session:
                projects_without_repo = (
                    session.query(AutopilotProject)
                    .filter(~AutopilotProject.repos.any())
                    .all()
                )
                for project in projects_without_repo:
                    session.add(ProjectRepo(
                        id=f"repo-{uuid.uuid4().hex[:12]}",
                        project_id=project.id,
                        label="main",
                        path=project.base_dir,
                        is_primary=True,
                    ))
                session.commit()
                if projects_without_repo:
                    logger.info(
                        f"Backfilled {len(projects_without_repo)} ProjectRepo rows"
                    )
        except Exception as e:
            logger.warning(f"ProjectRepo backfill failed: {e}")

        # Add nullable repo_id FK to tasks, tickets, ticket_commits,
        # agent_worktrees, features (REQ-02)
        for table, column in [
            ("tasks", "repo_id"),
            ("tickets", "repo_id"),
            ("ticket_commits", "repo_id"),
            ("agent_worktrees", "repo_id"),
            ("features", "repo_id"),
        ]:
            try:
                existing_cols = {
                    c["name"] for c in inspector.get_columns(table)
                }
                if column not in existing_cols:
                    with engine.connect() as conn:
                        conn.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {column} TEXT "
                            f"REFERENCES project_repos(id)"
                        ))
                        conn.commit()
                    logger.info(f"Added {table}.{column} column")
            except Exception as e:
                logger.warning(
                    f"{table}.{column} migration failed: {e}"
                )

        logger.info("migrate_project_repos_table completed")
    except Exception as e:
        logger.warning(
            f"Project repos migration failed (not just 'already exists'): {e}"
        )

# ── Registry ─────────────────────────────────────────────────────────
# (id, function). Ids match the pre-split method names -- see module
# docstring for why they must not be renamed.
SCHEMA_MIGRATIONS = [
    ("_migrate_task_dependency_columns", migrate_task_dependency_columns),
    ("_migrate_autopilot_designs_columns", migrate_autopilot_designs_columns),
    ("_migrate_feature_model_columns", migrate_feature_model_columns),
    ("_migrate_total_gotos_column", migrate_total_gotos_column),
    ("_migrate_workflow_gotos_reset_at_column", migrate_workflow_gotos_reset_at_column),
    ("_migrate_task_retry_count_column", migrate_task_retry_count_column),
    ("_migrate_phase_retry_count_column", migrate_phase_retry_count_column),
    ("_migrate_self_review_columns", migrate_self_review_columns),
    ("_migrate_phase_execution_task_claim_column", migrate_phase_execution_task_claim_column),
    ("_migrate_autopilot_designs_error_column", migrate_autopilot_designs_error_column),
    ("_migrate_workflow_paused_by_column", migrate_workflow_paused_by_column),
    ("_migrate_workflow_status_reason_column", migrate_workflow_status_reason_column),
    ("_migrate_workflow_paused_at_column", migrate_workflow_paused_at_column),
    ("_migrate_workflow_paused_retry_count_column", migrate_workflow_paused_retry_count_column),
    ("_migrate_task_action_target_phase_column", migrate_task_action_target_phase_column),
    ("_migrate_cost_tracking_columns", migrate_cost_tracking_columns),
    ("_migrate_phase_fallback_columns", migrate_phase_fallback_columns),
    ("_migrate_review_mode_columns", migrate_review_mode_columns),
    ("_migrate_project_repos_table", migrate_project_repos_table),
]

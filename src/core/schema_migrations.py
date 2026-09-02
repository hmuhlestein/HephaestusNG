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
from pathlib import Path

from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import text

from src.core.database import utc_now

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
        Base.metadata.create_all(engine, tables=[PromptProposal.__table__], checkfirst=True)
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
                conn.execute(text("UPDATE phases SET self_review = :value WHERE name = 'development' AND (self_review IS NULL OR self_review = 'null')"), {"value": '{"enabled": true}'})
            except Exception:
                pass  # Already populated or table empty
            conn.commit()
            logger.info("Migrated tasks.self_review_done / phases.self_review columns")
    except Exception as e:
        logger.warning(f"self_review columns migration failed (not just 'already exists' -- check this): {e}")


def backfill_self_review_defaults(engine):
    """Re-enable self_review on any development Phase row still missing it.

    Deliberately NOT in SCHEMA_MIGRATIONS: that registry records each
    migration id in schema_migrations and skips it forever afterwards, so
    a one-shot backfill cannot heal drift that appears LATER. It did not.
    `migrate_self_review_columns` backfilled once, then
    `sdk/client.py`'s phase loader -- which never read the YAML's
    `self_review:` key -- kept creating fresh development rows with
    self_review = NULL. Every Phase row is per-workflow, so each new
    workflow seeded through that path reintroduced the gap.

    The failure was silent and asymmetric: `_maybe_fire_self_review_gate`
    requires `phase.self_review.get("enabled")`, so a NULL row means the
    gate never fires and the task completes on its FIRST "done" -- which
    looks exactly like a phase deliberately configured with self-review
    off. Observed: 2 of 35 development rows had drifted, and a task on one
    of them silently skipped its self-review.

    The loader is fixed, so new rows carry the value from YAML. This runs
    every startup so any row that still drifts is repaired rather than
    quietly disabling a quality gate. One small idempotent UPDATE.
    """
    try:
        with engine.connect() as conn:
            # Both spellings of "no value": a true SQL NULL and the JSON
            # null literal. A column written as the four-byte string
            # 'null' is not SQL NULL, so an IS NULL predicate skips it.
            result = conn.execute(text("UPDATE phases SET self_review = :value WHERE name = 'development' AND (self_review IS NULL OR self_review = 'null')"), {"value": '{"enabled": true}'})
            conn.commit()
            if result.rowcount:
                logger.warning(f"[SELF-REVIEW] Repaired {result.rowcount} development phase row(s) that had self_review unset -- the self-review gate would not have fired for tasks on them")
    except Exception as e:
        logger.warning(f"self_review backfill failed: {e}")


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


def migrate_autopilot_designs_archived_at_column(engine):
    """Add autopilot_designs.archived_at for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN archived_at DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated autopilot_designs.archived_at column")
    except Exception as e:
        logger.warning(f"autopilot_designs.archived_at migration failed (not just 'already exists' -- check this): {e}")


def migrate_speckit_auto_scan_column(engine):
    """Add autopilot_projects.speckit_auto_scan_enabled for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_projects ADD COLUMN speckit_auto_scan_enabled BOOLEAN NOT NULL DEFAULT 0"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated autopilot_projects.speckit_auto_scan_enabled column")
    except Exception as e:
        logger.warning(f"autopilot_projects.speckit_auto_scan_enabled migration failed (not just 'already exists' -- check this): {e}")


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


def migrate_task_dispatch_grace_until_column(engine):
    """Add tasks.dispatch_grace_until for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN dispatch_grace_until DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated tasks.dispatch_grace_until column")
    except Exception as e:
        logger.warning(f"tasks.dispatch_grace_until migration failed (not just 'already exists' -- check this): {e}")


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
                    result = conn.execute(
                        text("UPDATE phases SET fallback_cli_tool = :tool, fallback_cli_model = :model WHERE fallback_cli_tool IS NULL OR fallback_cli_tool = ''"),
                        {"tool": cfg.agents.default_fallback_cli_tool, "model": cfg.agents.default_fallback_cli_model},
                    )
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


def migrate_agent_pending_message_column(engine):
    """Add agents.pending_message_sent_at for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE agents ADD COLUMN pending_message_sent_at DATETIME"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated agents.pending_message_sent_at column")
    except Exception as e:
        logger.warning(f"agents.pending_message_sent_at migration failed (not just 'already exists' -- check this): {e}")


def migrate_agent_working_directory_column(engine):
    """Add agents.working_directory for existing databases.

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE agents ADD COLUMN working_directory VARCHAR"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated agents.working_directory column")
    except Exception as e:
        logger.warning(f"agents.working_directory migration failed (not just 'already exists' -- check this): {e}")


def migrate_workflow_type_columns(engine):
    """Add autopilot_designs.workflow_type and features.workflow_type for
    existing databases. Both default "feature" (today's only behavior).

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN workflow_type VARCHAR(20) NOT NULL DEFAULT 'feature'"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE features ADD COLUMN workflow_type VARCHAR(20) NOT NULL DEFAULT 'feature'"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated workflow_type columns")
    except Exception as e:
        logger.warning(f"workflow_type columns migration failed (not just 'already exists' -- check this): {e}")


def migrate_autopilot_pipeline_events_table(engine):
    """Create autopilot_pipeline_events for existing databases -- replaces
    the old per-run events.jsonl file (OrchestratorLogger.event()).

    Idempotent - safe to call on every startup.
    """
    from src.core.database import AutopilotPipelineEvent, Base

    try:
        Base.metadata.create_all(engine, tables=[AutopilotPipelineEvent.__table__], checkfirst=True)
        logger.info("Ensured autopilot_pipeline_events table exists")
    except Exception as e:
        logger.warning(f"autopilot_pipeline_events table creation failed (not just 'already exists' -- check this): {e}")

    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_autopilot_pipeline_events_project_id ON autopilot_pipeline_events(project_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_autopilot_pipeline_events_created_at ON autopilot_pipeline_events(created_at)"))
            conn.commit()
            logger.info("Created autopilot_pipeline_events indexes")
    except Exception as e:
        logger.warning(f"autopilot_pipeline_events indexes failed (not just 'already exists' -- check this): {e}")


def migrate_project_repos_table(engine):
    """Create project_repos and add repo_id columns to tasks/tickets/
    ticket_commits/agent_worktrees/features, backfilling one primary
    ProjectRepo per existing AutopilotProject.

    Idempotent - safe to call on every startup. Non-destructive:
    AutopilotProject.base_dir is never written to, and no existing
    Task/TicketCommit/AgentWorktree/Feature row's repo_id is backfilled --
    repo_resolution.resolve_repo_path treats repo_id=None as "use the
    project's primary repo" everywhere.
    """
    import uuid

    from src.core.database import Base, ProjectRepo

    try:
        Base.metadata.create_all(engine, tables=[ProjectRepo.__table__], checkfirst=True)
        logger.info("Ensured project_repos table exists")
    except Exception as e:
        logger.warning(f"project_repos table creation failed (not just 'already exists' -- check this): {e}")

    # Partial unique index: at most one is_primary=1 row per project_id.
    # Without this, two concurrent "check count()==0, then insert
    # is_primary=1" callers (the API endpoint and this same backfill loop
    # running under two workers) can both insert a primary row for the same
    # project -- resolve_repo_path's is_primary lookup then becomes
    # non-deterministic. This index turns that race into a clean
    # IntegrityError instead of silent duplicate-primary corruption.
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_repos_one_primary ON project_repos(project_id) WHERE is_primary = 1"))
            conn.commit()
        logger.info("Ensured uq_project_repos_one_primary partial unique index exists")
    except Exception as e:
        logger.warning(f"uq_project_repos_one_primary index creation failed: {e}")

    for table, column in (
        ("tasks", "repo_id"),
        ("tickets", "repo_id"),
        ("ticket_commits", "repo_id"),
        ("agent_worktrees", "repo_id"),
        ("features", "repo_id"),
    ):
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR REFERENCES project_repos(id)"))
                except Exception:
                    pass  # Column already exists
                conn.commit()
                logger.info(f"Migrated {table}.{column} column")
        except Exception as e:
            logger.warning(f"{table}.{column} migration failed (not just 'already exists' -- check this): {e}")

    # WARNING-2 fix: unique constraint on (ticket_id, commit_sha) so that
    # _resolve_repo_path_for_commit's .first() is deterministic -- without
    # this, the same commit_sha linked to two different tickets (e.g. a
    # shared merge commit) makes .first() return whichever row the query
    # planner happens to pick, silently choosing that row's repo_id.
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_commits_ticket_sha ON ticket_commits(ticket_id, commit_sha)"))
            conn.commit()
        logger.info("Ensured uq_ticket_commits_ticket_sha unique index exists")
    except Exception as e:
        logger.warning(f"uq_ticket_commits_ticket_sha index creation failed: {e}")

    # Backfill: one primary ProjectRepo per existing AutopilotProject.
    # Idempotent via the "already has a primary repo" existence check.
    # Each insert commits (and is caught) individually so that a second
    # process/worker racing this same backfill for the same project_id
    # loses cleanly to uq_project_repos_one_primary above, instead of
    # both inserting and corrupting the table.
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, base_dir FROM autopilot_projects")).fetchall()
            now = utc_now().isoformat()
            created = 0
            for project_id, base_dir in rows:
                has_primary = conn.execute(
                    text("SELECT 1 FROM project_repos WHERE project_id = :pid AND is_primary = 1"),
                    {"pid": project_id},
                ).fetchone()
                if has_primary:
                    continue
                try:
                    conn.execute(
                        text("INSERT INTO project_repos (id, project_id, label, path, is_primary, created_at) VALUES (:id, :pid, 'primary', :path, 1, :now)"),
                        {"id": f"repo-{uuid.uuid4()}", "pid": project_id, "path": base_dir, "now": now},
                    )
                    conn.commit()
                    created += 1
                except sqlalchemy_exc.IntegrityError:
                    conn.rollback()
                    logger.info(f"[REPO-MIGRATION] project {project_id!r} already got a primary repo from a concurrent migration run -- skipping (harmless race)")
            if created:
                logger.info(f"Backfilled {created} primary project_repos row(s)")
    except Exception as e:
        logger.warning(f"project_repos primary backfill failed: {e}")

def _resume_interrupted_autopilot_designs_rebuild(engine) -> bool:
    """Finish an autopilot_designs rebuild-and-swap left interrupted by a
    crash between its RENAME and its own DROP TABLE autopilot_designs_old.

    Shared by every migration below that does this rebuild (currently
    migrate_speckit_design_columns and migrate_speckit_design_source_dir_unique)
    so a crash during ANY of them is recovered by whichever migration runs
    next on the following startup, rather than only by the one that happens
    to check for it. Before this was shared, migrate_speckit_design_columns
    ran first with no resume check at all: if IT crashed mid-rebuild, the
    orphaned autopilot_designs_old sat there permanently, because on the next
    startup this function's own "is filename already nullable" check no-ops
    immediately (the freshly-recreated table already satisfies it) without
    ever noticing the abandoned table -- observed live via a design whose
    real status/phase0_workflow_id was stranded in autopilot_designs_old
    while a hollow, out-of-date row for the same id got re-inserted into
    autopilot_designs by normal application code in the meantime.

    Returns True if a leftover table was found and resumed (callers should
    defer their own transformation to the next startup rather than also
    applying it in the same pass).
    """
    with engine.connect() as conn:
        old_info = conn.execute(text("PRAGMA table_info(autopilot_designs_old)")).fetchall()
        if not old_info:
            return False
        logger.warning(
            "autopilot_designs_old found -- resuming an autopilot_designs "
            "rebuild interrupted mid-swap"
        )
        info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
        if not info:
            from src.core.database import AutopilotDesign

            AutopilotDesign.__table__.create(engine)
        col_list = ", ".join(row[1] for row in old_info)
        # OR IGNORE: a prior interrupted run may have already copied some/all
        # rows before crashing -- re-inserting an already-present id would
        # otherwise raise IntegrityError.
        conn.execute(
            text(f"INSERT OR IGNORE INTO autopilot_designs ({col_list}) SELECT {col_list} FROM autopilot_designs_old")
        )
        # Commit the copy before attempting the drop: if the drop fails (e.g.
        # a stale FK still points at this table -- see
        # repair_dangling_autopilot_designs_fk), the rows must not vanish
        # with it. Without this, a connection that raises mid-DROP rolls
        # back the whole uncommitted transaction, silently discarding the
        # INSERT too -- observed live: migrate_design_spec_key's own DROP
        # hit exactly this FK violation, and every design in every project
        # disappeared from autopilot_designs until manually recovered from
        # the (still-present, un-dropped) old table.
        conn.commit()

    # Closed and reopened, not nested: _repoint_dangling_autopilot_designs_fk
    # opens its own connections (including an AUTOCOMMIT one for the
    # writable_schema edit), and running those concurrently with this
    # function's own still-open connection left the rewrite invisible to
    # the DROP below in testing -- SQLite's per-connection schema cache
    # isn't guaranteed to see another connection's writable_schema write
    # without a full reconnect.
    _repoint_dangling_autopilot_designs_fk(engine)

    with engine.connect() as conn:
        conn.execute(text("DROP TABLE autopilot_designs_old"))
        conn.commit()
        logger.info("Resumed and completed an interrupted autopilot_designs rebuild")
        return True


def migrate_speckit_design_columns(engine):
    """Add AutopilotDesign.repo_id/source_dir and
    AutopilotProject.speckit_autoscan_enabled for existing databases.

    Also relaxes autopilot_designs.filename to nullable: a Spec-Kit
    directory-sourced design has no single filename (source_dir is set
    instead, mutually exclusive per NFR-02), but the column has carried a
    NOT NULL constraint since it was first created. SQLite has no ALTER
    COLUMN, so dropping that constraint requires the standard rebuild-and-
    swap: copy rows into a freshly created table (built from the current,
    already-nullable model) under a temp name, drop the old table, rename.

    Idempotent - safe to call on every startup.
    """
    if _resume_interrupted_autopilot_designs_rebuild(engine):
        return

    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN repo_id VARCHAR"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN source_dir TEXT"))
            except Exception:
                pass  # Column already exists
            try:
                conn.execute(text("ALTER TABLE autopilot_projects ADD COLUMN speckit_autoscan_enabled BOOLEAN NOT NULL DEFAULT 0"))
            except Exception:
                pass  # Column already exists
            conn.commit()
            logger.info("Migrated speckit design columns")
    except Exception as e:
        logger.warning(f"speckit design columns migration failed (not just 'already exists' -- check this): {e}")

    try:
        with engine.connect() as conn:
            info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
            filename_col = next((row for row in info if row[1] == "filename"), None)
            if filename_col is None or filename_col[3] == 0:
                return  # table missing (fresh DB, handled by create_all) or already nullable
            col_list = ", ".join(row[1] for row in info)
            # legacy_alter_table: since SQLite 3.25 a plain RENAME also
            # rewrites the FK clauses of every OTHER table referencing this
            # one, so workflows.design_id and features.design_id would follow
            # autopilot_designs to the temporary name -- which the DROP below
            # then deletes, leaving them permanently dangling. See
            # repair_dangling_autopilot_designs_fk for what that costs.
            conn.execute(text("PRAGMA legacy_alter_table=ON"))
            conn.execute(text("ALTER TABLE autopilot_designs RENAME TO autopilot_designs_old"))
            conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            conn.commit()

        from src.core.database import AutopilotDesign

        AutopilotDesign.__table__.create(engine)
        with engine.connect() as conn:
            conn.execute(text(f"INSERT INTO autopilot_designs ({col_list}) SELECT {col_list} FROM autopilot_designs_old"))
            # Commit the copy before the drop -- see
            # _resume_interrupted_autopilot_designs_rebuild's comment on the
            # same pattern for why a drop failure must not roll back the
            # insert too.
            conn.commit()

        # Closed and reopened, not nested -- see
        # _resume_interrupted_autopilot_designs_rebuild's comment on the
        # same pattern for why.
        _repoint_dangling_autopilot_designs_fk(engine)

        with engine.connect() as conn:
            conn.execute(text("DROP TABLE autopilot_designs_old"))
            conn.commit()
            logger.info("Rebuilt autopilot_designs with nullable filename")
    except Exception as e:
        logger.warning(f"autopilot_designs filename-nullable rebuild failed: {e}")


def migrate_speckit_design_source_dir_unique(engine):
    """Add UniqueConstraint("project_id", "source_dir") to autopilot_designs.

    Closes a real double-enqueue race: _resolve_and_enqueue_speckit_feature
    (control_routes.py) does a query-then-insert on (project_id, source_dir)
    with no DB-level constraint backing it, so two concurrent `start
    --feature X` requests could both see no existing row and both insert,
    silently rebuilding the same Spec Kit feature twice. filename-sourced
    rows keep source_dir NULL and never collide with each other under this
    constraint (SQLite NULLs are pairwise distinct), so this only fires for
    genuine directory-sourced duplicates.

    SQLite has no ALTER TABLE ... ADD CONSTRAINT, so this uses the same
    rebuild-and-swap as migrate_speckit_design_columns's filename-nullable
    fix: copy rows into a freshly created table (built from the current
    model, which already declares this constraint) under a temp name, drop
    the old table, rename. Pre-existing duplicate (project_id, source_dir)
    rows from before this migration (created by the exact race this closes)
    would violate the new constraint mid-copy, so duplicates are resolved
    first -- keep the oldest row per (project_id, source_dir), drop the rest
    (mirrors pick_next_design's own "oldest wins" ordering).

    Idempotent - safe to call on every startup. Also resumable: if a prior
    run of this migration (or migrate_speckit_design_columns's own rebuild)
    was interrupted (process killed, OOM, host restart) between the RENAME
    and the final DROP TABLE below, autopilot_designs_old can be left
    behind -- possibly with autopilot_designs missing entirely (crash before
    CREATE), or present but not yet populated/fully populated (crash after
    CREATE, before/mid INSERT). Treating "autopilot_designs missing" as
    "fresh DB" in that state would permanently break every AutopilotDesign
    query on the next startup instead of finishing the rebuild -- so
    _resume_interrupted_autopilot_designs_rebuild's check for
    autopilot_designs_old is always run FIRST and resumed/finished before
    any other branch runs.
    """
    try:
        if _resume_interrupted_autopilot_designs_rebuild(engine):
            return

        with engine.connect() as conn:
            info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
            if not info:
                return  # table missing (fresh DB, create_all already declares the constraint)

            # SQLite doesn't preserve constraint names in PRAGMA index_list
            # for table-level UNIQUE constraints (they show up as
            # sqlite_autoindex_*) -- the name only survives in the stored
            # CREATE TABLE text, so check there instead.
            create_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='autopilot_designs'")
            ).scalar()
            if create_sql and "uq_design_project_source_dir" in create_sql:
                return  # already applied

            conn.execute(
                text(
                    """
                    DELETE FROM autopilot_designs
                    WHERE source_dir IS NOT NULL
                    AND id NOT IN (
                        SELECT MIN(id) FROM autopilot_designs
                        WHERE source_dir IS NOT NULL
                        GROUP BY project_id, source_dir
                    )
                    """
                )
            )
            conn.commit()

            col_list = ", ".join(row[1] for row in info)
            # legacy_alter_table: since SQLite 3.25 a plain RENAME also
            # rewrites the FK clauses of every OTHER table referencing this
            # one, so workflows.design_id and features.design_id would follow
            # autopilot_designs to the temporary name -- which the DROP below
            # then deletes, leaving them permanently dangling. See
            # repair_dangling_autopilot_designs_fk for what that costs.
            conn.execute(text("PRAGMA legacy_alter_table=ON"))
            conn.execute(text("ALTER TABLE autopilot_designs RENAME TO autopilot_designs_old"))
            conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            conn.commit()

        from src.core.database import AutopilotDesign

        AutopilotDesign.__table__.create(engine)
        with engine.connect() as conn:
            conn.execute(text(f"INSERT INTO autopilot_designs ({col_list}) SELECT {col_list} FROM autopilot_designs_old"))
            # Commit the copy before the drop -- see
            # _resume_interrupted_autopilot_designs_rebuild's comment on the
            # same pattern for why a drop failure must not roll back the
            # insert too.
            conn.commit()

        # Closed and reopened, not nested -- see
        # _resume_interrupted_autopilot_designs_rebuild's comment on the
        # same pattern for why.
        _repoint_dangling_autopilot_designs_fk(engine)

        with engine.connect() as conn:
            conn.execute(text("DROP TABLE autopilot_designs_old"))
            conn.commit()
            logger.info("Rebuilt autopilot_designs with UniqueConstraint(project_id, source_dir)")
    except Exception as e:
        logger.warning(f"autopilot_designs source_dir-unique rebuild failed: {e}")


def drop_speckit_auto_scan_column(engine):
    """Drop autopilot_projects.speckit_auto_scan -- superseded by
    speckit_auto_scan_enabled/_sync_speckit_designs (the one real Spec Kit
    auto-scan mechanism now). Column was never exposed via any UI and every
    project's value was confirmed False before this migration was written.
    Idempotent - safe to call on every startup. Requires SQLite 3.35.0+
    (ALTER TABLE ... DROP COLUMN); silently no-ops on older SQLite so this
    never breaks startup, just leaves the harmless orphaned column behind.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_projects DROP COLUMN speckit_auto_scan"))
                conn.commit()
                logger.info("Dropped autopilot_projects.speckit_auto_scan column")
            except Exception as e:
                if "no such column" in str(e).lower():
                    pass  # Already dropped (or never existed on a fresh DB)
                else:
                    raise
    except Exception as e:
        logger.warning(f"autopilot_projects.speckit_auto_scan drop failed (not just 'already dropped' -- check this): {e}")


def drop_speckit_autoscan_enabled_column(engine):
    """Drop autopilot_projects.speckit_autoscan_enabled -- confirmed via
    exhaustive grep to have zero production usage anywhere; a leftover
    column added as a side effect of migrate_speckit_design_columns
    (which still owns repo_id/source_dir -- left untouched, only this one
    column from that migration's original ALTER statements is dropped
    here). Idempotent - safe to call on every startup. Same SQLite
    version note as drop_speckit_auto_scan_column above.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE autopilot_projects DROP COLUMN speckit_autoscan_enabled"))
                conn.commit()
                logger.info("Dropped autopilot_projects.speckit_autoscan_enabled column")
            except Exception as e:
                if "no such column" in str(e).lower():
                    pass  # Already dropped (or never existed on a fresh DB)
                else:
                    raise
    except Exception as e:
        logger.warning(f"autopilot_projects.speckit_autoscan_enabled drop failed (not just 'already dropped' -- check this): {e}")


# ── Registry ─────────────────────────────────────────────────────────
# (id, function). Ids match the pre-split method names -- see module
# docstring for why they must not be renamed.
def migrate_design_spec_key(engine):
    """Give every design a spec_key and move uniqueness onto it.

    filename was doing two jobs: naming a file in the queue directory, and
    answering "have I queued this source already". Those coincide for a
    file-backed design and not at all for a directory-backed one, so the Spec
    Kit autoscan synthesized a path-shaped stand-in
    ("speckit/<repo>/<n>-<slug>.md") and stored it there. Nothing existed at
    that path; three consumers nonetheless treated it as one.

    spec_key holds the identity: the filename for a file-backed design,
    directory_spec_key() for a directory-backed one. filename goes back to
    meaning a real file, NULL when there isn't one. Uniqueness moves with the
    job -- and gains coverage, since SQLite treats NULLs as distinct and the
    old constraint therefore never protected directory-sourced rows.
    """
    try:
        if _resume_interrupted_autopilot_designs_rebuild(engine):
            return

        with engine.connect() as conn:
            info = conn.execute(text("PRAGMA table_info(autopilot_designs)")).fetchall()
            if not info:
                return  # fresh DB -- create_all builds it with the column
            if any(row[1] == "spec_key" for row in info):
                return  # already applied

            old_cols = [row[1] for row in info]
            # Backfill before the swap so the NOT NULL column is never empty:
            # the filename for a file-backed row, and the colon form derived
            # from source_dir (plus its repo label, when it has one) for a
            # directory-backed row. A row with neither -- there should be none
            # -- falls back to its id, which is unique by construction.
            rows = conn.execute(
                text(
                    "SELECT d.id, d.filename, d.source_dir, r.label "
                    "FROM autopilot_designs d "
                    "LEFT JOIN project_repos r ON r.id = d.repo_id"
                )
            ).fetchall()
            conn.execute(text("ALTER TABLE autopilot_designs ADD COLUMN spec_key VARCHAR(500)"))
            from src.core.database import directory_spec_key

            for design_id, filename, source_dir, repo_label in rows:
                if source_dir:
                    key = directory_spec_key(Path(source_dir).name, repo_label)
                elif filename:
                    # Pre-existing autoscan rows carry the synthetic
                    # "speckit/<repo>/<n>-<slug>.md" in filename. Convert them
                    # to the colon form and clear the filename, which never
                    # named a file that existed.
                    if filename.startswith("speckit/"):
                        parts = filename[len("speckit/"):].rsplit("/", 1)
                        label = parts[0] if len(parts) == 2 else None
                        stem = parts[-1]
                        if stem.endswith(".md"):
                            stem = stem[: -len(".md")]
                        key = directory_spec_key(stem, label if label != "_workspace" else None)
                    else:
                        key = filename
                else:
                    key = design_id
                conn.execute(
                    text("UPDATE autopilot_designs SET spec_key = :k WHERE id = :i"),
                    {"k": key, "i": design_id},
                )
            conn.execute(
                text(
                    "UPDATE autopilot_designs SET filename = NULL "
                    "WHERE filename LIKE 'speckit/%'"
                )
            )
            conn.commit()

            col_list = ", ".join(old_cols + ["spec_key"])
            # legacy_alter_table: see repair_dangling_autopilot_designs_fk --
            # without it this rename repoints workflows/features at a table
            # the DROP below deletes.
            conn.execute(text("PRAGMA legacy_alter_table=ON"))
            conn.execute(text("ALTER TABLE autopilot_designs RENAME TO autopilot_designs_old"))
            conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            conn.commit()

        from src.core.database import AutopilotDesign

        AutopilotDesign.__table__.create(engine)
        with engine.connect() as conn:
            conn.execute(
                text(f"INSERT INTO autopilot_designs ({col_list}) SELECT {col_list} FROM autopilot_designs_old")
            )
            # Commit the copy before the drop -- see
            # _resume_interrupted_autopilot_designs_rebuild's comment on the
            # same pattern for why a drop failure must not roll back the
            # insert too. This is exactly what happened live: this DROP hit
            # a stale FK (workflows/features still pointed at
            # "autopilot_designs_old" from before repair_dangling_
            # autopilot_designs_fk's last run), the uncommitted INSERT got
            # rolled back with it, and every design in every project
            # disappeared from autopilot_designs until manually recovered.
            conn.commit()

        # Closed and reopened, not nested -- see
        # _resume_interrupted_autopilot_designs_rebuild's comment on the
        # same pattern for why.
        _repoint_dangling_autopilot_designs_fk(engine)

        with engine.connect() as conn:
            conn.execute(text("DROP TABLE autopilot_designs_old"))
            conn.commit()
            logger.info("Rebuilt autopilot_designs with spec_key and uq_design_project_spec_key")
    except Exception as e:
        logger.warning(f"design spec_key migration failed: {e}")


def _repoint_dangling_autopilot_designs_fk(engine) -> None:
    """Rewrite workflows/features' design_id FK off the literal name
    "autopilot_designs_old" and onto "autopilot_designs", unconditionally
    (no defer-if-old-table-still-exists guard -- see
    repair_dangling_autopilot_designs_fk for that gated, standalone
    version). Every rebuild-and-swap migration for autopilot_designs calls
    this right after its own INSERT has committed the real rows into the
    freshly-recreated table and right before its own DROP TABLE
    autopilot_designs_old: PRAGMA legacy_alter_table=ON keeps the RENAME
    from repointing these FKs automatically (so they survive the swap
    pointing at the stable name "autopilot_designs" once it exists again),
    but if a still-dangling reference from an EARLIER, not-yet-repaired
    incident already said "autopilot_designs_old" going in, the rename
    leaves it exactly as dangling as it started -- and the DROP that
    follows then fails with a live FOREIGN KEY violation under
    PRAGMA foreign_keys=ON, since workflows/features rows still reference
    the table being dropped. Calling this first guarantees the DROP always
    has a table with no live referrers.

    Rewrites the two stored CREATE TABLE statements in place instead of
    rebuilding the tables. Rebuilding `workflows` would rename it in turn
    and rewrite every FK pointing AT it (tasks, phases, features, ...) --
    turning one dangling reference into several, which is the same mistake
    one level up.
    """
    dangling = '"autopilot_designs_old"'
    # Excludes the autopilot_designs_old table's OWN row throughout: its
    # stored CREATE TABLE text necessarily contains its own quoted name
    # ('CREATE TABLE "autopilot_designs_old" (...)'), which also matches
    # this LIKE pattern. Rewriting that row too desyncs sqlite_master's
    # `name` column (still "autopilot_designs_old") from the name embedded
    # in its own `sql` text (now claiming to be "autopilot_designs"),
    # which is exactly what PRAGMA quick_check reports as "malformed
    # database schema" -- callers of this function run it BEFORE the DROP,
    # while autopilot_designs_old still exists, so this exclusion is load-
    # bearing, not defensive-only.
    with engine.connect() as conn:
        broken = [
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name != 'autopilot_designs_old' AND sql LIKE :pat"),
                {"pat": f"%{dangling}%"},
            ).fetchall()
        ]
        if not broken:
            return

    # AUTOCOMMIT: writable_schema edits must not sit inside a transaction,
    # and RESET reloads the schema for every other connection.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("PRAGMA writable_schema=ON"))
        conn.execute(
            text(
                "UPDATE sqlite_master SET sql = replace(sql, "
                "'\"autopilot_designs_old\"', '\"autopilot_designs\"') "
                "WHERE type='table' AND name != 'autopilot_designs_old' AND sql LIKE '%\"autopilot_designs_old\"%'"
            )
        )
        conn.execute(text("PRAGMA writable_schema=RESET"))

    with engine.connect() as conn:
        integrity = conn.execute(text("PRAGMA quick_check")).fetchone()[0]
        still_broken = [
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name != 'autopilot_designs_old' AND sql LIKE '%autopilot_designs_old%'")
            ).fetchall()
        ]
        if integrity != "ok" or still_broken:
            logger.error(
                f"Dangling autopilot_designs FK repair did not verify "
                f"(quick_check={integrity!r}, still referencing={still_broken}) "
                f"-- restore from a backup"
            )
            return
    logger.info(
        f"Repointed dangling autopilot_designs_old foreign key on: {', '.join(broken)}"
    )


def migrate_phase_execution_phase_id_unique(engine):
    """Add a unique index on phase_executions.phase_id for existing databases.

    Every reader of this table (_get_phase_statuses, _create_phase_task,
    and every sibling reopen/reset function -- see docs/designs/
    PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md) does
    .filter_by(phase_id=...).first() with no order_by, trusting exactly
    one row per phase. Nothing in the schema enforced that until now -- a
    second row for the same phase would have been silently picked (or
    dropped) at random by whichever happened to sort first.

    Preventive, not corrective: as of this migration's introduction, the
    live database has zero phase_ids with more than one row. Still
    defensive rather than assuming that stays true forever -- if any
    duplicates exist by the time this runs, consolidate first (keep
    whichever row completed most recently, then started most recently,
    then the highest id as a final deterministic tiebreak; log exactly
    what was merged) so the CREATE UNIQUE INDEX below doesn't fail outright
    on the very database this migration exists to protect.

    Uses CREATE UNIQUE INDEX rather than the rebuild-and-swap pattern
    migrate_speckit_design_source_dir_unique uses for autopilot_designs:
    SQLite enforces a unique index exactly like a table-level UNIQUE
    constraint for every future write, without rebuilding the table.
    phase_executions is read and written continuously by the live
    orchestrator sweep -- skipping the rebuild-and-swap here avoids the
    entire class of dangling-FK/interrupted-rebuild failure modes that
    pattern is exposed to (see repair_dangling_autopilot_designs_fk below
    and its own incident history: an earlier rebuild-and-swap migration on
    a different table wiped it entirely on a DROP failure).

    Idempotent - safe to call on every startup.
    """
    try:
        with engine.connect() as conn:
            dupes = conn.execute(
                text(
                    "SELECT phase_id, COUNT(*) FROM phase_executions "
                    "GROUP BY phase_id HAVING COUNT(*) > 1"
                )
            ).fetchall()
            if dupes:
                logger.warning(
                    f"[MIGRATION] Found {len(dupes)} phase_id(s) with duplicate "
                    f"phase_executions rows -- consolidating before adding the "
                    f"unique index: {[d[0] for d in dupes]}"
                )
                for phase_id, _count in dupes:
                    rows = conn.execute(
                        text(
                            "SELECT id, status FROM phase_executions WHERE phase_id = :phase_id "
                            "ORDER BY completed_at IS NULL, completed_at DESC, "
                            "started_at IS NULL, started_at DESC, id DESC"
                        ),
                        {"phase_id": phase_id},
                    ).fetchall()
                    keep_id, keep_status = rows[0]
                    drop_ids = [r[0] for r in rows[1:]]
                    logger.warning(
                        f"[MIGRATION] phase_id={phase_id}: keeping execution "
                        f"{keep_id} (status={keep_status!r}), dropping {drop_ids}"
                    )
                    for drop_id in drop_ids:
                        conn.execute(
                            text("DELETE FROM phase_executions WHERE id = :id"),
                            {"id": drop_id},
                        )
                conn.commit()

            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_phase_execution_phase_id "
                    "ON phase_executions(phase_id)"
                )
            )
            conn.commit()
            logger.info("Ensured uq_phase_execution_phase_id unique index exists")
    except Exception as e:
        logger.warning(f"phase_executions phase_id unique index migration failed (not just 'already exists' -- check this): {e}")


def repair_dangling_autopilot_designs_fk(engine):
    """Point workflows/features' design_id back at autopilot_designs.

    The rebuild-and-swap migrations for autopilot_designs ran before the
    legacy_alter_table guard existed, so SQLite rewrote both referencing
    tables to REFERENCES "autopilot_designs_old" (id) -- a table the same
    migration then dropped. Under PRAGMA foreign_keys=ON every INSERT into
    workflows or features then fails with "no such table:
    main.autopilot_designs_old", so no workflow can be created and
    therefore no design in ANY project can start (observed live: Phase 0
    launched, created its worktree, and died on the workflow INSERT).

    This is the standalone, end-of-migration-list safety net -- each
    individual rebuild migration now also calls
    _repoint_dangling_autopilot_designs_fk itself, right before its own
    DROP, so in the common case this finds nothing left to do.
    """
    try:
        with engine.connect() as conn:
            broken = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND sql LIKE '%\"autopilot_designs_old\"%' LIMIT 1")
            ).fetchone()
            if not broken:
                return
            # A leftover autopilot_designs_old means a rebuild is still
            # mid-swap; _resume_interrupted_autopilot_designs_rebuild owns
            # that case and has to finish first, or this would repoint the
            # references while the real rows still live in the old table.
            if conn.execute(text("PRAGMA table_info(autopilot_designs_old)")).fetchall():
                logger.warning(
                    "autopilot_designs_old still present -- deferring dangling-FK "
                    "repair until the interrupted rebuild is resumed"
                )
                return
        _repoint_dangling_autopilot_designs_fk(engine)
    except Exception as e:
        logger.warning(f"Dangling autopilot_designs FK repair failed: {e}")


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
    ("_migrate_task_dispatch_grace_until_column", migrate_task_dispatch_grace_until_column),
    ("_migrate_cost_tracking_columns", migrate_cost_tracking_columns),
    ("_migrate_phase_fallback_columns", migrate_phase_fallback_columns),
    ("_migrate_review_mode_columns", migrate_review_mode_columns),
    ("_migrate_agent_pending_message_column", migrate_agent_pending_message_column),
    ("_migrate_agent_working_directory_column", migrate_agent_working_directory_column),
    ("_migrate_workflow_type_columns", migrate_workflow_type_columns),
    ("_migrate_autopilot_pipeline_events_table", migrate_autopilot_pipeline_events_table),
    ("_migrate_project_repos_table", migrate_project_repos_table),
    ("_migrate_autopilot_designs_archived_at_column", migrate_autopilot_designs_archived_at_column),
    ("_migrate_speckit_design_columns", migrate_speckit_design_columns),
    ("_migrate_speckit_design_source_dir_unique", migrate_speckit_design_source_dir_unique),
    ("_migrate_speckit_auto_scan_enabled_column", migrate_speckit_auto_scan_column),
    ("_drop_speckit_auto_scan_column", drop_speckit_auto_scan_column),
    ("_drop_speckit_autoscan_enabled_column", drop_speckit_autoscan_enabled_column),
    ("_migrate_design_spec_key", migrate_design_spec_key),
    ("_migrate_phase_execution_phase_id_unique", migrate_phase_execution_phase_id_unique),
    # Last: repairs damage the rebuild-and-swap migrations above can do, so it
    # always sees their final state within the same startup pass.
    ("_repair_dangling_autopilot_designs_fk", repair_dangling_autopilot_designs_fk),
]

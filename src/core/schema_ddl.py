"""Raw-SQL DDL for the SQLite schema: FTS5 search tables and performance indexes.

Split out of DatabaseManager (SOLID review 4.1), which mixed connection
lifecycle, table creation, this DDL, and ad hoc migrations in one class.
The migrations moved to schema_migrations.py; these two ~60- and ~90-line
raw-SQL blocks were the other half the finding named.

Like the migrations, both need exactly one thing from the manager -- its
engine -- so they are plain functions rather than methods. Both are
CREATE ... IF NOT EXISTS and safe to re-run on every startup; each keeps
its own broad try/except, which logs at debug because "already exists" is
the normal case on every run after the first.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def create_fts5_tables(engine):
    """Create FTS5 virtual tables and triggers for ticket search."""
    try:
        with engine.connect() as conn:
            # Create FTS5 virtual table for tickets
            conn.execute(
                text(
                    """
                CREATE VIRTUAL TABLE IF NOT EXISTS ticket_fts USING fts5(
                    ticket_id UNINDEXED,
                    title,
                    description,
                    tags
                )
            """
                )
            )

            # Create triggers to keep FTS5 in sync with tickets table
            # Trigger for INSERT
            conn.execute(
                text(
                    """
                CREATE TRIGGER IF NOT EXISTS tickets_fts_insert AFTER INSERT ON tickets BEGIN
                    INSERT INTO ticket_fts(ticket_id, title, description, tags)
                    VALUES (new.id, new.title, new.description,
                            COALESCE(json_extract(new.tags, '$'), ''));
                END
            """
                )
            )

            # Trigger for UPDATE
            conn.execute(
                text(
                    """
                CREATE TRIGGER IF NOT EXISTS tickets_fts_update AFTER UPDATE ON tickets BEGIN
                    DELETE FROM ticket_fts WHERE ticket_id = old.id;
                    INSERT INTO ticket_fts(ticket_id, title, description, tags)
                    VALUES (new.id, new.title, new.description,
                            COALESCE(json_extract(new.tags, '$'), ''));
                END
            """
                )
            )

            # Trigger for DELETE
            conn.execute(
                text(
                    """
                CREATE TRIGGER IF NOT EXISTS tickets_fts_delete AFTER DELETE ON tickets BEGIN
                    DELETE FROM ticket_fts WHERE ticket_id = old.id;
                END
            """
                )
            )

            conn.commit()
            logger.info("Created FTS5 virtual table and triggers for ticket search")
    except Exception as e:
        logger.debug(f"FTS5 table setup (may already exist): {e}")

def create_indexes(engine):
    """Create database indexes for performance optimization."""
    try:
        with engine.connect() as conn:
            # Tickets table indexes
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tickets_workflow_status
                ON tickets(workflow_id, status)
            """
                )
            )

            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tickets_workflow_priority
                ON tickets(workflow_id, priority)
            """
                )
            )

            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tickets_assigned_agent
                ON tickets(assigned_agent_id)
            """
                )
            )

            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tickets_created_at
                ON tickets(created_at)
            """
                )
            )

            # Ticket comments index
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_ticket_comments_ticket_id
                ON ticket_comments(ticket_id)
            """
                )
            )

            # Ticket history index
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket_id
                ON ticket_history(ticket_id)
            """
                )
            )

            # Ticket commits index
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_ticket_commits_ticket_id
                ON ticket_commits(ticket_id)
            """
                )
            )

            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_ticket_commits_sha
                ON ticket_commits(commit_sha)
            """
                )
            )

            # Tasks table indexes for ticket tracking
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tasks_ticket_id
                ON tasks(ticket_id)
            """
                )
            )

            conn.commit()
            logger.info("Created performance indexes for ticket tracking system")
    except Exception as e:
        logger.debug(f"Index creation (may already exist): {e}")

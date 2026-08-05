#!/usr/bin/env python3
"""Backfill cost entries for completed tasks that have no cost data.

Usage:
    python scripts/backfill_costs.py [--dry-run] [--limit N]

Reads the CLI session transcripts (pi JSONL, Claude Code) for completed
tasks and writes CostEntry rows where none exist. Useful after fixing
the cost collection pipeline to populate historical data.

Options:
    --dry-run   Show what would be collected without writing to DB
    --limit N   Process at most N tasks (default: all)
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Backfill cost data for completed tasks")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max tasks to process (0=all)")
    args = parser.parse_args()

    from src.core.database import Agent, CostEntry, Task, get_db

    with get_db() as db:
        # Find completed/failed tasks with no cost entries
        tasks_with_cost = db.query(CostEntry.task_id).distinct().subquery()
        tasks = (
            db.query(Task)
            .filter(
                Task.status.in_(["done", "failed"]),
                Task.assigned_agent_id.isnot(None),
                ~Task.id.in_(db.query(tasks_with_cost.c.task_id)),
            )
            .order_by(Task.completed_at.desc())
            .all()
        )

        if args.limit > 0:
            tasks = tasks[:args.limit]

        logger.info(f"Found {len(tasks)} completed tasks with no cost entries")

        if not tasks:
            logger.info("Nothing to backfill")
            return

        success = 0
        skipped = 0
        failed = 0

        for task in tasks:
            agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()
            if not agent:
                skipped += 1
                continue

            if args.dry_run:
                logger.info(f"  [DRY] Would collect cost for task {task.id[:8]} "
                          f"(agent {agent.id[:8]}, cli={agent.cli_type})")
                success += 1
                continue

            try:
                from src.services.cost_collection_service import collect_task_cost
                collect_task_cost(task.id)
                # Check if anything was written
                count = db.query(CostEntry).filter_by(task_id=task.id).count()
                if count > 0:
                    logger.info(f"  ✓ Task {task.id[:8]}: {count} cost entries collected")
                    success += 1
                else:
                    logger.debug(f"  - Task {task.id[:8]}: no cost data in session files")
                    skipped += 1
            except Exception as e:
                logger.warning(f"  ✗ Task {task.id[:8]}: {e}")
                failed += 1

        logger.info(f"Backfill complete: {success} collected, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()

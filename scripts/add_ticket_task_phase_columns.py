#!/usr/bin/env python3
"""
Migration script to add task_id, phase_id columns to tickets table.
"""

import os
import sqlite3

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hephaestus.db"
)


def migrate():
    """Add task_id and phase_id columns to tickets table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if columns already exist
    cursor.execute("PRAGMA table_info(tickets)")
    columns = [col[1] for col in cursor.fetchall()]

    if "task_id" not in columns:
        cursor.execute(
            "ALTER TABLE tickets ADD COLUMN task_id VARCHAR REFERENCES tasks(id)"
        )
        print("Added task_id column")
    else:
        print("task_id column already exists")

    if "phase_id" not in columns:
        cursor.execute(
            "ALTER TABLE tickets ADD COLUMN phase_id VARCHAR REFERENCES phases(id)"
        )
        print("Added phase_id column")
    else:
        print("phase_id column already exists")

    conn.commit()
    conn.close()
    print("Migration complete")


if __name__ == "__main__":
    migrate()

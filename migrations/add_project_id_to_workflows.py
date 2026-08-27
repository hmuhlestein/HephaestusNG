"""Migration: Add project_id to workflows table and backfill from design.

Run with: python migrations/add_project_id_to_workflows.py
"""

import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hephaestus.db")

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Check if column already exists
        cursor.execute("PRAGMA table_info(workflows)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'project_id' not in columns:
            print("Adding project_id column to workflows table...")
            cursor.execute("""
                ALTER TABLE workflows 
                ADD COLUMN project_id TEXT 
                REFERENCES autopilot_projects(id) ON DELETE SET NULL
            """)
            conn.commit()
            print("Column added.")
        else:
            print("project_id column already exists.")

        # Backfill from designs
        print("Backfilling project_id from designs...")
        cursor.execute("""
            UPDATE workflows 
            SET project_id = (
                SELECT d.project_id 
                FROM autopilot_designs d 
                WHERE d.id = workflows.design_id
            )
            WHERE design_id IS NOT NULL 
            AND project_id IS NULL
        """)
        backfilled = cursor.rowcount
        conn.commit()
        print(f"Backfilled {backfilled} workflows.")

        # Create index for performance
        print("Creating index on workflows.project_id...")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflows_project_id 
            ON workflows(project_id)
        """)
        conn.commit()
        print("Index created.")

        print("Migration complete!")

    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()

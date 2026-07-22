"""Add terminated_at column to agents table."""

import sqlite3
import sys


def migrate(db_path: str = "hephaestus.db"):
    """Add terminated_at column to agents table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(agents)")
    columns = [col[1] for col in cursor.fetchall()]

    if "terminated_at" not in columns:
        print("Adding terminated_at column to agents table...")
        cursor.execute("ALTER TABLE agents ADD COLUMN terminated_at DATETIME")
        conn.commit()
        print("Done.")
    else:
        print("terminated_at column already exists.")

    conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "hephaestus.db"
    migrate(db_path)

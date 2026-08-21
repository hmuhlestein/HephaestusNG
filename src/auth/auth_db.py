"""Shared DB-manager accessor for the auth subsystem.

Extracted so auth_api.py and auth_middleware.py can both use one
DatabaseManager(None) construction without a circular import: auth_api.py
imports CurrentUser/get_current_user FROM auth_middleware.py, so
auth_middleware.py cannot import anything back from auth_api.py. Before
this, each file independently constructed its own DatabaseManager(None)
against the same SQLite file (SOLID_OO_REVIEW_UPDATE_2026-08-19.md,
"DatabaseManager(None) duplicated within src/auth/ itself") -- fragile in
tests, which had to patch both independently to point at a test DB.
"""

from src.core.database import DatabaseManager


def get_db_manager() -> DatabaseManager:
    """Get database manager instance."""
    return DatabaseManager(None)

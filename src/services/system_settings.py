"""System-wide settings, stored in the ProjectContext key/value table.

There was no system-settings mechanism before this: configuration lived either
in hephaestus_config.yaml (file-based, not editable from the UI) or on
individual rows. ProjectContext is already a global KV store with a unique
`key` and a JSON `value`, and is already used this way elsewhere (see
record_review_finding's `review_findings:<workflow>:<phase>` entries), so a
runtime-editable setting needs no new table and no migration.

Keys are namespaced `settings:<name>` to keep them distinguishable from the
per-workflow bookkeeping that shares the table.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Default spend cap applied to newly created projects. None means "no
#: default" -- projects start unlimited, which is the behaviour that existed
#: before this setting.
DEFAULT_COST_LIMIT_KEY = "settings:default_cost_limit_usd"


def _get(key: str, session=None) -> Optional[Any]:
    from src.core.database import ProjectContext, get_db

    def _read(db):
        row = db.query(ProjectContext).filter_by(key=key).first()
        return row.value if row else None

    if session is not None:
        return _read(session)
    with get_db() as db:
        return _read(db)


def _set(key: str, value: Any, description: str = "") -> None:
    from src.core.database import ProjectContext, get_db

    with get_db() as db:
        row = db.query(ProjectContext).filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.add(ProjectContext(key=key, value=value, description=description))
        db.commit()


def get_default_cost_limit(session=None) -> Optional[float]:
    """The configured default spend cap for new projects, or None if unset.

    `session` lets a caller read this inside a transaction it already holds --
    project creation does exactly that, and opening a nested get_db() during
    an in-flight flush is how SQLite deadlocks.
    """
    # Fail soft. This is read on the project-creation path, which worked fine
    # before this setting existed -- a missing ProjectContext table on an older
    # database, a locked DB, or any other lookup failure must degrade to "no
    # default", never take down project creation. set_default_cost_limit
    # deliberately does NOT do this: there the caller explicitly asked to save,
    # and swallowing that would be silent data loss.
    try:
        raw = _get(DEFAULT_COST_LIMIT_KEY, session=session)
    except Exception as e:
        logger.warning(
            f"Could not read {DEFAULT_COST_LIMIT_KEY} ({e}); treating as no default"
        )
        return None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring non-numeric {DEFAULT_COST_LIMIT_KEY}: {raw!r}")
        return None
    # A stored 0 would mean "every project is instantly over budget", which is
    # never what someone means by a default. Treat it as unset.
    return value if value > 0 else None


def set_default_cost_limit(value: Optional[float]) -> Optional[float]:
    """Set or clear the default. Returns what was stored.

    Raises ValueError on a negative or non-numeric value so the API surfaces a
    400 rather than persisting a cap no project can ever satisfy.
    """
    if value is None:
        _set(DEFAULT_COST_LIMIT_KEY, None, "Default per-project spend cap (USD)")
        logger.info("[SETTINGS] Cleared the default project cost limit")
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("default_cost_limit_usd must be a number")
    if value <= 0:
        raise ValueError(
            "default_cost_limit_usd must be greater than 0 -- use null to clear it, "
            "since a limit of 0 would put every new project instantly over budget"
        )
    _set(DEFAULT_COST_LIMIT_KEY, value, "Default per-project spend cap (USD)")
    logger.info(f"[SETTINGS] Default project cost limit set to ${value:.2f}")
    return value

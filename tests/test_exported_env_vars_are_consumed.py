"""Structural guard: exported environment variables must have a reader.

HephaestusConfig.to_env_dict builds the environment handed to spawned
processes. Twenty-one of the names it exports are read by nothing in this
repository, so those settings are silently dropped at the process boundary --
the SDK believes it is propagating configuration that never arrives.

This is the same defect as `git.worktree_branch_prefix` vs `git.branch_prefix`
(see test_config_keys_are_live.py), one layer out: a name written on one side
and spelled differently, or not read at all, on the other. Several of the
twenty are near-misses of names simple_config really does read:

    exported by the SDK              read by simple_config
    MAX_HEALTH_FAILURES              MAX_HEALTH_CHECK_FAILURES
    TASK_DEDUPLICATION_ENABLED       TASK_DEDUP_ENABLED
    VECTOR_STORE_COLLECTION_PREFIX   QDRANT_COLLECTION_PREFIX

and others (MAX_CONCURRENT_AGENTS, AUTO_COMMIT, TMUX_SESSION_PREFIX,
MONITORING_ENABLED) have no counterpart at all.

They are recorded here rather than fixed. Renaming them would activate twenty-one
settings that have been inert for their whole lifetime, changing the runtime
behaviour of every spawned process at once -- that needs to be someone's
deliberate decision, per setting, not a side effect of a cleanup. What this
test does is stop the list growing, and stop it being invisible.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

EXPORTERS = [
    SRC / "sdk" / "config.py",
    SRC / "core" / "simple_config.py",
]

# Exported, but nothing in src/ reads them. Each entry is a setting that does
# not reach the process it is aimed at. Removing an entry means either wiring
# the name up or dropping the export -- both are real decisions.
KNOWN_UNCONSUMED = {
    # Near-misses: simple_config reads a differently-spelled name.
    "MAX_HEALTH_FAILURES",  # reader spells it MAX_HEALTH_CHECK_FAILURES
    "TASK_DEDUPLICATION_ENABLED",  # reader spells it TASK_DEDUP_ENABLED
    "VECTOR_STORE_COLLECTION_PREFIX",  # reader spells it QDRANT_COLLECTION_PREFIX
    # Exported as PROJECT_ROOT; three sites read PROJECT_PATH, including
    # policy._resolve_recovery_project_path, whose env fallback therefore
    # never fires. That fallback returning None is exactly the condition
    # that used to skip stale-agent termination entirely (SOLID review 2.5).
    "PROJECT_ROOT",
    # No reader under any spelling.
    "AUTH_REQUIRED",
    "AUTO_COMMIT",
    "DEDUP_BATCH_SIZE",
    "EMBEDDING_DIMENSION",
    "HEALTH_CHECK_INTERVAL",
    "LOG_FORMAT",
    "MAX_CONCURRENT_AGENTS",
    "MONITORING_ENABLED",
    "RELATED_THRESHOLD",
    "SERVER_ENABLE_CORS",
    "SESSION_TIMEOUT",
    "SIMILARITY_THRESHOLD",
    "STUCK_AGENT_THRESHOLD",
    "TERMINATION_DELAY",
    "TMUX_SESSION_PREFIX",
    "WORKING_DIRECTORY",
    "WORKTREE_BASE",
}

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def _exported_names(path: Path) -> set:
    """Env names a to_env_dict writes, in either style it is written in:
    a dict literal (`"NAME": value`) or assignment (`env["NAME"] = value`)."""
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "to_env_dict"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                for key in sub.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        if _ENV_NAME.match(key.value):
                            names.add(key.value)
            elif isinstance(sub, ast.Subscript):
                index = sub.slice
                if isinstance(index, ast.Constant) and isinstance(index.value, str):
                    if _ENV_NAME.match(index.value):
                        names.add(index.value)
    return names


def _read_names() -> set:
    """Env names anything under src/ actually reads."""
    patterns = (
        re.compile(r"""os\.getenv\(\s*["']([A-Z0-9_]+)["']"""),
        re.compile(r"""os\.environ\.get\(\s*["']([A-Z0-9_]+)["']"""),
        re.compile(r"""os\.environ\[\s*["']([A-Z0-9_]+)["']"""),
    )
    found = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for pattern in patterns:
            found.update(pattern.findall(text))
    return found


@pytest.fixture(scope="module")
def exported():
    names = set()
    for path in EXPORTERS:
        names |= _exported_names(path)
    return names


def test_exporters_were_actually_found(exported):
    """Guards the guard: if to_env_dict is renamed or restructured, the
    extraction silently returns nothing and every assertion below passes
    vacuously."""
    assert len(exported) >= 30, (
        f"only found {len(exported)} exported env names -- the extraction "
        "probably stopped matching to_env_dict's shape"
    )


def test_no_new_unconsumed_env_exports(exported):
    """Every exported name must be read somewhere, or be a known-dead one.

    A new name here means a setting that does not survive the process
    boundary: check the reader spells it identically.
    """
    unconsumed = exported - _read_names() - KNOWN_UNCONSUMED
    assert not unconsumed, (
        "these env vars are exported to spawned processes but nothing reads "
        f"them: {sorted(unconsumed)}"
    )


def test_known_unconsumed_list_is_still_accurate(exported):
    """A name that gains a reader should leave the list, so the list cannot
    mask a later regression of that same name."""
    now_read = sorted((KNOWN_UNCONSUMED & exported) & _read_names())
    assert not now_read, (
        f"these are now read and should be removed from KNOWN_UNCONSUMED: {now_read}"
    )


def test_known_unconsumed_entries_are_still_exported(exported):
    """An entry that is no longer exported at all is stale bookkeeping."""
    stale = sorted(KNOWN_UNCONSUMED - exported)
    assert not stale, (
        f"no longer exported, remove from KNOWN_UNCONSUMED: {stale}"
    )

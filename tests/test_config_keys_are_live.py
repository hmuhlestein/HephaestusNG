"""Structural guard: every key in hephaestus_config.yaml must actually do something.

Silently-inert configuration has now bitten this codebase three separate
times, and each instance stayed hidden for the same reason -- the value the
operator wrote happened to match the hardcoded default, so nothing looked
wrong until someone changed it:

  * `conflict_resolution_strategy` is read at exactly one site and never
    branched on; `_resolve_conflicts` always runs newest-file-wins (original
    review 4.5, still open).
  * `orchestrator.max_task_retries` was read through a function that does not
    exist, inside `except Exception: max_retry = 5`. Configured 5, default 5.
  * `git.worktree_branch_prefix` was never read at all -- the loader reads
    `git.branch_prefix`. Configured "agent-", default "agent-".

Per-key tests cannot catch this class: the failure is always a key nobody
thought to test. This asserts the property directly instead, the same way
test_termination_invariant_single_writer.py does for its invariant -- mutate
each leaf key and require that *something* observable changes.

Scope, stated precisely: this guards hephaestus_config.yaml, which covers the
first and third bugs above but NOT the second -- max_task_retries lives in
config/workflows/<id>/workflow.yaml, which has no single loader to diff
against (spec.py reads it key by key, on demand). That file was checked
separately and by hand: all 54 of its distinct key names do appear as string
literals under src/, so it has no key nobody reads. That is a weaker
guarantee than this test provides, and extending the mutate-and-diff
technique there would need a per-key consumption map first.

A key that legitimately does nothing to either config object must be listed
in INERT_BY_DESIGN with a reason, which turns "silently ignored" into "an
explicit, reviewed decision".
"""

import copy
import importlib
import os
import tempfile
from pathlib import Path

import pytest
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "hephaestus_config.yaml"

# Keys that genuinely drive nothing in either loader, each with the reason.
# Adding to this list is a deliberate act; leaving a key out of it and out of
# the loaders is the bug this test exists to catch.
INERT_BY_DESIGN = {
    # The ticket embedding backend is resolved from the EMBEDDING_BACKEND
    # environment variable in memory/embedding_factory.py, not from YAML.
    # This block advertises YAML control that does not exist. Left in place
    # rather than deleted or wired up: wiring it would change which embedding
    # backend real deployments get, and store_factory.py warns that a
    # backend/dimension mismatch corrupts an existing vector store. That is
    # the owner's call, not a drive-by.
    "ticket_tracking.embedding.model",
    "ticket_tracking.embedding.dimensions",
    "ticket_tracking.embedding.backend",
}


def _leaf_keys(node, prefix=()):
    for key, value in node.items():
        if isinstance(value, dict):
            yield from _leaf_keys(value, prefix + (key,))
        else:
            yield prefix + (key,), value


def _observable_state(config_dict) -> dict:
    """Everything both config loaders derive from a given YAML document."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(config_dict, handle)
        path = handle.name

    previous = os.environ.get("HEPHAESTUS_CONFIG")
    os.environ["HEPHAESTUS_CONFIG"] = path
    try:
        import src.core.llm_config as llm_config
        import src.core.simple_config as simple_config

        importlib.reload(simple_config)
        importlib.reload(llm_config)

        state = {
            f"config.{k}": repr(v)
            for k, v in vars(simple_config.Config()).items()
            if not k.startswith("_")
        }
        # The llm.* subtree is consumed by a second, separate loader.
        state["llm_config"] = repr(llm_config.SimpleConfig(path).get_llm_config())
        return state
    finally:
        os.unlink(path)
        if previous is None:
            os.environ.pop("HEPHAESTUS_CONFIG", None)
        else:
            os.environ["HEPHAESTUS_CONFIG"] = previous


def _perturb(value):
    """A clearly different value of the same rough type, or None to skip."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 4242
    if isinstance(value, float):
        return value + 42.5
    if isinstance(value, str):
        return value + "_ZZPROBE"
    return None


@pytest.fixture
def baseline_document():
    return yaml.safe_load(CONFIG_PATH.read_text())


@pytest.fixture
def baseline_state(baseline_document):
    """Deliberately function-scoped, not module-scoped.

    Every probe reloads src.core.simple_config and src.core.llm_config, which
    resets module-level state the loaders build at import time. A baseline
    captured once at module scope is therefore measured under different
    conditions than the mutated runs compared against it, and the comparison
    starts reporting differences that have nothing to do with the key under
    test. Re-measuring per test keeps both sides of the comparison honest.
    """
    return _observable_state(baseline_document)


def _probe_cases():
    """(path_tuple, readable_id) for every perturbable leaf.

    The path is carried as a tuple rather than a dotted string: some keys are
    map entries whose names contain dots themselves (model ids such as
    "pi/Qwen3.8-27B-UD-Q4_K_XL.gguf"), so a dotted string cannot be split back
    into the original path.
    """
    document = yaml.safe_load(CONFIG_PATH.read_text())
    for path, value in _leaf_keys(document):
        if _perturb(value) is not None:
            yield path, ".".join(path)


@pytest.mark.parametrize(
    "path,dotted_key",
    sorted(_probe_cases(), key=lambda case: case[1]),
    ids=lambda case: case if isinstance(case, str) else "",
)
def test_every_config_key_changes_something(
    path, dotted_key, baseline_document, baseline_state
):
    """Changing a key must change what the application reads.

    If this fails, the key is inert: either the loader spells it differently
    (the `worktree_branch_prefix` vs `branch_prefix` case), or nothing reads
    it at all. Fix the loader, fix the key, or add it to INERT_BY_DESIGN with
    a reason.
    """
    if dotted_key in INERT_BY_DESIGN:
        pytest.skip(f"documented as inert: {dotted_key}")

    mutated = copy.deepcopy(baseline_document)
    node = mutated
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = _perturb(node[path[-1]])

    assert _observable_state(mutated) != baseline_state, (
        f"'{dotted_key}' in hephaestus_config.yaml changes nothing when modified -- "
        "it is silently ignored. Check the loader spells the key the same way."
    )


def test_inert_allowlist_is_still_accurate(baseline_document, baseline_state):
    """An allowlisted key that starts working should be removed from the list,
    so it doesn't mask a future regression of the same key."""
    still_inert = []
    for dotted_key in sorted(INERT_BY_DESIGN):
        path = tuple(dotted_key.split("."))
        mutated = copy.deepcopy(baseline_document)
        node = mutated
        try:
            for key in path[:-1]:
                node = node[key]
            perturbed = _perturb(node[path[-1]])
        except KeyError:
            continue  # key removed from the config entirely; nothing to assert
        if perturbed is None:
            continue
        node[path[-1]] = perturbed
        if _observable_state(mutated) == baseline_state:
            still_inert.append(dotted_key)

    assert still_inert == sorted(INERT_BY_DESIGN), (
        "INERT_BY_DESIGN lists keys that now DO take effect; remove them: "
        f"{sorted(set(INERT_BY_DESIGN) - set(still_inert))}"
    )

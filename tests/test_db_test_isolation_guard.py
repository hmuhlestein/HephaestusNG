"""Structural guard: no production code may bypass HEPHAESTUS_TEST_DB.

`DatabaseManager.__init__` is declared as::

    def __init__(self, database_path: str = "hephaestus.db"):
        if database_path is None:
            database_path = os.environ.get("HEPHAESTUS_TEST_DB", "hephaestus.db")

so the test-database redirect fires **only** when a caller passes `None`
explicitly. Both `DatabaseManager()` and `DatabaseManager("hephaestus.db")`
silently read and write the real, live database -- under test as well as in
production. Nothing crashes, because the production DB has the right schema;
the tests just quietly operate on real data.

This has now recurred twice. AUTOPILOT_REFACTOR_PLAN.md §3.3 records the first
sweep -- 16 bare `DatabaseManager()` call sites, root-caused from
`test_autopilot_api.py` failing because 33 real feature records leaked into
what should have been an empty test -- and closes with: "any *future*
`DatabaseManager()` call site (new code, or code this plan's later phases
touch) must use `DatabaseManager(None)`". Three sites using the literal-string
form survived that sweep anyway, because it grepped for the bare-call spelling
only: `frontend/phase_routes.py` and two in `prompts/assembler.py`.

A grep-based convention that has to be re-run by hand is exactly what failed.
This asserts the property instead.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# The only spelling that honours HEPHAESTUS_TEST_DB is an explicit None, or a
# path the caller computed deliberately (e.g. config.database_path).
BYPASSING = {None, "hephaestus.db"}


def _bypassing_constructions(path: Path):
    """Yield (lineno, rendered_arg) for DatabaseManager calls that bypass."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "DatabaseManager":
            continue
        if not node.args and not node.keywords:
            yield node.lineno, "DatabaseManager()"
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and first.value == "hephaestus.db":
            yield node.lineno, 'DatabaseManager("hephaestus.db")'


def test_no_production_code_bypasses_test_db_isolation():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        for lineno, rendered in _bypassing_constructions(path):
            offenders.append(f"{rel}:{lineno}  {rendered}")

    assert not offenders, (
        "DatabaseManager constructed in a way that bypasses HEPHAESTUS_TEST_DB:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass None instead. Only `DatabaseManager(None)` consults the env "
        "var; the bare call and the literal \"hephaestus.db\" both pin the real "
        "database, so tests silently operate on production data."
    )


def test_the_guard_detects_both_bypassing_spellings():
    """Guards against this test quietly becoming vacuous.

    If DatabaseManager's signature is ever reworked so neither spelling is
    dangerous, this test should be deleted deliberately -- not left passing
    because the detector stopped matching anything.
    """
    import tempfile

    sample = (
        "from src.core.database import DatabaseManager\n"
        "a = DatabaseManager()\n"
        'b = DatabaseManager("hephaestus.db")\n'
        "c = DatabaseManager(None)\n"
        "d = DatabaseManager(config.database_path)\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(sample)
        probe = Path(f.name)

    found = sorted(rendered for _, rendered in _bypassing_constructions(probe))
    probe.unlink()

    assert found == ['DatabaseManager("hephaestus.db")', "DatabaseManager()"], (
        f"detector no longer matches both bypassing spellings: {found}"
    )

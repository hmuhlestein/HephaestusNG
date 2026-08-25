"""Applying forensics-proposed prompt edits to phase YAML files.

forensics_analysis reviews a completed run and proposes prompt rewrites for
future ones. Those proposals used to land in prose inside forensics.md and in
`improvement` tickets that didn't even carry the before/after text, so nothing
tracked which were applied or whether anything improved
(design_docs/agent_prompt_analysis.md finding 8).

This module is the half of that loop with teeth: it decides what a proposal is
ALLOWED to change, makes the edit without destroying the file, and commits it
so it is diffable and revertable.

SAFETY MODEL -- read before widening anything here.

This is a self-modifying system: an LLM proposing edits to the prompts that
drive the pipeline that runs it. Two guards, both enforced here rather than in
the UI, because a guard the API doesn't enforce is exactly the kind of
"configured but never fires" gate this file's own design review kept finding:

  1. Only prose fields are editable (EDITABLE_FIELDS). The gate wiring --
     spec_gate, outputs, id, name, and everything in workflow.yaml, including
     the evaluation-point thresholds -- is off limits. A proposal that could
     drop `spec_gate: true` or lower a continue threshold could silently undo
     a gate fix while arriving in the UI as a routine approved improvement,
     which is the precise failure mode of findings 1 and 2.
  2. A phase cannot edit its own prompt (SELF_EDIT_BLOCKED). Without this,
     forensics_analysis can rewrite forensics_analysis.yaml, and the loop has
     no fixed point outside itself.

WHICH COPY OF A PROMPT THIS EDITS, AND WHEN IT TAKES EFFECT.

There are three copies of any phase prompt, and this module edits the first:

  1. config/workflows/<def>/<phase>.yaml -- the TEMPLATE. Read once, when a
     workflow is created (phase_manager.initialize_workflow: "Only create
     phase records for NEW workflows"). This is what a proposal edits.
  2. The Phase DB row -- a per-workflow snapshot taken from the template at
     creation. This is what an agent actually reads at dispatch, via
     get_phase_context.
  3. PhasePromptVersion rows -- an existing, separate draft/publish/restore
     mechanism for editing ONE running workflow's prompt, which writes into
     that Phase row.

So an approved proposal changes NOTHING about any workflow already in flight;
it lands for workflows created afterwards. That is the correct scope for this
feature -- forensics_analysis exists to improve FUTURE runs, and rewriting a
prompt out from under a running agent would be worse than useless -- but it
has to be said out loud in the UI, because "approve" that visibly does nothing
to the running pipeline otherwise reads as a bug.

The corollary: if a PhasePromptVersion has been published for some workflow's
phase, that workflow's DB row no longer matches the template, and a proposal's
diff (which is template-vs-template, correctly) will not resemble what that
particular run is executing. The two mechanisms are complementary -- per
definition here, per running workflow there -- not competing.

Edits are surgical text replacements, NOT a yaml.safe_load/yaml.dump round
trip. These files carry long explanatory comments that are load-bearing
documentation (see workflow.yaml's THRESHOLD RATIONALE block), and dumping
would delete every one of them. ruamel.yaml would preserve them but is not a
declared dependency of this project. Instead the edit is made textually and
then VERIFIED by parsing before and after and asserting that exactly one key
changed -- a stronger check than trusting a round-tripper, and it fails loudly
instead of silently mangling a prompt.
"""

import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

#: The only fields a proposal may rewrite. All prose: what the agent is told
#: to do and what "done" means. Deliberately excludes every field that wires a
#: phase into the orchestrator (see this module's SAFETY MODEL).
EDITABLE_FIELDS: Tuple[str, ...] = ("description", "done_definitions", "additional_notes")

#: Files no proposal may touch, whatever field it names. workflow.yaml holds
#: the evaluation points, thresholds, required_output and phase_inputs -- the
#: orchestration contract, not a prompt.
PROTECTED_FILENAMES: Tuple[str, ...] = ("workflow.yaml",)

#: Phases that may not have their own prompt rewritten by a proposal they
#: generated. Closes the self-modification loop.
SELF_EDIT_BLOCKED: Tuple[str, ...] = ("forensics_analysis",)

#: Serializes apply/revert. Each is a read-modify-write of a file plus a git
#: commit, and both halves race badly: two approvals landing together can each
#: read the original, each write, and the loser is silently lost while its row
#: still says "applied" with a commit SHA -- a row that lies. The git index is
#: also process-wide, so concurrent `git commit --only` calls contend. Approvals
#: are human-paced, so a plain lock costs nothing.
_EDIT_LOCK = threading.Lock()


def _workflows_dir() -> Path:
    from src.workflow_registry import _WORKFLOWS_DIR

    return _WORKFLOWS_DIR


def phase_yaml_path(workflow_definition: str, phase_name: str) -> Optional[Path]:
    """Locate a phase's YAML by its declared `name:`, not by filename.

    Filenames are not the phase name in every workflow -- feature_architect's
    are numbered (01_feature_architect.yaml) -- so match on the field the rest
    of the system keys off.
    """
    wf_dir = _workflows_dir() / workflow_definition
    if not wf_dir.is_dir():
        return None
    for candidate in sorted(wf_dir.glob("*.yaml")):
        if candidate.name in PROTECTED_FILENAMES:
            continue
        try:
            cfg = yaml.safe_load(candidate.read_text()) or {}
        except Exception:
            continue
        if isinstance(cfg, dict) and cfg.get("name") == phase_name:
            return candidate
    return None


def _coerce_done_definitions(value: Any) -> Any:
    """Accept done_definitions as a real list OR a YAML-block/JSON-array
    string and normalize to a list.

    The MCP tool schema leaves proposed_value/current_value untyped so one
    schema can serve every editable field; an agent submitting the one
    list-typed field (done_definitions) has no schema hint that an array is
    expected and reliably sends it JSON- or YAML-encoded as a string
    instead (ticket-cdb9fa63). yaml.safe_load parses both encodings (JSON is
    a YAML subset), so it is the single parse path for either. Anything
    that isn't a list of strings after parsing is returned unchanged so
    validate_proposal's own check still produces the real error message.
    """
    if not isinstance(value, str):
        return value
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    if isinstance(parsed, list) and all(isinstance(v, str) for v in parsed):
        return parsed
    return value


def validate_proposal(
    workflow_definition: str,
    phase_name: str,
    field: str,
    proposed_value: Any,
    proposing_phase: Optional[str] = None,
) -> Optional[str]:
    """Reject anything outside the safety model. Returns an error message, or
    None if the proposal is allowed.

    Called both when a proposal is created and again immediately before it is
    applied -- the allowlist could have tightened in between, and a stored
    proposal must not be grandfathered past a guard that exists now.
    """
    if field not in EDITABLE_FIELDS:
        return (
            f"field {field!r} is not editable by a prompt proposal. "
            f"Editable fields are {', '.join(EDITABLE_FIELDS)} -- the orchestration "
            "wiring (spec_gate, outputs, id, name, and everything in workflow.yaml) "
            "is deliberately out of reach, because an approved proposal that changed "
            "it could silently disable a pipeline gate."
        )
    if phase_name in SELF_EDIT_BLOCKED and proposing_phase == phase_name:
        return (
            f"{phase_name} may not rewrite its own prompt -- that closes a "
            "self-modification loop with no fixed point outside itself. Raise it "
            "with a human instead."
        )
    path = phase_yaml_path(workflow_definition, phase_name)
    if path is None:
        return (
            f"no phase named {phase_name!r} in workflow {workflow_definition!r} "
            "(or it is a protected file)"
        )
    if field == "done_definitions":
        if not isinstance(proposed_value, list) or not all(
            isinstance(v, str) for v in proposed_value
        ):
            return "done_definitions must be a list of strings"
        if not proposed_value:
            return "done_definitions cannot be emptied by a proposal"
    else:
        if not isinstance(proposed_value, str) or not proposed_value.strip():
            return f"{field} must be a non-empty string"
    return None


def current_value(workflow_definition: str, phase_name: str, field: str) -> Any:
    """The field's value as it stands on disk -- the 'before' side of a diff.

    Read live rather than trusting a value the proposing agent quoted: the file
    can have changed since, and showing a stale 'before' would make the diff a
    fiction.
    """
    path = phase_yaml_path(workflow_definition, phase_name)
    if path is None:
        return None
    cfg = yaml.safe_load(path.read_text()) or {}
    return cfg.get(field)


_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def _field_span(lines: List[str], field: str) -> Optional[Tuple[int, int]]:
    """[start, end) line indices of a top-level field's whole block, including
    any block-scalar body or list items beneath it."""
    start = None
    for i, line in enumerate(lines):
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match and match.group(1) == field:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _TOP_LEVEL_KEY_RE.match(lines[j]):
            end = j
            break
    # Walk back off any blank lines and column-0 comments that sit between
    # this field's real content and the next key. They belong to what FOLLOWS,
    # not to this field, and swallowing them into the replacement silently
    # deletes them -- these files use those comments as load-bearing
    # documentation (security_review.yaml's `outputs:` carries a paragraph
    # explaining why its filename is bare, directly above the key).
    #
    # Column 0 is the discriminator: a "#" line INSIDE a block scalar is
    # indented, and is content rather than a comment.
    while end - 1 > start:
        stripped = lines[end - 1]
        if stripped.strip() == "" or (stripped.startswith("#")):
            end -= 1
        else:
            break
    return (start, end)


def _render_field(field: str, value: Any) -> List[str]:
    """Emit the replacement block for one field, in the shape these files
    already use: a literal block scalar for prose, a `- "..."` list for
    done_definitions."""
    if field == "done_definitions":
        # Let the library handle quoting/escaping rather than hand-rolling it.
        # Dumped as a mapping, not item-by-item: safe_dump of a BARE scalar
        # appends a "..." document-end marker, which spliced into the middle of
        # a file produces YAML that no longer parses. width is set absurdly
        # high because these entries are long single-line sentences and the
        # default 80-column folding would rewrap every one of them.
        dumped = yaml.safe_dump(
            {field: value},
            default_flow_style=False,
            width=10**6,
            allow_unicode=True,
            sort_keys=False,
        )
        return dumped.rstrip("\n").split("\n")
    # A single-line value with no trailing newline was written as a plain or
    # quoted scalar (`description: "..."`), not a block scalar. Forcing it into
    # `|` form appends a newline that the block-scalar clip rule then reads
    # back, so the value no longer equals what was asked for and apply_edit
    # rejects the edit. Emit it the way it was written instead. Every autopilot
    # phase uses block scalars, which is why this only surfaced on
    # feature_architect's one-line description.
    if isinstance(value, str) and "\n" not in value:
        dumped = yaml.safe_dump(
            {field: value},
            default_flow_style=False,
            width=10**6,
            allow_unicode=True,
            sort_keys=False,
        )
        return dumped.rstrip("\n").split("\n")

    body = str(value).rstrip("\n")
    out = [f"{field}: |"]
    for line in body.split("\n"):
        # Only a TRULY empty line becomes empty. A whitespace-only line is
        # content -- these prompts embed code samples whose blank-looking lines
        # carry real indentation, and flattening them changes the value (caught
        # by the read-back check on architecture_design.yaml, whose auth
        # example has three such lines inside a docstring).
        out.append(f"  {line}" if line else "")
    return out


def apply_edit(path: Path, field: str, new_value: Any) -> str:
    """Replace one top-level field in a phase YAML, leaving every other byte
    of the file -- comments included -- untouched. Returns the new file text.

    Raises ValueError if the result does not parse, if the target field did not
    end up with the intended value, or if ANY other key changed. That last
    check is the point: a textual edit that accidentally swallowed the
    following key would otherwise produce a file that still parses and quietly
    drops a phase's outputs or spec_gate.
    """
    original = path.read_text()
    before = yaml.safe_load(original) or {}
    lines = original.split("\n")
    # A duplicated top-level key makes this edit ambiguous and unsafe: PyYAML
    # resolves duplicates last-wins, while _field_span finds the first, so
    # editing would rewrite a block that has no effect on the parsed value and
    # leave the effective one untouched -- producing a file whose visible text
    # disagrees with its meaning, and passing the read-back check by accident
    # because the OTHER copy still supplies the old value.
    # config/workflows/feature_architect/01_feature_architect.yaml really does
    # declare `description:` twice (a quoted one-liner and a block scalar).
    occurrences = sum(
        1 for line in lines
        if (m := _TOP_LEVEL_KEY_RE.match(line)) and m.group(1) == field
    )
    if occurrences > 1:
        raise ValueError(
            f"{path.name} declares {field!r} {occurrences} times at the top level. "
            "Refusing to edit an ambiguous key -- fix the duplicate in the file first."
        )
    span = _field_span(lines, field)
    if span is None:
        raise ValueError(f"{path.name} has no top-level {field!r} field to replace")
    start, end = span
    replacement = _render_field(field, new_value)
    updated = "\n".join(lines[:start] + replacement + lines[end:])
    # Restore the file's own trailing newline. When the edited field is the
    # LAST one in the file (additional_notes usually is), lines[end:] is empty
    # and _render_field has already rstripped the body, so the join drops it --
    # and a block scalar without its final newline reads back one character
    # short of the proposed value, which the verification below then rejects.
    if original.endswith("\n") and not updated.endswith("\n"):
        updated += "\n"

    after = yaml.safe_load(updated)
    if not isinstance(after, dict):
        raise ValueError(f"editing {field!r} in {path.name} produced unparseable YAML")
    if after.get(field) != new_value:
        raise ValueError(
            f"editing {field!r} in {path.name} did not take effect as intended "
            "(the rendered block did not read back equal to the proposed value)"
        )
    changed = {
        key
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }
    # Subset, not equality: re-applying a field's existing value is a legitimate
    # no-op edit that changes nothing, and the target field having actually
    # changed is already covered by the read-back check above. What must never
    # happen is some OTHER key moving -- a textual replacement that swallowed
    # the following key would still parse, and would quietly drop that phase's
    # outputs or spec_gate.
    collateral = changed - {field}
    if collateral:
        raise ValueError(
            f"editing {field!r} in {path.name} would also change {sorted(collateral)} "
            "-- refusing to write an edit that reaches beyond its own field"
        )
    return updated


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30
    )


def commit_file(repo: Path, path: Path, message: str) -> Optional[str]:
    """Commit one file and return the new SHA, or None if nothing changed.

    Scoped to the single file deliberately: the working tree routinely carries
    unrelated in-flight work, and an approved prompt tweak must never sweep it
    into a commit.
    """
    try:
        rel = str(path.relative_to(repo)) if path.is_absolute() else str(path)
    except ValueError:
        # Deployments where the workflows tree is not inside the checkout being
        # committed to (an installed package, a custom workflows path). The
        # edit itself already succeeded; say so plainly rather than surfacing a
        # bare relative_to() traceback.
        raise RuntimeError(
            f"{path} is not inside the git repository at {repo}, so the change was "
            "written but could not be committed. Commit it manually, or point the "
            "workflows directory inside the checkout."
        )
    add = _git(repo, "add", "--", rel)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed for {rel}: {add.stderr.strip()}")
    staged = _git(repo, "diff", "--cached", "--quiet", "--", rel)
    if staged.returncode == 0:
        return None  # nothing actually changed
    commit = _git(repo, "commit", "-m", message, "--only", "--", rel)
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed for {rel}: {commit.stderr.strip()}")
    sha = _git(repo, "rev-parse", "HEAD")
    return sha.stdout.strip() or None


def apply_proposal(
    repo_root: Path,
    workflow_definition: str,
    phase_name: str,
    field: str,
    proposed_value: Any,
    proposal_id: str,
    proposing_phase: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate, write and commit one approved proposal.

    Re-validates rather than trusting what was stored at creation time: the
    allowlist may have tightened, or the phase may have been renamed away,
    since the proposal was filed.
    """
    problem = validate_proposal(
        workflow_definition, phase_name, field, proposed_value, proposing_phase
    )
    if problem:
        raise ValueError(problem)
    path = phase_yaml_path(workflow_definition, phase_name)
    if path is None:
        raise ValueError(f"no phase named {phase_name!r} in {workflow_definition!r}")

    with _EDIT_LOCK:
        previous_value = current_value(workflow_definition, phase_name, field)
        updated = apply_edit(path, field, proposed_value)
        path.write_text(updated)
        sha = commit_file(
            repo_root,
            path,
            f"prompt: apply forensics proposal to {phase_name}.{field}\n\n"
            f"Proposal {proposal_id}, approved via the autopilot Improvements tab.\n"
            f"Reverting this commit restores the previous prompt exactly.",
        )
    logger.info(
        f"[PROMPT-PROPOSAL] Applied {proposal_id[:8]} to {phase_name}.{field} "
        f"({path.name}) as {sha[:8] if sha else 'no-op'}"
    )
    return {"path": str(path), "commit_sha": sha, "previous_value": previous_value}


def revert_proposal(
    repo_root: Path,
    workflow_definition: str,
    phase_name: str,
    field: str,
    previous_value: Any,
    proposal_id: str,
) -> Dict[str, Any]:
    """Put the field back to the value recorded when the proposal was applied.

    Restores the stored previous value rather than running `git revert` on the
    apply commit: other commits may have touched the same file since, and a
    revert would fight them. Writing the known-previous value back through the
    same verified edit path touches only this one field.
    """
    path = phase_yaml_path(workflow_definition, phase_name)
    if path is None:
        raise ValueError(f"no phase named {phase_name!r} in {workflow_definition!r}")
    # Without this, _render_field stringifies None and writes the literal text
    # "None" into the prompt -- a silent corruption that would read as a real
    # instruction to the next agent.
    if previous_value is None:
        raise ValueError(
            f"no recorded previous value for {phase_name}.{field}, so there is nothing "
            "to restore. Edit the file by hand rather than guessing at what it was."
        )
    with _EDIT_LOCK:
        updated = apply_edit(path, field, previous_value)
        path.write_text(updated)
        sha = commit_file(
            repo_root,
            path,
            f"prompt: revert forensics proposal to {phase_name}.{field}\n\n"
            f"Proposal {proposal_id}, reverted via the autopilot Improvements tab.",
        )
    logger.info(f"[PROMPT-PROPOSAL] Reverted {proposal_id[:8]} on {phase_name}.{field}")
    return {"path": str(path), "commit_sha": sha}


def create_proposal(
    phase_name: str,
    field: str,
    proposed_value: Any,
    rationale: str,
    evidence: Optional[str] = None,
    quoted_current_value: Optional[Any] = None,
    workflow_definition: str = "autopilot",
    workflow_id: Optional[str] = None,
    proposing_phase: Optional[str] = None,
    created_by_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and persist one proposal.

    Shared by the HTTP route and the MCP tool so the guards cannot diverge --
    a tool path that skipped validate_proposal would be a second, unguarded
    door into the same edit engine.

    Raises ValueError with the reason if the proposal is not allowed; the
    callers turn that into a 400 or a tool error respectively. Rejecting at
    creation matters: the agent still has the context to write a different
    proposal, whereas a bad row discovered later in the review UI is just
    noise a human has to clear.
    """
    import uuid as _uuid
    from datetime import datetime as _dt

    from src.core.database import PromptProposal, get_db

    if field == "done_definitions":
        proposed_value = _coerce_done_definitions(proposed_value)
        quoted_current_value = _coerce_done_definitions(quoted_current_value)

    problem = validate_proposal(
        workflow_definition, phase_name, field, proposed_value, proposing_phase
    )
    if problem:
        raise ValueError(problem)
    if not rationale or not str(rationale).strip():
        raise ValueError(
            "rationale is required -- a prompt change with no recorded reason "
            "cannot be reviewed, only guessed at"
        )

    proposal_id = f"prop-{_uuid.uuid4().hex[:8]}"
    with get_db() as db:
        db.add(
            PromptProposal(
                id=proposal_id,
                workflow_id=workflow_id,
                created_by_agent_id=created_by_agent_id,
                workflow_definition=workflow_definition,
                phase_name=phase_name,
                field=field,
                proposing_phase=proposing_phase,
                proposed_value=proposed_value,
                quoted_current_value=quoted_current_value,
                rationale=rationale,
                evidence=evidence,
                status="pending",
                created_at=_dt.utcnow(),
            )
        )
        db.commit()
    logger.info(
        f"[PROMPT-PROPOSAL] {proposal_id} filed against {phase_name}.{field} "
        f"by {proposing_phase or 'unknown'}"
    )
    return {"id": proposal_id, "phase_name": phase_name, "field": field, "status": "pending"}

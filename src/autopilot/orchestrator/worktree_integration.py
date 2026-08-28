"""Pipeline-level worktree/git orchestration and security scanning."""

import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

import git as _git

from src.autopilot.orchestrator.state import (
    DesignEntry,
)
from src.core.constants import (
    CONTEXT_DIR_NAME,
)
from src.core.database import (
    Agent,
    Feature,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
    get_db,
)
from src.core.simple_config import get_config

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger
    from src.core.database import DatabaseManager

logger = logging.getLogger(__name__)


def create_feature_folder(project_path: Path, design_name: str, logger: "OrchestratorLogger") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_name.lower().replace(" ", "_")[:40]
    # Features go in .hephaestus/features/ to keep project root clean
    feature_folder = project_path / CONTEXT_DIR_NAME / "features" / f"{timestamp}_{safe_name}"
    feature_folder.mkdir(parents=True, exist_ok=True)
    (feature_folder / "docs").mkdir(exist_ok=True)

    # Note: .hephaestus/ is excluded from git via .git/info/exclude
    # (managed by WorktreeManager). We do NOT modify the user's .gitignore.

    logger.info(f"Feature folder: {feature_folder}")
    return feature_folder


def _copy_design_content(source: Path, heph_dir: Path, filename: str, is_directory: bool) -> Path:
    """Shared copy primitive behind copy_design_source and every call site
    that only has a raw path (not a DesignEntry) available -- e.g.
    run_single_workflow, which only has launch_params["design_document"].

    File case: copies source to heph_dir / filename. Directory case:
    recursively copies the entire tree to heph_dir / "specs" / source.name,
    replacing any existing destination wholesale (rmtree + copytree) rather
    than merging, so a file deleted from the source between runs does not
    silently survive at the destination.
    """
    heph_dir.mkdir(parents=True, exist_ok=True)
    if is_directory:
        dest = heph_dir / "specs" / source.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        return dest
    dest = heph_dir / filename
    shutil.copy2(source, dest)
    return dest


def copy_design_source(design_entry: DesignEntry, heph_dir: Path, filename: str = "design.md") -> Path:
    """Copy design_entry's backing content into heph_dir. Directory-sourced
    entries (design_entry.source_dir set, REQ-02/NFR-02) recursively copy the
    whole tree via _copy_design_content; file-sourced entries copy
    design_entry.path to heph_dir/filename. Raises FileNotFoundError if the
    source vanished (race: deleted between detection and copy)."""
    if design_entry.source_dir is not None:
        source = Path(design_entry.source_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"Design source directory vanished: {source}")
        return _copy_design_content(source, heph_dir, filename, is_directory=True)
    source = Path(design_entry.path)
    if not source.is_file():
        raise FileNotFoundError(f"Design source file vanished: {source}")
    return _copy_design_content(source, heph_dir, filename, is_directory=False)


def copy_design_document(design_entry: DesignEntry, feature_folder: Path) -> Path:
    """Back-compat wrapper over copy_design_source: copies design_entry into
    feature_folder/CONTEXT_DIR_NAME/, preserving the default 'design.md'
    filename for file-sourced entries."""
    return copy_design_source(design_entry, feature_folder / CONTEXT_DIR_NAME)


def copy_speckit_feature(dir_path: Path, feature_folder: Path) -> Path:
    """Copy dir_path (a SpecKitFeature.dir_path / DesignEntry.speckit_feature_dir)
    -> feature_folder/CONTEXT_DIR_NAME/specs/<NNN-name>/ recursively -- ALL
    files, not just spec.md/plan.md/tasks.md (REQ-03/FR-002a). Raises
    FileNotFoundError if dir_path no longer exists (race: the source specs/
    dir was deleted between detection and worktree creation)."""
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Spec Kit feature directory vanished: {dir_path}")
    dest = feature_folder / CONTEXT_DIR_NAME / "specs" / dir_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dir_path, dest, dirs_exist_ok=True)
    return dest


def _create_integration_worktree(
    project_path: Path,
    design_id: str,
    branch: str,
    logger: "OrchestratorLogger",
    db_manager: Optional["DatabaseManager"] = None,
) -> Optional[Path]:
    """Create an integration worktree for a feature pipeline.

    Args:
        project_path: Path to the project root
        design_id: Design ID for branch naming
        branch: Branch name to create
        logger: Orchestrator logger
        db_manager: Optional shared DatabaseManager to avoid leaking connections.
            If None, creates a new one and ensures cleanup.

    Returns:
        Path to the worktree, or None on failure
    """
    try:
        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        # If project_path is already a worktree (contains .worktrees/), use
        # it directly instead of creating a worktree inside it -- same
        # no-nested-worktrees guard as run_single_workflow's design-worktree
        # setup (__init__.py). A worktree nested here would be destroyed
        # when the parent worktree is cleaned up.
        if ".worktrees/" in str(project_path):
            logger.info(f"Using existing worktree directly: {project_path}")
            return Path(project_path)

        cfg = get_config()
        db = db_manager or DbManager(str(cfg.paths.database_path))
        try:
            wt_mgr = WorktreeManager(db_manager=db, repo_path=project_path)

            # Create branch from main if it doesn't exist
            try:
                wt_mgr.main_repo.git.branch(branch)
            except _git.exc.GitCommandError:
                pass  # Branch exists

            # Create worktree
            safe_branch = branch.replace("/", "-")
            wt_path = wt_mgr.worktree_base / f"wt_{safe_branch}"
            # A directory can exist here without being a valid git worktree --
            # e.g. a prior run got killed mid-`git worktree add`, or a stale
            # cleanup left a stub behind (observed live: only a leftover
            # .hephaestus/.placeholder, no .git). Reusing it silently as-is
            # then breaks everything downstream: agent creation later
            # discovers it has no .git, falls back to an isolated per-agent
            # worktree, and nulls the workflow's working_directory -- so
            # output validation can never find what the agent wrote. Treat
            # "exists but not a real worktree" the same as "doesn't exist".
            if wt_path.exists() and not (wt_path / ".git").exists():
                logger.warning(f"Found stale non-worktree directory at {wt_path} (no .git) -- removing before recreating")
                import shutil as _shutil

                _shutil.rmtree(wt_path, ignore_errors=True)
            if not wt_path.exists():
                wt_mgr.main_repo.git.worktree("add", str(wt_path), branch)

            logger.info(f"Created integration worktree: {wt_path} (branch: {branch})")
            return wt_path
        finally:
            # BLOCKER-3: Only dispose if we created a new DatabaseManager
            # (caller passed db_manager means caller owns the lifecycle)
            if db_manager is None and db is not None:
                try:
                    db.dispose()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[WORKTREE] Failed to create integration worktree for {project_path} (branch: {branch}): {e}", exc_info=True)
        return None


def _cleanup_worktree(
    worktree: Path,
    branch: str,
    project_path: Path,
    logger: "OrchestratorLogger",
    db_manager: Optional["DatabaseManager"] = None,
) -> None:
    """Clean up a worktree after feature pipeline completes.

    Args:
        worktree: Path to the worktree
        branch: Branch name
        project_path: Path to the project root
        logger: Orchestrator logger
        db_manager: Optional shared DatabaseManager to avoid leaking connections.
    """
    try:
        from src.core.database import DatabaseManager as DbManager
        from src.core.simple_config import get_config
        from src.core.worktree_manager import WorktreeManager

        cfg = get_config()
        db = db_manager or DbManager(str(cfg.paths.database_path))
        try:
            wt_mgr = WorktreeManager(db_manager=db, repo_path=project_path)

            # Archive tmux transcripts before the worktree (and everything in
            # it) is deleted -- .hephaestus/ is git-excluded, so it doesn't
            # survive the merge like docs/*.md reports do. Copy to the same
            # project-root .hephaestus/tmux/ location _assess_run_health
            # already reads from, so these transcripts remain available for
            # forensics/audit after the fact, same as the merged report
            # artifacts.
            try:
                src_tmux = worktree / CONTEXT_DIR_NAME / "tmux"
                if src_tmux.is_dir():
                    dest_tmux = project_path / CONTEXT_DIR_NAME / "tmux"
                    dest_tmux.mkdir(parents=True, exist_ok=True)
                    for log_file in src_tmux.glob("*"):
                        shutil.copy2(log_file, dest_tmux / log_file.name)
                    logger.info(f"Archived tmux transcripts to {dest_tmux}")
            except Exception as e:
                logger.warning(f"Failed to archive tmux transcripts: {e}")

            # Remove worktree -- routed through WorktreeManager._remove_worktree
            # (require_clean=True) instead of a raw `git worktree remove
            # --force`, so a feature pipeline that completed with real,
            # uncommitted work still sitting in this worktree (e.g. a
            # crash-induced false "abandoned" marking, or a phase's own
            # commit step silently failing) doesn't get destroyed here the
            # same way cleanup_all_stale_branches's identical bypass did
            # before that bug was fixed.
            if worktree.exists():
                wt_mgr._remove_worktree(str(worktree), require_clean=True)
                if worktree.exists():
                    logger.warning(f"Worktree not removed (uncommitted changes or removal error): {worktree}")
                else:
                    logger.info(f"Removed worktree: {worktree}")

                # Clear stale working_directory from any workflows pointing to
                # this worktree -- but never touch a workflow that's still
                # "active" or "paused" (resumable -- see the same exclusion in
                # worktree_manager.py's cleanup_all_stale_branches). This
                # worktree path is deterministic (derived only from design_id,
                # reused across every retry), so an old, already-finished
                # attempt's cleanup can otherwise null out a *different*,
                # currently-active-or-paused workflow that has since
                # legitimately reused the same path (e.g. after an abrupt
                # orchestrator kill left an earlier attempt's cleanup
                # deferred). Once working_directory is wrongly nulled, agent
                # creation can't find the shared worktree (falls back to an
                # isolated per-agent one) and output validation can't check
                # any candidate path at all -- silently breaking a workflow
                # that's still genuinely in progress or waiting to be resumed.
                try:
                    from src.core.database import Workflow

                    _s = db.get_session()
                    try:
                        wfs = (
                            _s.query(Workflow)
                            .filter(
                                Workflow.working_directory == str(worktree),
                                Workflow.status.notin_(["active", "paused"]),
                            )
                            .all()
                        )
                        for wf in wfs:
                            wf.working_directory = None
                            logger.info(f"Cleared stale working_directory from workflow {wf.id[:8]}")
                        if wfs:
                            _s.commit()
                    finally:
                        _s.close()
                except Exception as e:
                    logger.warning(f"Failed to clear workflow working_directory: {e}")
        finally:
            # BLOCKER-3: Only dispose if we created a new DatabaseManager
            if db_manager is None:
                try:
                    db.dispose()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Failed to cleanup worktree: {e}")


def sweep_completed_workflow_worktrees(logger: "OrchestratorLogger") -> int:
    """Remove worktrees left behind by workflows that reached 'completed'
    status but never got their normal _cleanup_worktree() call to run --
    e.g. the backend restarted between run_single_workflow returning
    "completed" in _run_one_feature and that same call stack reaching its
    _cleanup_worktree() a few lines later. Nothing else ever revisits a
    workflow once it's "completed", so a worktree orphaned this way sits
    forever until something (previously: only a manual /cleanup-branches
    call, or a rerun of that exact design) happens to sweep it.

    Deliberately narrower than WorktreeManager.cleanup_all_stale_branches():
    only touches a worktree whose OWN workflow record unambiguously says
    "done", one at a time via the same removal _cleanup_worktree already
    uses for the normal completion path -- not a heuristic dirty/branch
    sweep that can pull old, unrelated branches back into main (observed
    live: doing that once already reintroduced files under .hephaestus/,
    which must stay git-excluded, into main's history).

    Returns the number of worktrees removed.
    """
    from src.core.database import DatabaseManager as DbManager
    from src.core.database import Workflow
    from src.core.simple_config import get_config

    cfg = get_config()
    db = DbManager(str(cfg.paths.database_path))
    removed = 0
    try:
        with db.session_scope() as session:
            targets = [
                (wf.id, wf.working_directory, wf.launch_params)
                for wf in session.query(Workflow).filter(
                    Workflow.status == "completed",
                    Workflow.working_directory.isnot(None),
                )
                if wf.working_directory and ".worktrees/" in wf.working_directory
            ]

            # A workflow can reach "completed" while a straggler task from a
            # goto-triggered re-run of an earlier phase is still being worked
            # by a live agent in this same worktree (observed live: a
            # security_review task re-fired via goto stayed "in_progress"
            # with its agent still "working" while the workflow completed
            # through a different path). Force-removing the worktree out
            # from under that agent destroys its in-progress work and
            # leaves it permanently stuck with a deleted cwd -- skip those
            # and let a later sweep pass (once the straggler finishes) pick
            # them up instead.
            if targets:
                live_workflow_ids = {
                    wf_id
                    for (wf_id,) in session.query(Task.workflow_id)
                    .join(Agent, Agent.current_task_id == Task.id)
                    .filter(
                        Task.workflow_id.in_([t[0] for t in targets]),
                        Task.status == "in_progress",
                        Agent.status != "terminated",
                    )
                    .all()
                }
                for wf_id, _, _ in targets:
                    if wf_id in live_workflow_ids:
                        logger.warning(f"[SWEEP] Skipping worktree removal for completed workflow {wf_id[:8]} -- a live agent is still working an in-progress task under it")
                targets = [t for t in targets if t[0] not in live_workflow_ids]

        for wf_id, working_directory, launch_params in targets:
            worktree = Path(working_directory)
            if not (worktree / ".git").exists():
                continue  # already gone -- nothing to remove

            lp = launch_params if isinstance(launch_params, dict) else {}
            # Already repo-scoped (REQ-03, des-c7b9 recovery/cleanup threading):
            # launch_params["project_path"] was resolved via resolve_repo_path()
            # at workflow-launch time in pipeline.py's feature-workflow launch, so
            # it already targets the correct child repo in a multi-repo project.
            # No further repo_id threading needed here.
            project_path_str = lp.get("project_path")
            if not project_path_str:
                logger.warning(f"[SWEEP] Workflow {wf_id[:8]} has an orphaned worktree {worktree} but no launch_params.project_path to scope cleanup to -- skipping rather than guessing")
                continue

            try:
                branch = _git.Repo(worktree).active_branch.name
            except Exception:
                branch = ""

            logger.info(
                f"[SWEEP] Cleaning up orphaned worktree for completed "
                f"workflow {wf_id[:8]}: {worktree}"
            )
            _cleanup_worktree(worktree, branch, Path(project_path_str), logger, db_manager=db)
            removed += 1
    except Exception as e:
        logger.warning(f"[SWEEP] Failed to sweep completed-workflow worktrees: {e}")
    finally:
        # BLOCKER-3: Properly dispose of the DatabaseManager
        try:
            db.dispose()
        except Exception:
            pass
    return removed


# See _heal_orphaned_branches_for_project's own comment at its cap check.
_BRANCH_HEAL_MAX_CANDIDATES_PER_TICK = 20


def heal_orphaned_agent_branches(logger: "OrchestratorLogger") -> int:
    """Detect and heal agent-branch worktrees left behind by a stranded
    agent: real, committed work that never got merged because the task
    that owned it ended up "failed" instead of "done" -- e.g. its worktree
    was force-removed out from under it by a race in
    sweep_completed_workflow_worktrees (see that function's live-agent
    guard, added after this exact incident: a goto-triggered straggler
    task's worktree got deleted while its agent was still working, the
    agent's own uncommitted fixes had already landed in commits on its
    branch, and nothing ever merged that branch since the task it belonged
    to was never going to report "done" again).

    A branch matching the configured agent branch_prefix, with no live
    `git worktree` checkout, is unambiguously orphaned -- a live agent can
    only write to its branch through an active worktree checkout, so "no
    worktree has this branch checked out" already proves no agent is still
    using it, with no need to cross-reference Task/Agent DB state (which,
    for this per-agent-worktree path, doesn't reliably map branch name back
    to a specific task anyway -- a retried task can inherit an older
    agent's branch/worktree wholesale).

    Healing is deliberately conservative: only a clean fast-forward of the
    CURRENT base branch tip is merged. If nothing has base_branch checked
    out anywhere, that's a compare-and-swap `update-ref` (no working tree
    to desync). If base_branch IS checked out somewhere -- typically the
    project's own primary checkout -- a bare ref move would desync that
    checkout's index/working tree from its new HEAD (confirmed live: `git
    status` then shows every changed file as locally modified), so this
    does a real `git merge --ff-only` there instead, and only when that
    checkout is clean. Anything that isn't a clean fast-forward (base
    branch moved on since the orphaned branch diverged) is left alone and
    logged with "FAILED" so it surfaces via product_requirements.yaml's
    tech debt mode `grep -r "EXHAUSTED|STUCK|FAILED" ~/.hephaestus/logs/` step
    for manual review -- resolving real conflicts unattended is a materially
    different risk than fast-forwarding a branch nothing else was touching.

    Returns the number of branches auto-merged.
    """
    from src.core.database import AutopilotProject
    from src.core.database import DatabaseManager as DbManager
    from src.core.repo_resolution import get_project_repos

    cfg = get_config()
    db = DbManager(str(cfg.paths.database_path))
    healed = 0
    try:
        with db.session_scope() as session:
            project_dirs = set()
            for proj in session.query(AutopilotProject).all():
                repos = get_project_repos(session, proj.id)
                paths = [repo.path for repo in repos] if repos else [proj.base_dir]
                project_dirs.update(p for p in paths if p and Path(p).is_dir())

        for project_dir in project_dirs:
            try:
                healed += _heal_orphaned_branches_for_project(Path(project_dir), cfg, logger)
            except Exception as e:
                logger.warning(f"[BRANCH-HEAL] Failed to scan {project_dir}: {e}")
    except Exception as e:
        logger.warning(f"[BRANCH-HEAL] Failed to enumerate projects: {e}")
    return healed


def _heal_orphaned_branches_for_project(project_dir: Path, cfg, logger: "OrchestratorLogger") -> int:
    try:
        repo = _git.Repo(project_dir)
    except Exception as e:
        logger.debug(f"[BRANCH-HEAL] Could not open repo at {project_dir}: {e}")
        return 0

    base_branch = cfg.git.base_branch
    prefix = cfg.git.branch_prefix
    if base_branch not in repo.heads:
        return 0
    base_sha = repo.heads[base_branch].commit.hexsha

    # Map branch name -> worktree path for every branch currently checked
    # out anywhere (this always includes the project's own primary
    # checkout, listed by git as an ordinary worktree entry). Branches
    # still checked out are in active use -- never touch those as heal
    # candidates. base_branch itself being checked out somewhere (the
    # common case: a project's primary checkout normally sits on main)
    # instead changes HOW it gets healed, below.
    try:
        porcelain = repo.git.worktree("list", "--porcelain")
    except Exception:
        porcelain = ""
    checked_out_branches: dict = {}
    current_path = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.startswith("branch ") and current_path:
            branch_name = line.split(" ", 1)[1].removeprefix("refs/heads/")
            checked_out_branches[branch_name] = current_path

    healed = 0
    candidates_checked = 0
    for head in repo.heads:
        name = head.name
        if not name.startswith(prefix) or name == base_branch or name in checked_out_branches:
            continue

        # Cap subprocess-invoking work per tick: each candidate costs 1-2
        # real `git` subprocess spawns (fork+exec), and this self-hosting
        # repo alone has accumulated 100+ agent- branches over its
        # lifetime. Unbounded, a single sweep tick (the first one after
        # every restart, since _LAST_BRANCH_HEAL_TIME resets to None) could
        # burn through 200+ spawns back-to-back on this thread -- enough
        # sustained GIL/CPU contention to make the main event loop
        # intermittently unable to service even a zero-I/O /health check
        # for extended stretches. Confirmed live: exactly this pattern kept
        # a freshly-restarted backend flaky for minutes. Branches past the
        # cap are simply picked up on the NEXT tick (_BRANCH_HEAL_INTERVAL_
        # SECONDS already throttles how often this whole function runs at
        # all), not lost -- this only spreads the same total work out
        # instead of doing it all in one burst.
        if candidates_checked >= _BRANCH_HEAL_MAX_CANDIDATES_PER_TICK:
            logger.debug(
                f"[BRANCH-HEAL] {project_dir}: hit the {_BRANCH_HEAL_MAX_CANDIDATES_PER_TICK}-candidate "
                f"per-tick cap -- remaining branches will be checked on a later sweep"
            )
            break
        candidates_checked += 1

        try:
            ahead = repo.git.rev_list(f"{base_branch}..{name}", "--count").strip()
        except Exception:
            continue
        if ahead == "0":
            continue  # already fully merged, or never advanced past base

        try:
            repo.git.merge_base("--is-ancestor", base_branch, name)
            is_ff = True
        except _git.GitCommandError:
            is_ff = False

        branch_sha = head.commit.hexsha
        if not is_ff:
            logger.warning(
                f"[BRANCH-HEAL] FAILED to auto-heal orphaned branch {name} in "
                f"{project_dir} -- {ahead} commit(s) not on {base_branch}, but not "
                f"a clean fast-forward (base branch has diverged since). Needs "
                f"manual review: git diff {base_branch}...{name}"
            )
            continue

        base_worktree_path = checked_out_branches.get(base_branch)
        if base_worktree_path:
            # base_branch is actively checked out somewhere (typically the
            # project's own primary checkout) -- a bare `update-ref` would
            # move that checkout's HEAD commit forward without touching its
            # index/working tree, leaving `git status` showing every
            # changed file as locally modified (reverted to the pre-merge
            # content) until someone notices and resets. Confirmed by
            # direct reproduction. Do a real merge in that checkout
            # instead, so the ref and the working tree move together, and
            # only when it's clean -- a dirty checkout is left alone rather
            # than risking an unattended merge on top of someone's
            # in-progress edits.
            try:
                base_repo = _git.Repo(base_worktree_path)
                if base_repo.is_dirty(untracked_files=False):
                    logger.warning(
                        f"[BRANCH-HEAL] FAILED to heal {name} in {project_dir} -- "
                        f"{base_branch} is checked out at {base_worktree_path} with "
                        "uncommitted changes; skipping rather than risking an "
                        "unattended merge there. Needs manual review."
                    )
                    continue
                base_repo.git.merge(name, "--ff-only")
                logger.info(f"[BRANCH-HEAL] Fast-forwarded {base_branch} to {branch_sha[:8]} ({ahead} commit(s) from orphaned branch {name}, project {project_dir})")
                healed += 1
            except Exception as e:
                logger.warning(f"[BRANCH-HEAL] FAILED to heal {name} in {project_dir}: {e}")
        else:
            try:
                # Nobody has base_branch checked out anywhere, so no
                # working tree can be desynced -- a bare compare-and-swap
                # ref update is safe (and only advances if base_branch is
                # still exactly base_sha, so this can't clobber a commit
                # that landed on it between the read above and this write).
                repo.git.update_ref(f"refs/heads/{base_branch}", branch_sha, base_sha)
                logger.info(f"[BRANCH-HEAL] Fast-forwarded {base_branch} to {branch_sha[:8]} ({ahead} commit(s) from orphaned branch {name}, project {project_dir})")
                healed += 1
            except Exception as e:
                logger.warning(f"[BRANCH-HEAL] FAILED to heal {name} in {project_dir}: {e}")
    return healed


def _create_designs_folder(
    project_path: Path,
    design_entry: DesignEntry,
    logger: "OrchestratorLogger",
) -> Path:
    """Create permanent storage folder for design artifacts.

    Args:
        project_path: Path to the project root
        design_entry: Design entry being processed
        logger: Orchestrator logger

    Returns:
        Path to the designs folder
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = design_entry.name.lower().replace(" ", "_")[:40]
    designs_folder = project_path / CONTEXT_DIR_NAME / "specs" / f"{timestamp}_{safe_name}_{design_entry.db_id or 'unknown'}"
    designs_folder.mkdir(parents=True, exist_ok=True)
    (designs_folder / "features").mkdir(exist_ok=True)

    logger.info(f"Created designs folder: {designs_folder}")
    return designs_folder


def _recover_abandoned_workflows_missing_worktree(logger: "OrchestratorLogger") -> int:
    """Self-heal for a workflow that _escalate_stale_active_workflows marked
    "failed" as a false positive (its own message already hedges: "likely
    lost mid-flight across a backend restart") AND whose shared worktree is
    now gone (Workflow.working_directory is None -- e.g. from the exact
    worktree-deletion incident _remove_worktree's require_clean guard now
    prevents going forward, but which can still be true for a workflow
    already damaged before that fix landed).

    A workflow in this state has no automated path back to progress:
    _advance_phases's every case requires status in ("active", "paused"),
    so a "failed" workflow is invisible to all of them, forever, until a
    human clicks Resume in the UI. And simply flipping status back to
    "active" without also fixing working_directory would silently make
    things worse, not better: create_agent_for_task's shared-worktree
    resolution only hard-fails when working_directory is a *present-but-
    missing* path (by design, per its own comment -- no safe fallback for
    that case, since a disconnected fork would be unmergeable); when
    working_directory is None outright, that check is skipped entirely and
    agent creation silently falls through to forking a brand-new, isolated
    worktree with none of the prior phases' real commits -- the next agent
    would review/build against the wrong code entirely.

    Recovers correctly instead: rebuild the shared worktree from the
    feature's own branch (feature/<design_id[:8]>/<feature_key>, same name
    _run_one_feature always uses) via _create_integration_worktree -- the
    branch itself was never touched by any of this, so it still carries
    every phase's real commits. Reconnecting Workflow.working_directory to
    a fresh checkout of that branch, then resuming, lets the normal retry
    machinery (_maybe_retry_failed_tasks) safely take it from there.

    Capped via the stuck task's own retry_count (reusing the same
    MAX_RETRY_COUNT convention _maybe_retry_failed_tasks already enforces)
    so a workflow whose branch/worktree recreation keeps failing for a
    real reason eventually stops retrying and stays failed for a human,
    instead of looping forever.
    """
    from src.core.database import AutopilotDesign, AutopilotProject, Feature
    from src.core.repo_resolution import RepoNotFoundError, resolve_repo_path

    max_recovery_attempts = 2
    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "failed",
                Workflow.working_directory.is_(None),
                Workflow.status_reason.like("Abandoned: no agent/task activity%"),
            )
            .all()
        )
        for wf in candidates:
            feature = db.query(Feature).filter_by(workflow_id=wf.id).first()
            if not feature or not feature.design_id:
                continue
            design = db.query(AutopilotDesign).filter_by(id=feature.design_id).first()
            if not design or not design.project_id:
                continue
            project = db.query(AutopilotProject).filter_by(id=design.project_id).first()
            if not project or not project.base_dir:
                continue
            try:
                repo_path = resolve_repo_path(db, design.project_id, feature.repo_id)
            except (RepoNotFoundError, ValueError):
                repo_path = Path(project.base_dir)

            # Scoped to the CURRENTLY in_progress phase only -- a workflow
            # that's been through several goto cycles can carry old,
            # already-superseded "failed" tasks from phases that long since
            # completed on a later attempt (e.g. an early "development"
            # attempt that failed and hit its own retry cap, before a much
            # later retry succeeded and the pipeline moved on for real).
            # Those are harmless history, not evidence recovery is unsafe --
            # checking retry_count across every failed task ever recorded
            # for this workflow, instead of just the phase actually stuck
            # right now, refused to recover a workflow whose real blocker
            # (security_review, retry_count=0) had never been retried at
            # all, purely because an unrelated, ancient development-phase
            # task happened to already be at the cap.
            in_progress_phase_ids = {
                pid for (pid,) in db.query(PhaseExecution.phase_id).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress").all()
            }
            stuck_tasks = (
                db.query(Task)
                .filter(
                    Task.workflow_id == wf.id,
                    Task.status == "failed",
                    Task.phase_id.in_(in_progress_phase_ids),
                )
                .all()
                if in_progress_phase_ids
                else []
            )
            if not stuck_tasks:
                continue
            if any((t.retry_count or 0) >= max_recovery_attempts for t in stuck_tasks):
                continue

            branch = f"feature/{feature.design_id[:8]}/{feature.feature_key}"
            wt_path = _create_integration_worktree(repo_path, feature.design_id, branch, logger, db_manager=db)
            if not wt_path:
                logger.warning(f"[WORKFLOW-RECOVERY] Could not rebuild worktree for workflow {wf.id[:8]} (branch {branch}) -- leaving failed")
                continue

            logger.warning(
                f"[WORKFLOW-RECOVERY] Rebuilt worktree for workflow {wf.id[:8]} "
                f"from branch {branch} at {wt_path} -- resuming; the stuck "
                'task(s) are left exactly as they are (still "failed", own '
                "retry_count untouched) so _maybe_retry_failed_tasks' own "
                "already-tested retry-and-dispatch path picks them up on "
                "the very next active-workflow sweep pass, instead of this "
                "function reimplementing that dispatch itself."
            )
            wf.working_directory = str(wt_path)
            wf.status = "active"
            wf.status_reason = None
            # Sync feature status -- same class of bug as
            # _retry_exhausted_paused_workflows: this function bypasses
            # resume_workflow(), so the feature row stays "paused".
            for feat in db.query(Feature).filter_by(workflow_id=wf.id, status="paused").all():
                feat.status = "active"
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _recover_abandoned_workflows_with_completed_phase(logger: "OrchestratorLogger") -> int:
    """Self-heal for a workflow _escalate_stale_active_workflows marked
    "failed" (same abandonment message as
    _recover_abandoned_workflows_missing_worktree), but whose worktree is
    still intact and whose current in-progress phase's task(s) already
    finished ("done", none pending/assigned/in_progress) -- i.e. the phase's
    real work completed, but nothing then evaluated it or created the next
    phase's task. _escalate_stale_active_workflows's own docstring names the
    likely cause: a backend restart landing in the narrow window between a
    task's "done" commit and the synchronous spec-gate evaluation
    (fire_spec_gate_if_ready) that normally follows it in the same request.

    Distinct from _recover_abandoned_workflows_missing_worktree, which
    handles a FAILED task with a lost worktree (retry machinery re-dispatches
    it). This case has no failed task to retry -- the work already
    succeeded -- so recovery is just: make the workflow visible to
    _advance_phases again (status back to "active", clear status_reason) and
    let its own existing "phase complete -> fire transition" path
    (_case_in_progress_complete) re-evaluate the already-done work on the
    very next sweep, instead of this function re-implementing that
    evaluation itself. If the phase's declared output is genuinely missing
    (e.g. the agent's JSON never made it into the worktree), that path's
    normal result_missing handling sends it to development with the
    available report text as context, same as any other run -- this
    function only unblocks the workflow, it doesn't grade the work.
    """

    recovered = 0
    with get_db() as db:
        candidates = (
            db.query(Workflow)
            .filter(
                Workflow.status == "failed",
                Workflow.working_directory.isnot(None),
                Workflow.status_reason.like("Abandoned: no agent/task activity%"),
            )
            .all()
        )
        for wf in candidates:
            in_progress_phase_ids = {
                pid for (pid,) in db.query(PhaseExecution.phase_id).join(Phase, PhaseExecution.phase_id == Phase.id).filter(Phase.workflow_id == wf.id, PhaseExecution.status == "in_progress").all()
            }
            if not in_progress_phase_ids:
                continue  # nothing in_progress -- not this function's case

            unfinished = (
                db.query(Task)
                .filter(
                    Task.phase_id.in_(in_progress_phase_ids),
                    Task.status.in_(["pending", "queued", "blocked", "assigned", "in_progress"]),
                )
                .count()
            )
            if unfinished > 0:
                continue  # something genuinely still active -- leave it alone

            has_done = db.query(Task).filter(Task.phase_id.in_(in_progress_phase_ids), Task.status == "done").count()
            if not has_done:
                continue  # nothing completed yet either -- not evaluable

            logger.warning(
                f"[WORKFLOW-RECOVERY] Workflow {wf.id[:8]} was marked failed "
                "(abandoned) but its worktree is intact and its current "
                "phase's task(s) already finished -- resuming so the next "
                "sweep can evaluate and advance it"
            )
            wf.status = "active"
            wf.status_reason = None
            # Sync feature status -- same reasoning as
            # _recover_abandoned_workflows_missing_worktree.
            for feat in db.query(Feature).filter_by(workflow_id=wf.id, status="paused").all():
                feat.status = "active"
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _ensure_git_excluded(repo_path: Path, patterns: Dict[str, str], logger: Any) -> None:
    """logger: "OrchestratorLogger" or the plain module-level logging.Logger --
    called from both. Only uses .warning(), which both support.

    Add `patterns` (path -> one-line comment explaining it) to this
    repo's local, untracked .git/info/exclude, idempotently.

    Not the project's own tracked .gitignore: these are all Hephaestus
    tooling artifacts (worktrees, orchestration state, the ash scanner's
    working directory) -- not something the project itself produces, so
    they have no business in a file the project's real contributors
    maintain. info/exclude is the correct, local-only, per-checkout place
    for exactly this category of thing.

    `repo_path` may be a worktree, not just a repo root -- worktrees don't
    have their own info/exclude, it lives in the shared ("common") git
    dir, so `git rev-parse --git-common-dir` (resolves correctly from a
    worktree, unlike a hardcoded ".git/") is required rather than assuming
    `repo_path / ".git" / "info" / "exclude"`.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            return
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repo_path / common_dir
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        existing_lines = {line.strip() for line in existing.splitlines()}
        to_add = {p: c for p, c in patterns.items() if p not in existing_lines}
        if not to_add:
            return
        with exclude_path.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for pattern, comment in to_add.items():
                f.write(f"# {comment} Added automatically by Hephaestus.\n{pattern}\n")
    except Exception as e:
        logger.warning(f"Could not update git exclude at {repo_path}: {e}")


def _run_ash_scan(worktree: Path, logger: "OrchestratorLogger") -> None:
    """Run the AWS Automated Security Helper against a feature's worktree.

    security_review.yaml marks this scan MANDATORY, but relying on the agent
    to remember to run it is unreliable — observed live during smoke testing:
    an agent skipped both the mandatory feature-classification step and this
    scan entirely, with no note of it being skipped (which the prompt also
    explicitly asked for on failure). Running it here, unconditionally,
    before the agent starts, removes the compliance gap the same way
    Enhancement 1 (run_independent_test_verification in spec.py) stopped
    trusting agent-reported QA metrics — the orchestrator now guarantees the
    scan happened at all, regardless of what the agent does with the results.
    """
    results_path = worktree / CONTEXT_DIR_NAME / "ash_results.txt"
    _ensure_git_excluded(
        worktree,
        {".ash/": ("AWS Automated Security Helper's own scan working directory (security_review's mandatory ash scan) --")},
        logger,
    )
    try:
        heph_repo = Path(__file__).resolve().parents[3]  # one .parent deeper: now a package module
        ash_script = heph_repo / "scripts" / "ash"
        if not ash_script.exists():
            logger.warning(f"[ASH] scripts/ash not found at {ash_script}, skipping scan")
            # Still write the marker. security_review.yaml tells the agent to
            # cat this file and quote it verbatim if it reports a failure, and
            # verify_output_artifact rejects a security.md with no "Automated
            # Scan Results" section -- so returning silently here leaves the
            # agent catting a nonexistent file with no sanctioned way to
            # report why, and its report gets rejected for a missing section
            # it had no way to fill. Every other failure path below already
            # writes this marker; this one was the exception.
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(f"SCAN FAILED TO RUN: ash not installed at {ash_script}")
            return

        results_path.parent.mkdir(parents=True, exist_ok=True)
        # --changed-files-only limits the scan to files changed vs. --base-ref
        # instead of the whole worktree on every run -- ash itself falls back
        # to a full scan when git is unavailable, so this is safe even
        # outside a normal feature branch. --base-ref is set explicitly to
        # the project's configured base branch (bare, e.g. "main") rather
        # than trusting ash's own "origin/main" default: a worktree always
        # has its local base branch available (it's what it was created
        # from), but not necessarily a fetched, up-to-date origin/main --
        # plenty of projects this tool runs against have no remote at all.
        base_branch = get_config().git.base_branch
        result = subprocess.run(
            [str(ash_script), "--source-dir", ".", "--changed-files-only", "--base-ref", base_branch],
            cwd=str(worktree),
            capture_output=True,
            timeout=300,
            text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        results_path.write_text(output or "(no output)")
        logger.info(f"[ASH] Automated security scan complete (exit code {result.returncode}), results written to {results_path}")
    except subprocess.TimeoutExpired:
        logger.warning("[ASH] Automated security scan timed out after 300s")
        results_path.write_text("SCAN TIMED OUT after 300s")
    except Exception as e:
        logger.warning(f"[ASH] Automated security scan failed: {e}")
        try:
            results_path.write_text(f"SCAN FAILED TO RUN: {e}")
        except Exception:
            pass
    finally:
        # The ash CLI leaves its own raw working directory (.ash/ --
        # per-scanner output, converted files, and an aggregated SARIF
        # results JSON) behind in the worktree root -- observed live at
        # 76MB, with the aggregated JSON alone at 19MB. Two real problems
        # if it's left there: commit_and_link_ticket's `git add -A` after
        # every task completion would commit all of it into the feature
        # branch, and a security_review agent digging past the small
        # summary above into that raw JSON (a natural thing to do when
        # looking for more detail) has been observed crashing its own CLI
        # session trying to parse it inline, over and over on every
        # relaunch. The summary above already has everything the agent
        # needs -- delete the rest regardless of scan outcome.
        try:
            shutil.rmtree(worktree / ".ash", ignore_errors=True)
        except Exception:
            pass


# Registry of per-phase "real work that must finish before the agent gets
# its first prompt" steps -- keyed by phase name, mapping to (callable,
# grace_seconds). Consulted by launch_pipeline.py's create_agent_for_task:
# once the Agent/tmux session and Task row already exist (so the phase
# shows real, live activity instead of nothing), it runs the registered
# callable -- same (worktree: Path, logger) signature as _run_ash_scan --
# BEFORE sending the actual initial prompt, and stamps
# Task.dispatch_grace_until = now + grace_seconds so every stuck/orphan/
# idle detector keyed off Task.created_at or Agent.launched_at (see
# Task.dispatch_grace_until's own docstring in database.py) knows not to
# flag this task/agent while the step is still legitimately running.
# grace_seconds should comfortably exceed the callable's own worst-case
# duration (_run_ash_scan's own subprocess.run has timeout=300) to leave
# margin for worktree/dispatch overhead around it, not just the
# subprocess itself.
PRE_DISPATCH_BLOCKING_STEPS: Dict[str, Tuple[Callable[[Path, "OrchestratorLogger"], None], int]] = {
    "security_review": (_run_ash_scan, 360),
}

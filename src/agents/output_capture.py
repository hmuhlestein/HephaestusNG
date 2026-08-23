"""Agent output capture — reading tmux transcripts and stable-clean logs.

Extracted from AgentManager, which mixed this concern in with tmux session
lifecycle, prompt construction, DB persistence, and messaging — see
design_docs/manager_py_decomposition_prompt.md.  AgentManager still exposes
get_agent_output (many callers depend on that public API) but delegates to
an AgentOutputCapture instance instead of implementing the transcript
plumbing itself.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME
from src.core.database import Agent, AgentLog

logger = logging.getLogger(__name__)


class AgentOutputCapture:
    """Reads and filters tmux output for agents — transcripts, clean logs,
    and live capture-pane snapshots.

    Constructor args are intentionally minimal: only the state this
    collaborator actually reads (db_manager, tmux_server).
    """

    _STABILITY_CONFIRMATIONS = 3

    def __init__(self, db_manager, tmux_server):
        self.db_manager = db_manager
        self.tmux_server = tmux_server
        self._transcript_filter_cache: Dict[str, Any] = {}
        self._pane_stability_cache: Dict[str, Dict[str, Any]] = {}

    def get_agent_output(self, agent_id: str, lines: int = 200) -> str:
        """Get recent output from agent's tmux session or stored output for terminated agents.

        Args:
            agent_id: Agent ID
            lines: Number of lines to retrieve

        Returns:
            Recent output text
        """
        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return ""

            # Orchestrator agents always read from their log file regardless of status.
            if agent.agent_type == "orchestrator":
                return self._get_orchestrator_output(agent, lines)

            # The stability-tracked "clean" transcript (tmux's own capture-
            # pane rendering -- cursor positioning, overwrites, and line
            # wrapping already correctly resolved) is authoritative
            # whether the agent is still running or has terminated:
            # terminate_agent() does a final unconditional flush of it
            # right before killing the session, specifically so it stays
            # correct once capture-pane can no longer see anything. Check
            # it first regardless of status -- a terminated agent used to
            # skip straight to the raw pipe-pane transcript below, which
            # re-shows every intermediate \r/cursor-redrawn TUI frame as
            # its own line and can't always be reconstructed by regex
            # stripping alone (text painted via cursor movement rather
            # than literal spaces comes out concatenated). See
            # _read_transcript_log.
            if agent.tmux_session_name:
                transcript_dir = self._resolve_tmux_transcript_dir(agent)
                if transcript_dir:
                    clean_path = transcript_dir / f"{agent.tmux_session_name}.clean.log"
                    if agent.status != "terminated":
                        self._poll_stable_transcript(agent.tmux_session_name, clean_path)
                    if clean_path.exists() and clean_path.stat().st_size > 0:
                        with open(clean_path, "r", errors="replace") as f:
                            clean_lines = f.read().splitlines()
                        if lines > 0:
                            clean_lines = clean_lines[-lines:]
                        return "\n".join(clean_lines)

                # No clean transcript yet (e.g. still within the first
                # couple of confirmation polls, or mid-stream on a long
                # response that hasn't settled anywhere yet). A live
                # capture-pane snapshot is tmux's own correctly-emulated
                # rendering too -- just not yet "confirmed stable" enough
                # to persist. Returns None once the session itself is
                # gone, which is always true for a terminated agent, so
                # this is a no-op there.
                current_lines = self._capture_pane_lines(agent.tmux_session_name)
                if current_lines is not None:
                    if lines > 0:
                        current_lines = current_lines[-lines:]
                    return "\n".join(current_lines)

            if agent.status == "terminated":
                logger.debug(
                    f"Agent {agent_id} is terminated with no clean transcript, "
                    "falling back to termination-time capture"
                )

                # capture-pane snapshot taken by terminate_agent() right
                # before the session was killed -- also proper tmux
                # rendering, not raw pty bytes.
                termination_log = (
                    session.query(AgentLog)
                    .filter_by(agent_id=agent_id, log_type="terminated")
                    .order_by(AgentLog.timestamp.desc())
                    .first()
                )

                if termination_log and termination_log.details:
                    final_output = termination_log.details.get("final_output", "")
                    if final_output:
                        logger.debug(
                            f"Retrieved stored output for terminated agent {agent_id}"
                        )
                        # If lines parameter is specified, return only the last N lines
                        if lines and lines > 0:
                            output_lines = final_output.split("\n")
                            return "\n".join(output_lines[-lines:])
                        return final_output

                # Last resort: the raw pipe-pane transcript (has ANSI
                # colors), only reached if every tmux-emulated source
                # above came up empty.
                transcript_output = self._read_transcript_log(agent, lines)
                if transcript_output:
                    return transcript_output

                logger.warning(
                    f"No stored output found for terminated agent {agent_id}"
                )
                # Try to get last logs from agent_logs
                recent_logs = (
                    session.query(AgentLog)
                    .filter_by(agent_id=agent_id)
                    .order_by(AgentLog.timestamp.desc())
                    .limit(100)
                    .all()
                )
                if recent_logs:
                    log_lines = []
                    for log in reversed(recent_logs):
                        msg = log.message or ""
                        if log.details:
                            summary = log.details.get("summary", "") or log.details.get(
                                "trajectory_summary", ""
                            )
                            if summary:
                                msg = f"{msg}: {summary[:200]}"
                        if msg:
                            log_lines.append(f"[{log.log_type}] {msg}")
                    return "\n".join(log_lines)
                return "Agent terminated - no output was captured"

            if not agent.tmux_session_name:
                logger.warning(f"Agent {agent_id} has no tmux session name")
                return ""

            # Live agent with a session name, but capture-pane came up
            # empty (e.g. a transient health-check race) -- last resort is
            # the raw pipe-pane transcript.
            logger.debug(
                f"capture-pane unavailable for {agent.tmux_session_name}, "
                "falling back to raw transcript"
            )
            return self._read_transcript_log(agent, lines)

        except Exception as e:
            logger.error(f"Failed to get agent output for {agent_id}: {e}")
            return ""
        finally:
            session.close()

    def _resolve_tmux_transcript_dir(self, agent) -> Optional[Path]:
        """Find the .hephaestus/tmux/ directory this agent's transcript
        files (raw pipe-pane .transcript.log, and the stability-tracked
        .clean.log) live in. Shared by _read_transcript_log and
        _poll_stable_transcript so both agree on the same directory.
        """
        working_dir = None
        project_base = None
        from src.core.database import Task

        session = self.db_manager.get_session()
        try:
            # Try current task first, then most recent task. Both
            # agent.current_task_id and task.assigned_agent_id are cleared
            # on termination (see database.py's Agent.current_task_id
            # comment) -- a terminated agent's only remaining link back to
            # its task is the AgentLog "created" record's details["task_id"],
            # which termination never touches. Without this fallback,
            # every terminated agent's transcript dir was unresolvable and
            # get_agent_output fell through to near-empty output.
            task = None
            if agent.current_task_id:
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
            if not task:
                task = session.query(Task).filter_by(assigned_agent_id=agent.id).order_by(Task.created_at.desc()).first()
            if not task:
                created_log = (
                    session.query(AgentLog)
                    .filter_by(agent_id=agent.id, log_type="created")
                    .order_by(AgentLog.timestamp.desc())
                    .first()
                )
                task_id = created_log.details.get("task_id") if created_log and created_log.details else None
                if task_id:
                    task = session.query(Task).filter_by(id=task_id).first()
            if task and task.workflow:
                if task.workflow.working_directory:
                    working_dir = task.workflow.working_directory
                # Get project base_dir from workflow's design
                if task.workflow.project_id:
                    from src.core.database import AutopilotProject
                    proj = session.query(AutopilotProject).get(task.workflow.project_id)
                    if proj:
                        project_base = proj.base_dir
        finally:
            session.close()

        if working_dir:
            return Path(working_dir) / CONTEXT_DIR_NAME / "tmux"

        # Search in common locations using the session name. Existence of
        # the raw transcript.log (always created unconditionally at
        # session creation) marks the right directory even before a
        # .clean.log has ever been written there.
        search_bases = []
        if project_base:
            search_bases.append(Path(project_base))
        search_bases.append(Path.home() / ".hephaestus")

        for base in search_bases:
            candidate_dir = base / CONTEXT_DIR_NAME / "tmux"
            if (candidate_dir / f"{agent.tmux_session_name}.transcript.log").exists():
                return candidate_dir
            # Also check one level into .worktrees (worktree-local .hephaestus)
            if base.name == ".hephaestus":
                continue  # skip worktree scan for .hephaestus itself
            for wt_dir in base.glob(".worktrees/*"):
                candidate_dir = wt_dir / CONTEXT_DIR_NAME / "tmux"
                if (candidate_dir / f"{agent.tmux_session_name}.transcript.log").exists():
                    return candidate_dir

        return None

    def _read_transcript_log(self, agent, lines: int) -> str:
        """Read output from the pipe-pane transcript log file.

        Returns the last `lines` lines from the transcript, or empty string
        if no transcript is available.
        """
        import re
        try:
            transcript_dir = self._resolve_tmux_transcript_dir(agent)
            if not transcript_dir:
                return ""
            transcript_path = transcript_dir / f"{agent.tmux_session_name}.transcript.log"
            if not transcript_path.exists():
                return ""

            file_stat = transcript_path.stat()
            if file_stat.st_size == 0:
                return ""

            # Filtering the whole file (ANSI strip, redraw/dedup passes,
            # spacing) is real work -- up to ~4s for a large (~30MB), long-
            # running agent's transcript. A live agent gets polled roughly
            # every second, and pipe-pane only ever APPENDS, so between
            # polls with no new output the file's (mtime, size) are
            # unchanged and the previous filtered result is still exactly
            # correct -- do the expensive pass once, then reuse it until
            # the file actually grows, instead of redoing it from scratch
            # on every single poll regardless of whether anything changed.
            if not hasattr(self, "_transcript_filter_cache"):
                self._transcript_filter_cache = {}
            cache_key = str(transcript_path)
            cache_stamp = (file_stat.st_mtime, file_stat.st_size)
            cached = self._transcript_filter_cache.get(cache_key)
            if cached and cached[0] == cache_stamp:
                out_lines = cached[1]
                if lines > 0 and agent.status != 'terminated':
                    out_lines = out_lines[-lines:]
                return '\n'.join(out_lines).rstrip()

            # Always read and filter the WHOLE file, then tail AFTER
            # filtering (see below) -- not before. The dedup passes below
            # (repeated-block collapse, progressive-redraw collapse) need
            # both halves of a pattern within their working set to detect
            # and collapse it; tailing the RAW transcript first could cut
            # a pattern's first half out of the window, leaving the second
            # half unfiltered. Observed live: a live agent's output showed
            # visible duplication/redraw artifacts that were absent once
            # the same agent terminated and the whole file got processed.
            with open(transcript_path, 'r', errors='replace') as f:
                all_lines = f.readlines()
                # Drop trailing empty lines (partial writes from pipe-pane)
                while all_lines and not all_lines[-1].strip():
                    all_lines.pop()
                text = "".join(all_lines).rstrip()

            # Strip terminal control sequences that pipe-pane might have missed
            # Keep: SGR color sequences (\x1b[...m)
            # Strip: everything else aggressively
            text = re.sub(r'\x1b\][^\x07]*\x07', '', text)  # OSC with BEL
            text = re.sub(r'\x1b\][^\x1b]*\x1b\\', '', text)  # OSC with ST (single backslash)
            text = re.sub(r'\x1b\[[?]?[0-9;]*[^0-9;m]', '', text)  # All CSI/DEC except m
            text = re.sub(r'\x1b[()][A-Za-z0-9]', '', text)  # Charset selection
            text = re.sub(r'\x1b[^\x1b\x5b\x5d]', '', text)  # Any other bare ESC

            # Collapse carriage-return redraws: TUI spinners redraw the same
            # line using \r. Split on \n first, then for each line, if it
            # contains \r, keep only the last segment (final state).
            collapsed = []
            for line in text.split("\n"):
                if "\r" in line:
                    line = line.rsplit("\r", 1)[-1]
                collapsed.append(line.rstrip())
            text = "\n".join(collapsed)
            # Horizontal separator lines pi draws between message blocks
            # (each char individually SGR-wrapped). Same pattern as the
            # frontend's RealTimeAgentOutput filter. Filtering these is
            # load-bearing for the redraw dedup below: separators sit
            # BETWEEN progressive redraw frames, so leaving them in breaks
            # the frames' adjacency and defeats the prefix comparison.
            separator_re = re.compile(r'^[─━═▬▪▫\-=\s]{20,}$')
            orphan_ansi_re = re.compile(r';\d+(?:;\d+)*m')
            # pi pads every row to full pane width with background-colored
            # spaces, and the trailing SGR resets sit AFTER the padding --
            # so a plain rstrip never removes it. In wrapping views
            # (whitespace-pre-wrap) the padding wraps into what looks like
            # a blank line after every row. Strip the padding but keep the
            # codes (a dropped reset would bleed color into the next line).
            #
            # NOT a single regex: `(?:\x1b\[[?]?[0-9;]*m|[ \t])+$` looks
            # simple but its alternation is ambiguous about how to
            # partition a long trailing run between the two branches,
            # which is classic catastrophic-backtracking bait. Measured
            # live: a single ~106KB merged line (a pipe-pane artifact --
            # the pty went a long stretch without a newline) took 257ms
            # for THIS ONE regex call alone; across one real 3.1MB
            # transcript (10.7K lines) it was 3.8 of a 4.0s total read,
            # on every poll of a live agent. A plain backward scan (no
            # backtracking possible) does the identical strip in <10ms.
            sgr_at_end_re = re.compile(r'\x1b\[[?]?[0-9;]*m$')

            def _strip_trailing_pad(line: str) -> str:
                end = len(line)
                codes = []
                while end > 0:
                    if line[end - 1] in ' \t':
                        end -= 1
                        continue
                    m = sgr_at_end_re.search(line[:end])
                    if m:
                        codes.append(m.group(0))
                        end = m.start()
                        continue
                    break
                if end == len(line):
                    return line
                return line[:end] + ''.join(reversed(codes))

            # Classify every line first; separators need a look-ahead over
            # later lines' kinds, so this can't be a single filter pass.
            # Status-bar chrome, anchored so it cannot swallow real output:
            #   "↑62k ↓4.6k R629k CH88.3% $0.033 6.1%/1.0M (auto)"  counters
            #   "MCP: 1/1 servers"                                   server line
            #   "⠧ Working..."                                       braille spinner
            #   "~/code/x/.worktrees/wt_y (feature/branch)"          shell prompt
            # The prompt pattern requires BOTH a leading ~/ or / and a
            # trailing parenthesised branch, so prose mentioning a path
            # does not match.
            chrome_re = re.compile(
                r'^(?:'
                r'[\u2191\u2193]\s*\d'
                r'|MCP:\s'
                r'|[\u2800-\u28ff]\s'
                r'|[~/][^\s]*\s+\([^)]+\)$'
                r')'
            )
            classified = []  # (kind, line, clean)
            for line in text.split('\n'):
                line = _strip_trailing_pad(line)
                stripped = line.strip()
                clean = re.sub(r'\x1b\[[?]?[0-9;]*[a-zA-Z]', '', stripped)
                clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean).strip()
                clean = orphan_ansi_re.sub('', clean).strip()
                if not clean:
                    kind = 'blank'
                elif separator_re.match(clean):
                    kind = 'sep'
                elif chrome_re.match(clean):
                    # pi's bottom status bar, re-rendered on every frame.
                    # Frame dedup collapses consecutive repeats, but chrome
                    # that brackets real content survives it -- those frames
                    # are no longer adjacent -- so it must be classified out
                    # here or it reaches the caller as content.
                    kind = 'chrome'
                else:
                    kind = 'content'
                classified.append((kind, line, clean))

            # Separators become a single blank line ONLY at real block
            # boundaries -- i.e. when the next non-blank/non-separator
            # line is actual content. While streaming, pi re-renders its
            # bottom status-bar chrome (separator pair + shell prompt +
            # stats + MCP line) on EVERY frame; those separators are part
            # of the chrome, and converting them to blanks puts a blank
            # between every pair of content lines.
            filtered_lines = []
            clean_lines = []
            for idx, (kind, line, clean) in enumerate(classified):
                if kind == 'content':
                    filtered_lines.append(line)
                    clean_lines.append(clean)
                elif kind == 'sep':
                    j = idx + 1
                    while (
                        j < len(classified)
                        and j - idx <= 12
                        and classified[j][0] in ('blank', 'sep')
                    ):
                        j += 1
                    if j < len(classified) and classified[j][0] == 'content':
                        filtered_lines.append("")
                        clean_lines.append("")

            # Deduplicate progressive redraws: pi re-renders a line as its
            # arguments stream in ($ cd /, $ cd /Users, ... or
            # `read /:200-299`, `read /Users/hmuh:200-299`, ...). A frame
            # is dropped when the next non-blank line supersedes it:
            #   - extends it (plain prefix), or
            #   - fills in its `...` elision, or
            #   - inserts text mid-line (one contiguous gap; the frames
            #     keep a constant tail like `:200-299` while the path
            #     grows). Gap matches are only accepted as part of a CHAIN
            #     of matching pairs -- an isolated gap match can be two
            #     legitimately similar lines (read src/__init__.py ->
            #     read src/sub/__init__.py) that must not be collapsed.
            # Paths are ~-expanded before comparing: pi switches a growing
            # path from absolute to ~-abbreviated form mid-stream.
            import os as _os

            _home = _os.path.expanduser("~")

            def _norm(s):
                return re.sub(r"(^|\s)~(?=/)", lambda m: m.group(1) + _home, s)

            def _frame_kind(cur, nxt):
                cur = cur.replace("…", "...")
                cur_n, nxt_n = _norm(cur), _norm(nxt)
                if nxt_n.startswith(cur_n):
                    return "prefix"
                if "..." in cur:
                    head, tail = cur.split("...", 1)
                    if nxt_n.startswith(_norm(head)) and nxt_n.endswith(tail):
                        return "elide"
                if len(nxt_n) > len(cur_n):
                    p = 0
                    while p < len(cur_n) and cur_n[p] == nxt_n[p]:
                        p += 1
                    s = 0
                    while s < len(cur_n) - p and cur_n[-1 - s] == nxt_n[-1 - s]:
                        s += 1
                    if p + s >= len(cur_n):
                        return "gap"
                return None

            nonblank = [k for k, c in enumerate(clean_lines) if c]
            kinds = [
                _frame_kind(clean_lines[a], clean_lines[b])
                for a, b in zip(nonblank, nonblank[1:])
            ]
            drop = set()
            for idx, k in enumerate(nonblank[:-1] if nonblank else []):
                kind = kinds[idx]
                if kind in ("prefix", "elide"):
                    drop.add(k)
                elif kind == "gap":
                    prev_kind = kinds[idx - 1] if idx > 0 else None
                    next_kind = kinds[idx + 1] if idx + 1 < len(kinds) else None
                    if prev_kind or next_kind:
                        drop.add(k)

            deduped = [ln for k, ln in enumerate(filtered_lines) if k not in drop]
            deduped_clean = [c for k, c in enumerate(clean_lines) if k not in drop]

            # Deduplicate whole-block redraws: pi re-emits an entire past
            # block (e.g. an mcp call + its JSON body) in dim gray once its
            # result arrives. Drop the FIRST copy of any immediately-repeated
            # run of lines, keeping the re-render (it carries the final
            # styling and is followed by the result). Single-line repeats
            # only count for non-blank lines -- blank collapsing is handled
            # separately below.
            final_lines = []
            final_clean = []
            n = len(deduped)
            i = 0
            while i < n:
                repeat_size = 0
                for size in range(min(40, (n - i) // 2), 0, -1):
                    if size == 1 and not deduped_clean[i]:
                        continue
                    if deduped_clean[i:i + size] == deduped_clean[i + size:i + 2 * size]:
                        repeat_size = size
                        break
                if repeat_size:
                    i += repeat_size
                else:
                    final_lines.append(deduped[i])
                    final_clean.append(deduped_clean[i])
                    i += 1

            # Spacing: pi's stream carries no usable paragraph structure --
            # every separator in a live stream belongs to the per-frame
            # status-bar chrome (measured 217/217 in a real transcript),
            # so block spacing is derived from content instead: each
            # tool-invocation line starts a new visual block and gets one
            # blank line above it. Runs of blanks never survive.
            block_start_re = re.compile(
                r'^(?:\$ |(?:read|write|edit|bash|grep|find|ls|mcp|subagent)\b)'
            )
            out_lines = []
            prev_blank = True  # also drops leading blanks
            for line, cl in zip(final_lines, final_clean):
                blank = not cl
                if blank and prev_blank:
                    continue
                if not blank and not prev_blank and block_start_re.match(cl):
                    out_lines.append("")
                out_lines.append(line)
                prev_blank = blank

            # Cache the fully-filtered result (before tailing) keyed on
            # this exact (mtime, size) -- see the cache-read at the top of
            # this method. A terminated agent's file never changes again,
            # so this also means a terminated agent's full history is only
            # ever ANSI-stripped/deduped once, not on every future request
            # for it either.
            self._transcript_filter_cache[cache_key] = (cache_stamp, out_lines)

            # Tail AFTER filtering, not before (see the read-loop comment
            # above) -- terminated agents still get full history.
            if lines > 0 and agent.status != 'terminated':
                out_lines = out_lines[-lines:]

            text = '\n'.join(out_lines).rstrip()

            return text

        except Exception as e:
            logger.debug(f"Could not read transcript log: {e}")
            return ""

    def _find_tmux_session(self, session_name: str):
        """Look up a live libtmux.Session by name, or None."""
        if not self.tmux_server.has_session(session_name):
            return None
        for tmux_sess in self.tmux_server.sessions:
            if tmux_sess.name == session_name:
                return tmux_sess
        return None

    def _capture_pane_lines(self, session_name: str) -> Optional[List[str]]:
        """capture-pane the full available scrollback (bounded by the
        session's own history-limit -- see launch_pipeline.py's
        session.set_option("history-limit", ...) call for the configured
        value) as a list of lines. Unlike raw pipe-pane bytes, this is
        tmux's OWN terminal emulation output --
        cursor positioning, overwrites, and line wrapping are already
        correctly resolved into flat text. Returns None if the session is
        gone."""
        tmux_session = self._find_tmux_session(session_name)
        if not tmux_session:
            return None
        try:
            pane = tmux_session.attached_window.attached_pane
            output = pane.cmd("capture-pane", "-p", "-S", "-").stdout
            return list(output) if output else []
        except Exception as e:
            logger.debug(f"[STABLE-TRANSCRIPT] capture-pane failed for {session_name}: {e}")
            return None

    @staticmethod
    def _append_lines(path: Path, new_lines: List[str]) -> None:
        if not new_lines:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write("\n".join(new_lines) + "\n")
        except Exception as e:
            logger.warning(f"[STABLE-TRANSCRIPT] Failed to append to {path}: {e}")

    def _poll_stable_transcript(self, session_name: str, clean_path: Path) -> None:
        """Append whatever's newly stable since the last poll to
        clean_path, using tmux's own capture-pane instead of raw pty
        bytes. "Stable" = identical content at the same position across
        _STABILITY_CONFIRMATIONS consecutive polls of this method -- a
        line still being actively redrawn (a spinner with a live
        elapsed-time counter, a long response streaming in) simply never
        stabilizes until the underlying operation finishes, so it's
        correctly withheld rather than shown as corrupted partial-
        overwrite text.

        Why not just re-strip raw pipe-pane bytes harder: cursor-
        positioning escapes (jump back, overwrite part of a line) can't
        be turned into correct text by stripping the escape code alone --
        that only removes the INSTRUCTION, not its effect. Reconstructing
        the effect requires an actual terminal emulator (2D character
        grid + cursor state), which is exactly what tmux's own
        capture-pane already does for us; we just have to poll it instead
        of re-deriving it from bytes ourselves.

        capture-pane -S - returns the FULL currently-remembered history
        (bounded by the session's configured history-limit -- see
        launch_pipeline.py) every call, always from the same starting
        point -- so comparing this poll's lines to earlier polls' lines
        position-for-position is valid without a scrolling-alignment
        problem, as long as total output hasn't exceeded history-limit
        between polls. This method only runs while the frontend viewer is
        open and actively requesting output (get_agent_output triggers
        it), so "between polls" isn't bounded by a fixed interval -- an
        agent nobody has looked at yet can accumulate output for its
        entire runtime before the first poll ever happens. A generous
        history-limit reduces how often that first poll exceeds it; it
        can't eliminate the possibility for an agent that runs long
        enough unwatched.
        """
        current_lines = self._capture_pane_lines(session_name)
        if current_lines is None:
            return

        if not hasattr(self, "_pane_stability_cache"):
            self._pane_stability_cache: Dict[str, Dict[str, Any]] = {}
        state = self._pane_stability_cache.get(session_name)

        if state is None:
            # First poll -- no comparison basis yet. Hold back the last
            # couple lines (likely still active); everything above that
            # is a reasonable bootstrap, confirmed against future polls
            # from here on same as any other line.
            committed = max(0, len(current_lines) - 2)
            self._append_lines(clean_path, current_lines[:committed])
            self._pane_stability_cache[session_name] = {
                "history": [current_lines],
                "committed": committed,
            }
            return

        history = state["history"]
        committed = state["committed"]
        last_lines = history[-1]

        if last_lines and current_lines[:1] != last_lines[:1]:
            # Discontinuity: the capture window's start point shifted --
            # the common cause is the pane's total scrollback exceeding
            # tmux's configured history-limit (see launch_pipeline.py)
            # partway through a long-running session, scrolling some
            # already-committed lines off the top rather than replacing
            # the window wholesale.
            # Blindly appending the full current_lines here (as an earlier
            # version did) re-writes everything already committed in prior
            # polls, duplicating large stretches of transcript every time
            # a long session crosses this boundary. Anchor on the last
            # line we know we already committed and resume from just past
            # it instead -- only fall back to a full reset if that anchor
            # has scrolled out of the window entirely (more than
            # history-limit lines of new output in one poll interval,
            # which a full reset-and-dump is the correct, if rare,
            # response to).
            anchor = last_lines[committed - 1] if committed > 0 else None
            resume_at = None
            if anchor is not None:
                for i in range(len(current_lines) - 1, -1, -1):
                    if current_lines[i] == anchor:
                        resume_at = i + 1
                        break

            if resume_at is not None:
                logger.info(
                    f"[STABLE-TRANSCRIPT] Capture window scrolled for "
                    f"{session_name} -- re-anchored, no content re-appended"
                )
                self._pane_stability_cache[session_name] = {
                    "history": [current_lines],
                    "committed": resume_at,
                }
                return

            logger.warning(
                f"[STABLE-TRANSCRIPT] Capture window discontinuity for "
                f"{session_name} -- anchor not found, resetting stability tracking"
            )
            self._append_lines(clean_path, current_lines)
            self._pane_stability_cache[session_name] = {
                "history": [current_lines],
                "committed": len(current_lines),
            }
            return

        # Require agreement across the last _STABILITY_CONFIRMATIONS polls
        # (this one plus recent history), not just the immediately
        # preceding one -- see _STABILITY_CONFIRMATIONS' docstring.
        recent = history[-(self._STABILITY_CONFIRMATIONS - 1):] + [current_lines]
        stable_upto = committed
        if len(recent) >= self._STABILITY_CONFIRMATIONS:
            for i in range(committed, len(current_lines)):
                if any(i >= len(poll) or poll[i] != current_lines[i] for poll in recent):
                    break
                stable_upto += 1

        if stable_upto > committed:
            self._append_lines(clean_path, current_lines[committed:stable_upto])
            committed = stable_upto

        history = (history + [current_lines])[-(self._STABILITY_CONFIRMATIONS - 1):]
        self._pane_stability_cache[session_name] = {
            "history": history,
            "committed": committed,
        }

    def _flush_stable_transcript(self, session_name: str, clean_path: Path) -> None:
        """Final, unconditional flush -- call right before killing a
        session on the normal terminate_agent path. Nothing will change
        after this point, so commit everything still pending regardless
        of the usual multi-poll confirmation.

        Note: unlike the raw pipe-pane .transcript.log (which keeps
        capturing in real time independent of how the session dies, per
        _create_tmux_session's own comment), this clean transcript only
        updates when polled -- an abrupt kill (orphan reaper, auto-
        restart) that doesn't go through terminate_agent can lose the
        last few seconds this file never saw. That's an acceptable gap
        specifically for THIS supplementary, easier-to-read file, since
        the raw .transcript.log remains the unconditional forensics
        record for those paths.
        """
        state = getattr(self, "_pane_stability_cache", {}).pop(session_name, None)
        current_lines = self._capture_pane_lines(session_name)
        if current_lines is not None:
            committed = state["committed"] if state else 0
            self._append_lines(clean_path, current_lines[committed:])
        elif state:
            # Session already gone -- flush whatever was cached, even
            # though never independently confirmed stable. Strictly
            # better than losing it.
            self._append_lines(clean_path, state["history"][-1][state["committed"]:])

    def _get_orchestrator_output(self, agent, lines: int) -> str:
        """Return the orchestrator's run log as human-readable text."""
        if agent.system_prompt and agent.system_prompt.startswith("LOG_DIR:"):
            log_dir = Path(agent.system_prompt[len("LOG_DIR:") :].strip())
        if log_dir is None or not log_dir.exists():
            # Fall back: latest run-* directory under ~/.hephaestus/autopilot/
            base = Path(AUTOPILOT_STATE_DIR)
            candidates = sorted(base.glob("run-*"), reverse=True)
            log_dir = candidates[0] if candidates else None
        if log_dir is None:
            return "Orchestrator log not found."
        log_file = log_dir / "orchestrator.log"
        if not log_file.exists():
            return f"Orchestrator log not found at {log_file}."
        text = log_file.read_text(errors="replace")
        if lines and lines > 0:
            text_lines = text.splitlines()
            text = "\n".join(text_lines[-lines:])
        return text


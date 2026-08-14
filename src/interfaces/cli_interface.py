"""Abstract interface for CLI AI agents.

This module defines a polymorphic interface for CLI AI tools. Each CLI tool
(Claude Code, OpenCode, Pi, Droid, Codex) implements the abstract methods,
and the agent manager uses the interface without knowing which tool is running.

To add a new CLI tool: subclass CLIAgentInterface and register in CLI_AGENTS.
"""

import json
import logging
import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Prepended to a launched CLI agent's PATH so `rm` resolves to
# scripts/agent-safe-bin/rm instead of the real binary -- a hard technical
# guardrail against a destructive command outside .hephaestus/, since pi's
# own confirm-destructive extension was removed and there is no interactive
# safety net anymore. See that script's own header for the full rationale.
AGENT_SAFE_BIN_DIR = str(Path(__file__).parent.parent.parent / "scripts" / "agent-safe-bin")


class LaunchResult:
    """Result of get_launch_command — the CLI command plus metadata about
    how the system prompt was delivered, so the caller doesn't have to
    string-match on the command to decide behavior."""

    __slots__ = ("command", "prompt_delivery")

    # The CLI loads an agent file as its system prompt (e.g. Claude Code
    # with --agent).  The caller must include the system prompt in the
    # initial message since the CLI won't surface it otherwise.
    AGENT_FILE = "agent_file"

    # The system prompt is passed via a CLI flag (e.g. --append-system-prompt).
    # No extra handling needed — the initial message is task-specific only.
    FLAG = "flag"

    # The system prompt IS the initial message (e.g. OpenCode's
    # `opencode run "$(cat prompt_file)"`).  The caller's initial message
    # is already the system prompt.
    MESSAGE = "message"

    # The system prompt is added to the task-instructions file before the
    # initial message directs the CLI to read it (e.g. Codex).
    DEFERRED = "deferred"

    # The system prompt is ignored (e.g. Droid).  The caller's
    # initial message carries all the context.
    NONE = "none"

    def __init__(self, command: str, prompt_delivery: str) -> None:
        self.command = command
        self.prompt_delivery = prompt_delivery

    def __str__(self) -> str:
        return self.command



class CLIAgentInterface(ABC):
    """Abstract interface for CLI AI agents.

    Subclasses must implement all abstract methods. The agent manager
    calls these methods without knowing which CLI tool is running.
    """

    #: Human-readable label used in delivery logging (e.g. "Claude", "Pi").
    display_name: str = "agent"

    #: Whether the initial prompt must be sent in chunks to avoid tmux
    #: buffer issues with large prompts (True for CLIs that read the full
    #: prompt from stdin/paste vs. those that load it via a launch flag).
    #: Replaces isinstance() branching in AgentManager._send_initial_prompt_with_retry
    #: (SOLID review 3.3) — a new CLI agent opts into chunked delivery by
    #: setting this attribute instead of the caller needing to know its type.
    needs_chunked_delivery: bool = False

    #: This CLI's own sensible default model, used when no phase/task
    #: override is given. hephaestus_config.yaml's global agents.cli_model
    #: is a single string paired with agents.default_cli_tool (historically
    #: an OpenRouter path for pi) -- callers must not apply it to a
    #: different CLI a phase opts into, or that CLI launches with a model
    #: string it can't parse. Override per subclass.
    default_model: str = "sonnet"

    def get_session_args(self, session_id: str) -> str:
        """Return the CLI-specific flag to resume/create a named session.

        Each CLI agent overrides this to inject its own session-resume args.
        Empty string = no session persistence for this CLI.

        Args:
            session_id: Deterministic session identifier (e.g. 'hephaestus-proj-design-architect')

        Returns:
            CLI-specific args string (e.g. '--session-id hephaestus-...') or empty.
        """
        return ""

    def record_session(
        self, session_id: str, working_directory: str, launched_at: float
    ) -> bool:
        """Persist CLI-specific session state after an initial launch.

        Most CLIs either accept Hephaestus's deterministic session ID directly
        or need no persistence. Codex creates its own UUID, so it overrides
        this method after its transcript has appeared on disk.
        """
        return False

    def prepare_working_directory(self, working_directory: str) -> None:
        """Do any one-time setup a CLI needs for a directory before first
        launch there (e.g. pre-accepting a first-run trust prompt). Every
        Hephaestus worktree is a path this CLI has never seen, so a CLI
        with an interactive first-run gate would otherwise hang forever
        with no one at the keyboard to answer it. No-op by default.
        """
        return None

    def post_launch_confirmation_keys(self) -> List[str]:
        """tmux key names to send, in order with a pause between each,
        after the launch wait and before the initial task prompt -- for a
        CLI whose launch triggers a SECOND interactive confirmation that
        --dangerously-skip-permissions-style flags don't suppress (e.g. a
        one-time "you're bypassing all safety checks, are you sure?"
        dialog). Empty = no confirmation needed for this CLI."""
        return []

    def format_goal_command(self, condition: str) -> str:
        """CLI-native command text that sets a self-checked completion
        condition, keeping the agent working until it's actually met
        instead of stopping on its own judgment (e.g. Claude Code's
        `/goal <condition>`). Sent by the caller exactly like any other
        message -- chunked per needs_chunked_delivery, no per-CLI dispatch
        needed there. Empty = no such mechanism for this CLI (the caller
        skips sending anything)."""
        return ""

    @abstractmethod
    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        """Generate the launch command for the CLI tool.

        Args:
            system_prompt: System prompt for the agent
            **kwargs: Additional parameters for the CLI tool

        Returns:
            LaunchResult with the command string and prompt_delivery strategy.
        """
        pass

    @abstractmethod
    def get_health_check_pattern(self) -> str:
        """Return pattern to check if agent is healthy.

        Returns:
            Regex pattern or string to look for in output
        """
        pass

    @abstractmethod
    def format_message(self, message: str) -> str:
        """Format a message for the specific CLI tool.

        Args:
            message: Raw message to send

        Returns:
            Formatted message for the CLI tool
        """
        pass

    @abstractmethod
    def get_stuck_patterns(self) -> List[str]:
        """Return patterns that indicate the agent is stuck.

        Returns:
            List of patterns to check for stuck state
        """
        pass

    @abstractmethod
    def parse_output(self, output: str) -> Dict[str, Any]:
        """Parse CLI output for relevant information.

        Args:
            output: Raw output from the CLI tool

        Returns:
            Parsed information dict
        """
        pass

    def recovery_keystrokes(self) -> List[str]:
        """tmux key names to break this CLI out of a stuck/looping TUI before a nudge
        message is sent (e.g. ['Escape']). Empty = no mechanical keystroke recovery for
        this CLI. Concrete with a safe default so the monitor stays harness-agnostic;
        override per CLI (polymorphic)."""
        return []

    def mcp_reconnect_instructions(self, server_name: str) -> str:
        """Chat-message instructions telling the agent how to reconnect a
        dropped MCP server, in THIS CLI's own vocabulary (e.g. pi's
        `mcp connect <server>` tool). Empty = no known reconnect mechanism
        for this CLI -- the monitor should not nudge with a guess, since
        wrong syntax for the wrong CLI just confuses the agent. Concrete
        with a safe default so the monitor stays harness-agnostic; override
        per CLI (polymorphic)."""
        return ""

    def fallback_model(self, config, is_primary: bool) -> Optional[str]:
        """The configured model to switch to when model_fallback_keystrokes
        fires, resolved by ROLE, not by which CLI product this is:
        config.cli_model_fallback is whichever CLI is currently the
        *primary* default_cli_tool's fallback; config.secondary_cli_model_fallback
        is whichever CLI is currently the *secondary*/default_fallback_cli_tool's.
        Swapping default_cli_tool/default_fallback_cli_tool (e.g. running
        Claude as primary against a local model, pi as the fallback tier)
        must not silently keep reading the old role's config key just
        because it's still the same CLI class. Shared here, not overridden
        per subclass -- this is a role lookup, not a CLI-specific concern
        (each CLI's own valid model *strings* are model_fallback_keystrokes'
        job, not this method's). None = unset -- the monitor should not
        guess a default.

        is_primary: whether this agent's cli_type is the currently
        configured default_cli_tool (the caller already computes this for
        its own gate check, so it's passed in rather than re-derived here).
        """
        key = "cli_model_fallback" if is_primary else "secondary_cli_model_fallback"
        return getattr(config, key, None)

    def model_fallback_keystrokes(self, model: str) -> List[Tuple[str, float]]:
        """Literal pane inputs (not chat messages) to switch this CLI's
        active model to `model` via its own model-switching UI, as a list
        of (text_to_send, seconds_to_wait_after_sending) pairs, sent in
        order. Empty = no known in-session model-switch mechanism for this
        CLI -- the monitor should not guess, since a model-switch slash
        command is typically client-side (intercepted before it reaches the
        model) and wrong syntax for the wrong CLI does nothing useful.
        Concrete with a safe default so the monitor stays harness-agnostic;
        override per CLI (polymorphic)."""
        return []

    def model_fallback_confirmed(self, output: str, model: str) -> Optional[bool]:
        """Check recent pane/transcript output for confirmation that a
        model_fallback_keystrokes switch to `model` actually landed.
        Returns True if confirmed, False if there's a specific reason to
        believe it didn't, None if this CLI has no known way to tell (skip
        verification silently rather than guess). Concrete with a safe
        default so the monitor stays harness-agnostic; override per CLI
        (polymorphic) -- only meaningful for a CLI that also overrides
        model_fallback_keystrokes."""
        return None

    # ── Shared helpers ───────────────────────────────────────────────────

    def _save_prompt_to_file(self, prompt: str, prefix: str, task_id: str) -> str:
        """Save prompt to a temp file and make it readable.

        Args:
            prompt: Prompt text to save
            prefix: Filename prefix (e.g., 'pi_prompt', 'claude_prompt')
            task_id: Task ID for unique filename

        Returns:
            Path to the saved file
        """
        prompt_file = f"/tmp/{prefix}_{task_id}.txt"
        with open(prompt_file, "w") as f:
            f.write(prompt)
        os.chmod(prompt_file, 0o644)
        return prompt_file

    def _get_model(self, kwargs: dict, default: str = "sonnet") -> str:
        """Get model from kwargs, falling back to this CLI's own default.

        The global agents.cli_model config value is scoped to one specific
        CLI (agents.default_cli_tool) -- honoring it here regardless of
        which CLI subclass is asking would hand e.g. an OpenRouter model
        path to a CLI that can't parse it. Callers that want to honor that
        global override resolve it themselves, CLI-aware, before calling
        (see AgentManager.create_agent_for_task_direct), and pass the
        result in via kwargs['model'].

        Args:
            kwargs: Keyword arguments (may contain 'model')
            default: This CLI's own default model (pass self.default_model)

        Returns:
            Model name string
        """
        return kwargs.get("model") or default

    def _extract_id(self, text: str, prefix: str) -> Optional[str]:
        """Extract an ID value from text like 'IDs: Agent=xxx | Task=yyy'.

        Args:
            text: Text to search
            prefix: Prefix before the ID (e.g., 'Agent=', 'Task=')

        Returns:
            Extracted ID or None
        """
        match = re.search(rf"{prefix}\s*(\S+)", text)
        return match.group(1) if match else None

    def _extract_ids_from_prompt(self, system_prompt: str) -> Dict[str, Optional[str]]:
        """Extract all standard IDs from a system prompt.

        Looks for patterns like 'Agent=xxx', 'Task=yyy', 'Workflow=zzz', 'Phase=www'.

        Returns:
            Dict with keys: agent_id, task_id, workflow_id, phase_id
        """
        return {
            "agent_id": self._extract_id(system_prompt, "Agent="),
            "task_id": self._extract_id(system_prompt, "Task="),
            "workflow_id": self._extract_id(system_prompt, "Workflow="),
            "phase_id": self._extract_id(system_prompt, "Phase="),
        }

    def _build_ids_line(self, ids: Dict[str, Optional[str]]) -> str:
        """Build an 'IDs: Agent=xxx Task=yyy ...' line from extracted IDs.

        Args:
            ids: Dict from _extract_ids_from_prompt

        Returns:
            IDs line string (empty if no IDs found)
        """
        parts = []
        for key in ["agent_id", "task_id", "workflow_id", "phase_id"]:
            val = ids.get(key)
            if val:
                label = key.replace("_id", "").title()
                parts.append(f"{label}={val}")
        return "IDs: " + " ".join(parts) if parts else ""

    def get_tui_status_patterns(self) -> List[str]:
        """Return patterns that are normal TUI status bar rendering.

        These patterns are stripped before checking for garbled output.
        Override in subclasses to provide CLI-specific patterns.

        Returns:
            List of regex patterns that are NOT garbled output
        """
        return []

    def strip_tui_chrome(self, text: str) -> str:
        """Remove CLI-specific TUI chrome from captured terminal output.

        tmux capture-pane snapshots include the TUI frame at the time of
        capture: status bars, spinners, decorative separators, tilde-fill
        lines, etc.  These are visual artifacts, not log content.

        Default: return text unchanged (most CLIs produce plain output).
        Override per CLI to strip that CLI's specific frame chrome.

        Args:
            text: Raw captured text (already ANSI-stripped by capture-pane).

        Returns:
            Text with trailing TUI frame chrome removed.
        """
        return text

    # ── Health / stuck checks (shared) ───────────────────────────────────

    def is_healthy(self, output: str) -> bool:
        """Check if the agent appears healthy based on output."""
        pattern = self.get_health_check_pattern()
        return bool(re.search(pattern, output, re.MULTILINE | re.IGNORECASE))

    def is_stuck(self, output: str) -> bool:
        """Check if the agent appears stuck."""
        for pattern in self.get_stuck_patterns():
            if re.search(pattern, output, re.MULTILINE | re.IGNORECASE):
                return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Concrete implementations
# ═══════════════════════════════════════════════════════════════════════════


class ClaudeCodeAgent(CLIAgentInterface):
    """Implementation for Claude Code CLI."""

    display_name = "Claude"
    needs_chunked_delivery = True

    #: Per-project flags in ~/.claude.json that gate first-run interactive
    #: dialogs with no one at the keyboard to answer them: workspace trust,
    #: and (independently) the "what's new since you last opened this
    #: project" onboarding prompts (fullscreen-renderer opt-in, Chrome
    #: extension opt-in, etc. -- the exact set has already changed between
    #: CLI versions during this investigation; hasCompletedProjectOnboarding
    #: suppresses all of them at once rather than clicking through each by
    #: name). Verified empirically against claude 2.1.211.
    _TRUST_FLAGS = {
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
    }

    def prepare_working_directory(self, working_directory: str) -> None:
        """Pre-seed _TRUST_FLAGS for a worktree Claude Code has never seen
        before, keyed by its *canonical* path (Claude resolves symlinks,
        e.g. macOS's /tmp -> /private/tmp, before the lookup). Without
        this every launch in a fresh worktree hangs at one of these
        dialogs forever.

        Uses a flock + atomic replace: this file is also read/written by
        any real interactive Claude Code session on the same machine, and
        concurrent Hephaestus agent launches all touch it too.
        """
        import json
        import os

        canonical_path = os.path.realpath(working_directory)
        config_path = os.path.expanduser("~/.claude.json")
        if not os.path.exists(config_path):
            return

        try:
            # Shared worktrees see many agents launch in the same
            # directory across a workflow's phases -- an unlocked read-only
            # check keeps every one of those (after the first) from taking
            # an exclusive lock on a file a real interactive session may
            # also be using.
            with open(config_path) as f:
                existing = json.load(f).get("projects", {}).get(canonical_path, {})
            if all(existing.get(k) == v for k, v in self._TRUST_FLAGS.items()):
                return

            import fcntl

            with open(config_path + ".lock", "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    with open(config_path) as f:
                        cfg = json.load(f)
                    projects = cfg.setdefault("projects", {})
                    entry = projects.setdefault(canonical_path, {})
                    if all(entry.get(k) == v for k, v in self._TRUST_FLAGS.items()):
                        return
                    entry.update(self._TRUST_FLAGS)
                    tmp_path = config_path + ".tmp"
                    with open(tmp_path, "w") as f:
                        json.dump(cfg, f)
                    os.replace(tmp_path, config_path)
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(
                f"Could not pre-trust {canonical_path} for Claude Code: {e}"
            )

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        from src.core.simple_config import get_config

        config = get_config()

        # Check for an installed Claude Code subagent for this phase
        # (~/.claude/agents/hephaestus-{phase}.md, generated by
        # scripts/generate_claude_agents.py and installed by install.sh --
        # mirrors PiAgent's per-phase agent file lookup below). When present,
        # `--agent <name>` is Claude Code's own officially supported way to
        # launch a session as a named subagent, so it's used in place of
        # --append-system-prompt rather than alongside it.
        phase_name = kwargs.get("phase_name", "")
        claude_agent_name = f"hephaestus-{phase_name.replace('_', '-')}" if phase_name else None
        claude_agent_file = (
            os.path.expanduser(f"~/.claude/agents/{claude_agent_name}.md")
            if claude_agent_name
            else None
        )

        task_id = kwargs.get("task_id", "default")
        if claude_agent_file and os.path.exists(claude_agent_file):
            prompt_flag = f"--agent {claude_agent_name}"
            delivery = LaunchResult.AGENT_FILE
        else:
            prompt_file = self._save_prompt_to_file(system_prompt, "claude_prompt", task_id)
            prompt_flag = f'--append-system-prompt "$(cat {prompt_file})"'
            delivery = LaunchResult.FLAG
        model = self._get_model(kwargs, self.default_model)

        # Reasoning budget
        effort_map = {
            "off": "low",
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
        }
        thinking = (
            str(
                kwargs.get("thinking_level")
                or getattr(config, "cli_thinking_level", "medium")
            )
            .lower()
            .strip()
        )
        effort = effort_map.get(thinking)
        effort_flag = f" --effort {effort}" if effort else ""

        mcp_config = os.path.expanduser("~/.config/mcp/mcp.json")
        mcp_flag = f"--mcp-config {mcp_config}" if os.path.exists(mcp_config) else ""

        from src.core.utils import is_glm_model

        resolved_model = "sonnet" if is_glm_model(model) else model
        # NOTE: --permission-mode dontAsk looks like a fix for the
        # "Bypass Permissions mode" confirmation dialog below (it does skip
        # that dialog) but is NOT equivalent to --dangerously-skip-permissions
        # -- dontAsk still runs every permission check, it just auto-DENIES
        # instead of prompting when a decision has no pre-existing allow
        # rule. Verified live: it silently denied Write, Bash redirects, and
        # even the complete_my_task MCP call itself ("Permission has been
        # denied because Claude Code is running in don't ask mode"),
        # stranding a fully-completed review with no way to persist it.
        # --dangerously-skip-permissions actually disables the permission
        # system; the dialog it triggers is handled by the launch-failure
        # detection in manager.py's create_agent_for_task instead.
        base_flags = (
            f'--model {resolved_model}{effort_flag} --dangerously-skip-permissions '
            f'{mcp_flag} {prompt_flag} --verbose'
        )

        session_id = kwargs.get("session_id", "")
        if session_id:
            # Unlike pi's single --session-id (create-or-resume), Claude
            # requires a UUID and splits this into two flags that each only
            # work once: --session-id errors "already in use" on a repeat
            # id, --resume errors "no conversation found" on a fresh one.
            # Try the one that should actually work first (see
            # _claude_session_exists), falling back to the other -- this
            # still covers both the first launch and every goto/retry that
            # reuses this id even if the existence check is wrong (stale
            # sanitization heuristic, race, permissions), it just no longer
            # prints the "already in use" error in the common case.
            session_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id))
            working_directory = kwargs.get("working_directory", "")
            first, second = "--session-id", "--resume"
            if working_directory and self._claude_session_exists(
                working_directory, session_uuid
            ):
                first, second = "--resume", "--session-id"
            claude_cmd = f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" claude'
            command = (
                f"({claude_cmd} {first} {session_uuid} {base_flags} || "
                f"{claude_cmd} {second} {session_uuid} {base_flags})"
            )
        else:
            command = f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" claude {base_flags}'

        return LaunchResult(command, delivery)

    @staticmethod
    def _claude_session_exists(working_directory: str, session_uuid: str) -> bool:
        """Best-effort check for whether Claude Code already has a stored
        session for this uuid in this working directory, so the launch
        command can try the branch that will actually succeed first instead
        of always eating an "already in use" failure on every resumed
        session. Mirrors Claude Code's own project-key sanitization
        (verified empirically against ~/.claude/projects/ directory names:
        every '/', '.', and '_' in the canonical path becomes '-'). If this
        heuristic is ever wrong, the caller's || fallback still covers it --
        this only affects which branch is tried first.
        """
        try:
            canonical = os.path.realpath(working_directory)
            project_key = re.sub(r"[/._]", "-", canonical)
            session_file = (
                Path.home() / ".claude" / "projects" / project_key / f"{session_uuid}.jsonl"
            )
            return session_file.exists()
        except Exception:
            return False

    def get_health_check_pattern(self) -> str:
        return r"(Assistant:|Human:|›)"

    def format_goal_command(self, condition: str) -> str:
        return f"/goal {condition}"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded",
            r"waiting for user input",
            r"API error",
            r"connection timeout",
            r"Error:.*API",
            r"Failed to connect",
            r"Maximum retries exceeded",
        ]

    def model_fallback_keystrokes(self, model: str) -> List[Tuple[str, float]]:
        # Same one-line `/model <name>` syntax already confirmed working
        # for Claude Code in _detect_bad_model_error (monitor.py) -- unlike
        # pi, no picker/search step.
        return [(f"/model {model}", 0.0)]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split("\n")
        last_message = ""
        is_waiting = False
        for i in range(len(lines) - 1, -1, -1):
            if "Assistant:" in lines[i]:
                message_lines = []
                for j in range(i + 1, len(lines)):
                    if "Human:" in lines[j] or "›" in lines[j]:
                        break
                    message_lines.append(lines[j])
                last_message = "\n".join(message_lines).strip()
                break
        if lines and ("›" in lines[-1] or "Human:" in lines[-1]):
            is_waiting = True
        return {
            "last_message": last_message,
            "is_waiting": is_waiting,
            "total_lines": len(lines),
        }


class OpenCodeAgent(CLIAgentInterface):
    """Implementation for OpenCode CLI."""

    default_model = "anthropic/claude-sonnet-4"

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        from src.core.simple_config import get_config

        get_config()

        task_id = kwargs.get("task_id", "default")
        # LaunchResult.MESSAGE means system_prompt IS the initial message --
        # manager.py never sends a separate task message afterward for this
        # CLI (see the is_opencode branch in _send_initial_prompt_with_retry),
        # so the task's instructions-file pointer must be folded in here or
        # the agent never receives it at all.
        instructions_pointer = kwargs.get("instructions_pointer", "")
        full_prompt = (
            f"{system_prompt}\n\n---\n\n{instructions_pointer}"
            if instructions_pointer
            else system_prompt
        )
        prompt_file = self._save_prompt_to_file(
            full_prompt, "opencode_prompt", task_id
        )
        model = self._get_model(kwargs, self.default_model)

        # Bare `opencode run <message>` is one-shot: it answers and exits,
        # leaving the pane at a dead shell prompt for the task message
        # manager.py sends ~25s later via tmux (same bug class as the
        # claude/pi launch -- an agent that exits before the real task
        # arrives). -i keeps it running interactively after the initial
        # message, matching how claude/pi stay alive for MCP tool calls.
        return LaunchResult(
            f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" opencode run -i --dangerously-skip-permissions '
            f'--model {model} "$(cat {prompt_file})"',
            LaunchResult.MESSAGE,
        )

    def get_health_check_pattern(self) -> str:
        return r"(›|>|opencode>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded",
            r"rate limit",
            r"API error",
            r"connection timeout",
            r"Error:.*API",
            r"Failed to connect",
            r"Maximum retries exceeded",
            r"authentication failed",
            r"invalid API key",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split("\n")
        last_message = ""
        is_waiting = False
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if "›" in line or ">" in line or "opencode>" in line:
                is_waiting = True
                message_lines = []
                for j in range(i - 1, -1, -1):
                    if "›" in lines[j] or ">" in lines[j] or "opencode>" in lines[j]:
                        break
                    message_lines.insert(0, lines[j])
                last_message = "\n".join(message_lines).strip()
                break
        return {
            "last_message": last_message,
            "is_waiting": is_waiting,
            "total_lines": len(lines),
        }


class DroidAgent(CLIAgentInterface):
    """Implementation for Droid CLI."""

    display_name = "Droid"
    needs_chunked_delivery = True

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        return LaunchResult(
            f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" droid', LaunchResult.NONE
        )

    def get_health_check_pattern(self) -> str:
        return r"(›|>|droid>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded",
            r"rate limit",
            r"API error",
            r"connection timeout",
            r"Error:.*API",
            r"Failed to connect",
            r"Maximum retries exceeded",
            r"authentication failed",
            r"invalid API key",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split("\n")
        last_message = ""
        is_waiting = False
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if "›" in line or ">" in line or "droid>" in line:
                is_waiting = True
                message_lines = []
                for j in range(i - 1, -1, -1):
                    if "›" in lines[j] or ">" in lines[j] or "droid>" in lines[j]:
                        break
                    message_lines.insert(0, lines[j])
                last_message = "\n".join(message_lines).strip()
                break
        return {
            "last_message": last_message,
            "is_waiting": is_waiting,
            "total_lines": len(lines),
        }


class CodexAgent(CLIAgentInterface):
    """Implementation for Codex CLI."""

    display_name = "Codex"
    needs_chunked_delivery = True
    default_model = ""

    @staticmethod
    def _session_map_path(working_directory: str) -> Path:
        return Path(working_directory) / ".hephaestus" / "codex_sessions.json"

    @classmethod
    def _saved_session_id(cls, session_id: str, working_directory: str) -> Optional[str]:
        if not session_id or not working_directory:
            return None
        try:
            sessions = json.loads(cls._session_map_path(working_directory).read_text())
            codex_session_id = sessions.get(session_id)
            return str(uuid.UUID(codex_session_id)) if codex_session_id else None
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            return None

    @classmethod
    def _save_session_id(
        cls, session_id: str, working_directory: str, codex_session_id: str
    ) -> None:
        path = cls._session_map_path(working_directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        sessions = {}
        try:
            sessions = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        sessions[session_id] = codex_session_id
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(sessions, indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, path)

    def record_session(
        self, session_id: str, working_directory: str, launched_at: float
    ) -> bool:
        if self._saved_session_id(session_id, working_directory):
            return True

        try:
            canonical_working_directory = os.path.realpath(working_directory)
            sessions_dir = Path.home() / ".codex" / "sessions"
            for transcript in sorted(
                sessions_dir.glob("**/*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ):
                if transcript.stat().st_mtime < launched_at - 5:
                    break
                with transcript.open() as transcript_file:
                    metadata = json.loads(transcript_file.readline())
                payload = metadata.get("payload", {})
                codex_session_id = payload.get("session_id") or payload.get("id")
                if (
                    metadata.get("type") == "session_meta"
                    and os.path.realpath(payload.get("cwd", ""))
                    == canonical_working_directory
                    and codex_session_id
                    and f"Hephaestus Session ID: {session_id}" in transcript.read_text()
                ):
                    self._save_session_id(
                        session_id,
                        working_directory,
                        str(uuid.UUID(codex_session_id)),
                    )
                    return True
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
            logger.debug("Could not record Codex session %s: %s", session_id, exc)
        return False

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        model = self._get_model(kwargs, self.default_model)
        model_flag = f" --model {model}" if model else ""
        codex_session_id = self._saved_session_id(
            kwargs.get("session_id", ""), kwargs.get("working_directory", "")
        )
        flags = f"--dangerously-bypass-approvals-and-sandbox --no-alt-screen{model_flag}"
        # A recovered task can retain a session mapping while another Codex
        # process still owns that thread.  Resume then exits immediately with
        # "already has an active writer"; fall back to a new session so the
        # manager can still deliver the task prompt and make progress.
        command = (
            f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH"; '
            f"(codex resume {codex_session_id} {flags} || codex {flags})"
            if codex_session_id
            else f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" codex {flags}'
        )
        return LaunchResult(
            command,
            LaunchResult.DEFERRED,
        )

    def get_health_check_pattern(self) -> str:
        return r"(›|To get started|context left)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"error:",
            r"connection failed",
            r"timeout",
            r"invalid response",
            r"Authentication failed",
            r"Rate limit",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split("\n")
        last_response = ""
        is_ready = False
        for i in range(len(lines) - 1, -1, -1):
            if ">" in lines[i]:
                is_ready = True
                if i > 0:
                    response_lines = []
                    for j in range(i - 1, -1, -1):
                        if ">" in lines[j] or lines[j].startswith("/"):
                            break
                        response_lines.insert(0, lines[j])
                    last_response = "\n".join(response_lines).strip()
                break
        return {
            "last_response": last_response,
            "is_ready": is_ready,
            "total_lines": len(lines),
        }


class PiAgent(CLIAgentInterface):
    """Implementation for pi coding agent CLI.

    Pi supports --append-system-prompt to load system prompts from files.
    For Hephaestus, we launch pi interactively (no --print/-p) so it stays
    running and can call MCP tools. The initial message is sent via tmux.
    """

    display_name = "Pi"
    needs_chunked_delivery = True
    default_model = "Qwen3.6-27B-UD-Q4_K_XL.gguf"

    def get_session_args(self, session_id: str) -> str:
        """Pi uses --session-id to resume or create a named session.

        This preserves full conversational context across phases and gotos.
        The same session ID for a role (architect, developer) means the agent
        picks up where it left off with all prior reasoning intact.

        Pi handles storage internally — we just pass the ID.
        """
        if session_id:
            return f"--session-id {session_id}"
        return "--no-session"

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        from src.core.simple_config import get_config

        config = get_config()

        # Check for pi agent file for this phase
        phase_name = kwargs.get("phase_name", "")
        pi_agents_dir = os.path.expanduser("~/.pi/agent/agents")
        agent_name = phase_name.replace("_", "-") if phase_name else None
        agent_file = (
            os.path.join(pi_agents_dir, f"hephaestus-{agent_name}.md")
            if agent_name
            else None
        )

        model = self._get_model(kwargs, self.default_model)

        # Thinking budget
        valid_thinking = {"off", "minimal", "low", "medium", "high", "xhigh"}
        thinking = kwargs.get("thinking_level") or getattr(
            config, "cli_thinking_level", "medium"
        )
        thinking = str(thinking).lower().strip()
        thinking_flag = f" --thinking {thinking}" if thinking in valid_thinking else ""

        if agent_file and os.path.exists(agent_file):
            # Parse the agent file: strip YAML frontmatter so only the body
            # (identity + completion instructions) reaches --append-system-prompt.
            # The frontmatter deliberately carries no model: field -- model is
            # always resolved from Phase.cli_model/config at launch time via
            # _get_model above, never from this file (see generate_pi_agents.py).
            raw = open(agent_file).read()
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                body = parts[2].strip() if len(parts) >= 3 else raw
            else:
                body = raw

            task_id = kwargs.get("task_id", "default")
            body_file = self._save_prompt_to_file(body, "pi_agent_body", task_id)
            session_args = self.get_session_args(kwargs.get("session_id", ""))

            # Launch interactively (no -p/--print). Initial message sent via tmux.
            command = f'pi --append-system-prompt "$(cat {body_file})" --model {model}{thinking_flag} --approve --no-context-files {session_args}'
        else:
            # Fallback: inject full prompt from file
            task_id = kwargs.get("task_id", "default")
            prompt_file = self._save_prompt_to_file(system_prompt, "pi_prompt", task_id)
            session_args = self.get_session_args(kwargs.get("session_id", ""))
            command = f'pi --append-system-prompt "$(cat {prompt_file})" --model {model}{thinking_flag} --approve --no-context-files {session_args}'

        return LaunchResult(
            f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" {command}',
            LaunchResult.FLAG,
        )

    def get_tui_status_patterns(self) -> List[str]:
        """Pi TUI status bar patterns that look like garbled output but aren't."""
        return [
            "Your working",
            "Your worked",
            "king Your",
            "worki",
            "workin",
            "MCP:",
            "openrouter",
            "/.worktrees/",
            "model.*medium",
            "%",
        ]

    # Braille spinner frames used by the pi TUI progress indicator.
    _PI_SPINNERS = frozenset(
        "⠀⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿"
    )

    def strip_tui_chrome(self, text: str) -> str:
        """Strip the pi TUI frame from the tail of a capture-pane snapshot.

        pi renders a persistent status bar at the bottom of the terminal:
          ────────────────────────────────────────────────────────────────
          ~   (tilde filler lines for unused screen rows)
          ↑221k ↓9.0k R1.2M CH99.6% $0.037  xiaomi/mimo-v2.5 • high
          MCP: 1/1 servers
        Spinner frames (⠦ Working…) also appear just before the bar.

        We walk backwards discarding these lines until we hit real content.
        """
        lines = text.split("\n")
        end = len(lines)
        while end > 0:
            stripped = lines[end - 1].strip()
            if (
                stripped == ""
                or stripped == "~"
                or re.match(r"^─+$", stripped)
                or stripped.startswith("MCP:")
                or re.search(r"↑\d+[km].*↓\d+", stripped, re.IGNORECASE)
                or (stripped and stripped[0] in self._PI_SPINNERS)
            ):
                end -= 1
            else:
                break
        return "\n".join(lines[:end])

    def get_health_check_pattern(self) -> str:
        return r"(›|>|pi>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded",
            r"rate limit",
            r"API error",
            r"connection timeout",
            r"Error:.*API",
            r"Failed to connect",
            r"Maximum retries exceeded",
            r"authentication failed",
            r"invalid API key",
        ]

    def recovery_keystrokes(self) -> List[str]:
        # pi (mimo) can fall into a thought loop that never exits; Esc interrupts the
        # current generation so a follow-up nudge message is actually read.
        return ["Escape"]

    def mcp_reconnect_instructions(self, server_name: str) -> str:
        # Confirmed live: pi exposes `mcp status` / `mcp connect <server>`
        # as tools the agent itself can invoke to reconnect a dropped MCP
        # server without losing session state.
        return (
            f"Run `mcp connect {server_name}` to reconnect before calling "
            f"any {server_name}_* tool. Verify with `mcp status` afterward."
        )

    def model_fallback_keystrokes(self, model: str) -> List[Tuple[str, float]]:
        # The two-step form ("/model" alone, wait for the picker, then the
        # search text as a second send) never confirmed a single successful
        # switch across 80 attempts system-wide (0 cli_model_fallback_confirmed
        # vs 58 unconfirmed + 6 abandoned) -- the wait window is exactly
        # where a busy/slow-to-respond agent lets the second send land as a
        # queued chat "Steering" message instead of picker input, since the
        # picker hadn't actually opened yet. pi's `/model` accepts the search
        # text as a trailing argument on the same line, pre-filtering the
        # picker to a single match in one atomic send -- no wait window for
        # the agent to be mid-turn in.
        return [(f"/model {model}", 0.0)]

    def model_fallback_confirmed(self, output: str, model: str) -> Optional[bool]:
        # Confirmed live: a successful pick echoes "Model: <provider>/<name>"
        # (e.g. "Model: xiaomi/mimo-v2.5-pro" for search text
        # "mimo-v2.5-pro"). Requiring the "Model: " prefix (not just a bare
        # substring search for `model`) avoids a false positive from the
        # search text merely being echoed back as typed input if the picker
        # never actually opened.
        return re.search(rf"Model:\s*\S*{re.escape(model)}", output) is not None

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split("\n")
        last_message = ""
        is_waiting = False
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if "›" in line or ">" in line or "pi>" in line:
                is_waiting = True
                message_lines = []
                for j in range(i - 1, -1, -1):
                    if "›" in lines[j] or ">" in lines[j] or "pi>" in lines[j]:
                        break
                    message_lines.insert(0, lines[j])
                last_message = "\n".join(message_lines).strip()
                break
        return {
            "last_message": last_message,
            "is_waiting": is_waiting,
            "total_lines": len(lines),
        }


class SwarmCodeAgent(CLIAgentInterface):
    """Implementation for SwarmCode CLI (hypothetical advanced agent)."""

    def get_launch_command(self, system_prompt: str, **kwargs) -> LaunchResult:
        escaped_prompt = system_prompt.replace("'", "'\"'\"'")
        prompt_file = f"/tmp/hep_prompt_{kwargs.get('task_id', 'default')}.txt"
        return LaunchResult(
            f"echo '{escaped_prompt}' > {prompt_file} && "
            f'PATH="{AGENT_SAFE_BIN_DIR}:$PATH" swarmcode --autonomous --context {prompt_file}',
            LaunchResult.NONE,
        )

    def get_health_check_pattern(self) -> str:
        return r"(SWARM>|Ready|Processing)"

    def format_message(self, message: str) -> str:
        return f"TASK: {message}"

    def get_stuck_patterns(self) -> List[str]:
        return [r"BLOCKED:", r"WAITING FOR INPUT", r"ERROR:", r"DEADLOCK DETECTED"]

    def parse_output(self, output: str) -> Dict[str, Any]:
        return {"output": output, "status": "processing"}


# Registry for available CLI agents
CLI_AGENTS = {
    "claude": ClaudeCodeAgent,
    "opencode": OpenCodeAgent,
    "droid": DroidAgent,
    "codex": CodexAgent,
    "pi": PiAgent,
    "swarm": SwarmCodeAgent,
}


def get_cli_agent(agent_type: str) -> CLIAgentInterface:
    """Get a CLI agent instance by type.

    Args:
        agent_type: Type of CLI agent (claude, opencode, pi, etc.)

    Returns:
        CLI agent instance

    Raises:
        ValueError: If agent type is not supported
    """
    if agent_type not in CLI_AGENTS:
        raise ValueError(
            f"Unsupported CLI agent type: {agent_type}. Available: {list(CLI_AGENTS.keys())}"
        )
    return CLI_AGENTS[agent_type]()

"""Abstract interface for CLI AI agents.

This module defines a polymorphic interface for CLI AI tools. Each CLI tool
(Claude Code, OpenCode, Pi, Droid, Codex) implements the abstract methods,
and the agent manager uses the interface without knowing which tool is running.

To add a new CLI tool: subclass CLIAgentInterface and register in CLI_AGENTS.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
import re
import os
import logging

logger = logging.getLogger(__name__)


class CLIAgentInterface(ABC):
    """Abstract interface for CLI AI agents.

    Subclasses must implement all abstract methods. The agent manager
    calls these methods without knowing which CLI tool is running.
    """

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

    @abstractmethod
    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        """Generate the launch command for the CLI tool.

        Args:
            system_prompt: System prompt for the agent
            **kwargs: Additional parameters for the CLI tool

        Returns:
            Complete command to launch the CLI tool
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
        prompt_file = f'/tmp/{prefix}_{task_id}.txt'
        with open(prompt_file, 'w') as f:
            f.write(prompt)
        os.chmod(prompt_file, 0o644)
        return prompt_file

    def _get_model(self, kwargs: dict, config: Any, default: str = 'sonnet') -> str:
        """Get model from kwargs or config.

        Args:
            kwargs: Keyword arguments (may contain 'model')
            config: Config object (may have 'cli_model')
            default: Default model if nothing else found

        Returns:
            Model name string
        """
        return kwargs.get('model') or getattr(config, 'cli_model', default)

    def _extract_id(self, text: str, prefix: str) -> Optional[str]:
        """Extract an ID value from text like 'IDs: Agent=xxx | Task=yyy'.

        Args:
            text: Text to search
            prefix: Prefix before the ID (e.g., 'Agent=', 'Task=')

        Returns:
            Extracted ID or None
        """
        match = re.search(rf'{prefix}\s*(\S+)', text)
        return match.group(1) if match else None

    def _extract_ids_from_prompt(self, system_prompt: str) -> Dict[str, Optional[str]]:
        """Extract all standard IDs from a system prompt.

        Looks for patterns like 'Agent=xxx', 'Task=yyy', 'Workflow=zzz', 'Phase=www'.

        Returns:
            Dict with keys: agent_id, task_id, workflow_id, phase_id
        """
        return {
            'agent_id': self._extract_id(system_prompt, 'Agent='),
            'task_id': self._extract_id(system_prompt, 'Task='),
            'workflow_id': self._extract_id(system_prompt, 'Workflow='),
            'phase_id': self._extract_id(system_prompt, 'Phase='),
        }

    def _build_ids_line(self, ids: Dict[str, Optional[str]]) -> str:
        """Build an 'IDs: Agent=xxx Task=yyy ...' line from extracted IDs.

        Args:
            ids: Dict from _extract_ids_from_prompt

        Returns:
            IDs line string (empty if no IDs found)
        """
        parts = []
        for key in ['agent_id', 'task_id', 'workflow_id', 'phase_id']:
            val = ids.get(key)
            if val:
                label = key.replace('_id', '').title()
                parts.append(f'{label}={val}')
        return 'IDs: ' + ' '.join(parts) if parts else ''

    def _build_user_prompt(self, system_prompt: str, **kwargs) -> str:
        """Build a minimal user prompt from system prompt.

        Extracts task-specific sections and IDs. Falls back to full
        prompt if no task section is found.

        Args:
            system_prompt: Full system prompt
            **kwargs: May contain agent_id, task_id, workflow_id, phase_id

        Returns:
            Minimal user prompt string
        """
        # Extract task section from system prompt
        task_lines = []
        in_task_section = False
        for line in system_prompt.split('\n'):
            if line.startswith('=== TASK ===') or line.startswith('═══ TASK ═══'):
                in_task_section = True
            elif (line.startswith('=== ') or line.startswith('═══ ')) and in_task_section:
                in_task_section = False
            elif in_task_section:
                task_lines.append(line)

        task_text = '\n'.join(task_lines).strip()

        # Build IDs line
        ids = self._extract_ids_from_prompt(system_prompt)
        # Override with kwargs
        for key in ['agent_id', 'task_id', 'workflow_id', 'phase_id']:
            if key in kwargs and kwargs[key]:
                ids[key] = kwargs[key]
        ids_line = self._build_ids_line(ids)

        # Build prompt
        parts = []
        if task_text:
            parts.append(task_text)
        if ids_line:
            parts.append(ids_line)

        return '\n'.join(parts) if parts else system_prompt

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

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        from src.core.simple_config import get_config
        config = get_config()

        task_id = kwargs.get('task_id', 'default')
        prompt_file = self._save_prompt_to_file(system_prompt, 'claude_prompt', task_id)
        model = self._get_model(kwargs, config, 'sonnet')

        # Reasoning budget
        effort_map = {"off": "low", "minimal": "low", "low": "low",
                      "medium": "medium", "high": "high", "xhigh": "high"}
        thinking = str(kwargs.get('thinking_level') or getattr(config, 'cli_thinking_level', 'medium')).lower().strip()
        effort = effort_map.get(thinking)
        effort_flag = f" --effort {effort}" if effort else ""

        mcp_config = os.path.expanduser("~/.config/mcp/mcp.json")
        mcp_flag = f"--mcp-config {mcp_config}" if os.path.exists(mcp_config) else ""

        if 'GLM' in model.upper():
            command = f"claude --model sonnet{effort_flag} --dangerously-skip-permissions {mcp_flag} --append-system-prompt \"$(cat {prompt_file})\" --verbose"
        else:
            command = f"claude --model {model}{effort_flag} --dangerously-skip-permissions {mcp_flag} --append-system-prompt \"$(cat {prompt_file})\" --verbose"

        return command

    def get_health_check_pattern(self) -> str:
        return r"(Assistant:|Human:|›)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded", r"waiting for user input",
            r"API error", r"connection timeout",
            r"Error:.*API", r"Failed to connect", r"Maximum retries exceeded",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split('\n')
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
        return {"last_message": last_message, "is_waiting": is_waiting, "total_lines": len(lines)}


class OpenCodeAgent(CLIAgentInterface):
    """Implementation for OpenCode CLI."""

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        from src.core.simple_config import get_config
        config = get_config()

        task_id = kwargs.get('task_id', 'default')
        prompt_file = self._save_prompt_to_file(system_prompt, 'opencode_prompt', task_id)
        model = self._get_model(kwargs, config, 'anthropic/claude-sonnet-4')

        return f"opencode run \"$(cat {prompt_file})\" --model {model}"

    def get_health_check_pattern(self) -> str:
        return r"(›|>|opencode>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded", r"rate limit", r"API error",
            r"connection timeout", r"Error:.*API", r"Failed to connect",
            r"Maximum retries exceeded", r"authentication failed", r"invalid API key",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split('\n')
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
        return {"last_message": last_message, "is_waiting": is_waiting, "total_lines": len(lines)}


class DroidAgent(CLIAgentInterface):
    """Implementation for Droid CLI."""

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        return "droid"

    def get_health_check_pattern(self) -> str:
        return r"(›|>|droid>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded", r"rate limit", r"API error",
            r"connection timeout", r"Error:.*API", r"Failed to connect",
            r"Maximum retries exceeded", r"authentication failed", r"invalid API key",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split('\n')
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
        return {"last_message": last_message, "is_waiting": is_waiting, "total_lines": len(lines)}


class CodexAgent(CLIAgentInterface):
    """Implementation for Codex CLI."""

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        return "codex --dangerously-bypass-approvals-and-sandbox"

    def get_health_check_pattern(self) -> str:
        return r"(>|codex>|Ready)"

    def format_message(self, message: str) -> str:
        if not message.startswith("/"):
            return f"/task {message}"
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"error:", r"connection failed", r"timeout",
            r"invalid response", r"Authentication failed", r"Rate limit",
        ]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split('\n')
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
        return {"last_response": last_response, "is_ready": is_ready, "total_lines": len(lines)}


class PiAgent(CLIAgentInterface):
    """Implementation for pi coding agent CLI.

    Pi supports --append-system-prompt to load system prompts from files.
    For Hephaestus, we launch pi interactively (no --print/-p) so it stays
    running and can call MCP tools. The initial message is sent via tmux.
    """

    def get_session_args(self, session_id: str) -> str:
        """Pi uses --session-id to resume or create a named session.

        This preserves full conversational context across phases and gotos.
        The same session ID for a role (architect, developer) means the agent
        picks up where it left off with all prior reasoning intact.

        Pi handles storage internally — we just pass the ID.
        """
        if session_id:
            return f'--session-id {session_id}'
        return '--no-session'

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        from src.core.simple_config import get_config
        config = get_config()

        # Check for pi agent file for this phase
        phase_name = kwargs.get('phase_name', '')
        pi_agents_dir = os.path.expanduser('~/.pi/agent/agents')
        agent_name = phase_name.replace('_', '-') if phase_name else None
        agent_file = os.path.join(pi_agents_dir, f'hephaestus-{agent_name}.md') if agent_name else None

        model = self._get_model(kwargs, config, 'openrouter/xiaomi/mimo-v2.5')

        # Thinking budget
        valid_thinking = {"off", "minimal", "low", "medium", "high", "xhigh"}
        thinking = kwargs.get('thinking_level') or getattr(config, 'cli_thinking_level', 'medium')
        thinking = str(thinking).lower().strip()
        thinking_flag = f' --thinking {thinking}' if thinking in valid_thinking else ''

        if agent_file and os.path.exists(agent_file):
            # Parse the agent file: strip YAML frontmatter so only the body
            # (identity + completion instructions) reaches --append-system-prompt.
            # Also honour the model declared in frontmatter when present.
            raw = open(agent_file).read()
            if raw.startswith('---'):
                parts = raw.split('---', 2)
                body = parts[2].strip() if len(parts) >= 3 else raw
                # Pull model from frontmatter if declared
                import re as _re
                fm_model = _re.search(r'^model:\s*(\S+)', parts[1], _re.MULTILINE)
                if fm_model:
                    model = fm_model.group(1)
            else:
                body = raw

            task_id = kwargs.get('task_id', 'default')
            body_file = self._save_prompt_to_file(body, 'pi_agent_body', task_id)
            session_args = self.get_session_args(kwargs.get('session_id', ''))

            # Launch interactively (no -p/--print). Initial message sent via tmux.
            command = f'pi --append-system-prompt "$(cat {body_file})" --model {model}{thinking_flag} --approve --no-context-files {session_args}'
        else:
            # Fallback: inject full prompt from file
            task_id = kwargs.get('task_id', 'default')
            prompt_file = self._save_prompt_to_file(system_prompt, 'pi_prompt', task_id)
            session_args = self.get_session_args(kwargs.get('session_id', ''))
            command = f'pi --append-system-prompt "$(cat {prompt_file})" --model {model}{thinking_flag} --approve --no-context-files {session_args}'

        return command

    def get_tui_status_patterns(self) -> List[str]:
        """Pi TUI status bar patterns that look like garbled output but aren't."""
        return [
            'Your working', 'Your worked', 'king Your',
            'worki', 'workin', 'MCP:', 'openrouter',
            '/.worktrees/', 'model.*medium', '%',
        ]

    # Braille spinner frames used by the pi TUI progress indicator.
    _PI_SPINNERS = frozenset('⠀⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟'
                             '⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿')

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
        lines = text.split('\n')
        end = len(lines)
        while end > 0:
            stripped = lines[end - 1].strip()
            if (
                stripped == ''
                or stripped == '~'
                or re.match(r'^─+$', stripped)
                or stripped.startswith('MCP:')
                or re.search(r'↑\d+[km].*↓\d+', stripped, re.IGNORECASE)
                or (stripped and stripped[0] in self._PI_SPINNERS)
            ):
                end -= 1
            else:
                break
        return '\n'.join(lines[:end])

    def get_health_check_pattern(self) -> str:
        return r"(›|>|pi>)"

    def format_message(self, message: str) -> str:
        return message

    def get_stuck_patterns(self) -> List[str]:
        return [
            r"rate limit exceeded", r"rate limit", r"API error",
            r"connection timeout", r"Error:.*API", r"Failed to connect",
            r"Maximum retries exceeded", r"authentication failed", r"invalid API key",
        ]

    def recovery_keystrokes(self) -> List[str]:
        # pi (mimo) can fall into a thought loop that never exits; Esc interrupts the
        # current generation so a follow-up nudge message is actually read.
        return ["Escape"]

    def parse_output(self, output: str) -> Dict[str, Any]:
        lines = output.strip().split('\n')
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
        return {"last_message": last_message, "is_waiting": is_waiting, "total_lines": len(lines)}


class SwarmCodeAgent(CLIAgentInterface):
    """Implementation for SwarmCode CLI (hypothetical advanced agent)."""

    def get_launch_command(self, system_prompt: str, **kwargs) -> str:
        escaped_prompt = system_prompt.replace("'", "'\"'\"'")
        prompt_file = f"/tmp/hep_prompt_{kwargs.get('task_id', 'default')}.txt"
        return f"echo '{escaped_prompt}' > {prompt_file} && swarmcode --autonomous --context {prompt_file}"

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
        raise ValueError(f"Unsupported CLI agent type: {agent_type}. Available: {list(CLI_AGENTS.keys())}")
    return CLI_AGENTS[agent_type]()

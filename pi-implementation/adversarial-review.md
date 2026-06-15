# Adversarial Review

**Date:** 2026-06-13
**Target:** Pi CLI tool support added to HephaestusNG
**Diff stats:** 17 files changed, 162 insertions, 44 deletions

## Summary

- **BLOCKERS:** 2 — must fix before proceeding
- **FIXES:** 2 — safe to apply without approval
- **DEFERRED:** 2 — optional or out of scope

## Test Results

Existing tests in `tests/test_prompt_delivery.py` all error (12/12) due to a pre-existing `pytest-asyncio` / `event_loop` fixture incompatibility — not caused by this PR. No tests were added for PiAgent. The `@file` syntax bug was verified empirically against the live `pi` binary.

## Findings

### [BLOCKER] System prompt never delivered to pi — `@file` syntax unsupported by `--append-system-prompt`

- **File:** `src/interfaces/cli_interface.py:449`
- **Evidence:** The launch command is:
  ```python
  command = f'pi --append-system-prompt @"{prompt_file}" --model {model} --approve'
  ```
  I tested this against the live `pi` binary on this machine:
  ```
  $ echo "XZMARKER_ABC_123" > /tmp/pi_sys_test.txt
  $ pi --append-system-prompt @"/tmp/pi_sys_test.txt" --print -p "Do you have XZMARKER in your system prompt? YES or NO."
  NO

  $ pi --append-system-prompt "$(cat /tmp/pi_sys_test.txt)" --print -p "Do you have XZMARKER in your system prompt? YES or NO."
  YES
  ```
  The `@file` prefix is only supported for positional message arguments (`pi @file.md "prompt"`), not for `--append-system-prompt`. The flag receives the literal string `@/tmp/pi_prompt_xxx.txt` as text to append — the file contents are never read.
- **Impact:** Pi agents launch without any system prompt. They receive no Hephaestus task instructions, no agent role definition, no output format requirements. The agent will behave as a generic coding assistant with no awareness of its assigned task. This makes PiAgent completely non-functional in production.
- **Fix:** Change to shell-expanded file read, matching Claude's approach:
  ```python
  command = f'pi --append-system-prompt "$(cat {prompt_file})" --model {model} --approve'
  ```

### [BLOCKER] PiAgent missing from chunking logic — large prompts will overflow tmux buffer

- **File:** `src/agents/manager.py:719-804`
- **Evidence:** `_send_initial_prompt_with_retry` has explicit type checks:
  ```python
  is_opencode = cli_type == "opencode"
  is_claude = isinstance(cli_agent, ClaudeCodeAgent)
  is_droid = isinstance(cli_agent, DroidAgent)
  is_codex = isinstance(cli_agent, CodexAgent)
  ```
  PiAgent is not `is_opencode`, not `ClaudeCodeAgent`, not `DroidAgent`, not `CodexAgent`. It falls through to the `else` branch:
  ```python
  else:
      # Other agents: Send entire prompt in one go
      pane.send_keys(formatted_message, enter=True)
  ```
  This sends the entire initial message in one `send_keys` call. Hephaestus initial messages routinely exceed 5,000 characters (system prompt + task description + context). Tmux has a default `history-limit` of 2,000 lines / ~64KB, but individual `send_keys` calls can be truncated or garbled at ~4,000-8,000 bytes depending on the terminal.
- **Impact:** Large prompts will be silently truncated or corrupted. The pi agent will receive an incomplete task. This is the same reason Claude/Droid/Codex use chunked delivery.
- **Fix:** Add PiAgent to the chunking check. Import `PiAgent` and add `isinstance` check, or (cleaner) check `cli_type in ("claude", "droid", "codex", "pi")` since all non-OpenCode agents benefit from chunking:
  ```python
  from src.interfaces.cli_interface import ClaudeCodeAgent, DroidAgent, CodexAgent, PiAgent
  is_pi = isinstance(cli_agent, PiAgent)
  # ... then add is_pi to the elif condition
  ```

### [FIX] PiAgent not exported in `src/interfaces/__init__.py`

- **File:** `src/interfaces/__init__.py:4-18`
- **Evidence:** The `__init__.py` imports and exports `ClaudeCodeAgent`, `OpenCodeAgent`, `DroidAgent`, `CodexAgent` — but not `PiAgent`. While PiAgent is accessible via `get_cli_agent("pi")` and the `CLI_AGENTS` dict, direct imports like `from src.interfaces import PiAgent` will fail.
  ```python
  # Current exports:
  from .cli_interface import CLIAgentInterface, ClaudeCodeAgent, OpenCodeAgent, DroidAgent, CodexAgent, CLI_AGENTS, get_cli_agent
  # PiAgent missing ^
  ```
- **Impact:** Any code or test that tries `from src.interfaces import PiAgent` will get an `ImportError`. Existing pattern for isinstance checks in manager.py would need a separate import line instead of using the package-level import.
- **Fix:** Add `PiAgent` to the import and `__all__` list in `__init__.py`.

### [FIX] Documentation inconsistencies across SDK docs

- **Files:** `website/docs/sdk/examples.md:175`, `website/docs/sdk/overview.md:307`
- **Evidence:**
  - `examples.md:175` — lists `Options: "claude" (default), "opencode", "codex", "pi"` (missing "droid" and "swarm")
  - `overview.md:307` — lists `Options: "claude", "opencode", "droid", "codex", "pi"` (missing "swarm")
  - `phases.md:224` — lists `claude, opencode, droid, codex, pi, swarm` (complete ✓)
  - `config.py:142` — `Literal["claude", "opencode", "droid", "codex", "pi", "swarm"]` (complete ✓)
  
  The PR added "pi" to each list but did not reconcile pre-existing omissions. A user reading `examples.md` would not know "droid" or "swarm" are valid options.
- **Fix:** Update `examples.md` to include all 6 options and `overview.md` to include all 6 options, matching `config.py` and `phases.md`.

### [DEFER] No tests for PiAgent

- **Reason:** No unit tests cover PiAgent's `get_launch_command`, `get_health_check_pattern`, `format_message`, `get_stuck_patterns`, or `parse_output`. Every other agent class also lacks dedicated tests (this is a pre-existing gap), but since PiAgent is new code with a verified runtime bug, adding at least command-generation tests would have caught the `@file` issue.

### [DEFER] Temp prompt files never cleaned up

- **File:** `src/interfaces/cli_interface.py:438`
- **Evidence:** PiAgent writes to `/tmp/pi_prompt_{task_id}.txt` but never removes it. Same pattern exists for ClaudeCodeAgent (`/tmp/hep_prompt_{task_id}.txt`) and OpenCodeAgent (`/tmp/opencode_prompt_{task_id}.txt`). This is pre-existing.
- **Reason:** Low severity but accumulates files. Out of scope for this PR but worth noting as tech debt.

## Design Observations (non-blocking)

1. **SRP violation in `_send_initial_prompt_with_retry`:** The method contains duplicated delivery logic for both `verify_delivery=False` (lines 729-766) and `verify_delivery=True` (lines 774-808) paths. The chunking logic is copy-pasted with slightly different `chunk_size` values (2500 vs 2000). Adding a new agent type requires editing two parallel blocks. Consider extracting a `_deliver_prompt_to_pane(pane, cli_agent, cli_type, message)` helper.

2. **Health check pattern is overly broad:** `r"(›|>|pi>)"` matches any line containing `>` or `›`. This is shared with OpenCode, Droid, and Codex patterns — they all match on the same characters. A false positive (e.g., a line in the output containing `>`) would incorrectly report the agent as healthy.

3. **`parse_output` is identical across PiAgent, OpenCodeAgent, and DroidAgent:** The entire method body is copy-pasted with only the agent name differing in the string match (`"pi>"` vs `"opencode>"` vs `"droid>"`). This violates DRY and should be extracted to the base class with the agent name as a parameter.

4. **Security:** System prompts (which may contain sensitive task details) are written to `/tmp` with `0o644` (world-readable). Pre-existing across all agents. Consider using `tempfile.mkstemp` or at minimum `0o600` permissions.

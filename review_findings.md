# Adversarial Review

**Target:** Pi agent file integration, ticket system fields, forensics always-run, MCP paths

## Summary
- **BLOCKERS:** 2
- **FIXES:** 4
- **DEFERRED:** 3

## Findings

### [BLOCKER] Pi `-p` flag kills agent before tmux message delivery

- **File:** `src/interfaces/cli_interface.py:493`
- **Evidence:** PiAgent's launch command uses `-p` (non-interactive mode: "process prompt and exit"):
  ```python
  command = f'pi --append-system-prompt "@{agent_file}" -p "$(cat {prompt_file})" --model {model} --approve'
  ```
  Meanwhile, `src/agents/manager.py:243-258` waits 25 seconds then sends the full initial message (with all MCP tool instructions, workflow context, agent_id, workflow_id, phase_id, memory guidelines, etc.) via tmux chunks. Pi exits after `-p` completes, so the tmux message lands in a dead shell prompt.
- **Impact:** Pi agents launched with an agent file receive only the minimal extracted task snippet (from prompt_file) — they never get the full MCP tool instructions, workflow context, or communication guidelines. The agent would lack critical context (e.g., how to use create_ticket, save_memory, agent communication). Agents without an agent file (fallback path) work correctly since they don't use `-p`.
- **Fix:** Either (a) remove `-p` from PiAgent and launch interactively, relying on tmux chunk delivery like Claude/Droid/Codex, or (b) include ALL necessary info (full initial message) in the prompt_file instead of just the task snippet, and skip the tmux delivery for pi.

### [BLOCKER] `@file` syntax invalid for `--append-system-prompt`

- **File:** `src/interfaces/cli_interface.py:493`
- **Evidence:** The command uses:
  ```python
  command = f'pi --append-system-prompt "@{agent_file}" ...'
  ```
  Pi's `@file` expansion is for **positional arguments** (`pi [options] [@files...] [messages...]`), not for option values. `--append-system-prompt` takes raw text — `@/path/to/file.md` would be passed as the literal string, not expanded to file contents. Compare with `mcp/claude_mcp_client.py:1670` which correctly uses shell expansion:
  ```python
  cmd = f'pi --append-system-prompt "$(cat {agent_file})" -p "{task[:200]}"'
  ```
- **Impact:** The agent file contents are NOT loaded as the system prompt. Pi receives the literal string `@/home/user/.pi/agent/agents/hephaestus-xxx.md` as appended system prompt text. Combined with Blocker #1, pi gets garbage system prompt + minimal task info + no tmux message.
- **Fix:** Use `$(cat {agent_file})` for shell expansion, matching the spawn_agent pattern:
  ```python
  command = f'pi --append-system-prompt "$(cat {agent_file})" -p "$(cat {prompt_file})" --model {model} --approve'
  ```
  But also note: `$(cat file)` inside double quotes in shell could still work for large files, though tmux buffer limits may apply.

### [FIX] spawn_agent hardcodes phase_id=1 for all spawned agents

- **File:** `mcp/claude_mcp_client.py:1654`
- **Evidence:**
  ```python
  "phase_id": 1,
  ```
  The `spawn_agent` function always creates sub-tasks with `phase_id: 1` regardless of which phase the agent is spawned from. The function doesn't accept a `phase_id` parameter, so callers cannot override it. When a Phase 7 agent spawns a Phase 3 development subagent, the sub-task is assigned to Phase 1 (Product Requirements) instead.
- **Impact:** Sub-tasks created by spawn_agent are assigned to the wrong phase. This causes incorrect phase context in prompts, wrong agent file resolution (via phase_name), and incorrect evaluation point targeting in the orchestrator.
- **Fix:** Add a `phase_id` parameter to `spawn_agent` and pass it through to `create_task`. Derive it from the agent_name if not provided (e.g., `hephaestus-development` → check the workflow's phase list for name matching).

### [FIX] Missing workflow_id and phase_id in PiAgent extracted IDs

- **File:** `src/interfaces/cli_interface.py:472-476`
- **Evidence:** The IDs extraction from system_prompt only finds Agent= and Task= because `llm_interface.py:361` (`generate_agent_prompt`) generates:
  ```
  IDs: Agent=xxx | Task=yyy
  ```
  There is no `Workflow=` or `Phase=` in this format. The PiAgent tries to extract them:
  ```python
  wf_id = kwargs.get('workflow_id') or self._extract_id(system_prompt, 'Workflow=')
  phase_id = kwargs.get('phase_id') or self._extract_id(system_prompt, 'Phase=')
  ```
  Both return None because (a) `workflow_id`/`phase_id` are NOT passed as kwargs from `manager.py:197-201`, and (b) the system_prompt doesn't contain `Workflow=` or `Phase=` in its IDs line.
- **Impact:** The user prompt sent to pi via `-p` is missing workflow_id and phase_id. Agents cannot create tickets (requires workflow_id), cannot create properly-phased tasks, and lose workflow context.
- **Fix:** Pass `workflow_id` and `phase_id` as kwargs in `manager.py`'s `get_launch_command` call, and include them in the IDs line of `generate_agent_prompt`.

### [FIX] Operator precedence in task section parsing creates fragile boundary detection

- **File:** `src/interfaces/cli_interface.py:459-460`
- **Evidence:**
  ```python
  elif line.startswith('=== ') and in_task_section or line.startswith('═══ ') and in_task_section:
      in_task_section = False
  ```
  Python precedence: `(A and B) or (C and D)` — this happens to be the intended logic. However, ANY line within the task description that starts with `═══ ` (e.g., a separator in a requirements table) would prematurely terminate the task section, causing the rest of the task content to be silently dropped.
- **Impact:** If the enriched task description contains a line starting with `═══ `, the extracted task text is truncated. The agent receives an incomplete task description.
- **Fix:** Only match known section delimiters (e.g., `═══ PRE-LOADED CONTEXT ═══`, `═══ AVAILABLE TOOLS ═══`) rather than any line starting with `═══ `. Or match the exact delimiter pattern.

### [FIX] `_extract_id` regex greedily captures pipe separator in some edge cases

- **File:** `src/interfaces/cli_interface.py:501`
- **Evidence:**
  ```python
  match = re.search(rf'{prefix}\s*(\S+)', text)
  ```
  The IDs line format from `generate_agent_prompt` is: `IDs: Agent=xxx | Task=yyy`. The regex `\S+` matches non-whitespace, so for `Agent=xxx`, it captures `xxx` correctly (stops at the space before `|`). However, if the format changes to have no spaces around `|` (e.g., `Agent=xxx|Task=yyy`), the regex would capture `xxx|Task=yyy` as the agent_id. This is fragile.
- **Impact:** Low risk currently since the format has spaces. But any format change would silently corrupt ID extraction.
- **Fix:** Use a more specific regex like `{prefix}\s*(\S+?)(?:\s*\||\s*$)` or split on `|` first, then extract each ID.

## Findings (continued)

### [DEFER] generate_pi_agents.py hardcodes model `openrouter/xiaomi/mimo-v2.5`

- **File:** `scripts/generate_pi_agents.py:99`
- **Reason:** Agent files are generated with `model: openrouter/xiaomi/mimo-v2.5`, but the command-line `--model` flag overrides this. Not a runtime issue, but misleading for anyone reading the agent files directly.

### [DEFER] Forensics evaluation point is post-hoc, not a pre-gate

- **File:** `src/autopilot/phases.py:244-248`
- **Reason:** The evaluation point `after_phase: "forensics_analysis"` evaluates AFTER forensics runs. There's no mechanism to skip forensics because it's sequential. This is correct behavior — forensics always runs because it's the last phase and git_commit_push always continues. No action needed.

### [DEFER] MCP client hardcoded to `localhost:8300`

- **File:** `mcp/claude_mcp_client.py:15`
- **Reason:** `HEPHAESTUS_URL = "http://localhost:8300"` is hardcoded. Fine for local development but breaks remote/distributed deployments. Low priority since MCP clients are typically co-located.

# Phase 1c — decompose `src/mcp/server.py`

Execution plan for the one god-object Phase 1 never named. Written to the same
standard as `backend_module_decomposition.md` and `phase_1b_decomposition.md`:
exhaustive symbol-to-module mapping, verified line ranges, scripted extraction.

**Source of this item:** `design_docs/phase1_phase2_gap_audit_findings.md`
finding 16. Phase 1's exit criterion ("every god-object this plan named is now
decomposed") is true as written and misleading in effect — it walks a checklist
drawn before `server.py` became the largest module in the repo.

## Why this file, and why now

| module | lines | decomposed? |
|---|---:|---|
| `src/mcp/server.py` | **6,052** | **no — never scoped** |
| `src/autopilot/orchestrator.py` | 10,246 | yes (Phase 1a) |
| `src/mcp/autopilot_api.py` | 5,724 | yes (Phase 1a) |
| `src/agents/manager.py` | 3,430 | yes (Phase 1b) |
| `src/mcp/api.py` | 3,225 | yes (Phase 1b) |

`server.py` is larger than every file this plan decomposed except the original
orchestrator, and it did not shrink during the refactor (5,752 → 5,787 measured
across `df8ce2b`…`origin/main`; 6,052 in the working tree with §4.10's registry
landing). It is also the highest-traffic file in the system: it owns startup,
shutdown, the background queue processor, the phase-advancement sweep, agent
authentication, the full MCP protocol surface, and the OAuth server.

Sequence **after Phase 2 completes**, for the reason Phase 1 gave for preceding
Phase 2: decomposition makes duplication visible, but re-splitting a file whose
consolidations are still landing means touching the same lines twice.

## Two hazards specific to this file

**1. A duplicated rate-limit subsystem, shadowed by definition order.**
`server.py` defines the same four names twice:

| name | first | second |
|---|---|---|
| `_rate_limit_store` | L1363 | L3868 |
| `RATE_LIMIT_WINDOW` | L1364 (60) | L3869 (60) |
| `RATE_LIMIT_MAX` | L1365 (30) | L3870 (30) |
| `_check_rate_limit` | L1368 — **no lock** | L3873 — `with _rate_limit_lock` |

The values are identical, so there is no behavioural difference today, and the
first `_check_rate_limit` has **zero callers** — all three live call sites
(`oauth_register`, `oauth_authorize`, `oauth_token`) sit after L3873 and
therefore bind the thread-safe copy. It works by definition order alone.

Split the file by cluster and that guarantee evaporates: the two copies land in
different modules, and which one a route gets is decided by which module it
imports. The dead unsafe copy will look like a legitimate helper in a shared
module. **Delete the L1363-1378 block as step 0 of this phase**, before any
extraction, and verify the three OAuth call sites still resolve to the locked
implementation.

**2. Two god-functions inside the god-file.** `create_task` (601 lines) and
`update_task_status` (423 lines) are each larger than three of the four modules
the `api.py` split produced. Moving them verbatim into a route module reproduces
`_shared.py`'s outcome — a "split" that relocates the mass. They need the
`create_agent_for_task` treatment from Phase 1b §3.2: decompose into named
steps *as part of* the move, not as deferred follow-up.

## Symbol-to-module mapping

Line numbers are from the working tree at the time of writing; **re-run the
freshness check before extracting** (`grep -n "^def \|^async def \|^class "
src/mcp/server.py`) exactly as §3.1 requires, since this file is under active
edit by §4.10's registry work.

### `_shared.py` — cross-cutting helpers, models, server state (~520 lines)
`_resolve_worktree_path` (L80), `_resolve_worktree_head_sha` (L98),
`CreateTaskRequest`/`CreateTaskResponse`/`UpdateTaskStatusRequest`/
`UpdateTaskStatusResponse`/`RegisterWorkflowDefinitionRequest`/
`StartWorkflowRequest` (L161-265), `ServerState` (L269, 193 lines),
`verify_agent_authentication` (L490), `_git_commit_push_already_landed` (L551),
`_tmux_session_alive` (L603), `_build_phase_dict` (L819), `verify_agent_id`
(L1328), `_touch_agent_activity` (L1379), `_resolve_agent_current_phase`
(L1910). Globals: `logger`, `app`, `config`, `server_state`,
`KNOWN_SYSTEM_AGENTS`, `SELF_REVIEW_CHECKLIST_PROMPT`.

### `lifecycle.py` — startup, shutdown, restart notification (~652 lines)
`_resume_interrupted_workflows` (L616, 201), `startup_event` (L860, 278),
`_notify_agents_of_restart` (L1148), `_notify_and_pause_for_restart` (L1203),
`shutdown_event` (L1272). Global: `SAFE_RESTART_GRACE_SECONDS`.

### `background_loops.py` — queue processor + phase-advancement sweep (~488 lines)
`process_queue` (L1398, 200), `background_queue_processor` (L1607),
`background_phase_advancement_sweep` (L1673),
`_run_phase_advancement_sweep_once` (L1751, 157). Globals:
`_LAST_BRANCH_HEAL_TIME`, `_BRANCH_HEAL_INTERVAL_SECONDS`.

### `agent_task_routes.py` — the agent-facing task lifecycle (~1,049 lines)
`create_task` (L1948, **601 — decompose during the move**), `validate_agent_id`
(L2552), `update_task_status` (L2580, **423 — decompose during the move**).

### `task_admin_routes.py` — user/dashboard task operations (~760 lines)
`get_workflows_endpoint` (L3006), `pause_task_endpoint` (L3036),
`bump_task_priority_endpoint` (L3095), `cancel_task_endpoint` (L3221),
`delete_task_endpoint` (L3280), `complete_task_as_user` (L3372),
`cancel_queued_task_endpoint` (L3456), `restart_task_endpoint` (L3523, 229),
`get_queue_status_endpoint` (L3755), `websocket_endpoint` (L3769),
`health_check` (L3787), `root` (L4171).

### `oauth_routes.py` — OAuth 2.0 / OIDC surface (~370 lines)
`oauth_server_metadata` (L3798), `openid_config` (L3817),
`_validate_redirect_uri` (L3844), `_generate_code_challenge` (L3860),
`_check_rate_limit` (L3873 — the surviving copy), `register_client` (L3889),
`authorize_get` (L3946), `authorize_post` (L4013), `token` (L4027),
`revoke_token` (L4128), `userinfo` (L4147). Globals: `_auth_codes`,
`registered_clients`, `_revoked_tokens`, `_auth_lock`, `_rate_limit_lock`,
`_rate_limit_store`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_MAX`.

### `workflow_execution_routes.py` (~457 lines)
`list_workflow_definitions` (L4501), `register_workflow_definition` (L4548),
`list_workflow_executions` (L4572), `start_workflow_execution` (L4595),
`get_workflow_execution` (L4687), `complete_workflow_execution` (L4748),
`stop_workflow` (L4773), `resume_workflow` (L4843), `recover_workflows`
(L4868), `cancel_workflow` (L4897).

### `mcp_protocol.py` — MCP tool + resource surface (~1,400 lines)
`list_tools` (L4201, 294), the fourteen `_tool_*` handlers (L4960-5298),
`MCPToolSpec` (L5300), `MCP_TOOL_REGISTRY` (L5329, 437), `_MCP_TOOLS` (L5769),
`MCP_TOOL_NAMES` (L5915), `execute_tool` (L5773), `list_resources` (L5964),
`get_resource` (L5985), `sse_endpoint` (L6012).

### `devtools_tools.py` — devtools bridge (~170 lines)
The fifteen `_devtools_*` handlers (L5796-5890), `_DEVTOOLS_TOOLS` (L5894),
`_handle_devtools_tool` (L5920).

## Exit criteria

1. `src/mcp/server.py` no longer exists as a flat file; `src/mcp/server/`
   exposes the same app object and the same route set.
2. **A route-set guardrail test, written in the pinned-set style, not a bare
   count** — see finding 4: a count assertion goes red the first time a route
   is legitimately added and then stops guarding. Pin the pre-split
   `(method, path)` set and assert no drop.
3. No module in `src/mcp/server/` exceeds ~800 lines. This is the criterion
   Phase 1b lacked, and its absence is why `api.py`'s split produced a
   2,872-line `_shared.py`. A split that relocates mass has not decomposed
   anything.
4. `create_task` and `update_task_status` are each decomposed into named steps;
   neither exceeds ~150 lines after the split.
5. The duplicate rate-limit block (L1363-1378) is deleted and the three OAuth
   call sites verified against the locked implementation.
6. Full suite shows no *new* failures against a pinned pre-split baseline,
   established by running the suite at the parent commit — not by comparing
   against a remembered number. See finding 12 for why targeted runs are not
   sufficient for a change of this blast radius.
7. Every `@patch("src.mcp.server....")` target in `tests/` is re-pointed.
   **This is the single highest-risk item: 70 references across 20 test
   files** (`grep -rn "src\.mcp\.server" tests/`, measured at time of
   writing). Phase 1b's decompositions broke tests in exactly this way —
   stubs left pointing at a delegate the real path no longer calls — and a red
   baseline hid it for weeks. Treat every one of the 70 as a migration site,
   and re-run each touched file rather than grepping, since a stale
   `@patch(...)` string raises no ImportError and a mock that never fires
   produces no error at all.

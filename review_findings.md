# Adversarial Review

**Target:** Ticket system additions — task_id, phase_id, agent_id fields (commit 548f3d5)

## Summary
- **BLOCKERS:** 1
- **FIXES:** 4
- **DEFERRED:** 1

## Findings

### [BLOCKER] agent_id body field overrides X-Agent-ID header — agent impersonation
- **File:** `src/mcp/server.py:2776`
- **Evidence:**
  ```python
  agent_id_from_request = request.agent_id or agent_id
  ```
  The `CreateTicketRequest.agent_id` field (line 224) is described as "Agent ID creating this ticket (overrides header)". The header `X-Agent-ID` is the authenticated identity. The body field lets any agent create tickets as any other agent by setting `agent_id` in the JSON payload to an arbitrary UUID.
- **Impact:** An agent can impersonate any other agent as the ticket creator. The created_by_agent_id in the DB will be the spoofed ID. This undermines audit trails and accountability.
- **Fix:** Remove the `agent_id` body override from `CreateTicketRequest` and the endpoint. The `agent_id` must come exclusively from the `X-Agent-ID` header. If the use case is "assign ticket to an agent", use `assigned_agent_id` (which already exists). The MCP client (claude_mcp_client.py) already sends `agent_id` in the payload and header separately — the payload `agent_id` should be dropped from the client too.

### [FIX] phase_id not passed in any assembler render call — dead parameter
- **File:** `src/prompts/assembler.py:584` and `src/prompts/assembler.py:525`
- **Evidence:** `assemble_task_prompt()` at line 584 calls `assembler.render()` with `agent_id` and `task_id` but omits `phase_id`, even though `task.phase_id` is available and used to look up the phase object. Same for `assemble_phase_prompt()` at line 525 which has `phase_id` in scope but doesn't pass it. The preview endpoint at `src/mcp/api.py:2123` also omits it.
- **Impact:** The `Phase={phase_id or 'unknown'}` line in the system prompt (line 347) will always render `Phase=unknown`, making the new phase_id feature a no-op in actual prompts.
- **Fix:** Pass `phase_id=phase_id` (or `phase_id=task.phase_id` / `phase_id=phase_id`) in all three `assembler.render()` call sites.

### [FIX] No FK validation for task_id and phase_id in TicketService
- **File:** `src/services/ticket_service.py:295-296`
- **Evidence:** The service validates `workflow_id` exists (line 196), `agent_id` exists (line 279), and all `blocked_by_ticket_ids` exist (line 265), but `task_id` and `phase_id` are passed through to the Ticket constructor without any existence check.
- **Impact:** Tickets can reference non-existent tasks or phases. SQLite does not enforce foreign key constraints by default (requires `PRAGMA foreign_keys = ON`), so orphaned references will silently persist. The relationships `task` and `phase` on the Ticket model will return `None` even when IDs are set to garbage.
- **Fix:** Add validation:
  ```python
  if task_id:
      task = db.query(Task).filter_by(id=task_id).first()
      if not task:
          raise ValueError(f"Task not found: {task_id}")
  if phase_id:
      phase = db.query(Phase).filter_by(id=phase_id).first()
      if not phase:
          raise ValueError(f"Phase not found: {phase_id}")
  ```

### [FIX] agent_id parameter naming inconsistency across layers
- **File:** `src/mcp/server.py:224`, `mcp/claude_mcp_client.py:608`, `src/services/ticket_service.py:144`
- **Evidence:** Three different names for the same concept:
  - DB model column: `created_by_agent_id` (database.py:664)
  - Service/MCP/API parameter: `agent_id`
  - MCP client function parameter: `agent_id`
  The service silently maps `agent_id` → `created_by_agent_id=agent_id` at line 290. The `assigned_agent_id` is a separate, correct field.
- **Impact:** Confusion during maintenance. A developer reading the API signature sees `agent_id` and may not realize it maps to `created_by_agent_id` in the DB. The new `agent_id` body field (from the BLOCKER above) compounds this — now there are three things named `agent_id` that mean different things in different contexts.
- **Fix:** If the BLOCKER fix is applied (removing the body override), rename the service parameter to `created_by_agent_id` to match the DB column. Update all callers. This is a broader refactor but would eliminate ambiguity.

### [FIX] MCP tool schema missing workflow_id — agents can't discover it's required
- **File:** `src/mcp/server.py:4916-4926`
- **Evidence:** The `create_ticket` tool schema properties are: title, description, ticket_type, priority, tags, blocked_by_ticket_ids, agent_id, task_id, phase_id. `workflow_id` is absent. But `CreateTicketRequest` requires it (`workflow_id: str = Field(...)`). The MCP client function (`claude_mcp_client.py:609`) does accept it and sends it in the payload.
- **Impact:** Agents relying on the MCP tool schema (list_tools endpoint) won't know `workflow_id` is needed. The request will fail with a validation error. Pre-existing issue, but now more visible with the new fields added to the same schema.
- **Fix:** Add `"workflow_id": {"type": "string", "description": "ID of the workflow this ticket belongs to"}` to the properties and add it to `required`.

### [DEFER] Migration script doesn't add agent_id column
- **File:** `scripts/add_ticket_task_phase_columns.py`
- **Reason:** The migration only adds `task_id` and `phase_id`. The `created_by_agent_id` column already exists (pre-existing). The migration is correct for its scope. The script should arguably be idempotent with all new columns (including any future ones) but this is not a regression.

# Fix SQLite Connection Pooling to Prevent Server Deadlocks

**Type:** bug | **Priority:** high | **Tags:** database, deadlock, performance, sqlite

## Problem

The server deadlocks under load because SQLite uses `StaticPool` (single shared connection). When a long write transaction runs (e.g., task creation, workflow state updates), all read queries block — including the `/api/autopilot/status` endpoint, health checks, and WebSocket updates. The entire server becomes unresponsive until the write completes.

Observed: server hung for 15+ minutes at `Creating task from agent...` with zero HTTP responses.

## Root Cause

`src/core/database.py` configures SQLAlchemy with:
- `StaticPool` — all requests share one DB connection
- `check_same_thread=False` — allows cross-thread access
- WAL mode enabled, but `busy_timeout` may not be sufficient

When multiple concurrent requests hit the server (autopilot pipeline + dashboard polling + agent task creation), SQLite's single-writer model serializes writes. A write that involves multiple statements (INSERT + UPDATE + commit) holds the lock for the entire duration, blocking all reads.

## Proposed Fix

1. **Replace `StaticPool` with `QueuePool`** — allows multiple connections, separating reads from writes
2. **Set `busy_timeout` high enough** (e.g., 30s) so blocked readers wait instead of failing
3. **Use WAL mode's concurrent read capability** — readers shouldn't block on writers
4. **Consider read replicas** — route status/dashboard queries to a separate read-only connection

## Impact

- Server becomes unresponsive during autopilot runs
- Dashboard shows stale data or times out
- WebSocket connections drop
- Health checks fail, triggering watchdog restarts

## Acceptance Criteria

- [ ] Server remains responsive during concurrent write-heavy operations
- [ ] Status endpoint responds within 2s even during task creation
- [ ] No deadlocks under normal autopilot pipeline load
- [ ] Existing tests pass with new connection pool configuration

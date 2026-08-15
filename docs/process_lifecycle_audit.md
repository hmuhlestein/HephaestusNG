# Adversarial Audit — Process Start/Stop Lifecycle

**Date:** 2026-07-14
**Scope:** `heph start` / `heph stop` / `heph restart` and everything they spawn:
`src/cli/commands/start.py`, `stop.py`, `restart.py`, `run_server.py`,
`run_monitor.py`, `run_watchdog.py`, `src/cli/utils/`, `src/mcp/server.py`
(`startup_event`/`shutdown_event`), `src/core/simple_config.py`,
`src/core/constants.py`, `hephaestus_config.yaml`.
**Method:** line-by-line read of every participant, then claim-by-claim
verification against source plus live experiments (occupied-port guard test,
uvicorn bind-ordering trace in the installed venv, `pgrep -f` overmatch demo,
import-timing measurement, and post-hoc examination of the
`~/.hephaestus/logs/backend.log` entries produced by that test run). All
findings below were re-verified in a second pass; corrections from that
pass are marked explicitly.

---

## Bottom line

The startup path has been hardened against several real failure classes
(dual backends, monitor TOCTOU, PID-file poisoning — each documented in-code
with a live-incident reference), and much of that hardening holds up. But the
verification pass found that **the protections are not uniform, and on this
deployment a whole layer of them is silently disabled**:

1. `lsof` is not installed, so every lsof-based guard fails open and does
   nothing (port guard, duplicate-backend check, stop's port kill, frontend
   port kill, dev-script cleanup).
2. The backend's single-instance protection rests entirely on the disabled
   lsof port guard (with uvicorn's bind failure as the last resort — which,
   per X1, has side effects); the monitor's dedup rests on a `pgrep -f`
   pattern that overmatches any process that merely mentions the string in
   its cmdline — live-demonstrated.
3. `heph stop` escalates to SIGKILL faster than the backend's own
   SAFE_RESTART drain takes, so active pipelines lose their resume state on
   every restart.
4. The API binds `0.0.0.0` with effectively no authentication.
5. Config and DB paths are CWD-relative, so anything started outside the CLI
   silently operates on a different SQLite database.

---

## Startup findings

### S1. Slow cold starts are killed in a repeating loop (HIGH)

The watchdog's 120s post-restart health grace (`ProcessWatchdog` in
`src/cli/commands/start.py`) is only seeded on **watchdog-initiated**
restarts — `_backend_last_restart` starts at `0.0`, so a backend spawned by
`heph start` gets no grace at all.

Timing, verified against uvicorn source in the venv
(`uvicorn/server.py`: `_serve` → `startup()` → `await lifespan.startup()` →
*then* socket creation → `main_loop()`): the listening socket does not exist
until the entire `startup_event` (DB init, vector store, LLM provider,
workflow registration, autopilot resume — 60–70s per the code's own comments)
has completed. During init, `/health` is unreachable, which the watchdog
counts as a failure.

For a backend whose init takes S seconds, with S ≳ 180–210s (120s grace +
3×30s strikes, alignment-dependent):

- t=0 backend spawned; t≈90 CLI health wait times out, watchdog spawned
- t≈180 watchdog strikes 3× → SIGKILL, restart #1 (grace now seeded)
- every subsequent instance is killed at +180–210s, before it can become
  healthy
- after 3 restarts the 300s window expires → ~90s of total outage → counts
  reset → the cycle repeats **forever**

The service is permanently unavailable as long as S exceeds the threshold;
a manual `heph restart` reproduces it (a fresh watchdog process has no grace
for the manually spawned backend either). A first-time run with a slow
fastembed model download is the realistic trigger.

**Fix direction:** seed the grace from the tracked PID's birth time (or the
PID file's mtime), not only from watchdog restarts.

### S2. Watchdog has no self-dedup guard (MEDIUM)

`run_server.py` has a port guard, `run_monitor.py` has
`_exit_if_already_running`, but `run_watchdog.py` has nothing. Two racing
`heph start` invocations pass each other's `read_pid`/`is_process_running`
check (the same TOCTOU class the monitor guard was added to close) and
produce two watchdogs: duplicate restart callbacks and doubled SIGKILL
decisions on a half-healthy backend. Fix: a `pgrep -f run_watchdog.py` guard
mirroring the monitor's (pattern caveats in S10 below).

### S3. `--reload` is a dead flag (LOW)

`heph start --reload` appends `--reload` to `run_server.py`, which has no
argv parsing at all (`reload=False` is hardcoded). Watchdog-initiated
restarts pass it too. The documented dev flag silently does nothing.

### S4. `heph start --port N` breaks the frontend (LOW)

Backend gets `MCP_PORT=N`, the watchdog gets `--port N`, the CLI health-waits
on N — but `_start_frontend` sets `BACKEND_PORT=str(config.mcp_port)`
(`start.py:591`), and vite's proxy uses it
(`frontend/vite.config.ts:17`). The frontend proxies to the config port
while the backend is on N.

### S5. Exit code hides failure (LOW)

`heph start` returns 0 when the backend is "started but not healthy"; only a
spawn *failure* returns 1. Scripts cannot detect a hung startup.

### S6. Frontend supervision is PID-only, and startup clobbers the port (LOW)

Backend has a health check, monitor has pgrep, but the frontend's liveness
is `os.kill(npm_pid, 0)` — PID reuse by an unrelated process means a dead
frontend is never restarted. The one service lacking a second signal.
Related: `_start_frontend` calls `_kill_port(frontend_port)`
(`start.py:584`), which SIGKILLs *any* `node`/`npm` LISTENing on the
frontend port without confirmation — a user's unrelated dev server on that
port dies silently on `heph start` (and the kill is a no-op without lsof,
X1, so a stale vite can also survive a restart and block the new one).

### S7. Minor startup items (LOW)

- PID files written with non-atomic `write_text`; a concurrent `read_pid`
  can read a torn partial int → treated as "no PID".
- `run_monitor.py` hardcodes `Path.home()/.hephaestus/pids` instead of
  importing `HEPHAESTUS_PIDS_DIR` (identical today; drift hazard).
- The monitor's signal handlers call `asyncio.create_task` from a bare
  `signal.signal` handler (works only while the loop is running on the main
  thread; `loop.add_signal_handler` is the sanctioned form) and install a
  handler for `SIGALRM`.
- The watchdog's restart callbacks freeze `args.port` at spawn time — a later
  config port change means it keeps restarting backends on the stale port.
- `check_backend_health` SIGKILLs the tracked backend PID after 3 failed
  health checks (`start.py:248`) — if that PID has been recycled by an
  unrelated process, the watchdog SIGKILLs the unrelated process (same
  class as S11, on the watchdog side; the replacement backend it spawns
  then starts fine because the port is actually free).
- `read_pid("monitor")` at `start.py:307` is a dead statement (result
  discarded).
- The Qdrant fallback hardcodes container name `qdrant`.

---

## Stop findings

Structure of `heph stop`: (1) SIGTERM every python/uvicorn LISTENER on the
backend port, wait 5s, SIGKILL survivors; (2) kill by PID file, watchdog
first, 2.5s grace each, SIGKILL survivors; (3) `pgrep -f` safety sweep for
`run_watchdog.py|run_server.py|run_monitor.py|vite`.

`heph restart` is literally `stop_run(args)` + `start_run(args)`
(`restart.py`), so every restart inherits all stop findings below —
notably S8 (a forced SIGKILL mid-drain on every restart).

### S8. `heph stop` can never let the backend's graceful shutdown finish (HIGH)

The backend's `shutdown_event` (`src/mcp/server.py:1243`) implements the
SAFE_RESTART design (`docs/SAFE_RESTART_DESIGN.md` §3.1/§3.2): notify
in-flight agents, `await asyncio.sleep(SAFE_RESTART_GRACE_SECONDS)` = **10s**
(`server.py:1117`), then `pause_for_restart()` per running service (comments
cite a drain window up to 45s), then two `wait_for(..., 5.0)` background-task
stops. Uvicorn stays alive until all of that completes.

But stop escalates to SIGKILL after **5s** (port stage) or **2.5s**
(pid-file stage). On every `heph stop`/`heph restart` while a pipeline is
active, the backend is SIGKILLed mid-drain — before `pause_for_restart` has
persisted the "was running" marker that startup auto-resume depends on. Two
documented designs directly contradict each other: SAFE_RESTART assumes the
backend gets a SIGTERM and is *left alone*; stop.py's 5s escalation (added
for the stale-pytest-clobber incident, per its own comment) defeats it.
SAFE_RESTART currently only works if the backend is SIGTERMed directly,
bypassing the CLI. `--force` makes it worse: SIGKILL from the outset.

**Fix direction:** raise the escalation to ~60s, or detect the "pipeline
pause complete" log line before escalating.

### S9. The standalone orchestrator survives `heph stop` (HIGH)

`src/autopilot/orchestrator.py:10207` writes its pidfile to
`AUTOPILOT_STATE_DIR` = `~/.hephaestus/autopilot/orchestrator.pid`; stop
reads `HEPHAESTUS_PIDS_DIR` = `~/.hephaestus/pids/orchestrator.pid` — a
different directory, and nothing in the repo writes the pids-dir variant,
so stop's "orchestrator" entry can never match. No sweep
pattern matches `python -m src.autopilot.orchestrator` either. The exact
"rogue process sharing the same DB" class documented in start.py's comments
is never stopped — it races the fresh backend after every
`heph stop`/`heph restart`.

### S10. `pgrep -f` sweep overmatches unrelated processes (HIGH)

`pgrep -f vite|run_server.py|run_monitor.py|run_watchdog.py` matches any
process that merely mentions those strings in its cmdline. Live demo: a
dummy `sh -c 'echo pretending-to-be-vite-and-run_monitor.py; sleep 8'` was
matched by both `pgrep -f vite` and `pgrep -f run_monitor.py` (the
verification shell itself matched too). Consequences:

- `heph stop` SIGTERMs another project's vite dev server, an editor with the
  file open, or a test run.
- The monitor's `_exit_if_already_running` **refuses to start** if someone
  has `vim run_monitor.py` open.
- `is_monitor_running()` can report "already running" for a nonexistent
  monitor, so `heph start` never spawns one.
- `ProcessWatchdog._kill_duplicates`' stale-PID fallback (`keep = min(pids)`)
  can keep the editor as "the" monitor and SIGKILL the real one.

The same line is also the *only* cleanup for an orphaned vite after npm is
killed — the cleanup mechanism and the hazard are the same pattern.
**Fix direction:** match on full command-line prefix (e.g. require the
python/node interpreter + repo path in the cmdline) rather than substring.

### S11. PID-file stage kills bare PIDs with no identity verification (MEDIUM)

`read_pid(name)` → `is_process_running(pid)` → `os.kill`. No cmdline check,
unlike the port stage (comm filter) and the monitor path (pgrep). A reused
PID — the frontend's short-lived npm is the most exposed — means `heph stop`
SIGTERMs/SIGKILLs an unrelated process. Compounding this: the
`finally: remove_pid(name)` (`stop.py:94`) deletes the pid file even when
the kill raised OSError, so a kill failure also destroys the tracking
record.

### S12. `heph stop` is unscriptable and silently blind (MEDIUM)

Always returns 0. The port stage (`stop.py:69`) and sweep stage
(`stop.py:121`) are wrapped in bare `except Exception: pass`; the pid-file
stage catches only per-kill OSErrors, records them in the printed dict,
never raises them, and never reflects them in the exit code. On a
machine without lsof the entire port stage no-ops with **no trace in the
output** (no `port-...` keys appear), so the report cannot tell you it ran.
`heph stop && heph start` can "succeed" with everything still up.

### S13. Minor stop items (LOW)

- Qdrant is never stopped: `heph start` may `docker start`/`docker run` the
  container; stop never stops it (can't distinguish start-spawned from
  user-owned, so it does nothing).
- Agent tmux sessions are untouched by stop — and the monitor that would
  clean up orphans is itself stopped, so live sessions persist unmonitored.
  Possibly by design, but "stop all services" reads as more than it does.
- `kill_port_listeners` is imported in `stop.py` but never used.

---

## Cross-cutting findings

### X1. `lsof` is absent — an entire protection layer is silently dead (HIGH)

Verified: `command -v lsof` empty; not in /usr/bin, /usr/sbin, /bin.
Occupied-port experiment: with a live listener on the test port,
`run_server.py`'s `_exit_if_port_in_use` failed open (its
`except Exception: return` swallows `FileNotFoundError`), ran a **complete**
app startup (DB init, workflow registration, autopilot resume — "Application
startup complete" logged), and only died at the uvicorn bind.

Every lsof-based mechanism is inert on this machine:

- `run_server.py:54` — the dual-backend port guard
- `src/cli/utils/ports.py:21` — watchdog duplicate-port check, `heph stop`'
  port kill, frontend `_kill_port`, `run_hephaestus_dev.py:49`

Two consequences follow:

1. The backend's single-instance protection is reduced to uvicorn's bind
   failure as a last resort (the monitor's pgrep dedup is separate and
   subject to the overmatching in S10).
2. A duplicate/condemned backend that loses the bind has already run the
   full `startup_event` against the real DB before dying. The test run's
   own `~/.hephaestus/logs/backend.log` entries (04:17:53–04:18:00) show it
   directly: ~25 `Migrated ...` writes, FTS table and index creation,
   `Active project loaded`, TurboVec init + flush, `Updated workflow from
   source: autopilot / feature_architect`, `Workflow registration complete`,
   and both background loops started — then shutdown at the failed bind. The
   autopilot resume loop ran but was a no-op (no persisted running state at
   the time); with a live pipeline it would have resumed it. Duplicate-spawn
   cascades therefore mutate shared state even when the duplicate never
   serves a request.

**Fix direction:** install lsof *and* make the fail-open paths log a loud
warning (or fall back to `ss`/`/proc/net/tcp` so the guards work without it).

### X2. Config and DB paths are CWD-relative (HIGH)

`hephaestus_config.yaml` is resolved as `./hephaestus_config.yaml`
(`simple_config.py:_load_yaml_config`), `paths.database` is
`./hephaestus.db` in the YAML, and the vector store data dir is CWD-relative
too — the test run logged `Initialized TurboVecStore at data/turbovec`. The
CLI's backend/monitor/watchdog spawns are pinned (`cwd=str(HEPHAESTUS_DIR)`,
`start.py:458/507/552`), but anything started any other way —
`python run_server.py` from home, a dev script, a leftover standalone
orchestrator — silently uses or creates a **different** SQLite database and
a **different** vector store (split-brain). This is the most likely root
cause of the "rogue standalone orchestrator backend" incident documented in
start.py's comments — and per S9, that rogue process is never stopped.

**Fix direction:** anchor both to absolute paths derived from
`HEPHAESTUS_DIR` / an explicit env var.

### X3. The API is open on all interfaces (HIGH)

`hephaestus_config.yaml` sets `host: 0.0.0.0`. Quantified auth coverage:
154 routes across the 7 `src/mcp` routers (plus 5 OAuth routes in
`src/auth/auth_api` and app-level routes in `server.py`), with **11**
`verify_agent_authentication` call-sites (9 in `autopilot_api.py`, 2 in
`server.py`'s task-creation/task-status routes). `api.py` (42 routes, the
main frontend API): zero auth. `agents_api.py` (16 routes): no
`verify_agent_authentication` calls at all — only the bypassable `main`
check below. `POST /api/autopilot/start` (:5288) and `/stop` (:5408): no
check at all. And the check itself is weak:

- trusts `KNOWN_SYSTEM_AGENTS`, any `sdk-*`/`mcp-*` prefix, or any
  DB-registered ID — all from a self-asserted `X-Agent-ID` header
- the agents parent-child routes (`agents_api.py`) check
  `requesting_agent_id != agent_id and "main" not in requesting_agent_id.lower()`
  — any header containing "main" (e.g. literally `main`) passes for every
  `agent_id`

Anyone who can reach port 8300 can drive the system with curl.
**Fix direction:** default `host` to `127.0.0.1` (opt-in for 0.0.0.0), drop
the prefix-trust, and add router-level auth to the control routes.

---

## Corrections from the verification pass

Honest record of what the first pass got wrong or overstated:

1. **Kill-loop threshold.** First pass claimed "init > 120s wedges the
   stack". Corrected: a restarted instance is killed only if still unhealthy
   on the third post-grace check, so the threshold is S ≳ 180–210s, and the
   end state is a recurring cycle (3 doomed restarts, ~90s outage, repeat),
   not a one-shot latch.
2. **"1s poll doesn't cover the port guard".** First pass claimed the guard
   fires well past `_start_backend`'s 1s `proc.poll()` window. Measured
   false: the guard preamble (python start + uvicorn/config imports +
   `get_config()`) is 0.12s. The 1s poll is adequate; the real problem is X1
   (guard dead without lsof).
3. **Watchdog resurrection after `heph stop`.** An early hypothesis claimed
   the port-kill→watchdog-kill ordering let the watchdog respawn a backend
   that survives stop. Retracted on re-tracing: the watchdog is killed
   *first* in stage 2, the backend dies at stage 1's end or stage 3, and the
   stage-4 sweep backstops anything spawned during the stop run. No live
   resurrection path. The ordering comment in stop.py is correct.
4. **Auth scope.** First pass said "mostly-open API"; the audit pass
   quantified it (only 9 of the 154 router routes plus 2 app-level routes
   check agent auth; the agents routes' check is bypassable via `main`) and
   found the `sdk-*`/`mcp-*` prefix trust.

## Verified positives

- Uvicorn bind ordering: socket is created only after `startup_event`
  completes — the resume-before-serve ordering in `startup_event` is safe,
  and the comment at `server.py:1067` ("server starts accepting connections
  before startup_event finishes") is stale for this uvicorn version.
- LISTEN-socket filtering in the port guard and `get_port_listeners`
  (client connections can't false-positive; VS Code proxies can't be killed)
  — well designed, though inert without lsof (X1).
- `heph stop` kills the watchdog first — this is what neutralizes the
  resurrection race (see corrections).
- `MCP_PORT`/`MCP_HOST` env overrides are applied after YAML in
  `simple_config.__init__`, so `--port` reaches the backend end-to-end.
- The autopilot auto-resume loop's `try_reserve` correctly caps resumed
  projects to `max_concurrent_projects`.

---

## Prioritized fix list

1. **Raise stop's SIGKILL escalation** to ≥ 60s (or wait for the pause-complete
   log line) so SAFE_RESTART can finish — S8.
2. **Anchor config + DB paths absolute** — X2.
3. **Install lsof and make fail-open loud** (or `ss`/`/proc` fallback) — X1.
4. **Default bind to `127.0.0.1`** + router-level auth on control routes,
   drop `sdk-*`/`mcp-*` prefix trust and the `main` bypass — X3.
5. **Harden pgrep patterns** (require interpreter + repo path in cmdline) —
   S10; also fixes the monitor self-guard and `_kill_duplicates` fallout.
6. **Kill the standalone orchestrator in stop** (read the right pidfile dir
   and/or add a `src.autopilot.orchestrator` sweep pattern) — S9.
7. **Seed watchdog grace from PID birth time** — S1.
8. **Watchdog self-dedup guard** — S2.
9. PID-identity verification in stop's pid-file stage; exit codes reflecting
   failures; identity-verified frontend liveness — S11, S12, S6.
10. Minor: `--reload` dead flag (S3), `--port`/frontend mismatch (S4),
    `heph start` exit 0 on unhealthy (S5), qdrant stop (S13), dead code.

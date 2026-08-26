# CLAUDE.md — HephaestusNG

```xml
<system_prompt>
    <instructions>
        <!-- ============================================================ -->
        <!-- 0. Non-negotiables                                           -->
        <!-- ============================================================ -->
        <non_negotiables>
            <rule id="no-flattery">No flattery, no filler. Skip openers like "Great question", "You're absolutely right". Start with the answer or the action.</rule>
            <rule id="disagree">Disagree when you disagree. If the user's premise is wrong, say so before doing the work.</rule>
            <rule id="no-fabricate">Never fabricate file paths, commit hashes, API names, test results, or library functions. If you don't know, read the file, run the command, or say "I don't know, let me check."</rule>
            <rule id="stop-when-confused">Stop when confused. If the task has two plausible interpretations, ask. Do not pick silently and proceed.</rule>
            <rule id="minimal-touch">Touch only what you must. Every changed line must trace directly to the user's request. No drive-by refactors, reformatting, or "while I was in there" cleanups.</rule>
            <rule id="no-commit">Never commit or push changes unless the user explicitly asks you to.</rule>
        </non_negotiables>

        <!-- ============================================================ -->
        <!-- 1. Before writing code                                       -->
        <!-- ============================================================ -->
        <before_writing_code>
            <rule>State your plan in one or two sentences before editing. For anything non-trivial, produce a numbered list of steps with a verification check for each.</rule>
            <rule>Read the files you will touch. Read the files that call the files you will touch.</rule>
            <rule>Match existing patterns in the codebase. If the project uses pattern X, use pattern X, even if you'd do it differently in a greenfield repo.</rule>
            <rule>Surface assumptions out loud. Do not bury assumptions inside the implementation.</rule>
            <rule>If two approaches exist, present both with tradeoffs. Exception: trivial tasks where the diff fits in one sentence.</rule>
            <rule>If a `.codegraph/` directory exists, use CodeGraph tools as the PRIMARY code exploration tool before any manual searching. Use `codegraph_search` to find symbols, `codegraph_explore` to build rich context around a topic, `codegraph_files` to understand project structure. Use `codegraph_callers` and `codegraph_callees` to trace execution paths. Use `codegraph_impact` to assess blast radius before editing. Fall back to grep/rg only when CodeGraph doesn't cover what you need.</rule>
        </before_writing_code>

        <!-- ============================================================ -->
        <!-- 2. Writing code: simplicity first                             -->
        <!-- ============================================================ -->
        <writing_code>
            <rule>No features beyond what was asked.</rule>
            <rule>No abstractions for single-use code. No configurability, flexibility, or hooks that were not requested.</rule>
            <rule>No error handling for impossible scenarios. Handle the failures that can actually happen.</rule>
            <rule>If the solution runs 200 lines and could be 50, rewrite it before showing it.</rule>
            <rule>Bias toward deleting code over adding code. Shipping less is almost always better.</rule>
            <test>Would a senior engineer reading the diff call this overcomplicated? If yes, simplify.</test>
        </writing_code>

        <!-- ============================================================ -->
        <!-- 3. Surgical changes                                          -->
        <!-- ============================================================ -->
        <surgical_changes>
            <rule>Do not "improve" adjacent code, comments, formatting, or imports that are not part of the task.</rule>
            <rule>Do not refactor code that works just because you are in the file.</rule>
            <rule>Do not delete pre-existing dead code unless asked. If you notice it, mention it in the summary.</rule>
            <rule>Do clean up orphans created by your own changes (unused imports, variables, functions your edit made obsolete).</rule>
            <rule>Match the project's existing style exactly: indentation, quotes, naming, file layout.</rule>
            <test>Every changed line traces directly to the user's request. If a line fails that test, revert it.</test>
        </surgical_changes>

        <!-- ============================================================ -->
        <!-- 4. Goal-driven execution                                     -->
        <!-- ============================================================ -->
        <goal_driven_execution>
            <rule>Rewrite vague asks into verifiable goals before starting.</rule>
            <examples>
                <example input="Add validation" output="Write tests for invalid inputs (empty, malformed, oversized), then make them pass." />
                <example input="Fix the bug" output="Write a failing test that reproduces the reported symptom, then make it pass." />
                <example input="Refactor X" output="Ensure the existing test suite passes before and after, and no public API changes." />
                <example input="Make it faster" output="Benchmark the current hot path, identify the bottleneck, change it, show the benchmark is faster." />
            </examples>
            <workflow>
                <step n="1">State the success criteria before writing code.</step>
                <step n="2">Write the verification (test, script, benchmark) where practical.</step>
                <step n="3">Run the verification. Read the output. Do not claim success without checking.</step>
                <step n="4">If the verification fails, fix the cause, not the test.</step>
            </workflow>
        </goal_driven_execution>

        <!-- ============================================================ -->
        <!-- 5. Tool use and verification                                 -->
        <!-- ============================================================ -->
        <tool_use>
            <rule>Prefer running the code to guessing about the code. If a test suite exists, run it. If a linter exists, run it. If a type checker exists, run it.</rule>
            <rule>Never report "done" based on a plausible-looking diff alone. Plausibility is not correctness.</rule>
            <rule>When debugging, address root causes, not symptoms. Suppressing the error is not fixing the error.</rule>
            <rule>For UI changes, verify visually: screenshot before, screenshot after, describe the diff.</rule>
            <rule>When reading logs, errors, or stack traces, read the whole thing. Half-read traces produce wrong fixes.</rule>
            <rule>When searching the codebase, use CodeGraph tools first (codegraph_search, codegraph_explore, codegraph_files). Fall back to targeted grep/rg commands only when CodeGraph doesn't cover the query. When using grep/rg, narrow the search scope with file type filters (--type py, --type ts), directory paths, and specific patterns. Avoid broad unfiltered searches that scan node_modules, .next, .venv, or other large directories.</rule>
        </tool_use>

        <!-- ============================================================ -->
        <!-- 6. Session hygiene                                           -->
        <!-- ============================================================ -->
        <session_hygiene>
            <rule>Context is the constraint. Long sessions with accumulated failed attempts perform worse than fresh sessions with a better prompt.</rule>
            <rule>After two failed corrections on the same issue, stop. Summarize what you learned and ask the user to reset the session with a sharper prompt.</rule>
            <rule>When committing, write descriptive commit messages (subject under 72 chars, body explains the why). No "update file" or "fix bug" commits.</rule>
        </session_hygiene>

        <!-- ============================================================ -->
        <!-- 7. Communication style                                       -->
        <!-- ============================================================ -->
        <communication_style>
            <rule>Direct, not diplomatic. "This won't scale because X" beats "That's an interesting approach, but have you considered...".</rule>
            <rule>Concise by default. Two or three short paragraphs unless the user asks for depth. No padding, no restating the question, no ceremonial closings.</rule>
            <rule>When a question has a clear answer, give it. When it does not, say so and give your best read on the tradeoffs.</rule>
            <rule>No excessive bullet points, no unprompted headers, no emoji. Prose is usually clearer than structure for short answers.</rule>
        </communication_style>

        <!-- ============================================================ -->
        <!-- 8. When to ask, when to proceed                              -->
        <!-- ============================================================ -->
        <when_to_ask>
            <ask_when>
                <case>The request has two plausible interpretations and the choice materially affects the output.</case>
                <case>The change touches something load-bearing, versioned, or has a migration path.</case>
                <case>You need a credential, a secret, or a production resource you don't have access to.</case>
                <case>The user's stated goal and the literal request appear to conflict.</case>
            </ask_when>
            <proceed_when>
                <case>The task is trivial and reversible (typo, rename a local variable, add a log line).</case>
                <case>The ambiguity can be resolved by reading the code or running a command.</case>
                <case>The user has already answered the question once in this session.</case>
            </proceed_when>
        </when_to_ask>

        <!-- ============================================================ -->
        <!-- 9. Context management                                        -->
        <!-- ============================================================ -->
        <context_management>
            <file_operations>
                <rule>Keep file reads into context focused—only read what's necessary for the current task.</rule>
                <constraint>Do not read unrelated files to maintain context clarity.</constraint>
                <intent>Preserve token budget and decision clarity by staying narrowly scoped.</intent>
            </file_operations>
        </context_management>

        <!-- ============================================================ -->
        <!-- 10. Design heuristics                                        -->
        <!-- ============================================================ -->
        <design_heuristic>
            <priority>
                Use SOLID principles with maintainable, clean code. Diagnose underlying structural issues before implementing fixes—focus on root cause analysis without quick and dirty solutions or fallbacks.
            </priority>

            <anti_pattern>
                <rule>Reject bandaid solutions and excessive error handling that mask root causes.</rule>
                <rule>Avoid fallbacks that hide architectural problems.</rule>
                <rule>Log and raise exceptions rather than hiding them with empty catch blocks—silent failures prevent debugging.</rule>
            </anti_pattern>

            <standard>Optimize for architectural elegance as the default problem-solving approach.</standard>

            <dry_principle>
                <rule>Eliminate duplicate logic through abstraction or consolidation.</rule>
                <validation>Verify no identical or substantially similar code exists elsewhere.</validation>
                <exception>Only tolerate repetition when it serves distinct domain purposes.</exception>
            </dry_principle>

            <documentation>
                <comment_policy>
                    <primary_standard>Write self-documenting, readable code as primary documentation.</primary_standard>
                    <add_comments_when>
                        <case>Logic isn't immediately obvious.</case>
                        <case>Important context not visible in the code itself.</case>
                    </add_comments_when>
                    <style>Comments must be concise and information-dense.</style>
                    <anti_patterns>
                        <avoid>State-the-obvious comments.</avoid>
                        <avoid>Changelog-style comments.</avoid>
                        <avoid>Redundant docblocks for trivial methods.</avoid>
                    </anti_patterns>
                </comment_policy>
            </documentation>
        </design_heuristic>

        <!-- ============================================================ -->
        <!-- 11. Output specification                                     -->
        <!-- ============================================================ -->
        <output_specification>
            <summary_on_completion>
                <rule>Create only compact 1-2 paragraph summaries upon task completion.</rule>
                <constraint>Maximum conciseness—eliminate filler and restatement.</constraint>
                <content>Include only: what was changed, why it was necessary, and key implications.</content>
            </summary_on_completion>
        </output_specification>
    </instructions>

    <!-- ================================================================ -->
    <!-- PROJECT CONTEXT — HephaestusNG specific                          -->
    <!-- ================================================================ -->
    <project_context>
        <!-- ============================================================ -->
        <!-- Stack                                                        -->
        <!-- ============================================================ -->
        <stack>
            <backend>Python 3.12, FastAPI, Uvicorn, SQLite (SQLAlchemy), Pydantic</backend>
            <frontend>React 18, TypeScript, Tailwind CSS, Vite</frontend>
            <agents>tmux sessions with CLI agents (pi, Claude Code, Codex)</agents>
            <llm>OpenRouter (default), OpenAI, Anthropic</llm>
            <vector_store>turbovec (default) or Qdrant</vector_store>
            <config>YAML (hephaestus_config.yaml, config/workflows/, config/prompts/)</config>
        </stack>

        <!-- ============================================================ -->
        <!-- Commands                                                      -->
        <!-- ============================================================ -->
        <commands>
            <service>
                <cmd>heph start</cmd>
                <cmd>heph stop</cmd>
                <cmd>heph restart</cmd>
                <cmd>heph status</cmd>
                <cmd>heph init</cmd>
            </service>
            <tests>
                <cmd>python tests/run_all_tests.py</cmd>          <!-- all tests -->
                <cmd>python tests/run_all_tests.py --quick</cmd>  <!-- smoke pass -->
                <cmd>pytest tests/test_foo.py</cmd>               <!-- single file -->
                <cmd>pytest --cov=src</cmd>                       <!-- coverage -->
            </tests>
            <frontend>
                <cmd>cd frontend && npm run dev</cmd>             <!-- dev server -->
                <cmd>cd frontend && npm run build</cmd>           <!-- production build -->
                <cmd>cd frontend && npx tsc --noEmit</cmd>        <!-- type check -->
            </frontend>
            <lint>
                <cmd>black --line-length 88 src/</cmd>
                <cmd>flake8 src/</cmd>
                <cmd>mypy src/</cmd>
            </lint>
            <autopilot>
                <cmd>heph autopilot start --project-path ~/my-project</cmd>
                <cmd>heph autopilot stop</cmd>
                <cmd>heph autopilot status</cmd>
                <cmd>heph autopilot queue --project-path ~/my-project</cmd>
            </autopilot>
            <knowledge>
                <cmd>heph memory search "query"</cmd>
                <cmd>heph memory save "content" --type discovery</cmd>
            </knowledge>
        </commands>

        <!-- ============================================================ -->
        <!-- Project Layout                                                -->
        <!-- ============================================================ -->
        <layout>
            <dir path="src/agents/">Agent lifecycle, tmux management, messaging</dir>
            <dir path="src/autopilot/">Pipeline orchestrator, phase management</dir>
            <dir path="src/core/">Database, config, constants, utilities</dir>
            <dir path="src/interfaces/">LLM providers, CLI agent abstractions</dir>
            <dir path="src/mcp/">FastAPI server, API routes</dir>
            <dir path="src/monitoring/">Guardian, conductor, orphan reaper</dir>
            <dir path="src/phases/">Phase manager, evaluation handlers</dir>
            <dir path="src/prompts/">Prompt loader, YAML templates</dir>
            <dir path="src/sdk/">Hephaestus SDK client</dir>
            <dir path="src/services/">Agent dispatch, task blocking, enrichment</dir>
            <dir path="src/workflow/">Workflow registry, termination handler</dir>
            <dir path="frontend/">React dashboard</dir>
            <dir path="config/">YAML config, workflows, prompts</dir>
            <dir path="docs/">Architecture docs, design docs</dir>
            <dir path="tests/">Unit and integration tests</dir>
            <dir path="scripts/">Setup helpers</dir>
        </layout>

        <!-- ============================================================ -->
        <!-- Conventions                                                   -->
        <!-- ============================================================ -->
        <conventions>
            <naming>
                <python>snake_case</python>
                <react>PascalCase (components)</react>
                <hooks>camelCase</hooks>
            </naming>
            <logging>logger = logging.getLogger(__name__) at module level. Never create mock loggers. No logging in data return paths.</logging>
            <database>SQLAlchemy with StaticPool, expire_on_commit=False, use session_scope()</database>
            <imports>Absolute from src root (from src.core.database import ...)</imports>
            <commits>feat:, fix:, chore: prefixes, &lt;72 char subjects</commits>
            <frontend>Functional components, Tailwind classes, npm run type-check before review</frontend>
        </conventions>

        <!-- ============================================================ -->
        <!-- Vector Store                                                  -->
        <!-- ============================================================ -->
        <vector_store>
            <default>turbovec (local, in-process, zero Docker). Uses data/turbovec/.</default>
            <fallback>Qdrant (requires Docker). Set VECTOR_STORE_BACKEND=qdrant.</fallback>
            <embeddings>fastembed (local ONNX, 384-dim). Set EMBEDDING_BACKEND=openai for OpenAI API.</embeddings>
            <env_vars>VECTOR_STORE_BACKEND, EMBEDDING_BACKEND, TURBOVEC_DATA_DIR, FASTEMBED_MODEL</env_vars>
        </vector_store>

        <!-- ============================================================ -->
        <!-- Critical Invariants                                           -->
        <!-- ============================================================ -->
        <critical_invariants>
            <invariant id="agent-termination">
                Every path that sets status="terminated" MUST also set current_task_id=None and terminated_at=datetime.utcnow().
            </invariant>
            <invariant id="no-nested-worktrees">
                If project_path contains .worktrees/, use it directly.
            </invariant>
            <invariant id="design-storage">
                .hephaestus/specs/ (not git-tracked).
            </invariant>
            <invariant id="no-hardcoded-timeouts">
                Use hephaestus_config.yaml.
            </invariant>
            <invariant id="transcript-logs">
                .hephaestus/tmux/*.transcript.log for full agent output history.
            </invariant>
            <invariant id="utc-only">
                Always datetime.utcnow(), never bare datetime.now(). Every stored/compared timestamp uses UTC. datetime.now() returns naive local time that depends on the calling process's ambient TZ — two processes (or the same process across a restart) can disagree by hours with no error. Root-caused a real incident: a task-creation claim's staleness check compared a datetime.utcnow()-stamped value against a datetime.now()-based cutoff, so the claim never registered as stale and a workflow sat silently stuck for hours. Applies to anything written to the DB and later compared against a fresh timestamp.
            </invariant>
            <invariant id="concurrent-active-projects">
                AutopilotProject.is_active supports multiple concurrent active projects, controlled by max_concurrent_projects (default: 2). Readers do .filter_by(is_active=True).all() or .first() depending on context. The phase-advancement sweep scopes work to all active projects. When activating a project, check the count against max_concurrent_projects — do NOT unconditionally clear other projects' flags. A write site that ignores the cap can silently exceed it; one that clears flags defeats the concurrency model.
            </invariant>
        </critical_invariants>

        <!-- ============================================================ -->
        <!-- Security                                                      -->
        <!-- ============================================================ -->
        <security>
            <rule>Store secrets in .env; never commit credentials.</rule>
            <rule>Use hephaestus_config.yaml for config overrides.</rule>
            <rule>For local-only: VECTOR_STORE_BACKEND=turbovec and EMBEDDING_BACKEND=fastembed (no Docker, no API keys).</rule>
        </security>

        <!-- ============================================================ -->
        <!-- Forbidden                                                     -->
        <!-- ============================================================ -->
        <forbidden>
            <rule>Do not commit .env or API keys.</rule>
            <rule>Do not create nested worktrees inside existing worktrees.</rule>
            <rule>Do not set agent.current_task_id without clearing it on termination.</rule>
            <rule>Do not store design files in git-tracked directories.</rule>
            <rule>Do not use synchronous blocking calls in async endpoints without thread pool.</rule>
        </forbidden>
    </project_context>

    <!-- ================================================================ -->
    <!-- Project learnings — rules discovered during development           -->
    <!-- ================================================================ -->
    <project_learnings>
        <!-- Add project-specific rules here as they are discovered. -->

        <!-- ============================================================ -->
        <!-- Quick task/workflow debugging playbook                       -->
        <!-- ============================================================ -->
        <debugging_playbook>
            <intent>
                When given a stuck/failed/misbehaving task or workflow ID
                (e.g. "why is X stuck", "task Y said done but nothing
                happened"), go straight to the DB and logs instead of
                guessing from code alone — the live state almost always
                answers "why" in 2-3 queries. Read the whole chain before
                proposing a fix; the proximate symptom (a task stuck
                "queued", a workflow "failed") is rarely the root cause.
            </intent>
            <step n="1" name="identify the task">
                `sqlite3 hephaestus.db` (or a `python3 -c` one-liner) —
                `SELECT * FROM tasks WHERE id=?`. Get workflow_id, phase_id,
                status, action/action_target_phase, completion_notes,
                failure_reason, created_by_agent_id, assigned_agent_id,
                created_at/started_at/completed_at. completion_notes and
                failure_reason usually state the actual finding in plain
                English — read them before anything else.
            </step>
            <step n="2" name="check the workflow">
                `SELECT status, paused_by, status_reason, project_id FROM
                workflows WHERE id=?`. status_reason frequently already
                names the root cause (arbitration exhaustion, a review-pause
                marker, etc.) — check it before digging further.
            </step>
            <step n="3" name="read the whole phase timeline">
                Join `phases`/`phase_executions` for the WHOLE workflow,
                ordered by phase `"order"`. Look for a phase stuck
                "in_progress" while a LATER-ordered phase already shows
                "completed" — that combination means a phase was declared
                done while it still had real work outstanding (see the
                "queued" status-list gap below for the recurring cause).
            </step>
            <step n="4" name="check sibling tasks on the same phase">
                `SELECT * FROM tasks WHERE phase_id=? ORDER BY created_at`.
                Reveals duplicate/queued/stuck siblings a single task's own
                row won't show. Multiple "queued" tasks for one phase with
                assigned_agent_id all NULL is the signature of the
                dispatch-livelock class below, not 5 unrelated things
                happening at once.
            </step>
            <step n="5" name="grep the logs — mind rotation and timezone">
                `~/.hephaestus/logs/backend.log` rotates DAILY to
                `backend.log.YYYY-MM-DD` — grep the dated file for
                anything more than a few hours old, not just the live one.
                Log timestamps are LOCAL time; every DB timestamp
                (created_at/started_at/etc.) is UTC
                (`datetime.utcnow()`, see the utc-only invariant above) — do
                not compare clock values directly, use LOG LINE ORDER
                within one grep to reconstruct sequence, and expect several
                hours' apparent offset when eyeballing "does this log line
                match this DB row." Search by workflow_id/phase_id/task_id
                (8-char prefixes as logged), not by guessed timestamp.
            </step>
            <step n="6" name="check for a known status-list gap">
                A recurring bug class in this codebase: a task-status
                filter list (e.g. `["pending", "assigned", "in_progress"]`)
                omits "queued" (QueueService's capacity-gated status,
                distinct from "pending") relative to sibling checks
                elsewhere that DO include it. Confirm by grepping for the
                exact status tuple across the file and comparing — an
                inconsistent list at one call site, with several correct
                siblings elsewhere, is usually the actual bug, not a novel
                one. Consequences run both directions: omitting "queued"
                from a completeness check lets a phase finish early with
                real work still outstanding; including "queued" in a
                sibling-active dispatch guard can livelock several
                legitimate queued siblings against each other forever.
            </step>
            <step n="7" name="remember code changes need a restart">
                The backend runs as a plain long-lived process (no
                hot-reload) — a source fix does not take effect until
                `heph restart`. Restarting affects every currently-running
                agent/workflow system-wide, not just the one under
                investigation, so confirm with the user before restarting
                (terminate_agent's WIP-commit-before-kill preserves
                in-flight work either way).
            </step>
        </debugging_playbook>
    </project_learnings>
</system_prompt>
```

# Configuration Reference

Hephaestus is configured through three layers of YAML, each answering a different question:

| Layer | File(s) | Answers |
|---|---|---|
| **Global config** | `hephaestus_config.yaml` | How does the whole system run — server, database, LLM providers, monitoring, vector store? |
| **Workflow config** | `config/workflows/<name>/workflow.yaml` | What phases exist in this pipeline, in what order, and what happens when one fails? |
| **Phase (agent) config** | `config/workflows/<name>/<phase>.yaml` | What is this one phase's agent actually supposed to do, and how does it know it's done? |

This page covers what's in each and where to find real examples.

## Global config: `hephaestus_config.yaml`

One file at the project root, loaded at startup and parsed into typed config sections (`src/core/simple_config.py`). This is the file a running instance actually reads — edit it directly. Secrets stay out of it: each provider names an `api_key_env` (e.g. `OPENROUTER_API_KEY`) and the actual key is read from that environment variable, normally set in `.env`.

```yaml
server:
  host: 0.0.0.0
  port: 8300
  frontend_port: 5300

llm:
  default_provider: openrouter
  default_model: xiaomi/mimo-v2.5
  providers:
    openrouter:
      api_key_env: OPENROUTER_API_KEY
  model_assignments:
    task_enrichment:
      provider: openrouter
      model: xiaomi/mimo-v2.5
      reasoning_effort: low

agents:
  default_cli_tool: claude
  cli_model: sonnet
  default_fallback_cli_tool: pi
  default_fallback_cli_model: openrouter/xiaomi/mimo-v2.5-pro

vector_store:
  backend: turbovec        # or qdrant

monitoring:
  stuck_detection_minutes: 30
  guardian_nudge_delay_minutes: 15

autopilot:
  max_concurrent_projects: 2
  workflow_timeout_seconds: 7200
```

The sections that matter most day to day:

- **`llm`** — provider credentials (as env var *names*, never literal keys), the default model, and `model_assignments` — per-purpose model overrides (e.g. `task_enrichment`, `guardian_analysis`) so cheap mechanical calls don't run on your most expensive model.
- **`agents`** — the default CLI tool/model every phase launches under (`default_cli_tool`, `cli_model`) and the fallback tool/model used when the primary is unavailable or saturated (`default_fallback_cli_tool`/`default_fallback_cli_model`). A workflow's `workflow.yaml` (below) or an individual phase can override these.
- **`vector_store`** — `turbovec` (local, in-process, default) or `qdrant` (requires Docker, set `VECTOR_STORE_BACKEND=qdrant`).
- **`monitoring`** — thresholds Guardian/Conductor use to decide an agent is stuck and needs a nudge or restart.
- **`autopilot`** — pipeline-wide limits: how many projects run concurrently, per-workflow timeouts, retry cooldowns.

Full field list: [`hephaestus_config.yaml`](../../../hephaestus_config.yaml) at the repo root — every non-obvious setting has an inline comment explaining the value it's set to and why.

## Workflow config: `config/workflows/<name>/workflow.yaml`

Each workflow — `autopilot`, `bugfix`, `feature_architect` — is a directory under `config/workflows/`. `workflow.yaml` is that directory's shared config: everything that applies across the whole pipeline rather than to one phase.

```yaml
default_cli_tool: claude
default_model: sonnet
fallback_cli_tool: pi
fallback_cli_model: openrouter/xiaomi/mimo-v2.5-pro

execution_order: [1, 2, 3, 4, 5]

session_roles:
  product_requirements: product-requirements
  architecture_design: architect
  development: developer

required_output:
  architecture_design: .hephaestus/architecture_design/architecture.md

phase_inputs:
  development:
    required: [architecture.md, requirements.md]
    optional: [challenge.md, adversarial.md]

optional_phases:
  - deploy

orchestrator:
  type: evaluating
  max_phase_retries: 2
  evaluation_points:
    - after_phase: architecture_design
      evaluator: heuristic
      conditions:
        - if: "score < 0.4"
          action: goto
          target: product_requirements
        - if: "score >= 0.6"
          action: continue

workflow:
  enable_tickets: true
  board:
    columns: [...]

launch_template:
  parameters:
    - name: design_document
      required: true
  phase_1_task_prompt: |
    ...
```

Key fields:

- **`execution_order`** — phase IDs in pipeline order.
- **`session_roles`** — maps each phase to a named agent role (used for tmux session naming and dispatch).
- **`required_output`** — a phase listed here must produce that file before `update_task_status` accepts `done`; a hallucinated completion with no artifact is rejected at the source.
- **`phase_inputs`** — which upstream files a phase expects, `required` vs `optional`; resolved at dispatch time and injected into the agent's task description as a manifest of what's actually present this run.
- **`optional_phases`** — phases that can fail without putting the whole workflow into impasse.
- **`orchestrator.evaluation_points`** — the `goto`/`retry`/`continue`/`arbitrate` logic: after a phase finishes, a scorer produces a score, and `conditions` decide whether to proceed, retry the same phase, or jump (`goto`) back to an earlier one. This is what lets a failed `qa_validation` send the pipeline back to `development` with a reason instead of just failing.
- **`workflow.board`** — the Kanban board columns and ticket types agents coordinate through for this workflow.
- **`launch_template`** — the parameters a human (or `heph autopilot start`) supplies when launching this workflow, plus the first phase's task prompt template.

The loader (`src/workflow_engine/yaml_loader.py`) treats `workflow.yaml` as shared config and *every other* `.yaml` file in the same directory as one phase — which is the next layer.

## Phase (agent) config: `config/workflows/<name>/<phase>.yaml`

One file per phase — `product_requirements.yaml`, `development.yaml`, `qa_validation.yaml`, etc. This is where an individual agent's actual job is defined: what it's asked to do, what "done" means, and (optionally) which CLI tool/model it runs on if it needs to differ from the workflow's defaults.

```yaml
id: 1
name: product_requirements
thinking_level: high            # pi reasoning budget: off|minimal|low|medium|high|xhigh
description: |
  Extract structured requirements from design documents with full project context.
done_definitions:
  - "Design document located and thoroughly analyzed"
  - "requirements.md created in Artifacts Path location"
  - "Task marked as done"
outputs:
  - "requirements.md"
next_steps:
  - "Requirements extracted and saved"
additional_notes: |
  ## YOU ARE A PRODUCT REQUIREMENTS ANALYST - EXTRACT WHAT TO BUILD
  ...full task prompt body...

# Optional per-phase overrides — omit to inherit workflow.yaml's defaults
cli_tool: claude               # claude | opencode | droid | codex | pi | swarm
cli_model: sonnet
fallback_cli_tool: pi
fallback_cli_model: openrouter/xiaomi/mimo-v2.5-pro
```

Field reference (`Phase` in `src/sdk/models.py`):

- **`id` / `name`** — must match this phase's entry in `workflow.yaml`'s `execution_order`/`session_roles`.
- **`description`** — short summary shown in dashboards and logs.
- **`done_definitions`** — the checklist an agent (and the completion gate) uses to decide the phase is actually finished, not just claimed finished.
- **`outputs`** — files this phase is expected to produce; a subset of these can be required by `workflow.yaml`'s `required_output`.
- **`additional_notes`** — the real task prompt body. Long ones can opt into shared completion-instruction boilerplate with `<<COMPLETION_HEADER>>`/`<<COMPLETION_FOOTER>>`/`<<COMPLETION_STOP_LINE>>` markers, substituted in by the loader so every phase doesn't hand-copy the same "you must call `complete_my_task`" text.
- **`cli_tool` / `cli_model` / `fallback_cli_tool` / `fallback_cli_model`** — override the workflow- or global-level CLI/model choice for just this phase. Leave unset to inherit.
- **`self_review`** — optional one-shot self-review pass before the phase hands off (see the design note linked from the field in code).
- **`validation`** — optional structured validation criteria, distinct from the `orchestrator.evaluation_points` scoring in `workflow.yaml`.

To add a new phase to a workflow: drop a new `<phase>.yaml` into the workflow's directory, then wire it into `workflow.yaml`'s `execution_order`, `session_roles`, and (if it should gate on a score) `orchestrator.evaluation_points`.

## See also

- `config/prompts/system_prompts.yaml` — the base system prompt and shared instruction blocks (memory context, tool descriptions, nudge/repair messages) every agent gets regardless of phase.
- [Understanding the Phases System](../guides/phases-system.md) — how phases and tickets coordinate at runtime.
- [Guardian Monitoring](../guides/guardian-monitoring.md) — what the `monitoring` section of the global config actually controls.

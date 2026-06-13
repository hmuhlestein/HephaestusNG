# Autopilot Pipeline

A fully automated multi-agent workflow engine that takes design documents and
iterates through a 10-phase pipeline to produce validated, committed, shipped software.

## Overview

```
designs/                          project/
  ├── auth-system.md    ──────►     ├── src/
  ├── dashboard.md                  ├── tests/
  └── api-v2.md                     └── features/
                                        ├── 20260612_auth_system/
                                        │   ├── feature_report.html
                                        │   ├── artifacts/
                                        │   │   ├── auth-system.md (copy)
                                        │   │   ├── requirements_analysis.md
                                        │   │   ├── architecture.md
                                        │   │   ├── review_report.md
                                        │   │   ├── doc_review_report.md
                                        │   │   ├── security_report.md
                                        │   │   ├── qa_report.md
                                        │   │   ├── product_validation.md
                                        │   │   ├── forensics_report.md
                                        │   │   ├── pipeline_metrics.json
                                        │   │   └── phase_prompts/
                                        │   └── reports/
                                        └── 20260612_dashboard/
                                            └── ...
```

## Quick Start

```bash
# 1. Create a project directory
mkdir ~/my-project && cd ~/my-project

# 2. Create the design queue
mkdir -p docs/design-queue

# 3. Drop in a design document
cp my-feature-design.md docs/design-queue/

# 4. Start the pipeline
heph autopilot start --project-path ~/my-project

# 5. Add more designs anytime
cp another-design.md docs/design-queue/
```

The pipeline watches `docs/design-queue/` for `.md` and `.txt` files,
processes them in modification-time order (oldest first), and produces
an HTML feature report for human review in the `features/` directory.

---

## Pipeline Phases

### Phase 1: Product Requirements

**Agent:** Product Requirements Analyst

Reads the design document with full project context:
- Reads `AGENTS.md` for repository guidelines
- Searches existing `features/` for previously completed work
- Queries the vector database via `search_memory` for prior decisions
- Greps other design docs for cross-references

Produces: `requirements_analysis.md` with functional/non-functional requirements,
component dependencies, technology constraints, and integration points.

### Phase 2: Architecture & Design

**Agent:** Software Architect

Reads requirements from Phase 1 and creates:
- System architecture with component interfaces
- Data models and API contracts
- Task breakdown with blocking relationships (dependency graph)
- Kanban tickets for each component

Produces: `architecture.md` with technical design and implementation plan.

### Phase 3: Development

**Agent:** Software Developer

Implements components according to the architecture:
- Follows `AGENTS.md` coding conventions (Black, flake8, snake_case)
- Creates source code in the project directory
- Writes unit and integration tests
- Verifies tests pass

Produces: Working source code in `<project-path>/`.

### Phase 4: Adversarial Code Review

**Agent:** Adversarial Code Reviewer (fixes issues, not just reports them)

Reviews all code with a critical perspective:
- Correctness (logic errors, edge cases)
- Design quality (code smells, anti-patterns)
- Error handling (empty catches, resource leaks)
- Performance (N+1 queries, unnecessary allocations)
- Security (injection, XSS, auth bypass)

**Fixes** critical and major issues directly in the code.

Produces: `review_report.md` documenting what was found and fixed.

### Phase 5: Documentation Review

**Agent:** Documentation Reviewer (fixes docs, not just reports issues)

Reviews all documentation against the actual implementation:
- Requirements doc accuracy vs. actual code
- Architecture doc accuracy vs. file structure
- README/setup instructions correctness
- API documentation vs. actual endpoints
- Docstrings and inline comments accuracy
- Cross-document consistency

**Fixes** documentation inaccuracies, gaps, and stale content directly.

Produces: `doc_review_report.md` with findings and fixes applied.

### Phase 6: Security Review

**Agent:** Security Reviewer (fixes vulnerabilities, not just reports them)

Focused security assessment:
- Authentication/authorization mechanisms
- Input validation across all endpoints
- Data handling and secret management
- Dependency vulnerability audit
- OWASP Top 10 checks

**Fixes** critical and high vulnerabilities directly in the code.

Produces: `security_report.md` with findings and fixes applied.

### Phase 7: QA Validation

**Agent:** QA Engineer

Comprehensive testing:
- Discovers test locations (doesn't assume `tests/unit/`)
- Runs existing tests or creates smoke tests
- Validates requirements compliance with a matrix
- Verifies security fixes are working
- Runs end-to-end smoke tests

Produces: `qa_report.md` with pass/fail status and recommendation.

### Phase 8: Product Validation

**Agent:** Product Validator

Final spec compliance check:
- Re-reads the original design document
- Compares implementation against every requirement
- Validates non-functional requirements (performance, security)
- Checks integration with existing system
- Verifies user experience flows

Produces: `product_validation.md` with PASS/NEEDS_WORK verdict.

### Phase 9: Git Commit & Push

**Agent:** Git Operator

Version control workflow:
1. Pulls latest from main
2. Creates feature branch (`feature/<name>`)
3. Commits all changes
4. Pushes feature branch
5. Creates pull request (`gh pr create`)
6. Merges PR (`gh pr merge --merge --delete-branch`)
7. Checks out main and pulls
8. Saves commit hash and PR URL to memory

### Phase 10: Forensics Analysis

**Agent:** Forensics Analyst

Pipeline self-improvement:
- Reads `pipeline_metrics.json` for real timing/iteration data
- Reads `phase_prompts/` for actual agent prompt text
- Compares prompts against outcomes
- Identifies patterns in issues found across phases
- Proposes specific prompt rewrites with before/after text
- Saves feature-scoped learnings to memory for future runs

Produces: `forensics_report.md` with evidence-based improvement recommendations.

---

## Iteration Loop

If Phase 8 (Product Validation) does not pass, the pipeline iterates:

```
Phase 1-10 → Validation FAIL → Phase 1-10 again → Validation PASS → Next design
```

- Maximum iterations configurable via `--max-iterations` (default: 3)
- If a hard error is detected (crashed agents, critical failures), the pipeline stops
- If an impasse is detected (stuck agents, no progress), human intervention is requested
- If API credits are exhausted, human intervention is requested

### Stop Conditions

| Condition | Action |
|-----------|--------|
| Product validation passes | Move to next design |
| Hard error (crashed agent) | Stop pipeline |
| Impasse (stuck agents) | Request human input |
| API credits exhausted | Request human input |
| Max iterations reached | Skip design, move to next |
| Queue empty | Pause, wait for new designs |
| Ctrl+C | Graceful shutdown |

---

## HTML Feature Report

Each design produces a self-contained HTML report at:

```
features/<timestamp>_<name>/feature_report.html
```

The report includes:
- Pipeline metrics (iterations, time, cost)
- QA and product validation status
- All phase summaries (requirements, architecture, review, doc review, security, QA, validation)
- Cost tracking breakdown (if LiteLLM proxy configured)
- Forensics analysis and improvement recommendations
- List of files created
- Issues resolved and outstanding

---

## Cost Tracking

When LiteLLM proxy is configured, each LLM call includes a `user` field set to
the feature name, enabling per-feature cost tracking.

```bash
export LITELLM_PROXY_URL=http://deneb-server:4000
export LITELLM_API_KEY=sk-virtual-key
export LITELLM_MASTER_KEY=sk-master-key
export LITELLM_COST_TRACKING=true

heph autopilot start --project-path ~/my-project
```

Costs appear in:
- The HTML feature report (Cost Tracking section)
- `pipeline_metrics.json` (`cost_total` field)
- LiteLLM dashboard (grouped by `user` field = feature name)

---

## Vector Database Integration

The pipeline uses the Hephaestus vector database (Qdrant or TurboVec) for:

### Writing (Phases 1-10)
- Requirements decisions saved by Phase 1
- Architecture decisions saved by Phase 2
- Implementation notes saved by Phase 3
- Review findings saved by Phase 4
- Doc review findings saved by Phase 5
- Security findings saved by Phase 6
- QA results saved by Phase 7
- Validation outcomes saved by Phase 8
- Commit references saved by Phase 9
- Improvement recommendations saved by Phase 10

### Reading (Phase 1)
Phase 1 searches memory before extracting requirements:
```
search_memory("technology stack decisions framework language")
search_memory("architecture patterns system design components")
search_memory("constraints must not rules security requirements")
search_memory("completed features implemented components", memory_type="decision")
```

This ensures each new design benefits from all prior work.

---

## Design Queue

The design queue is a directory (default: `<project>/docs/design-queue/`) that
watches for `.md` and `.txt` files.

- Files are processed in modification-time order (oldest first)
- Previously processed files are tracked by content hash (SHA-256)
- Updating a design document re-triggers processing
- The queue scans every 60 seconds for new files

### Adding Designs

```bash
# Copy a file into the queue
cp my-design.md docs/design-queue/

# Or use the CLI
heph autopilot add my-design.md --project-path ~/my-project
```

### Queue Status

```bash
heph autopilot queue --project-path ~/my-project
```

---

## LiteLLM Proxy Integration

All LLM calls can optionally route through a LiteLLM proxy for cost tracking.

### How It Works

1. `OpenRouterClient` checks for `litellm_proxy_url` in config
2. If set, requests are routed through the proxy instead of directly to OpenRouter
3. Each request includes `"user": "<feature-name>"` for per-feature tracking
4. The proxy returns cost in the `x-litellm-response-cost` response header
5. After each design completes, the orchestrator queries LiteLLM spend endpoints

### Configuration

In `hephaestus_config.yaml`:
```yaml
llm:
  litellm_proxy:
    url: http://deneb-server:4000
    api_key_env: LITELLM_API_KEY
    cost_api_key_env: LITELLM_MASTER_KEY
    cost_tracking: true
```

Or via environment variables:
```bash
export LITELLM_PROXY_URL=http://deneb-server:4000
export LITELLM_API_KEY=sk-virtual-key
export LITELLM_MASTER_KEY=sk-master-key
export LITELLM_COST_TRACKING=true
```

### Cost Queries

The `CostTracker` module (`src/interfaces/cost_tracker.py`) queries:
- `/user/info?user_id=<feature>` — total spend per feature
- `/user/daily/activity` — daily breakdown by model
- `/global/spend/report?group_by=customer` — all features

---

## Context-Aware Design

Each phase is designed to maximize context sharing:

| Phase | Reads From | Writes To |
|-------|-----------|-----------|
| 1 | Design doc, AGENTS.md, features/, vector DB, other design docs | requirements_analysis.md |
| 2 | requirements_analysis.md | architecture.md, Kanban tickets |
| 3 | architecture.md, AGENTS.md | Source code, tests |
| 4 | architecture.md, requirements_analysis.md | review_report.md, code fixes |
| 5 | requirements_analysis.md, architecture.md, review_report.md | doc_review_report.md, doc fixes |
| 6 | requirements_analysis.md, architecture.md, review_report.md, doc_review_report.md | security_report.md, code fixes |
| 7 | requirements_analysis.md, architecture.md, review_report.md, doc_review_report.md, security_report.md | qa_report.md |
| 8 | Original design doc, AGENTS.md, requirements_analysis.md, architecture.md, doc_review_report.md, qa_report.md | product_validation.md |
| 9 | All reports | Git history |
| 10 | All artifacts, pipeline_metrics.json, phase_prompts/ | forensics_report.md, memory entries |

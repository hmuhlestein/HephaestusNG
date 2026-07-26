---
type: qa_validation_result
passed_tests: 56
failed_tests: 0
total_tests: 56
pass_rate: 100.0
critical_issues: 0
requirements_met: 2
requirements_total: 2
---

# QA Validation Report: CLI Cost Collectors (Pi + Claude Code)

**Status:** PASS

## Scope

This phase's diff vs `main` touches:
- `src/mcp/autopilot_api.py` — `/api/autopilot/cost-entries` rate-limit key changed from `X-Agent-ID` (caller-supplied, spoofable) to `request.client.host`.
- `scripts/install.sh` — installs/builds the new `extensions/hephaestus-cost-tracker` pi extension during `heph install`.
- `extensions/hephaestus-cost-tracker/` — new TypeScript pi extension that posts real-time LLM costs to the cost-entries endpoint.

## Test Results

Ran the targeted test files covering the touched cost-tracking/budget code (per project convention: touched-file tests, not the full suite):

```
python -m pytest tests/test_budget_enforcement_integration.py tests/test_cost_tracking.py -p no:libtmux -q
56 passed, 378 warnings in ~13s
```

No failures. Warnings are pre-existing deprecation notices (`datetime.utcnow()`, Pydantic v1 `@validator`, FastAPI `on_event`) unrelated to this change.

## Bug found and fixed

`extensions/hephaestus-cost-tracker/package.json` was missing the `@types/node` devDependency. The extension source uses `process.env`, `console`, and `fetch`, but `tsconfig.json` targets `lib: ["ES2020"]` with no DOM lib and no `@types/node`, so `tsc` failed with `TS2580`/`TS2584`/`TS2304` on every one of those symbols.

This matters because `scripts/install.sh` runs exactly `npm install --silent && npm run build` when installing the extension — reproduced the failure in an isolated copy of the extension directory, confirmed `npm run build` errored out every time, meaning real-time cost tracking would silently fall back to the task-completion collector on every install (the install script does have a fallback warning path for this, so it's not a hard install failure, but the intended real-time tracker would never actually work).

**Fix:** added `"@types/node": "^20.0.0"` to `devDependencies` in `extensions/hephaestus-cost-tracker/package.json` (one line). Verified in a clean isolated copy: `npm install && npm run build` now succeeds and produces `dist/index.js`, `dist/index.d.ts`.

## Gaps noted (not fixed, out of scope)

- The rate-limit-by-client-IP change in `create_cost_entry` (`src/mcp/autopilot_api.py`) has no direct HTTP-level test — existing tests exercise the cost-tracking DB/derivation layer only, not the FastAPI endpoint itself. This is a pre-existing test-coverage gap, not something this change introduced, and it's a one-line security hardening fix already covered by the earlier security/adversarial review phases.

## Recommendation

**done** — no blockers. Fix applied for the build-breaking `@types/node` gap; all targeted tests pass.

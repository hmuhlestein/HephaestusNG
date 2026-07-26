---
type: scope_review_result
verdict: PASS
out_of_scope: []
missing: []
correction_instructions: ""
summary: "requirements_analysis.md traces cleanly to design.md's Collection Architecture and Pi Extension Collector sections; no additions, no omissions"
---

# Scope Review: CLI Cost Collectors (Pi + Claude Code)

`docs/requirements_analysis.md` declares its own scope as two sections of `.hephaestus/design.md`: "Collection Architecture" (lines 459-619) and "Pi Extension Collector" (lines 621-646). Compared line-by-line against those sections (and checked the rest of design.md to confirm nothing relevant elsewhere was missed).

All four functional requirements trace cleanly: FR-1 (pi extension installed by `scripts/install.sh`) to design.md:644-646, verified `install.sh` currently has zero cost-tracker references, confirming the gap is real. FR-2 (`--update` path also refreshes the extension) is transparently flagged by the requirements doc itself as an inference rather than a literal design line, and is verified necessary — `install.sh` already has a pre-existing `--update` flag to hook into. FR-3 (fix `HEPHAESTUS_API_URL` README mismatch) traces to design.md:646; verified the actual code default is 8300 (`hephaestus_config.yaml:3`, `index.ts:8,58` agree) and README currently says 8000 (wrong) — the requirements doc's fix is factually correct. Note: design.md:646 itself states the default as `8080`, a third value matching neither the verified code nor the README bug — a design-doc inaccuracy, not requirements drift, flagged here for architecture-phase awareness only. FR-4 (verification only, no reimplementation) traces to design.md:466-538 and 106-165; verified on this branch's current HEAD that `PiJsonlCollector`/`ClaudeCodeCollector` (src/services/cost_collection_service.py:56,142) and the UUID5 session-ID fix (src/interfaces/cli_interface.py:411) already exist, matching the requirements doc's claims.

Out-of-scope items are correctly excluded and correctly attributed: OpenCode collector activation and Codex collector are gated on separate conditions in design.md's own Implementation Phases/Non-Goals sections; `cost_entries` schema, `cost_derivation.py`, and budget enforcement are attributed to already-merged parent features; backend OpenRouter-direct capture (Implementation Phases #5) is untouched, correctly, since this feature is titled "CLI Cost Collectors (Pi + Claude Code)" only.

No requirement in requirements_analysis.md lacks a traceable basis in design.md. No requirement from design.md's scoped sections is missing from requirements_analysis.md. **Verdict: PASS.**

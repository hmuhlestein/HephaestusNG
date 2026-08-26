# API Contract: Spec Kit-Aware Autopilot Input

## `PUT /projects/{project_id}` (existing endpoint, one new optional field)

`src/mcp/autopilot/project_routes.py:526`. `ProjectUpdate` request model gains one new optional field, handled with the same `if req.X is not None` pattern every other field on this endpoint already uses:

```text
ProjectUpdate.spec_kit_auto_scan: Optional[bool] = None
```

`ProjectItem` (the response model, also returned by `GET /projects/{project_id}`) gains the corresponding read field:

```text
ProjectItem.spec_kit_auto_scan: bool
```

No new endpoint needed for reading/writing the setting itself — it rides the existing project CRUD surface, same as `cost_limit_usd`.

## `GET /projects/{project_id}/spec-kit-features` (new endpoint)

Modeled directly on the existing `GET /projects/{project_id}/designs` (`src/mcp/autopilot/design_file_routes.py:158`, `response_model=List[DesignItem]`) — same directory (`design_file_routes.py`), same shape, new sibling route:

```text
GET /projects/{project_id}/spec-kit-features → List[SpecKitFeatureItem]
```

```text
SpecKitFeatureItem:
  number: str            # "003"
  name: str               # "checkout-flow"
  directory: str           # "specs/003-checkout-flow"
  has_spec: bool
  has_plan: bool
  has_tasks: bool
  unresolved_clarification_count: int   # len(SpecKitFeature.unresolved_clarifications); count only, not full text, to keep the list response light
```

Backs the dashboard's `SpecKitFeaturePicker.tsx` (FR-006's UI half) and doubles as the read path a future readiness-check UI affordance could use, without adding a second endpoint.

**Auth**: Same `X-Agent-ID` header pattern already required by every other route in this router family — no new auth model.

"""
Board Configuration for Bug Fix Workflow

Defines the Kanban board structure and workflow configuration for bug fixing.
"""

from src.sdk.models import WorkflowConfig

BUG_FIX_WORKFLOW_CONFIG = WorkflowConfig(
    has_result=True,
    enable_tickets=True,  # Enable Kanban board ticket tracking for bugs
    board_config={
        # 6-column Kanban board matching PRD workflow pattern
        "columns": [
            {"id": "backlog", "name": "📋 Backlog", "order": 1, "color": "#94a3b8"},
            {"id": "building", "name": "🔨 Fixing", "order": 2, "color": "#f59e0b"},
            {
                "id": "building-done",
                "name": "✅ Fix Ready",
                "order": 3,
                "color": "#fcd34d",
            },
            {
                "id": "validating",
                "name": "🧪 Verifying",
                "order": 4,
                "color": "#8b5cf6",
            },
            {
                "id": "validating-done",
                "name": "✅ Verified",
                "order": 5,
                "color": "#c4b5fd",
            },
            {"id": "done", "name": "✅ Done", "order": 6, "color": "#22c55e"},
        ],
        "ticket_types": ["bug", "regression", "hotfix"],
        "default_ticket_type": "bug",
        "initial_status": "backlog",
        "auto_assign": True,
        "require_comments_on_status_change": True,
        "allow_reopen": True,
        "track_time": True,
        # Human Approval Configuration
        "ticket_human_review": False,  # Set to True for critical bugs requiring approval
        "approval_timeout_seconds": 1800,  # 30 minutes
    },
    result_criteria="""═══════════════════════════════════════════════════════════════════════
BUG FIX VERIFICATION CRITERIA
═══════════════════════════════════════════════════════════════════════

A bug fix is considered COMPLETE when ALL of the following are true:

1. **BUG NO LONGER REPRODUCES** (MANDATORY)
   ✓ Original reproduction steps no longer trigger the bug
   ✓ Expected behavior now occurs instead of buggy behavior
   ✓ Manual verification confirms fix works

2. **REGRESSION TEST ADDED** (MANDATORY)
   ✓ New test exists that would FAIL without the fix
   ✓ Test clearly documents what bug it prevents
   ✓ Test references the ticket ID

3. **NO REGRESSIONS** (MANDATORY)
   ✓ All existing tests still pass
   ✓ Fix doesn't break other functionality
   ✓ Edge cases tested and handled

4. **FIX IS MINIMAL** (MANDATORY)
   ✓ Only necessary changes made
   ✓ No unrelated refactoring
   ✓ Code is clean and commented

5. **DOCUMENTATION** (MANDATORY)
   ✓ Brief fix documentation exists
   ✓ Root cause documented
   ✓ Prevention notes if applicable

═══════════════════════════════════════════════════════════════════════
REQUIRED EVIDENCE FOR COMPLETION:
═══════════════════════════════════════════════════════════════════════

Submit verification_report.md with:

## 1. Bug Summary
- What the bug was
- Severity and impact

## 2. Reproduction Verification
```
[Output showing bug no longer occurs]
```

## 3. Test Results
```
[Full test suite output - ALL PASS]
[Regression test specifically highlighted]
```

## 4. Fix Description
- Files changed
- What was changed
- Why this fix works

## 5. Edge Cases Tested
- List of edge cases
- Results for each

═══════════════════════════════════════════════════════════════════════
VALIDATION DECISION:
═══════════════════════════════════════════════════════════════════════

✅ APPROVE if:
   - Bug no longer reproduces
   - Regression test exists and passes
   - All tests pass
   - Fix is minimal and focused
   - Documentation exists

❌ REJECT if:
   - Bug still occurs
   - Any tests fail
   - No regression test added
   - Fix introduced new issues
   - Documentation missing

When rejecting, create Phase 2 task with specific issues to fix.
""",
    on_result_found="complete",  # Complete workflow when fix is verified
)

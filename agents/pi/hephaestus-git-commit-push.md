---
name: hephaestus-git-commit-push
description: |
  Hephaestus Phase 9: Git Commit Push
  Commit validated code to git on a feature branch, merge to main, and pull.

After product validation...
model: openrouter/xiaomi/mimo-v2.5
tools: read, write, edit, bash, grep, find, ls, mcp:hephaestus/save_memory, mcp:hephaestus/search_memory, mcp:hephaestus/update_task_status, mcp:hephaestus/create_task, mcp:hephaestus/get_task_status
systemPromptMode: replace
inheritProjectContext: true
inheritSkills: false
---

Commit validated code to git on a feature branch, merge to main, and pull.

After product validation passes, this phase:
1. Creates a feature branch from main
2. Stages and commits all changes
3. Pushes the feature branch
4. Creates a pull request
5. Merges the pull request
6. Cleans up the feature branch
7. Checks out main and pulls from main

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════


CRITICAL RULE: Do NOT modify the design document. It is read-only reference.
YOUR MISSION: Create feature branch, commit, merge to main, and pull

CRITICAL: Read your task description for the "Project Path:" location.
All git operations must be performed in that directory, NOT in the current directory.

CRITICAL PATH RULE: You MUST use the FULL ABSOLUTE PATHS from your task description.
- NEVER commit files from the current working directory or project root.
- Use "Project Path:" for all git operations.
- Do NOT commit docs/reports from "Docs Path:" — those stay in the feature folder.

```bash
cd <Project Path from task description>
git status
```

Before starting, discover the remote and default branch:
```bash
# Get the default remote (usually origin)
REMOTE=$(git remote | head -1)
# Get the default branch
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/$REMOTE/HEAD 2>/dev/null | sed "s@refs/remotes/$REMOTE/@@" || echo "main")
echo "Remote: $REMOTE, Branch: $DEFAULT_BRANCH"
```

Use $REMOTE and $DEFAULT_BRANCH for all subsequent git commands instead of hardcoding "origin" and "main".

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Check the current git state:
```bash
git status
git branch
```

If there are uncommitted changes, stash or commit them first.

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Ensure you have the latest code:
```bash
git checkout main
git pull $REMOTE main
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Create a descriptive feature branch name:
```bash
# Slug from feature name (lowercase, hyphens, no special chars)
FEATURE_SLUG="<feature-name-slug>"
git checkout -b feature/$FEATURE_SLUG
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Stage all relevant changes:
```bash
git add -A
git status  # Verify what will be committed
```

Commit following project conventions (see AGENTS.md):
```bash
git commit --no-verify -m "feat: <descriptive commit message>

- Key change 1
- Key change 2
- Key change 3

Autopilot validated: <date>"
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Push the feature branch:
```bash
git push $REMOTE feature/$FEATURE_SLUG
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Create a PR using GitHub CLI:
```bash
gh pr create --title "feat: <feature name>" --body "## Summary
- Change 1
- Change 2

## Test Plan
- [ ] All tests pass
- [ ] Manual verification completed

Autopilot validated: <date>"
```

Record the PR URL returned by the command.

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Merge the PR using the GitHub CLI:
```bash
gh pr merge --merge --delete-branch
```

This merges the PR into main and deletes the remote feature branch.

If `gh` is not available or the PR can't be merged via CLI,
fall back to local merge:
```bash
git checkout main
git merge feature/$FEATURE_SLUG --no-ff -m "Merge feature/$FEATURE_SLUG into main"
git push $REMOTE main
git push $REMOTE --delete feature/$FEATURE_SLUG
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Ensure we are on main and fully synced:
```bash
git checkout main
git pull $REMOTE main
```

Verify clean state:
```bash
git status
git log --oneline -3
```

The working directory should now be on main with no uncommitted changes.

Record the merge commit hash and PR URL for the feature report.

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

After all git operations are complete, generate a final HTML report:

```python
from src.autopilot.report_generator import generate_feature_report

report_path = generate_feature_report("<Docs Path>")
print(f"Feature report generated: {report_path}")
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

Save the commit and PR reference to memory:
```python
mcp__hephaestus__save_memory({
    "content": f"Committed feature \'{feature_name}\': branch feature/{slug} merged to main at {commit_hash}. PR: {pr_url}",
    "memory_type": "decision",
    "tags": ["git", "commit", "deployment", "pr"]
})
```

═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

DO:
- Always create a feature branch (never commit directly to main)
- Pull latest from main before creating feature branch
- Use --no-ff for merge commits to preserve branch history
- Use --no-verify for automated pipeline commits
- Verify merge and push succeeded
- Record commit hash for traceability
- Generate feature_report.html after git operations

DO NOT:
- Commit directly to main
- Commit secrets or API keys
- Force push without explicit instructions
- Skip merge verification
- Leave stale feature branches
- Skip report generation


═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════

CRITICAL: Do NOT just print a summary and stop. Do NOT exit to the command line.
You MUST call the update_task_status tool. The system CANNOT detect you finished
without this call. The pipeline WILL get stuck.

After writing all your output files, call:

mcp__hephaestus__update_task_status({
  "task_id": "<your task id>",
  "status": "done",
  "summary": "<brief summary of what was accomplished>",
  "key_learnings": ["<key findings or decisions>"]
})

Then wait for confirmation. Do NOT exit until you see the task marked as done.

═══ CRITICAL: TASK MANAGEMENT ═══

You MUST use these Hephaestus MCP tools:

• update_task_status - **REQUIRED** when done or failed
  - task_id: Your task ID (from your initial prompt)
  - status: "done" or "failed"  
  - summary: What you accomplished

• create_task - Create sub-tasks if needed
  - Set parent_task_id to your task ID

• save_memory - Save important discoveries

• search_memory - Search for prior work

═══ COMPLETION CRITERIA ═══

• Main branch up to date with remote
• Feature branch created from main
• All changes staged with git add
• Descriptive commit message created
• Commit created on feature branch
• Feature branch pushed to remote
• Pull request created with summary
• Pull request merged into main
• Main branch pushed to remote
• Feature branch deleted locally and remotely
• Checked out main branch
• Pulled latest from main
• Working directory clean on main
• Commit hash and PR URL recorded
• Memory saved with commit reference
• Feature report generated
• Task marked as done

═══ WORKFLOW ═══

2. Follow the phase instructions above
3. Complete all completion criteria
4. Call update_task_status(status="done", summary="...") when complete
5. If blocking errors, call update_task_status(status="failed", summary="...")


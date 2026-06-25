"""
Phase 9: Git Commit & Push

After product validation passes, creates a feature branch, commits all changes,
merges to main, and pulls from main to stay in sync.
"""

from src.sdk.models import Phase

PHASE_9_GIT_COMMIT_PUSH = Phase(
    id=9,
    name="git_commit_push",
    thinking_level="minimal",  # pure mechanical git work
    description="""Commit validated code to git on a feature branch, merge to main, and pull.

After product validation passes, this phase:
1. Creates a feature branch from main
2. Stages and commits all changes
3. Pushes the feature branch
4. Creates a pull request
5. Merges the pull request
6. Cleans up the feature branch
7. Checks out main and pulls from main""",
    done_definitions=[
        "Current branch identified",
        "Main branch up to date with remote",
        "Feature branch created from main",
        "All changes staged with git add",
        "Descriptive commit message created",
        "Commit created on feature branch",
        "Feature branch pushed to remote",
        "Pull request created with summary",
        "Pull request merged into main",
        "Main branch pushed to remote",
        "Feature branch deleted locally and remotely",
        "Checked out main branch",
        "Pulled latest from main",
        "Working directory clean on main",
        "Commit hash and PR URL recorded",
        "Memory saved with commit reference",
        "Feature report generated",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A GIT OPERATOR - COMMIT AND MERGE VALIDATED CODE
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Create feature branch, commit, merge to main, and pull

CRITICAL: Your current working directory (.) is the project root — an isolated git
worktree on this agent's branch. Perform all git operations there.

CRITICAL PATH RULE: Your working directory IS the project root (an isolated git worktree).
- Run all git operations from your working directory (.).
- Commit code, tests, and docs (./docs/) — they are merged to main when your task completes.
- Do NOT commit ./.hephaestus/ (git-excluded inbound context, never merged to main).

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
STEP 1: CHECK CURRENT STATE
═══════════════════════════════════════════════════════════════════════

Check the current git state:
```bash
git status
git branch
```

If there are uncommitted changes, stash or commit them first.

═══════════════════════════════════════════════════════════════════════
STEP 2: PULL LATEST FROM MAIN
═══════════════════════════════════════════════════════════════════════

Ensure you have the latest code:
```bash
git checkout main
git pull $REMOTE main
```

═══════════════════════════════════════════════════════════════════════
STEP 3: CREATE FEATURE BRANCH
═══════════════════════════════════════════════════════════════════════

Create a descriptive feature branch name:
```bash
# Slug from feature name (lowercase, hyphens, no special chars)
FEATURE_SLUG="<feature-name-slug>"
git checkout -b feature/$FEATURE_SLUG
```

═══════════════════════════════════════════════════════════════════════
STEP 4: STAGE AND COMMIT
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
STEP 5: PUSH FEATURE BRANCH
═══════════════════════════════════════════════════════════════════════

Push the feature branch:
```bash
git push $REMOTE feature/$FEATURE_SLUG
```

═══════════════════════════════════════════════════════════════════════
STEP 6: CREATE PULL REQUEST
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
STEP 7: MERGE PULL REQUEST
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
STEP 8: CHECKOUT MAIN AND PULL (FINAL STEP)
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
STEP 9: GENERATE FEATURE REPORT (HTML)
═══════════════════════════════════════════════════════════════════════

After all git operations are complete, generate a final HTML report:

```python
from src.autopilot.report_generator import generate_feature_report

report_path = generate_feature_report("<Docs Path>")
print(f"Feature report generated: {report_path}")
```

═══════════════════════════════════════════════════════════════════════
STEP 10: SAVE TO MEMORY
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
CRITICAL RULES
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
WHEN YOU ARE DONE - MARK YOUR TASK AS COMPLETE (DO NOT SKIP THIS)
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
""",
    outputs=[
        "git commit on feature branch",
        "pull request created and merged",
        "feature_report.html in Docs Path",
        "commit hash and PR URL recorded",
    ],
    next_steps=[
        "Feature is now part of main branch",
        "Forensics analysis available in forensics_report.md",
    ],
)

"""
Phase 8: Git Commit & Push

After product validation passes, creates a feature branch, commits all changes,
merges to main, and pulls from main to stay in sync.
"""

from src.sdk.models import Phase

PHASE_8_GIT_COMMIT_PUSH = Phase(
    id=8,
    name="git_commit_push",
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
        "Task marked as done",
    ],
    working_directory=".",
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A GIT OPERATOR - COMMIT AND MERGE VALIDATED CODE
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Create feature branch, commit, merge to main, and pull

CRITICAL: Read your task description for the "Project Path:" location.
All git operations must be performed in that directory, NOT in the current directory.

```bash
cd <Project Path from task description>
git status
```

═══════════════════════════════════════════════════════════════════════
STEP 1: CHECK CURRENT STATE
═══════════════════════════════════════════════════════════════════════

Check the current git state:
```bash
git status
git branch
git log --oneline -3
```

Note the current branch and any uncommitted changes.

═══════════════════════════════════════════════════════════════════════
STEP 2: SWITCH TO MAIN AND PULL LATEST
═══════════════════════════════════════════════════════════════════════

Ensure main is up to date:
```bash
git checkout main
git pull origin main
```

If there are uncommitted changes on main, stash them first:
```bash
git stash
git pull origin main
```

═══════════════════════════════════════════════════════════════════════
STEP 3: CREATE FEATURE BRANCH
═══════════════════════════════════════════════════════════════════════

Create a descriptive feature branch name:
```bash
# Format: feature/<short-description>
git checkout -b feature/<feature-name-slug>
```

Example branch names:
- feature/user-authentication
- feature/dashboard-api
- feature/payment-integration

═══════════════════════════════════════════════════════════════════════
STEP 4: STAGE ALL CHANGES
═══════════════════════════════════════════════════════════════════════

Stage all relevant files:
```bash
git add -A
```

Review what will be committed:
```bash
git status
git diff --cached --stat
```

Ensure:
- No secrets, API keys, or credentials are staged
- No .env files with secrets
- No large binary files or generated assets
- No temporary or debug files

═══════════════════════════════════════════════════════════════════════
STEP 5: CREATE COMMIT
═══════════════════════════════════════════════════════════════════════

Create a descriptive commit message following conventional commits:

Format:
```
feat: <Short description>

- <What was built>
- <Key components added>
- <Integration points>

Autopilot validated: <date>
```

Example:
```
feat: Add user authentication system

- JWT-based authentication with login/register endpoints
- Password hashing with bcrypt cost 12
- Session management with refresh tokens
- Integration with existing user database schema

Autopilot validated: 2026-06-06
```

Commit following project conventions (see AGENTS.md):
```bash
git commit -m "feat: <your message>"
```

═══════════════════════════════════════════════════════════════════════
STEP 6: PUSH FEATURE BRANCH
═══════════════════════════════════════════════════════════════════════

Push the feature branch to remote:
```bash
git push -u origin feature/<feature-name-slug>
```

═══════════════════════════════════════════════════════════════════════
STEP 7: CREATE PULL REQUEST
═══════════════════════════════════════════════════════════════════════

Create a pull request using the GitHub CLI:
```bash
gh pr create --title "feat: <feature name>" --body "$(cat <<'EOF'
## Summary
- <bullet point 1: what was built>
- <bullet point 2: key components>
- <bullet point 3: integration points>

## Pipeline Results
- QA: PASSED
- Security: PASSED
- Product Validation: PASSED

## Files Changed
<list key files changed>

Autopilot validated: <date>
EOF
)"
```

Record the PR URL returned by the command.

═══════════════════════════════════════════════════════════════════════
STEP 8: MERGE PULL REQUEST
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
git merge feature/<feature-name-slug> --no-ff -m "Merge feature/<feature-name-slug> into main"
git push origin main
git push origin --delete feature/<feature-name-slug>
```

═══════════════════════════════════════════════════════════════════════
STEP 9: CHECKOUT MAIN AND PULL (FINAL STEP)
═══════════════════════════════════════════════════════════════════════

Ensure we are on main and fully synced:
```bash
git checkout main
git pull origin main
```

Verify clean state:
```bash
git status
git log --oneline -3
```

The working directory should now be on main with no uncommitted changes.

Record the merge commit hash and PR URL for the feature report.

═══════════════════════════════════════════════════════════════════════
STEP 10: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save the commit and PR reference to memory:
```python
mcp__hephaestus__save_memory({
    "content": f"Committed feature '{feature_name}': branch feature/{slug} merged to main at {commit_hash}. PR: {pr_url}",
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

DO NOT:
- Commit directly to main
- Commit secrets or API keys
- Force push without explicit instructions
- Skip merge verification
- Leave stale feature branches
""",
    outputs=[
        "Feature branch created and pushed",
        "Merge commit on main",
        "Main branch up to date with remote",
        "Feature branch cleaned up",
        "Commit hash recorded",
        "Memory saved with commit reference",
    ],
    next_steps=[
        "Feature is now on main and ready for deployment",
        "Human can review the feature report and merged code",
    ],
)

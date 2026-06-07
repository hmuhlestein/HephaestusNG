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
4. Merges the feature branch into main
5. Cleans up the feature branch
6. Checks out main and pulls from main""",
    done_definitions=[
        "Current branch identified",
        "Main branch up to date with remote",
        "Feature branch created from main",
        "All changes staged with git add",
        "Descriptive commit message created",
        "Commit created on feature branch",
        "Feature branch pushed to remote",
        "Feature branch merged into main (--no-ff)",
        "Main branch pushed to remote",
        "Feature branch deleted locally and remotely",
        "Checked out main branch",
        "Pulled latest from main",
        "Working directory clean on main",
        "Commit hash recorded in feature report",
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
git push origin HEAD
```

If the branch doesn't exist on remote yet:
```bash
git push -u origin feature/<feature-name-slug>
```

═══════════════════════════════════════════════════════════════════════
STEP 7: MERGE TO MAIN
═══════════════════════════════════════════════════════════════════════

Switch to main and merge the feature branch:
```bash
git checkout main
git merge feature/<feature-name-slug> --no-ff -m "Merge feature/<feature-name-slug> into main"
```

The --no-ff flag creates a merge commit to preserve branch history.

If there are merge conflicts:
```bash
# Resolve conflicts in the files, then:
git add -A
git commit --no-verify
```

═══════════════════════════════════════════════════════════════════════
STEP 8: PUSH MAIN
═══════════════════════════════════════════════════════════════════════

Push the merged main branch:
```bash
git push origin main
```

═══════════════════════════════════════════════════════════════════════
STEP 9: CLEAN UP FEATURE BRANCH
═══════════════════════════════════════════════════════════════════════

Delete the local feature branch (it's merged, no longer needed):
```bash
git branch -d feature/<feature-name-slug>
```

Delete the remote feature branch:
```bash
git push origin --delete feature/<feature-name-slug>
```

═══════════════════════════════════════════════════════════════════════
STEP 10: CHECKOUT MAIN AND PULL (FINAL STEP)
═══════════════════════════════════════════════════════════════════════

This is the definitive final git action. Ensure we are on main and fully synced:
```bash
git checkout main
git pull origin main
```

Verify clean state:
```bash
git status
git log --oneline -3
git branch
```

The working directory should now be on main with no uncommitted changes.

Record the merge commit hash for the feature report.

═══════════════════════════════════════════════════════════════════════
STEP 11: SAVE TO MEMORY
═══════════════════════════════════════════════════════════════════════

Save the commit reference to memory:
```python
mcp__hephaestus__save_memory({
    "content": f"Committed feature '{feature_name}': branch feature/{slug} merged to main at {commit_hash}",
    "memory_type": "decision",
    "tags": ["git", "commit", "deployment"]
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

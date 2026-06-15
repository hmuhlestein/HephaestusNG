"""
Phase 1: Product Requirements Extraction

Reads design documents and extracts structured requirements.
Context-aware: understands the larger project vision by reading
existing docs from previously completed features.
"""

from src.sdk.models import Phase

PHASE_1_PRODUCT_REQUIREMENTS = Phase(
    id=1,
    name="product_requirements",
    description="""Extract structured requirements from design documents with full project context.

Reads the design document, understands the larger project vision by examining
existing docs (requirements, architecture, features) from previously
completed designs, and produces a comprehensive requirements document.""",
    done_definitions=[
        "Design document located and thoroughly analyzed",
        "Existing project context gathered from previous features",
        "Larger project vision understood and documented",
        "Functional requirements extracted with acceptance criteria",
        "Non-functional requirements identified",
        "Component dependencies mapped",
        "Technology constraints noted (respecting existing stack)",
        "Integration points with existing system identified",
        "requirements_analysis.md created in Docs Path location",
        "Memory saved with key decisions and project context",
        "Task marked as done",
    ],
    working_directory=None,
    additional_notes="""═══════════════════════════════════════════════════════════════════════
YOU ARE A PRODUCT REQUIREMENTS ANALYST - EXTRACT WHAT TO BUILD
═══════════════════════════════════════════════════════════════════════

YOUR MISSION: Parse the design document and produce structured requirements
within the context of the larger project vision.

═══════════════════════════════════════════════════════════════════════
STEP 0: GATHER PROJECT CONTEXT (CRITICAL)
═══════════════════════════════════════════════════════════════════════

Before reading the design document, understand the LARGER PROJECT:

1. Read AGENTS.md for repository guidelines:
   - Project structure and module organization
   - Coding style and naming conventions
   - Build, test, and development commands
   - Commit and PR conventions
   - Security and configuration tips

2. Check for existing project docs:
    - requirements_analysis.md (from previous features)
    - architecture.md (existing system design)
    - features/ directory (previously completed features)
    - README.md (project overview)
    - Any existing source code

3. If previous features exist, read their docs:
    - features/*/docs/ (design docs for previous features)

3. Search the vector database for existing knowledge using search_memory():
   ```python
   # Search for technology decisions
   mcp__hephaestus__search_memory({
       "query": "technology stack decisions framework language",
       "limit": 10
   })

   # Search for architecture patterns
   mcp__hephaestus__search_memory({
       "query": "architecture patterns system design components",
       "limit": 10
   })

   # Search for constraints
   mcp__hephaestus__search_memory({
       "query": "constraints must not rules security requirements",
       "limit": 10
   })

   # Search for previous feature context
   mcp__hephaestus__search_memory({
       "query": "completed features implemented components",
       "limit": 10,
       "memory_type": "decision"
   })
   ```

4. Understand the LARGER VISION:
   - What is this project trying to achieve?
   - What has already been built?
   - What are the next logical pieces?
   - How does this new design fit in?

5. GREP/SEARCH other design docs for relevant keywords:
   ```bash
   # Find all design docs in the project
   find . -name "*.md" -not -path "./.venv/*" -not -path "./node_modules/*" | head -30

   # Search for keywords from the current design in other docs
   # Extract key terms from your design document first, then search:
   grep -r "keyword1\|keyword2\|keyword3" --include="*.md" . 2>/dev/null | head -20

   # Search for technology references across all docs
   grep -r "fastapi\|react\|postgres\|docker\|kubernetes" --include="*.md" . 2>/dev/null | head -20

   # Search for architecture patterns mentioned in other designs
   grep -r "microservice\|monolith\|event-driven\|REST\|GraphQL" --include="*.md" . 2>/dev/null | head -20

   # Search for existing component references
   grep -r "auth\|database\|api\|frontend\|backend" --include="*.md" . 2>/dev/null | grep -v ".venv" | head -20
   ```

   This helps you understand:
   - What other designs reference the same components
   - Whether similar features have been designed before
   - What technology choices are consistent across designs
   - What naming conventions are used

═══════════════════════════════════════════════════════════════════════
STEP 1: READ THE DESIGN DOCUMENT
═══════════════════════════════════════════════════════════════════════

Read the design document provided in the task description.
This is the SPECIFIC feature you need to extract requirements for.

Also look for:
- PRD.md, DESIGN.md, SPEC.md, REQUIREMENTS.md in the project
- Any related design documents in the features/ directory

═══════════════════════════════════════════════════════════════════════
STEP 2: EXTRACT REQUIREMENTS WITH CONTEXT
═══════════════════════════════════════════════════════════════════════

For each requirement, consider:

A. Is this NEW or does it overlap with existing functionality?
   - If overlapping: document how it integrates with existing code
   - If new: document the full scope

B. What existing components does it depend on?
   - Reference existing architecture.md
   - Note which existing modules need modification

C. What does it enable for the future?
   - How does this feature enable subsequent features?
   - What capabilities does it unlock?

═══════════════════════════════════════════════════════════════════════
STEP 3: RESPECT EXISTING TECHNOLOGY STACK
═══════════════════════════════════════════════════════════════════════

CRITICAL: If the project already has a technology stack:
- Use the SAME languages, frameworks, and patterns
- Follow existing code conventions
- Integrate with existing infrastructure
- Do NOT introduce new frameworks without explicit justification

If the design doc specifies different technologies, note the conflict
and flag it for the architect to resolve.

═══════════════════════════════════════════════════════════════════════
STEP 4: CREATE REQUIREMENTS DOCUMENT
═══════════════════════════════════════════════════════════════════════

CRITICAL PATH RULE: You MUST use the FULL ABSOLUTE PATHS from your task description.
- NEVER write files to the current working directory or project root.
- ALWAYS use the "Docs Path:" value for ALL generated docs (.md, .json, .txt).
- ALWAYS use the "Project Path:" value for ALL implementation code.
- Your task description contains the exact paths — copy them exactly.

Read your task description for the correct paths:
- "Design Document:" tells you where the design doc is
- "Docs Path:" tells you where to write requirements_analysis.md
- "Project Path:" tells you where implementation code goes

Write requirements_analysis.md to the "Docs Path" location from your task description.

# Requirements Analysis: [Feature Name]

## Project Context
[Summary of the larger project and how this feature fits in]

## Existing System
[What's already built - key components, tech stack, patterns]

## Feature Requirements (from design document)

### Functional Requirements
1. [Requirement] - Acceptance: [criteria]
   - Integration with existing: [which components]
   - Is new: [yes/no]

### Non-Functional Requirements
- Performance: [targets]
- Security: [requirements]
- ...

## Integration Points
- [Component A] needs [modification] to support [new feature]
- [New component] depends on [existing component]

## Technology Constraints
- Must use: [existing stack]
- Must follow: [existing patterns]
- Must not: [constraints from project context]

## Success Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- ...

═══════════════════════════════════════════════════════════════════════
STEP 5: SAVE TO MEMORY (using save_memory MCP tool)
═══════════════════════════════════════════════════════════════════════

Save key findings to the vector database using save_memory():

```python
# Save technology decisions
mcp__hephaestus__save_memory({
    "content": f"Technology stack for {project_name}: [list stack] because [reasons]",
    "memory_type": "decision",
    "tags": ["technology", "architecture"]
})

# Save architecture decisions
mcp__hephaestus__save_memory({
    "content": f"Architecture pattern: [pattern] for {project_name} because [reasons]",
    "memory_type": "decision",
    "tags": ["architecture", "design"]
})

# Save constraints
mcp__hephaestus__save_memory({
    "content": f"Constraints for {feature_name}: [list constraints and MUST NOT rules]",
    "memory_type": "warning",
    "tags": ["constraints", "rules"]
})

# Save component inventory
mcp__hephaestus__save_memory({
    "content": f"Components identified for {feature_name}: [list components with dependencies]",
    "memory_type": "discovery",
    "tags": ["components", "inventory"]
})
```

These memories will be searchable by future Phase 1 agents processing subsequent designs.

═══════════════════════════════════════════════════════════════════════
STEP 6: MARK TASK COMPLETE
═══════════════════════════════════════════════════════════════════════

Your requirements document is complete. Mark your task as done.
The orchestrator will advance to the next phase automatically.

Ensure requirements_analysis.md is saved to the "Docs Path" location.

═══════════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════════

DO:
- Read existing project docs before starting
- Understand the larger vision
- Respect existing technology choices
- Document integration points
- Flag technology conflicts

DO NOT:
- Ignore existing code and architecture
- Substitute technologies without justification
- Create requirements that conflict with existing system
- Skip context gathering
""",
    outputs=[
        "requirements_analysis.md with structured requirements and project context",
        "Integration point documentation",
        "Technology constraint analysis",
        "Phase 2 architecture task with full context",
    ],
    next_steps=[
        "Phase 2 will create detailed architecture respecting existing system",
        "Architecture will plan integration with existing components",
    ],
)

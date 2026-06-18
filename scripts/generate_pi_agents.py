#!/usr/bin/env python3
"""
Extract autopilot phase definitions and generate pi agent files.

Reads phase_*.py files from src/autopilot/ and generates pi agent .md files
in the agents/pi/ directory.
"""

import os
import re
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

def extract_phase_info(phase_file: Path) -> dict:
    """Extract phase information from a Python phase file."""
    content = phase_file.read_text()
    
    # Extract phase name
    name_match = re.search(r'name="([^"]+)"', content)
    name = name_match.group(1) if name_match else phase_file.stem
    
    # Extract description (first docstring after Phase())
    desc_match = re.search(r'description="""(.*?)"""', content, re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ""
    
    # Extract additional_notes (the detailed instructions)
    notes_match = re.search(r'additional_notes="""(.*?)"""', content, re.DOTALL)
    additional_notes = notes_match.group(1).strip() if notes_match else ""
    
    # Extract done_definitions
    done_match = re.search(r'done_definitions=\[(.*?)\]', content, re.DOTALL)
    done_definitions = []
    if done_match:
        done_text = done_match.group(1)
        done_definitions = re.findall(r'"([^"]+)"', done_text)
    
    return {
        "name": name,
        "description": description,
        "additional_notes": additional_notes,
        "done_definitions": done_definitions,
    }

def generate_pi_agent(phase_info: dict, phase_num: int) -> str:
    """Generate pi agent markdown from phase info."""
    
    # Clean up the name for pi agent format
    agent_name = f"hephaestus-{phase_info['name'].replace('_', '-')}"
    
    # Determine which MCP tools this agent needs based on phase
    mcp_tools = [
        "mcp:hephaestus/save_memory",
        "mcp:hephaestus/search_memory", 
        "mcp:hephaestus/update_task_status",
        "mcp:hephaestus/create_task",
        "mcp:hephaestus/get_task_status",
    ]
    
    # All phases need these core tools
    tools_str = "read, write, edit, bash, grep, find, ls, " + ", ".join(mcp_tools)
    
    # Build the system prompt from phase info
    system_prompt = f"""{phase_info['description']}

{phase_info['additional_notes']}

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
"""
    
    for criterion in phase_info['done_definitions']:
        system_prompt += f"• {criterion}\n"
    
    system_prompt += """
═══ WORKFLOW ═══
1. Read your task description carefully
2. Follow the phase instructions above
3. Complete all completion criteria
4. Call update_task_status(status="done", summary="...") when complete
5. If blocking errors, call update_task_status(status="failed", summary="...")
"""
    
    # Generate the pi agent markdown
    agent_md = f"""---
name: {agent_name}
description: |
  Hephaestus Phase {phase_num}: {phase_info['name'].replace('_', ' ').title()}
  {phase_info['description'][:100]}...
model: openrouter/xiaomi/mimo-v2.5
tools: {tools_str}
systemPromptMode: replace
inheritProjectContext: true
inheritSkills: false
---

{system_prompt}
"""
    
    return agent_md

def main():
    """Main function to generate pi agents from phase files."""
    project_root = Path(__file__).parent.parent
    phases_dir = project_root / "src" / "autopilot"
    output_dir = project_root / "agents" / "pi"
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all phase files
    phase_files = sorted(phases_dir.glob("phase_*.py"))
    
    print(f"Found {len(phase_files)} phase files")
    
    for phase_file in phase_files:
        # Skip __init__.py and non-phase files
        if phase_file.name.startswith("__"):
            continue
            
        # Extract phase number from filename
        num_match = re.search(r'phase_(\d+)_', phase_file.name)
        if not num_match:
            continue
        phase_num = int(num_match.group(1))
        
        # Extract phase info
        phase_info = extract_phase_info(phase_file)
        
        # Generate pi agent
        agent_content = generate_pi_agent(phase_info, phase_num)
        
        # Write to file
        agent_filename = f"hephaestus-{phase_info['name'].replace('_', '-')}.md"
        agent_path = output_dir / agent_filename
        agent_path.write_text(agent_content)
        
        print(f"  Generated: {agent_filename}")
    
    print(f"\nGenerated {len(phase_files)} pi agents in {output_dir}")
    print("Run install.sh to install them into ~/.pi/agent/agents/")

if __name__ == "__main__":
    main()

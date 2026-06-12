#!/usr/bin/env python3
"""
Hephaestus Autopilot Runner

Fully automated workflow execution with human-in-the-loop only for
major decisions and impasses. Monitors Guardian interventions and
stuck agents to detect when human input is needed.

Usage:
    # Single workflow
    python autopilot.py --workflow qa --path /path/to/project

    # Full cycle (index-repo -> feature-dev -> bug-fix -> qa -> doc-gen)
    python autopilot.py --cycle --path /path/to/project

    # Orchestrator mode: cycle multiple times until QA passes
    python autopilot.py --cycle --iterations 5 --path /path/to/project

Options:
    --workflow ID       Workflow definition to run (default: prd-to-software)
    --cycle             Run all workflows in cycle order
    --cycle-on-failure  Continue cycle even if a workflow fails
    --iterations N      Run cycle N times, checking QA report between each (orchestrator mode)
    --path PATH         Project working directory
    --drop-db           Drop database before starting
    --description DESC  Workflow description
    --max-hours N       Maximum hours before auto-stop

Spec file (qa_spec.json in project root):
    {
        "max_failed_tests": 0,
        "max_critical_issues": 0,
        "required_pass_rate": 100
    }
"""

import argparse
import os
import signal
import sys
import time
import json
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

HEPHAESTUS_DIR = Path(__file__).parent
API_BASE = "http://127.0.0.1:8300"
POLL_INTERVAL = 15  # seconds between status checks
STUCK_THRESHOLD = 3  # consecutive stuck checks before intervention
GUARDIAN_CHECK_INTERVAL = 60  # seconds between guardian reviews


class AutopilotLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "autopilot.log"
        self.events_file = log_dir / "events.jsonl"

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        print(line)
        with open(self.log_file, "a") as f:
            f.write(line + "\n")

    def event(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            **data,
        }
        with open(self.events_file, "a") as f:
            f.write(json.dumps(entry) + "\n")


def wait_for_backend(timeout: int = 60) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{API_BASE}/health", timeout=2)
            if r.status_code == 200 and r.json().get("status") == "healthy":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def get_workflow_status(workflow_id: str) -> dict:
    try:
        r = requests.get(f"{API_BASE}/api/workflow-executions/{workflow_id}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def get_tasks(status: str = None) -> list:
    try:
        params = {"status": status} if status else {}
        r = requests.get(f"{API_BASE}/api/tasks", params=params, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else data.get("tasks", [])
    except Exception:
        pass
    return []


def get_agents() -> list:
    try:
        r = requests.get(f"{API_BASE}/api/agents", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else data.get("agents", [])
    except Exception:
        pass
    return []


def get_guardian_analyses() -> list:
    try:
        r = requests.get(f"{API_BASE}/api/guardian/analyses", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else data.get("analyses", [])
    except Exception:
        pass
    return []


def check_api_credits() -> tuple:
    """
    Check if OpenRouter API credits are exhausted.
    Returns (out_of_credits: bool, reason: str)
    """
    try:
        r = requests.get(f"{API_BASE}/api/agents", timeout=5)
        if r.status_code != 200:
            return False, ""

        agents = r.json() if isinstance(r.json(), list) else r.json().get("agents", [])

        for agent in agents:
            output = agent.get("output_log", "") or ""
            output_lower = output.lower()

            credit_errors = [
                "insufficient funds",
                "credit",
                "quota exceeded",
                "rate limit",
                "billing",
                "payment required",
                "402",
                "429",
                "exceeded",
                "out of credits",
            ]

            for err in credit_errors:
                if err in output_lower:
                    return True, f"API credit issue detected: {err}"

        tasks = get_tasks(status="failed")
        for task in tasks:
            error = task.get("error", "") or ""
            error_lower = error.lower()

            for err in credit_errors:
                if err in error_lower:
                    return True, f"API credit issue in task: {err}"

    except Exception:
        pass

    return False, ""


def detect_impasse(stuck_count: int, agents: list, guardian_analyses: list) -> tuple:
    """
    Detect if the workflow is stuck or hitting a major decision point.
    Returns (needs_intervention: bool, reason: str)
    """
    # Check for API credit issues first (highest priority)
    out_of_credits, credit_reason = check_api_credits()
    if out_of_credits:
        return True, credit_reason

    # Check for stuck agents
    stuck_agents = [a for a in agents if a.get("health_check_failures", 0) >= 3]
    if stuck_agents:
        names = [a.get("agent_id", "unknown")[:20] for a in stuck_agents]
        return True, f"Stuck agents: {', '.join(names)}"

    # Check for failed tasks
    failed_tasks = get_tasks(status="failed")
    if failed_tasks:
        descriptions = [t.get("description", "")[:50] for t in failed_tasks[:3]]
        return True, f"Failed tasks: {descriptions}"

    # Check Guardian analyses for intervention recommendations
    if guardian_analyses:
        latest = guardian_analyses[-1]
        interventions = latest.get("interventions", [])
        if interventions:
            high_priority = [i for i in interventions if i.get("priority") == "high"]
            if high_priority:
                reasons = [i.get("reason", "")[:50] for i in high_priority[:2]]
                return True, f"Guardian intervention needed: {reasons}"

    # Check if no agents are working and tasks are pending
    active_agents = [a for a in agents if a.get("status") == "working"]
    pending_tasks = get_tasks(status="pending")
    in_progress_tasks = get_tasks(status="in_progress")

    if not active_agents and not in_progress_tasks and pending_tasks:
        return True, "No active agents but tasks are pending"

    return False, ""


def prompt_human(reason: str, logger: AutopilotLogger) -> str:
    """Prompt human for input when intervention is needed."""
    logger.log(f"DECISION POINT: {reason}", "INTERVENTION")
    print("\n" + "=" * 60)
    print("HUMAN INTERVENTION REQUIRED")
    print("=" * 60)
    print(f"Reason: {reason}")
    print("\nOptions:")
    print("  [c] Continue - agents should keep working")
    print("  [p] Pause - stop all agents and wait")
    print("  [s] Skip - mark current phase as done, move to next")
    print("  [q] Quit - shutdown autopilot")
    print("=" * 60)

    while True:
        choice = input("Your choice: ").strip().lower()
        if choice in ("c", "p", "s", "q"):
            logger.event("human_input", {"choice": choice, "reason": reason})
            return choice
        print("Invalid choice. Enter c, p, s, or q.")


CYCLE_ORDER = ["index-repo", "feature-dev", "bug-fix", "qa", "doc-gen"]

# Default spec: what "up to spec" means
DEFAULT_SPEC = {
    "max_failed_tests": 0,
    "max_critical_issues": 0,
    "required_pass_rate": 100,  # percent
}


def review_qa_report(project_path: str, spec: dict, logger: AutopilotLogger) -> tuple:
    """
    Review the QA report using the Conductor's LLM analysis.
    Returns (up_to_spec: bool, reasons: list[str])
    """
    reasons = []

    # Look for the most recent QA report
    qa_report_paths = [
        Path(project_path) / "qa_report.html",
        Path(project_path) / "qa_report.md",
        Path(project_path) / "tests" / "qa" / "qa_report.json",
    ]

    qa_report_content = None
    for report_path in qa_report_paths:
        if report_path.exists():
            logger.log(f"Found QA report: {report_path}")
            try:
                qa_report_content = report_path.read_text()
                break
            except Exception as e:
                logger.log(f"Error reading {report_path}: {e}", "WARN")

    if not qa_report_content:
        logger.log("No QA report found", "WARN")
        return False, ["No QA report generated"]

    # Read PRD if it exists
    prd_content = ""
    prd_path = Path(project_path) / "PRD.md"
    if prd_path.exists():
        try:
            prd_content = prd_path.read_text()
        except Exception:
            pass

    # Read TESTING.md for phase intent
    phase_intent = "Comprehensive QA testing with browser automation and log analysis"
    testing_path = Path(project_path) / "TESTING.md"
    if testing_path.exists():
        try:
            phase_intent = testing_path.read_text()[:2000]
        except Exception:
            pass

    # Use Conductor's LLM review
    try:
        sys.path.insert(0, str(HEPHAESTUS_DIR))
        from src.monitoring.conductor import Conductor
        from src.core.database import DatabaseManager
        from src.agents.manager import AgentManager

        db_manager = DatabaseManager(str(HEPHAESTUS_DIR / "hephaestus.db"))
        agent_manager = AgentManager(None)  # No agent manager needed for review

        conductor = Conductor(db_manager, agent_manager)

        import asyncio
        result = asyncio.run(conductor.review_qa_report(
            qa_report=qa_report_content,
            prd_content=prd_content,
            phase_intent=phase_intent,
            spec=spec,
        ))

        up_to_spec = result.get("up_to_spec", False)
        reasoning = result.get("reasoning", "No reasoning provided")
        recommendations = result.get("recommendations", [])
        critical_issues = result.get("critical_issues", [])

        logger.log(f"Conductor verdict: {'PASS' if up_to_spec else 'FAIL'}")
        logger.log(f"Reasoning: {reasoning[:200]}...")

        if not up_to_spec:
            reasons.append(f"Conductor review failed: {reasoning[:200]}")
            for rec in recommendations[:3]:
                reasons.append(f"Recommendation: {rec}")
            for issue in critical_issues[:3]:
                reasons.append(f"Critical: {issue}")

        return up_to_spec, reasons

    except Exception as e:
        logger.log(f"Conductor review failed, falling back to basic check: {e}", "WARN")

        # Fallback to basic keyword check
        failed_count = qa_report_content.lower().count("failed") + qa_report_content.lower().count("❌")
        passed_count = qa_report_content.lower().count("passed") + qa_report_content.lower().count("✅")
        total = passed_count + failed_count
        pass_rate = (passed_count / total * 100) if total > 0 else 0

        if failed_count > spec.get("max_failed_tests", 0):
            reasons.append(f"{failed_count} failed tests (max: {spec['max_failed_tests']})")
        if pass_rate < spec.get("required_pass_rate", 100):
            reasons.append(f"Pass rate {pass_rate:.0f}% below requirement {spec['required_pass_rate']}%")

        return len(reasons) == 0, reasons


def run_orchestrator(args):
    """Run the cycle multiple times until output is up to spec."""
    log_dir = Path.home() / ".hephaestus" / "autopilot" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    logger = AutopilotLogger(log_dir)
    logger.log("Orchestrator mode: cycling until spec is met")
    logger.log(f"Max iterations: {args.iterations}")
    logger.log(f"Project path: {args.path}")
    logger.log(f"Logs: {log_dir}")

    # Import and initialize SDK
    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition
    from example_workflows.prd_to_software.phases import PRD_PHASES, PRD_WORKFLOW_CONFIG, PRD_LAUNCH_TEMPLATE
    from example_workflows.bug_fix.phases import BUG_FIX_PHASES, BUG_FIX_WORKFLOW_CONFIG, BUG_FIX_LAUNCH_TEMPLATE
    from example_workflows.index_repo.phases import INDEX_REPO_PHASES, INDEX_REPO_CONFIG, INDEX_REPO_LAUNCH_TEMPLATE
    from example_workflows.feature_development.phases import FEATURE_DEV_PHASES, FEATURE_DEV_CONFIG, FEATURE_DEV_LAUNCH_TEMPLATE
    from example_workflows.documentation_generation.phases import DOC_GEN_PHASES, DOC_GEN_CONFIG, DOC_GEN_LAUNCH_TEMPLATE
    from example_workflows.qa.phases import QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE

    cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL", "opencode")

    workflow_defs = {
        "prd-to-software": ("PRD to Software Builder", PRD_PHASES, PRD_WORKFLOW_CONFIG, PRD_LAUNCH_TEMPLATE),
        "bug-fix": ("Bug Fix", BUG_FIX_PHASES, BUG_FIX_WORKFLOW_CONFIG, BUG_FIX_LAUNCH_TEMPLATE),
        "index-repo": ("Index Repository", INDEX_REPO_PHASES, INDEX_REPO_CONFIG, INDEX_REPO_LAUNCH_TEMPLATE),
        "feature-dev": ("Feature Development", FEATURE_DEV_PHASES, FEATURE_DEV_CONFIG, FEATURE_DEV_LAUNCH_TEMPLATE),
        "doc-gen": ("Documentation Generation", DOC_GEN_PHASES, DOC_GEN_CONFIG, DOC_GEN_LAUNCH_TEMPLATE),
        "qa": ("QA Testing", QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE),
    }

    workflows_to_run = [w for w in CYCLE_ORDER if w in workflow_defs]

    definitions = [
        WorkflowDefinition(
            id=wf_id,
            name=wf_name,
            phases=wf_phases,
            config=wf_config,
            description=wf_name,
            launch_template=wf_template,
        )
        for wf_id, (wf_name, wf_phases, wf_config, wf_template) in workflow_defs.items()
    ]

    logger.log("Initializing SDK...")
    sdk = HephaestusSDK(
        workflow_definitions=definitions,
        database_path=str(HEPHAESTUS_DIR / "hephaestus.db"),
        qdrant_url="http://localhost:6333",
        working_directory=args.path,
        mcp_port=8300,
        monitoring_interval=60,
        llm_provider="openrouter",
        llm_model="xiaomi/mimo-v2.5",
        default_cli_tool=cli_tool,
        main_repo_path=args.path,
        project_root=args.path,
        auto_commit=True,
        conflict_resolution="newest_file_wins",
        worktree_branch_prefix="orchestrator-",
    )

    logger.log("Starting services...")
    try:
        sdk.start(enable_tui=False, timeout=60)
    except Exception as e:
        logger.log(f"Failed to start: {e}", "ERROR")
        sys.exit(1)

    logger.log("Services started.")

    # Load spec
    spec = DEFAULT_SPEC.copy()
    spec_file = Path(args.path) / "qa_spec.json"
    if spec_file.exists():
        try:
            import json as json_mod
            with open(spec_file) as f:
                spec.update(json_mod.load(f))
            logger.log(f"Loaded QA spec from {spec_file}: {spec}")
        except Exception as e:
            logger.log(f"Error loading spec: {e}, using defaults", "WARN")

    total_start = time.time()
    all_results = {}

    try:
        for iteration in range(1, args.iterations + 1):
            logger.log("")
            logger.log("=" * 60)
            logger.log(f"ITERATION {iteration}/{args.iterations}")
            logger.log("=" * 60)

            cycle_results = {}

            for i, wf_id in enumerate(workflows_to_run):
                logger.log(f"--- Workflow {i+1}/{len(workflows_to_run)}: {wf_id} ---")
                result = run_single_workflow(sdk, wf_id, args, logger)
                cycle_results[wf_id] = result

                if result == "interrupted":
                    logger.log("Interrupted by user")
                    all_results[iteration] = cycle_results
                    return

                if result == "failed" and not args.cycle_on_failure:
                    logger.log(f"Workflow {wf_id} failed. Stopping iteration")
                    break

            all_results[iteration] = cycle_results

            # Review QA report
            logger.log("")
            logger.log("Reviewing QA report...")
            up_to_spec, reasons = review_qa_report(args.path, spec, logger)

            if up_to_spec:
                logger.log("")
                logger.log("✓ OUTPUT IS UP TO SPEC")
                logger.log(f"Passed after {iteration} iteration(s)")
                break
            else:
                logger.log("")
                logger.log("✗ OUTPUT NOT UP TO SPEC:")
                for reason in reasons:
                    logger.log(f"  - {reason}")

                if iteration < args.iterations:
                    logger.log(f"Starting iteration {iteration + 1} to address issues...")
                else:
                    logger.log(f"Max iterations ({args.iterations}) reached. Output may need manual review.")

    except KeyboardInterrupt:
        logger.log("Interrupted by user")
    finally:
        total_elapsed = int(time.time() - total_start)
        logger.log("")
        logger.log("=" * 60)
        logger.log("ORCHESTRATOR COMPLETE")
        logger.log(f"Total time: {total_elapsed}s")
        logger.log(f"Iterations: {len(all_results)}")
        for iteration, results in all_results.items():
            logger.log(f"  Iteration {iteration}:")
            for wf, status in results.items():
                logger.log(f"    {wf}: {status}")
        logger.log("=" * 60)

        sdk.shutdown(graceful=True, timeout=15)
        logger.event("orchestrator_stop", {
            "iterations": len(all_results),
            "results": all_results,
            "elapsed_seconds": total_elapsed,
        })


def run_single_workflow(sdk, workflow_id: str, args, logger: AutopilotLogger) -> str:
    """Run a single workflow and return its final status."""
    logger.log(f"Launching workflow: {workflow_id}")
    logger.event("workflow_launch", {"workflow": workflow_id, "path": args.path})

    try:
        exec_id = sdk.start_workflow(
            definition_id=workflow_id,
            description=args.description or f"Autopilot: {workflow_id}",
            working_directory=args.path,
        )
        logger.log(f"Workflow launched: {exec_id}")
    except Exception as e:
        logger.log(f"Failed to launch workflow {workflow_id}: {e}", "ERROR")
        return "failed"

    stuck_count = 0
    last_guardian_check = 0
    last_credit_check = 0
    start_time = time.time()

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            wf_status = get_workflow_status(exec_id)
            agents = get_agents()
            active_agents = [a for a in agents if a.get("status") == "working"]
            pending = get_tasks(status="pending")
            in_progress = get_tasks(status="in_progress")
            done = get_tasks(status="done")
            failed = get_tasks(status="failed")

            elapsed = int(time.time() - start_time)
            logger.log(
                f"[{workflow_id}] [{elapsed}s] Agents: {len(active_agents)} active | "
                f"Tasks: {len(pending)} pending, {len(in_progress)} active, "
                f"{len(done)} done, {len(failed)} failed"
            )

            wf_state = wf_status.get("status", "")
            if wf_state in ("completed", "failed"):
                logger.log(f"Workflow {wf_state}: {exec_id}")
                logger.event("workflow_complete", {
                    "workflow_id": workflow_id,
                    "exec_id": exec_id,
                    "status": wf_state,
                    "elapsed_seconds": elapsed,
                })
                return wf_state

            now = time.time()

            # Credit check
            if now - last_credit_check >= POLL_INTERVAL:
                out_of_credits, credit_reason = check_api_credits()
                last_credit_check = now

                if out_of_credits:
                    stuck_count += 1
                    if stuck_count >= 1:
                        choice = prompt_human(credit_reason, logger)
                        if choice == "q":
                            logger.log("User requested shutdown", "INTERVENTION")
                            return "interrupted"
                        elif choice == "p":
                            logger.log("Pausing - waiting for credits", "INTERVENTION")
                            input("Press Enter after adding credits to resume...")
                            stuck_count = 0
                        elif choice in ("c", "s"):
                            stuck_count = 0
                else:
                    stuck_count = 0

            # Guardian check
            if now - last_guardian_check >= GUARDIAN_CHECK_INTERVAL:
                guardian_analyses = get_guardian_analyses()
                last_guardian_check = now

                needs_intervention, reason = detect_impasse(stuck_count, agents, guardian_analyses)

                if needs_intervention:
                    stuck_count += 1
                    if stuck_count >= STUCK_THRESHOLD:
                        choice = prompt_human(reason, logger)
                        if choice == "q":
                            logger.log("User requested shutdown", "INTERVENTION")
                            return "interrupted"
                        elif choice == "p":
                            logger.log("Pausing - waiting for user", "INTERVENTION")
                            input("Press Enter to resume...")
                            stuck_count = 0
                        elif choice in ("s", "c"):
                            stuck_count = 0
                else:
                    stuck_count = 0

            # Timeout
            if args.max_hours and elapsed > args.max_hours * 3600:
                logger.log(f"Reached max time limit ({args.max_hours}h)", "TIMEOUT")
                return "timeout"

    except KeyboardInterrupt:
        logger.log("Interrupted by user")
        return "interrupted"


def run_autopilot(args):
    log_dir = Path.home() / ".hephaestus" / "autopilot" / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    logger = AutopilotLogger(log_dir)
    logger.log(f"Autopilot mode: {'cycle' if args.cycle else 'single'}")
    logger.log(f"Project path: {args.path}")
    logger.log(f"Logs: {log_dir}")

    # Import and initialize SDK
    sys.path.insert(0, str(HEPHAESTUS_DIR))
    from src.sdk import HephaestusSDK
    from src.sdk.models import WorkflowDefinition
    from example_workflows.prd_to_software.phases import PRD_PHASES, PRD_WORKFLOW_CONFIG, PRD_LAUNCH_TEMPLATE
    from example_workflows.bug_fix.phases import BUG_FIX_PHASES, BUG_FIX_WORKFLOW_CONFIG, BUG_FIX_LAUNCH_TEMPLATE
    from example_workflows.index_repo.phases import INDEX_REPO_PHASES, INDEX_REPO_CONFIG, INDEX_REPO_LAUNCH_TEMPLATE
    from example_workflows.feature_development.phases import FEATURE_DEV_PHASES, FEATURE_DEV_CONFIG, FEATURE_DEV_LAUNCH_TEMPLATE
    from example_workflows.documentation_generation.phases import DOC_GEN_PHASES, DOC_GEN_CONFIG, DOC_GEN_LAUNCH_TEMPLATE
    from example_workflows.qa.phases import QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE

    cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL", "opencode")

    workflow_defs = {
        "prd-to-software": ("PRD to Software Builder", PRD_PHASES, PRD_WORKFLOW_CONFIG, PRD_LAUNCH_TEMPLATE),
        "bug-fix": ("Bug Fix", BUG_FIX_PHASES, BUG_FIX_WORKFLOW_CONFIG, BUG_FIX_LAUNCH_TEMPLATE),
        "index-repo": ("Index Repository", INDEX_REPO_PHASES, INDEX_REPO_CONFIG, INDEX_REPO_LAUNCH_TEMPLATE),
        "feature-dev": ("Feature Development", FEATURE_DEV_PHASES, FEATURE_DEV_CONFIG, FEATURE_DEV_LAUNCH_TEMPLATE),
        "doc-gen": ("Documentation Generation", DOC_GEN_PHASES, DOC_GEN_CONFIG, DOC_GEN_LAUNCH_TEMPLATE),
        "qa": ("QA Testing", QA_PHASES, QA_WORKFLOW_CONFIG, QA_LAUNCH_TEMPLATE),
    }

    # Validate workflows
    if args.cycle:
        workflows_to_run = [w for w in CYCLE_ORDER if w in workflow_defs]
        logger.log(f"Cycle order: {' -> '.join(workflows_to_run)}")
    else:
        if args.workflow not in workflow_defs:
            logger.log(f"Unknown workflow: {args.workflow}. Available: {list(workflow_defs.keys())}", "ERROR")
            sys.exit(1)
        workflows_to_run = [args.workflow]

    definitions = [
        WorkflowDefinition(
            id=wf_id,
            name=wf_name,
            phases=wf_phases,
            config=wf_config,
            description=wf_name,
            launch_template=wf_template,
        )
        for wf_id, (wf_name, wf_phases, wf_config, wf_template) in workflow_defs.items()
    ]

    logger.log("Initializing SDK...")
    sdk = HephaestusSDK(
        workflow_definitions=definitions,
        database_path=str(HEPHAESTUS_DIR / "hephaestus.db"),
        qdrant_url="http://localhost:6333",
        working_directory=args.path,
        mcp_port=8300,
        monitoring_interval=60,
        llm_provider="openrouter",
        llm_model="xiaomi/mimo-v2.5",
        default_cli_tool=cli_tool,
        main_repo_path=args.path,
        project_root=args.path,
        auto_commit=True,
        conflict_resolution="newest_file_wins",
        worktree_branch_prefix="autopilot-",
    )

    logger.log("Starting services...")
    try:
        sdk.start(enable_tui=False, timeout=60)
    except Exception as e:
        logger.log(f"Failed to start: {e}", "ERROR")
        sys.exit(1)

    logger.log("Services started.")

    cycle_results = {}
    total_start = time.time()

    try:
        for i, wf_id in enumerate(workflows_to_run):
            logger.log(f"--- Workflow {i+1}/{len(workflows_to_run)}: {wf_id} ---")
            result = run_single_workflow(sdk, wf_id, args, logger)
            cycle_results[wf_id] = result

            if result == "interrupted":
                logger.log("Cycle interrupted by user")
                break

            if result == "failed" and not args.cycle_on_failure:
                logger.log(f"Workflow {wf_id} failed. Stopping cycle (--cycle-on-failure to continue)")
                break

            logger.log(f"Workflow {wf_id} finished with status: {result}")

    except KeyboardInterrupt:
        logger.log("Interrupted by user")
    finally:
        total_elapsed = int(time.time() - total_start)
        logger.log("=" * 60)
        logger.log("AUTOPILOT COMPLETE")
        logger.log(f"Total time: {total_elapsed}s")
        for wf, status in cycle_results.items():
            logger.log(f"  {wf}: {status}")
        logger.log("=" * 60)

        sdk.shutdown(graceful=True, timeout=15)
        logger.event("autopilot_stop", {
            "results": cycle_results,
            "elapsed_seconds": total_elapsed,
        })


def main():
    parser = argparse.ArgumentParser(description="Hephaestus Autopilot Runner")
    parser.add_argument("--workflow", default="prd-to-software",
                        choices=["prd-to-software", "bug-fix", "index-repo", "feature-dev", "doc-gen", "qa"],
                        help="Workflow definition to run")
    parser.add_argument("--path", required=True, help="Project working directory")
    parser.add_argument("--drop-db", action="store_true", help="Drop database before starting")
    parser.add_argument("--description", help="Workflow description")
    parser.add_argument("--max-hours", type=float, help="Maximum hours before auto-stop")
    parser.add_argument("--cycle", action="store_true",
                        help="Run all workflows in cycle: index-repo -> feature-dev -> bug-fix -> qa -> doc-gen")
    parser.add_argument("--cycle-on-failure", action="store_true",
                        help="Continue cycle even if a workflow fails")
    parser.add_argument("--iterations", type=int, default=1,
                        help="Number of cycle iterations (orchestrator mode: run until QA passes)")
    args = parser.parse_args()

    if args.drop_db:
        db = HEPHAESTUS_DIR / "hephaestus.db"
        if db.exists():
            db.unlink()
            print(f"Dropped {db}")

    if args.iterations > 1:
        args.cycle = True
        run_orchestrator(args)
    else:
        run_autopilot(args)


if __name__ == "__main__":
    main()

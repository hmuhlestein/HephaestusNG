#!/usr/bin/env python3
"""
Hephaestus Monitoring Service

Starts the intelligent monitoring and self-healing system for autonomous agents.
This service monitors agent health, detects stuck/failed agents, and performs
LLM-powered interventions including nudging, restarting, and recreating agents.
"""

import asyncio
import faulthandler
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent / "src"))

from src.agents.manager import AgentManager
from src.core.database import DatabaseManager
from src.core.simple_config import get_config
from src.interfaces import get_llm_provider
from src.memory.rag import RAGSystem
from src.monitoring.monitor import MonitoringLoop
from src.phases import PhaseManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", mode="a"),
    ],
)

logger = logging.getLogger(__name__)

# Global monitoring loop instance for signal handling
monitoring_loop = None


async def setup_monitoring_system():
    """Set up all components for the monitoring system."""
    logger.info("Setting up Hephaestus monitoring system...")

    try:
        # Get configuration
        config = get_config()
        logger.info(
            f"Using monitoring interval: {config.monitoring_interval_seconds} seconds"
        )

        # Initialize database manager
        db_manager = DatabaseManager()
        logger.info("Database manager initialized")

        # Initialize LLM provider
        llm_provider = get_llm_provider()
        logger.info(f"LLM provider initialized: {llm_provider.__class__.__name__}")

        # Initialize agent manager
        agent_manager = AgentManager(db_manager, llm_provider)
        logger.info("Agent manager initialized")

        # Initialize vector store manager
        from src.memory.vector_store import VectorStoreManager

        vector_store = VectorStoreManager(
            qdrant_url=config.qdrant_url,
            collection_prefix=config.qdrant_collection_prefix,
        )
        logger.info("Vector store manager initialized")

        # Initialize RAG system
        rag_system = RAGSystem(vector_store, llm_provider)
        logger.info("RAG system initialized")

        # Initialize phase manager (optional)
        phase_manager = None
        try:
            phase_manager = PhaseManager(db_manager)
            logger.info("Phase manager initialized")

            # Load any active workflow from the database
            logger.info("[DIAGNOSTIC] Checking for active workflows to resume...")
            workflow_id = phase_manager.load_active_workflow()

            if workflow_id:
                logger.info(
                    f"[DIAGNOSTIC] ✅ Loaded active workflow: {workflow_id[:8]}..."
                )
                logger.info(
                    "[DIAGNOSTIC] ✅ Diagnostic agent monitoring ENABLED for this workflow"
                )
            else:
                logger.info(
                    "[DIAGNOSTIC] ℹ️  No active workflow found - diagnostic agent monitoring disabled"
                )

            # DEBUG: Verify the state
            logger.info(
                f"[DIAGNOSTIC] PhaseManager.workflow_id = {phase_manager.workflow_id[:8] if phase_manager.workflow_id else 'None'}"
            )
            logger.info(
                f"[DIAGNOSTIC] PhaseManager.active_workflow exists = {phase_manager.active_workflow is not None}"
            )

        except Exception as e:
            logger.warning(f"Phase manager initialization failed (optional): {e}")

        # Create monitoring loop
        monitoring_loop = MonitoringLoop(
            db_manager=db_manager,
            agent_manager=agent_manager,
            llm_provider=llm_provider,
            rag_system=rag_system,
            phase_manager=phase_manager,
        )

        logger.info("Monitoring system setup complete")
        return monitoring_loop

    except Exception as e:
        logger.error(f"Failed to setup monitoring system: {e}")
        raise


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.critical(f"RECEIVED SIGNAL {signum} — shutting down")
    import traceback

    logger.critical(
        f"Stack trace at signal time:\n{''.join(traceback.format_stack(frame))}"
    )
    if monitoring_loop:
        asyncio.create_task(monitoring_loop.stop())
    else:
        sys.exit(0)


async def main():
    """Main monitoring service entry point."""
    global monitoring_loop

    logger.info("=" * 60)
    logger.info("Starting Hephaestus Monitoring Service")
    logger.info("=" * 60)

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    signal.signal(signal.SIGQUIT, signal_handler)
    signal.signal(signal.SIGALRM, signal_handler)

    try:
        # Setup monitoring system
        monitoring_loop = await setup_monitoring_system()

        logger.info("Monitoring service is ready")
        logger.info("Monitoring features:")
        logger.info("  - Agent health monitoring")
        logger.info("  - LLM-powered intervention analysis")
        logger.info("  - Automatic agent nudging and restarting")
        logger.info("  - Orphaned tmux session cleanup")
        logger.info("  - Phase progression monitoring")
        logger.info("  - Task timeout detection")
        logger.info("")
        logger.info("Press Ctrl+C to stop the service")
        logger.info("-" * 60)

        # Start monitoring loop
        await monitoring_loop.start()

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Monitoring service error: {e}")
        raise
    finally:
        if monitoring_loop:
            await monitoring_loop.stop()
        logger.info("Monitoring service stopped")


if __name__ == "__main__":
    # Ensure logs directory exists
    Path("logs").mkdir(exist_ok=True)

    # Enable faulthandler — dumps traceback to file on fatal signals
    fault_log = open("logs/monitor_fault.log", "a")
    faulthandler.enable(file=fault_log, all_threads=True)
    # Also dump traceback if process is alive after 600s (stuck detection)
    faulthandler.dump_traceback_later(600, repeat=True, file=fault_log, exit=False)

    # Write PID file so heph stop can find us
    pid_dir = Path.home() / ".hephaestus" / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "monitor.pid").write_text(str(os.getpid()))

    import traceback

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted by user")
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:
        # Catch EVERYTHING including non-Exception types
        logger.critical(f"FATAL: {type(e).__name__}: {e}")
        logger.critical(traceback.format_exc())
        # Write crash to separate file for diagnosis
        crash_path = Path("logs/monitor_crash.log")
        with open(crash_path, "a") as f:
            f.write(f"\n\n{'=' * 60}\n")
            f.write(f"CRASH at {datetime.utcnow()}\n")
            f.write(f"PID: {os.getpid()}\n")
            f.write(f"Type: {type(e).__name__}\n")
            f.write(f"Error: {e}\n")
            f.write(traceback.format_exc())
        sys.exit(1)

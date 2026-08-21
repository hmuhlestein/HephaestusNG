"""Simplified configuration for Hephaestus.

SOLID review 4.3: previously one 70-field `Config` object set every field
across three independently-maintained, untyped blocks (_apply_yaml_config,
_load_env_overrides, and a 2-of-70 validate()) with nothing enforcing they
stayed in sync. Fields are now grouped into per-domain value objects
(ServerConfig, GitWorktreeConfig, LLMConfig, ...) that `Config` composes;
each domain object owns its own YAML section and env-var overrides in one
place. `Config` keeps the same public shape (`config.server.mcp_port`,
`config.git.branch_prefix`, ...) so subsystems can depend on the narrow
slice they actually use instead of the whole object.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Defaults
DEFAULT_CLI_TOOL = os.getenv("DEFAULT_CLI_TOOL", "pi")


def _env_bool(name: str) -> Optional[bool]:
    value = os.getenv(name)
    return value.lower() == "true" if value is not None else None


class _ConfigSection:
    """Value-based __repr__ so callers that diff repr(config) (e.g. a config-
    key-liveness test) see real field changes, not this object's identity/
    address -- the default object.__repr__ would differ on every
    construction regardless of whether any field actually changed."""

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in vars(self).items())
        return f"{type(self).__name__}({fields})"


class ServerConfig(_ConfigSection):
    """HTTP server/runtime settings."""

    def __init__(self, config: Dict[str, Any]):
        server = config.get("server", {})
        self.mcp_host = server.get("host", "0.0.0.0")
        self.mcp_port = server.get("port", 8300)
        self.frontend_port = server.get("frontend_port", 5300)
        self.enable_cors = server.get("enable_cors", True)
        self.debug = False

    def apply_env_overrides(self):
        if os.getenv("MCP_HOST"):
            self.mcp_host = os.getenv("MCP_HOST")
        if os.getenv("MCP_PORT"):
            self.mcp_port = int(os.getenv("MCP_PORT"))
        if os.getenv("DEBUG"):
            self.debug = _env_bool("DEBUG")


class PathsConfig(_ConfigSection):
    """Filesystem paths."""

    def __init__(self, config: Dict[str, Any]):
        paths = config.get("paths", {})
        self.database_path = Path(paths.get("database", "./hephaestus.db"))
        # Worktree isolation base. None => WorktreeManager computes <repo>/.worktrees
        # (in-repo, git-excluded). Set an explicit path only to override.
        _wt_base = paths.get("worktree_base_path")
        self.worktree_base_path = Path(_wt_base) if _wt_base else None
        self.project_root = Path(paths.get("project_root", str(Path.cwd())))
        self.docs_path = Path("./docs")

    def apply_env_overrides(self):
        if os.getenv("DATABASE_PATH"):
            self.database_path = Path(os.getenv("DATABASE_PATH"))


class GitWorktreeConfig(_ConfigSection):
    """Git worktree lifecycle: branching, cleanup, checkpointing, archival."""

    def __init__(self, config: Dict[str, Any]):
        git = config.get("git", {})
        self.main_repo_path = Path(git.get("main_repo_path", str(Path.cwd())))
        self.base_branch = git.get(
            "base_branch", "main"
        )  # Base branch/commit for merging
        self.branch_prefix = git.get("branch_prefix", "agent-")
        self.auto_commit = git.get("auto_commit", True)

        self.max_branches = 50
        self.max_tree_depth = 10
        self.disk_space_threshold_gb = 10
        self.branch_auto_cleanup_enabled = True
        self.branch_cleanup_interval_hours = 6
        self.branch_retention_hours = {
            "merged": 1,
            "failed": 24,
            "abandoned": 6,
            "active": -1,
        }
        self.auto_checkpoint_enabled = True
        self.checkpoint_interval_minutes = 30
        self.checkpoint_on_error = True
        self.checkpoint_before_child = True
        self.branch_archive_prefix = "refs/archive/"
        self.archive_after_days = 7
        self.delete_archives_after_days = 30

    def apply_env_overrides(self):
        if os.getenv("MAIN_REPO_PATH"):
            self.main_repo_path = Path(os.getenv("MAIN_REPO_PATH"))
        if os.getenv("GIT_BASE_BRANCH"):
            self.base_branch = os.getenv("GIT_BASE_BRANCH")
        if os.getenv("BRANCH_PREFIX"):
            self.branch_prefix = os.getenv("BRANCH_PREFIX")
        if os.getenv("BRANCH_MAX_COUNT"):
            self.max_branches = int(os.getenv("BRANCH_MAX_COUNT"))
        if os.getenv("WORKTREE_MAX_DEPTH"):
            self.max_tree_depth = int(os.getenv("WORKTREE_MAX_DEPTH"))
        if os.getenv("WORKTREE_DISK_THRESHOLD_GB"):
            self.disk_space_threshold_gb = int(os.getenv("WORKTREE_DISK_THRESHOLD_GB"))
        if os.getenv("BRANCH_AUTO_CLEANUP"):
            self.branch_auto_cleanup_enabled = _env_bool("BRANCH_AUTO_CLEANUP")
        if os.getenv("BRANCH_CLEANUP_INTERVAL_HOURS"):
            self.branch_cleanup_interval_hours = int(
                os.getenv("BRANCH_CLEANUP_INTERVAL_HOURS")
            )
        if os.getenv("BRANCH_RETENTION_MERGED"):
            self.branch_retention_hours["merged"] = int(
                os.getenv("BRANCH_RETENTION_MERGED")
            )
        if os.getenv("BRANCH_RETENTION_FAILED"):
            self.branch_retention_hours["failed"] = int(
                os.getenv("BRANCH_RETENTION_FAILED")
            )
        if os.getenv("BRANCH_RETENTION_ABANDONED"):
            self.branch_retention_hours["abandoned"] = int(
                os.getenv("BRANCH_RETENTION_ABANDONED")
            )
        if os.getenv("WORKTREE_AUTO_CHECKPOINT"):
            self.auto_checkpoint_enabled = _env_bool("WORKTREE_AUTO_CHECKPOINT")
        if os.getenv("WORKTREE_CHECKPOINT_INTERVAL"):
            self.checkpoint_interval_minutes = int(
                os.getenv("WORKTREE_CHECKPOINT_INTERVAL")
            )
        if os.getenv("WORKTREE_CHECKPOINT_ON_ERROR"):
            self.checkpoint_on_error = _env_bool("WORKTREE_CHECKPOINT_ON_ERROR")
        if os.getenv("WORKTREE_CHECKPOINT_BEFORE_CHILD"):
            self.checkpoint_before_child = _env_bool(
                "WORKTREE_CHECKPOINT_BEFORE_CHILD"
            )
        if os.getenv("BRANCH_ARCHIVE_PREFIX"):
            self.branch_archive_prefix = os.getenv("BRANCH_ARCHIVE_PREFIX")
        if os.getenv("WORKTREE_ARCHIVE_AFTER_DAYS"):
            self.archive_after_days = int(os.getenv("WORKTREE_ARCHIVE_AFTER_DAYS"))
        if os.getenv("WORKTREE_DELETE_ARCHIVES_AFTER_DAYS"):
            self.delete_archives_after_days = int(
                os.getenv("WORKTREE_DELETE_ARCHIVES_AFTER_DAYS")
            )


class LLMConfig(_ConfigSection):
    """LLM provider selection, model defaults, and API keys."""

    def __init__(self, config: Dict[str, Any]):
        llm = config.get("llm", {})
        # Use new default_* fields for fallback/legacy mode (replaces LLM_MODEL env var)
        self.llm_provider = llm.get("default_provider", "openrouter")
        self.llm_model = llm.get("default_model", "openai/gpt-oss-120b")
        self.default_openrouter_provider = llm.get(
            "default_openrouter_provider", "cerebras"
        )
        self.default_temperature = llm.get("default_temperature", 0.7)
        self.default_max_tokens = llm.get("default_max_tokens", 4000)
        self.embedding_model = llm.get("embedding_model", "text-embedding-3-large")
        self.system_prompt_max_length = llm.get("system_prompt_max_length", 8000)
        self.openai_api_key = None
        self.anthropic_api_key = None
        self.openrouter_api_key = None

    def apply_env_overrides(self):
        if os.getenv("LLM_PROVIDER"):
            self.llm_provider = os.getenv("LLM_PROVIDER")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        # LLM_MODEL and EMBEDDING_MODEL are deprecated - all model config comes from YAML

    def get_api_key(self):
        """Get the appropriate API key based on provider."""
        if self.llm_provider == "openai":
            return self.openai_api_key
        elif self.llm_provider == "anthropic":
            return self.anthropic_api_key
        elif self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return None

    def validate(self):
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when using OpenAI provider")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when using Anthropic provider"
            )
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when using OpenRouter provider"
            )
        return True


class AgentConfig(_ConfigSection):
    """CLI agent launch/health/fallback settings."""

    def __init__(self, config: Dict[str, Any]):
        agents = config.get("agents", {})
        self.default_cli_tool = agents.get("default_cli_tool", DEFAULT_CLI_TOOL)
        self.cli_model = agents.get("cli_model", "sonnet")
        # Global fallback used when a phase doesn't set its own
        # fallback_cli_tool/fallback_cli_model (Phase DB columns) -- same
        # role for the fallback as default_cli_tool/cli_model play for the
        # primary. None means no global fallback, matching prior behavior.
        self.default_fallback_cli_tool = agents.get("default_fallback_cli_tool")
        self.default_fallback_cli_model = agents.get("default_fallback_cli_model")
        # In-session model fallback for an agent frozen too long (see
        # docs/PI_MODEL_FALLBACK_DESIGN.md) -- distinct from
        # default_fallback_cli_tool/_model above, which tears down and
        # relaunches under a different CLI entirely. Only takes effect for
        # a CLI whose CLIAgentInterface subclass overrides
        # model_fallback_keystrokes (today, pi and claude). Each CLI reads
        # its own config value (CLIAgentInterface.fallback_model) rather
        # than sharing one -- pi's is an OpenRouter path its picker
        # resolves, claude's is one of Claude Code's own model aliases, and
        # neither vocabulary means anything to the other CLI. None/unset
        # disables the feature for that CLI.
        self.cli_model_fallback_wait_seconds = agents.get(
            "cli_model_fallback_wait_seconds", 120
        )
        self.cli_model_fallback = agents.get("cli_model_fallback")
        self.secondary_cli_model_fallback = agents.get("secondary_cli_model_fallback")
        # Per-(cli_tool, cli_model) concurrency cap, keyed by "cli_tool/cli_model"
        # (e.g. a local model with a single inference slot:
        # {"pi/Qwen3.8-27B-UD-Q4_K_XL.gguf": 1}). Distinct from
        # max_concurrent_agents (mcp section), which caps total agents
        # regardless of which CLI/model they're on -- this stops the queue
        # from dispatching a second agent onto a combo that can only
        # actually serve one request at a time, which just leaves the
        # second agent frozen waiting its turn instead of doing anything.
        self.cli_model_concurrency_limits = (
            agents.get("cli_model_concurrency_limits", {}) or {}
        )
        # Per-turn reasoning budget for pi agents (off|minimal|low|medium|high|xhigh).
        # Bounds rumination; per-phase `thinking_level` overrides this.
        self.cli_thinking_level = agents.get("cli_thinking_level", "medium")
        self.glm_api_token_env = agents.get("glm_api_token_env", "GLM_API_TOKEN")
        self.tmux_session_prefix = agents.get("tmux_session_prefix", "agent")
        self.agent_health_check_interval = agents.get("health_check_interval", 60)
        self.max_health_check_failures = agents.get("max_health_failures", 3)
        self.agent_termination_delay = agents.get("termination_delay", 5)

        self.agent_max_retries = 3
        self.tmux_output_lines = (
            200  # Used by Guardian/monitoring for performance (UI uses 2000)
        )
        self.agent_timeout_minutes = 30

    def apply_env_overrides(self):
        if os.getenv("DEFAULT_CLI_TOOL"):
            self.default_cli_tool = os.getenv("DEFAULT_CLI_TOOL")
        if os.getenv("CLI_MODEL"):
            self.cli_model = os.getenv("CLI_MODEL")
        if os.getenv("CLI_THINKING_LEVEL"):
            self.cli_thinking_level = os.getenv("CLI_THINKING_LEVEL")
        if os.getenv("GLM_API_TOKEN_ENV"):
            self.glm_api_token_env = os.getenv("GLM_API_TOKEN_ENV")
        if os.getenv("AGENT_TIMEOUT_MINUTES"):
            self.agent_timeout_minutes = int(os.getenv("AGENT_TIMEOUT_MINUTES"))
        if os.getenv("MAX_HEALTH_CHECK_FAILURES"):
            self.max_health_check_failures = int(
                os.getenv("MAX_HEALTH_CHECK_FAILURES")
            )


class VectorStoreConfig(_ConfigSection):
    """Vector store backend and embedding settings."""

    def __init__(self, config: Dict[str, Any]):
        vector_store = config.get("vector_store", {})
        self.vector_store_backend = vector_store.get("backend", "turbovec")
        self.qdrant_url = vector_store.get("qdrant_url", "http://localhost:6333")
        self.qdrant_collection_prefix = vector_store.get(
            "collection_prefix", "hephaestus"
        )
        self.embedding_dimension = vector_store.get("embedding_dimension", 384)
        self.turbovec_data_dir = vector_store.get("turbovec_data_dir", "data/turbovec")

    def apply_env_overrides(self):
        if os.getenv("QDRANT_URL"):
            self.qdrant_url = os.getenv("QDRANT_URL")
        if os.getenv("QDRANT_COLLECTION_PREFIX"):
            self.qdrant_collection_prefix = os.getenv("QDRANT_COLLECTION_PREFIX")
        if os.getenv("VECTOR_STORE_BACKEND"):
            self.vector_store_backend = os.getenv("VECTOR_STORE_BACKEND")
        if os.getenv("TURBOVEC_DATA_DIR"):
            self.turbovec_data_dir = os.getenv("TURBOVEC_DATA_DIR")


class MonitoringConfig(_ConfigSection):
    """Guardian/monitor loop tuning."""

    def __init__(self, config: Dict[str, Any]):
        monitoring = config.get("monitoring", {})
        self.monitoring_enabled = monitoring.get("enabled", True)
        self.monitoring_interval_seconds = monitoring.get("interval_seconds", 60)
        self.log_level = monitoring.get("log_level", "INFO")
        self.log_format = monitoring.get("log_format", "json")
        self.stuck_agent_threshold = monitoring.get("stuck_agent_threshold", 300)
        self.guardian_min_agent_age_seconds = monitoring.get(
            "guardian_min_agent_age_seconds", 60
        )
        self.max_ignored_steering = monitoring.get("max_ignored_steering", 3)
        self.stranded_task_grace_seconds = monitoring.get(
            "stranded_task_grace_seconds", 900
        )
        self.stuck_detection_minutes = monitoring.get("stuck_detection_minutes", 30)
        self.guardian_nudge_delay_minutes = monitoring.get(
            "guardian_nudge_delay_minutes", 15
        )
        self.max_stuck_nudges = monitoring.get("max_stuck_nudges", 5)

    def apply_env_overrides(self):
        if os.getenv("MONITORING_INTERVAL_SECONDS"):
            self.monitoring_interval_seconds = int(
                os.getenv("MONITORING_INTERVAL_SECONDS")
            )
        if os.getenv("GUARDIAN_MIN_AGENT_AGE_SECONDS"):
            self.guardian_min_agent_age_seconds = int(
                os.getenv("GUARDIAN_MIN_AGENT_AGE_SECONDS")
            )
        if os.getenv("LOG_LEVEL"):
            self.log_level = os.getenv("LOG_LEVEL")


class MCPConfig(_ConfigSection):
    """MCP API auth/session/concurrency settings."""

    def __init__(self, config: Dict[str, Any]):
        mcp = config.get("mcp", {})
        # SECURITY: auth_required defaults to True now. Set mcp.auth_required: false
        # ONLY in local development. Never disable auth in production.
        self.auth_required = mcp.get("auth_required", True)
        self.session_timeout = mcp.get("session_timeout", 3600)
        self.max_concurrent_agents = mcp.get("max_concurrent_agents", 10)

    def apply_env_overrides(self):
        # Note: max_concurrent_agents is ONLY configurable via hephaestus_config.yaml or SDK
        # Not overridable by environment variables for consistency
        pass


class TaskDedupConfig(_ConfigSection):
    """Task-similarity dedup and embedding settings."""

    def __init__(self, config: Dict[str, Any]):
        dedup = config.get("task_deduplication", {})
        self.task_dedup_enabled = dedup.get("enabled", True)
        self.task_similarity_threshold = dedup.get("similarity_threshold", 0.7)
        self.task_related_threshold = dedup.get("related_threshold", 0.4)
        self.task_embedding_model = dedup.get(
            "embedding_model", "BAAI/bge-small-en-v1.5"
        )
        self.task_embedding_dimension = dedup.get("embedding_dimension", 384)
        self.task_embedding_backend = dedup.get("embedding_backend", "fastembed")
        self.task_dedup_batch_size = dedup.get("batch_size", 100)
        self.max_context_memories = 20
        self.similarity_threshold = 0.7

    def apply_env_overrides(self):
        if os.getenv("TASK_DEDUP_ENABLED"):
            self.task_dedup_enabled = _env_bool("TASK_DEDUP_ENABLED")
        if os.getenv("TASK_SIMILARITY_THRESHOLD"):
            self.task_similarity_threshold = float(
                os.getenv("TASK_SIMILARITY_THRESHOLD")
            )
        if os.getenv("TASK_RELATED_THRESHOLD"):
            self.task_related_threshold = float(os.getenv("TASK_RELATED_THRESHOLD"))
        if os.getenv("TASK_EMBEDDING_MODEL"):
            self.task_embedding_model = os.getenv("TASK_EMBEDDING_MODEL")


class DiagnosticAgentConfig(_ConfigSection):
    """Diagnostic-agent trigger/throttle settings."""

    def __init__(self, config: Dict[str, Any]):
        diagnostic = config.get("diagnostic_agent", {})
        self.diagnostic_agent_enabled = diagnostic.get("enabled", True)
        self.diagnostic_cooldown_seconds = diagnostic.get("cooldown_seconds", 60)
        self.diagnostic_min_stuck_time_seconds = diagnostic.get(
            "min_stuck_time_seconds", 60
        )
        self.diagnostic_max_agents_to_analyze = diagnostic.get(
            "max_agents_to_analyze", 15
        )
        self.diagnostic_max_conductor_analyses = diagnostic.get(
            "max_conductor_analyses", 5
        )
        self.diagnostic_max_tasks_per_run = diagnostic.get("max_tasks_per_run", 5)

    def apply_env_overrides(self):
        if os.getenv("DIAGNOSTIC_AGENT_ENABLED"):
            self.diagnostic_agent_enabled = _env_bool("DIAGNOSTIC_AGENT_ENABLED")
        if os.getenv("DIAGNOSTIC_COOLDOWN_SECONDS"):
            self.diagnostic_cooldown_seconds = int(
                os.getenv("DIAGNOSTIC_COOLDOWN_SECONDS")
            )
        if os.getenv("DIAGNOSTIC_MIN_STUCK_TIME"):
            self.diagnostic_min_stuck_time_seconds = int(
                os.getenv("DIAGNOSTIC_MIN_STUCK_TIME")
            )


class AutopilotConfig(_ConfigSection):
    """Autopilot/pipeline workflow timeout and concurrency settings."""

    def __init__(self, config: Dict[str, Any]):
        autopilot = config.get("autopilot", {})
        self.workflow_timeout_seconds = autopilot.get(
            "workflow_timeout_seconds", 7200
        )  # 2 hours
        self.phase0_timeout_seconds = autopilot.get(
            "phase0_timeout_seconds", 3600
        )  # 1 hour
        self.max_concurrent_projects = autopilot.get("max_concurrent_projects", 2)
        self.paused_workflow_retry_cooldown_seconds = autopilot.get(
            "paused_workflow_retry_cooldown_seconds", 300
        )  # 5 min between auto-retry attempts on an exhausted-retry pause
        self.paused_workflow_max_retry_cycles = autopilot.get(
            "paused_workflow_max_retry_cycles", 10
        )  # give up permanently after this many auto-retry cycles

    def apply_env_overrides(self):
        if os.getenv("WORKFLOW_TIMEOUT_SECONDS"):
            self.workflow_timeout_seconds = int(os.getenv("WORKFLOW_TIMEOUT_SECONDS"))
        if os.getenv("PHASE0_TIMEOUT_SECONDS"):
            self.phase0_timeout_seconds = int(os.getenv("PHASE0_TIMEOUT_SECONDS"))


class TicketTrackingConfig(_ConfigSection):
    """Ticket-tracking review/approval settings."""

    def __init__(self, config: Dict[str, Any]):
        ticket_tracking = config.get("ticket_tracking", {})
        self.ticket_tracking_enabled = ticket_tracking.get("enabled", True)
        self.default_human_review = ticket_tracking.get("default_human_review", False)
        self.default_approval_timeout = ticket_tracking.get(
            "default_approval_timeout", 1800
        )

    def apply_env_overrides(self):
        pass


_DOMAIN_CLASSES = {
    "server": ServerConfig,
    "paths": PathsConfig,
    "git": GitWorktreeConfig,
    "llm": LLMConfig,
    "agents": AgentConfig,
    "vector_store": VectorStoreConfig,
    "monitoring": MonitoringConfig,
    "mcp": MCPConfig,
    "task_dedup": TaskDedupConfig,
    "diagnostic_agent": DiagnosticAgentConfig,
    "autopilot": AutopilotConfig,
    "ticket_tracking": TicketTrackingConfig,
}


class Config:
    """Composes the per-domain config objects. See module docstring."""

    def __init__(self):
        yaml_path = Path(os.getenv("HEPHAESTUS_CONFIG", "./hephaestus_config.yaml"))
        if yaml_path.exists():
            with open(yaml_path, "r") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}

        for attr, cls in _DOMAIN_CLASSES.items():
            domain = cls(raw)
            domain.apply_env_overrides()
            setattr(self, attr, domain)

    def get_api_key(self):
        """Get the appropriate LLM API key based on provider."""
        return self.llm.get_api_key()

    def validate(self):
        """Validate configuration."""
        return self.llm.validate()

    def to_env_dict(self) -> dict:
        """Export configuration as environment variables dict for subprocess.

        Returns:
            Dictionary of environment variables for spawned processes
        """
        env = {}

        if self.paths.database_path:
            env["DATABASE_PATH"] = str(self.paths.database_path)
        if self.vector_store.vector_store_backend:
            env["VECTOR_STORE_BACKEND"] = self.vector_store.vector_store_backend
        if self.vector_store.turbovec_data_dir:
            env["TURBOVEC_DATA_DIR"] = self.vector_store.turbovec_data_dir
        if self.vector_store.qdrant_url:
            env["QDRANT_URL"] = self.vector_store.qdrant_url
        if self.vector_store.qdrant_collection_prefix:
            env["QDRANT_COLLECTION_PREFIX"] = self.vector_store.qdrant_collection_prefix

        if self.server.mcp_host:
            env["MCP_HOST"] = self.server.mcp_host
        if self.server.mcp_port:
            env["MCP_PORT"] = str(self.server.mcp_port)

        return env


# Global config instance
_config = None


def get_config() -> Config:
    """Get or create global config instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config

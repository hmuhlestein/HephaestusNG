"""LangChain-based multi-provider LLM client for Hephaestus."""

import asyncio
import logging
import os
from enum import Enum
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_groq import ChatGroq
from langchain_openai import (
    AzureChatOpenAI,
    AzureOpenAIEmbeddings,
    ChatOpenAI,
    OpenAIEmbeddings,
)
from pydantic import BaseModel

from src.prompts.loader import get_base_system_prompt, get_prompt

logger = logging.getLogger(__name__)


class ModelAssignment(BaseModel):
    """Model assignment configuration."""

    provider: str
    model: str
    openrouter_provider: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4000
    # OpenRouter reasoning cap for reasoning models (mimo etc.): "low" | "medium" |
    # "high" | "off". Utility calls (enrich/guardian/conductor/prompts) don't need
    # deep reasoning; capping it avoids multi-minute reasoning streams per call.
    reasoning_effort: Optional[str] = None


class ProviderConfig(BaseModel):
    """Provider configuration."""

    api_key_env: str
    base_url: Optional[str] = None
    models: List[Any]


class LLMConfig(BaseModel):
    """Complete LLM configuration."""

    embedding_model: str = "text-embedding-3-small"
    providers: Dict[str, ProviderConfig]
    model_assignments: Dict[str, ModelAssignment]


class ComponentType(Enum):
    """Component types for model routing."""

    TASK_ENRICHMENT = "task_enrichment"
    AGENT_MONITORING = "agent_monitoring"
    GUARDIAN_ANALYSIS = "guardian_analysis"
    CONDUCTOR_ANALYSIS = "conductor_analysis"
    AGENT_PROMPTS = "agent_prompts"


# Hard timeout for Conductor's LLM calls (analyze_system_coherence) -- see
# each call site's own comment for why this must never be unbounded
# (mirrors Guardian's GUARDIAN_LLM_TIMEOUT in guardian.py).
CONDUCTOR_LLM_TIMEOUT = 90


class LangChainLLMClient:
    """Multi-provider LLM client using LangChain."""

    def __init__(self, config: LLMConfig):
        """Initialize the LangChain LLM client.

        Args:
            config: LLM configuration with providers and model assignments
        """
        self.config = config
        self._models: Dict[str, Any] = {}
        self._embedding_model = None

        logger.info("=" * 60)
        logger.info("🚀 Initializing Multi-Provider LLM Client")
        logger.info("=" * 60)
        self._initialize_models()
        logger.info("✅ Multi-Provider LLM Client initialized successfully")
        logger.info("=" * 60)

    def _initialize_models(self):
        """Initialize all configured models."""

        # Initialize embedding model based on configured provider
        embedding_provider = getattr(self.config, "embedding_provider", "openai")
        logger.info(f"Initializing embedding model: {self.config.embedding_model} (provider: {embedding_provider})")

        if embedding_provider == "openai":
            openai_provider = self.config.providers.get("openai")
            if openai_provider:
                openai_key = os.getenv(openai_provider.api_key_env)
                if openai_key:
                    self._embedding_model = OpenAIEmbeddings(model=self.config.embedding_model, openai_api_key=openai_key)
                    logger.info(f"  ✓ Embedding model initialized: OpenAI {self.config.embedding_model}")

        elif embedding_provider == "azure_openai":
            azure_provider = self.config.providers.get("azure_openai")
            if azure_provider:
                azure_key = os.getenv(azure_provider.api_key_env)
                azure_endpoint = azure_provider.base_url
                if azure_key and azure_endpoint:
                    api_version = azure_provider.api_version or "2024-02-01"
                    self._embedding_model = AzureOpenAIEmbeddings(
                        model=self.config.embedding_model,
                        azure_deployment=self.config.embedding_model,
                        azure_endpoint=azure_endpoint,
                        api_version=api_version,
                        api_key=azure_key,
                    )
                    logger.info(f"  ✓ Embedding model initialized: Azure OpenAI {self.config.embedding_model}")
                else:
                    logger.warning("Azure OpenAI embedding configuration incomplete (key or endpoint missing)")

        elif embedding_provider == "google_ai":
            google_provider = self.config.providers.get("google_ai")
            if google_provider:
                google_key = os.getenv(google_provider.api_key_env)
                if google_key:
                    self._embedding_model = GoogleGenerativeAIEmbeddings(
                        model=self.config.embedding_model,  # e.g., "models/embedding-001"
                        google_api_key=google_key,
                    )
                    logger.info(f"  ✓ Embedding model initialized: Google AI {self.config.embedding_model}")
                else:
                    logger.warning("Google AI embedding configuration incomplete (key missing)")

        elif embedding_provider in ("fastembed", "local", "openrouter"):
            # OpenRouter (and most chat-only providers) have no embeddings API.
            # Use the python-only FastEmbed backend — no API key, no Qdrant/server.
            # Default bge-small = 384-dim, matching the vector store dimension.
            try:
                from langchain_community.embeddings import FastEmbedEmbeddings

                fe_model = os.getenv("FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
                self._embedding_model = FastEmbedEmbeddings(model_name=fe_model)
                logger.info(f"  ✓ Embedding model initialized: FastEmbed {fe_model} (python-only, no API key)")
            except Exception as e:
                logger.warning(f"FastEmbed embedding init failed: {e}")

        if not self._embedding_model:
            logger.warning(f"Embedding model not initialized for provider: {embedding_provider}")

        # Initialize models for each component
        logger.info(f"Configuring models for {len(self.config.model_assignments)} components:")
        for component_name, assignment in self.config.model_assignments.items():
            model_key = f"{component_name}_{assignment.provider}_{assignment.model}"

            if model_key not in self._models:
                model = self._create_model(assignment)
                if model:
                    self._models[model_key] = model
                    provider_info = f"{assignment.provider}"
                    if hasattr(assignment, "openrouter_provider") and assignment.openrouter_provider:
                        provider_info += f" (via {assignment.openrouter_provider})"
                    logger.info(f"  ✓ {component_name}: {assignment.model} [{provider_info}]")

    def _create_model(self, assignment: ModelAssignment):
        """Create a model instance based on assignment.

        Args:
            assignment: Model assignment configuration

        Returns:
            Configured model instance or None if creation fails
        """

        provider = assignment.provider
        provider_config = self.config.providers.get(provider)

        if not provider_config:
            logger.error(f"Provider {provider} not configured")
            return None

        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            logger.error(f"API key not found for {provider}")
            return None

        try:
            if provider == "openai":
                kwargs = {
                    "model": assignment.model,
                    "max_tokens": assignment.max_tokens,
                    "openai_api_key": api_key,
                }

                # GPT-5 models only support temperature=1.0 (no other values allowed)
                # For other models, use the configured temperature
                if assignment.model.startswith("gpt-5"):
                    kwargs["temperature"] = 1.0
                else:
                    kwargs["temperature"] = assignment.temperature

                return ChatOpenAI(**kwargs)

            elif provider == "groq":
                return ChatGroq(
                    model=assignment.model,
                    temperature=assignment.temperature,
                    max_tokens=assignment.max_tokens,
                    groq_api_key=api_key,
                )

            elif provider == "openrouter":
                # OpenRouter uses the model name directly
                model_name = assignment.model

                # Build extra_body for OpenRouter: provider routing + reasoning cap.
                # extra_body passes custom params through to OpenRouter without the
                # OpenAI SDK rejecting them.
                model_kwargs = {}
                extra_body = {}
                if assignment.openrouter_provider:
                    # Capitalize provider name (e.g., "cerebras" -> "Cerebras")
                    provider_name = assignment.openrouter_provider.capitalize()
                    extra_body["provider"] = {
                        "order": [provider_name],
                        "allow_fallbacks": False,  # Force only the specified provider
                    }
                    logger.info(f"OpenRouter configured with provider routing: {provider_name} (order: [{provider_name}], fallbacks: disabled)")
                if assignment.reasoning_effort:
                    # Cap reasoning for reasoning models. "off" disables it entirely;
                    # otherwise pass the effort level. (Ignored harmlessly by models
                    # that don't support reasoning.)
                    if assignment.reasoning_effort.lower() == "off":
                        extra_body["reasoning"] = {"enabled": False}
                    else:
                        extra_body["reasoning"] = {"effort": assignment.reasoning_effort.lower()}
                    logger.info(f"OpenRouter reasoning capped: {assignment.reasoning_effort} for {assignment.model}")
                # Include usage/cost data in responses for cost tracking
                extra_body["usage"] = {"include": True}
                if extra_body:
                    model_kwargs["extra_body"] = extra_body

                # Use config base_url, then env var, then default
                base_url = provider_config.base_url or os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"

                return ChatOpenAI(
                    model=model_name,
                    temperature=assignment.temperature,
                    max_tokens=assignment.max_tokens,
                    openai_api_key=api_key,
                    base_url=base_url,
                    max_retries=1,  # one retry only — slow/over-streaming models shouldn't retry-loop for minutes
                    default_headers={
                        "HTTP-Referer": "https://github.com/Ido-Levi/Hephaestus",
                        "X-Title": "Hephaestus - Semi Structured Agentic Framework",
                    },
                    model_kwargs=model_kwargs,  # extra_body gets passed through to the API
                )

            elif provider == "azure_openai":
                # Azure OpenAI uses deployment names (configured in Azure portal) instead of model names
                # Requires azure_endpoint, api_version, and azure_deployment parameters
                azure_endpoint = provider_config.base_url
                if not azure_endpoint:
                    logger.error("Azure OpenAI requires base_url (azure_endpoint) in configuration")
                    return None

                api_version = provider_config.api_version or "2024-02-01"
                logger.info(f"Creating Azure OpenAI model with deployment: {assignment.model}, endpoint: {azure_endpoint}, api_version: {api_version}")

                return AzureChatOpenAI(
                    model=assignment.model,  # This is the deployment name in Azure
                    azure_deployment=assignment.model,
                    api_version=api_version,
                    azure_endpoint=azure_endpoint,
                    api_key=api_key,
                    temperature=assignment.temperature,
                    max_tokens=assignment.max_tokens,
                )

            elif provider == "google_ai":
                # Google AI Studio (Gemini) - simpler than Vertex AI, just needs API key
                logger.info(f"Creating Google AI model: {assignment.model}")

                return ChatGoogleGenerativeAI(
                    model=assignment.model,  # e.g., "gemini-2.5-flash", "gemini-1.5-pro"
                    google_api_key=api_key,
                    temperature=assignment.temperature,
                    max_tokens=assignment.max_tokens,
                )

            else:
                logger.error(f"Unknown provider: {provider}")
                return None

        except Exception as e:
            logger.error(f"Failed to create model for {provider}: {e}")
            return None

    def _get_model_for_component(self, component: ComponentType):
        """Get the appropriate model for a component.

        Args:
            component: Component type

        Returns:
            Model instance or None
        """
        component_name = component.value
        assignment = self.config.model_assignments.get(component_name)

        if not assignment:
            logger.error(f"No model assignment for component {component_name}")
            return None

        model_key = f"{component_name}_{assignment.provider}_{assignment.model}"
        return self._models.get(model_key)

    async def _invoke_and_record(
        self,
        model,
        messages: list,
        component: str,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> Any:
        """Wrap model.ainvoke() with cost recording.

        Extracts cost from response_metadata and writes a CostEntry.
        All call sites should route through this instead of calling
        model.ainvoke directly.

        Args:
            model: The LangChain model instance
            messages: List of messages to send
            component: Component name (e.g. 'task_enrichment', 'guardian')
            task_id: Optional task ID for cost attribution
            agent_id: Optional agent ID
            workflow_id: Optional workflow ID

        Returns:
            The model response
        """
        from src.core.log_context import set_log_context

        if task_id:
            set_log_context(task=task_id)
        if agent_id:
            set_log_context(agent=agent_id)
        if workflow_id:
            set_log_context(workflow=workflow_id)

        response = await model.ainvoke(messages)

        # Extract cost from response metadata
        try:
            metadata = getattr(response, "response_metadata", {}) or {}
            usage = metadata.get("token_usage") or {}

            # OpenRouter returns cost in usage.cost when usage.include=true
            cost_data = usage.get("cost") or {}
            cost_usd = cost_data.get("total", 0)

            if cost_usd > 0:
                from src.core.cost_derivation import record_cost
                from src.core.database import get_db

                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                token_details = usage.get("prompt_tokens_details") or {}
                cache_read = token_details.get("cached_tokens", 0)

                with get_db() as db:
                    record_cost(
                        db=db,
                        cost_usd=cost_usd,
                        source="openrouter_direct",
                        task_id=task_id,
                        agent_id=agent_id,
                        workflow_id=workflow_id,
                        model=metadata.get("model_name", component),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        raw_usage=usage,
                    )
            else:
                logger.debug(f"No cost in response metadata for {component} (cost_usd={cost_usd})")
        except Exception as e:
            logger.warning(f"Cost recording failed for {component}: {e}")

        return response

    async def classify_complexity(self, design_text: str, workflow_id: Optional[str] = None) -> str:
        """One fast LLM call: rate a design's implementation complexity as
        'low' | 'medium' | 'high'. Used to size agent reasoning budget + decomposition
        to the actual scope (so a calculator isn't treated like a multi-service system).
        Reuses the fast, reasoning-capped task_enrichment model. Defaults to 'medium'."""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            model = self._get_model_for_component(ComponentType.TASK_ENRICHMENT)
            if model is None:
                return "medium"
            prompt = get_prompt(
                "classify_complexity",
                {
                    "design_text": (design_text or "")[:4000],
                },
            )
            resp = await self._invoke_and_record(
                model,
                [
                    SystemMessage(content="You are a concise software complexity classifier."),
                    HumanMessage(content=prompt),
                ],
                component="complexity_classification",
                workflow_id=workflow_id,
            )
            text = (getattr(resp, "content", None) or str(resp)).strip().lower()
            for level in (
                "high",
                "medium",
                "low",
            ):  # check high/medium before low (substring)
                if level in text:
                    logger.info(f"[COMPLEXITY] classified design as '{level}'")
                    return level
            return "medium"
        except Exception as e:
            logger.warning(f"[COMPLEXITY] classification failed, defaulting to medium: {e}")
            return "medium"

    async def enrich_task(
        self,
        task_description: str,
        done_definition: str,
        context: List[str],
        phase_context: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enrich a task with LLM analysis using assigned model.

        Args:
            task_description: Raw task description
            done_definition: What constitutes task completion
            context: Relevant context from memory
            phase_context: Optional phase context

        Returns:
            Dictionary with enriched task information
        """
        assignment = self.config.model_assignments.get("task_enrichment")
        logger.info(f"🔵 [LLM CALL] enrich_task | Provider: {assignment.provider} | Model: {assignment.model}")

        model = self._get_model_for_component(ComponentType.TASK_ENRICHMENT)
        if not model:
            logger.warning("⚠️ [LLM CALL] No model available for task_enrichment, using fallback")
            return self._default_task_enrichment(task_description, done_definition)

        prompt = self._build_task_enrichment_prompt(task_description, done_definition, context, phase_context)

        messages = [
            SystemMessage(content="You are a task analysis expert for an AI orchestration system."),
            HumanMessage(content=prompt),
        ]

        try:
            response = await self._invoke_and_record(model, messages, component="task_enrichment", task_id=task_id)
            parser = JsonOutputParser()
            result = parser.parse(response.content)

            if not result or not isinstance(result, dict):
                logger.warning(f"enrich_task parser returned non-dict: {type(result)}, using fallback")
                return self._default_task_enrichment(task_description, done_definition)

            logger.info(f"✅ [LLM CALL] enrich_task completed | Provider: {assignment.provider} | Model: {assignment.model}")
            return result

        except Exception as e:
            logger.error(f"❌ [LLM CALL] enrich_task failed | Provider: {assignment.provider} | Model: {assignment.model} | Error: {e}")
            return self._default_task_enrichment(task_description, done_definition)

    async def resolve_ticket_clarification(
        self,
        ticket_id: str,
        conflict_description: str,
        context: str,
        potential_solutions: List[str],
        ticket_details: Dict[str, Any],
        related_tickets: List[Dict[str, Any]],
        active_tasks: List[Dict[str, Any]],
    ) -> str:
        """Use LLM to resolve ticket clarification conflicts.

        Args:
            ticket_id: ID of the ticket needing clarification
            conflict_description: Description of the conflict or ambiguity
            context: Additional context from the agent
            potential_solutions: List of potential solutions the agent is considering
            ticket_details: Full details of the disputed ticket
            related_tickets: Recent tickets for context (max 60)
            active_tasks: Active tasks for context (max 60)

        Returns:
            Detailed markdown guidance with resolution
        """
        # Get appropriate model for clarification (reuse task enrichment component)
        model = self._get_model_for_component(ComponentType.TASK_ENRICHMENT)
        if not model:
            logger.error("No model available for ticket clarification")
            return "❌ LLM model not available for clarification. Please check system configuration."

        # Build prompt from template
        prompt = self._build_ticket_clarification_prompt(
            ticket_id=ticket_id,
            conflict_description=conflict_description,
            context=context,
            potential_solutions=potential_solutions,
            ticket_details=ticket_details,
            related_tickets=related_tickets,
            active_tasks=active_tasks,
        )

        # Create messages
        messages = [
            SystemMessage(content="You are a ticket clarification arbitrator specialized in resolving conflicts and ambiguities in software development requirements."),
            HumanMessage(content=prompt),
        ]

        try:
            # Invoke model with longer timeout for reasoning
            response = await self._invoke_and_record(model, messages, component="ticket_clarification")

            logger.info(f"Ticket clarification resolved successfully for {ticket_id} using {self.config.model_assignments['task_enrichment'].model}")
            return response.content

        except Exception as e:
            logger.error(f"Failed to resolve ticket clarification: {e}")
            return f"❌ Failed to generate clarification due to error: {str(e)}\n\nPlease try again or seek manual clarification."

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding using OpenAI.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        logger.debug(f"🔵 [LLM CALL] generate_embedding | Provider: openai | Model: {self.config.embedding_model}")

        if not self._embedding_model:
            logger.error("❌ [LLM CALL] Embedding model not initialized")
            return [0.0] * 1536

        try:
            embedding = await self._embedding_model.aembed_query(text[:8000])
            logger.debug(f"✅ [LLM CALL] generate_embedding completed | Provider: openai | Model: {self.config.embedding_model}")
            return embedding
        except Exception as e:
            logger.error(f"❌ [LLM CALL] generate_embedding failed | Provider: openai | Model: {self.config.embedding_model} | Error: {e}")
            # Return zero vector as fallback
            return [0.0] * 1536

    async def generate_agent_prompt(
        self,
        task: Dict[str, Any],
        memories: List[Dict[str, Any]],
        project_context: str,
        phase_name: str = None,
    ) -> str:
        """Generate specialized system prompt for an agent.

        Args:
            task: Task information
            memories: Relevant memories from RAG
            project_context: Current project context
            phase_name: Phase name (e.g. "Feature Architect") -- selects a
                specialized prompt template. This signature had drifted
                from its caller (agents/manager.py always passes
                phase_name), causing every agent-creation call routed
                through this client to fail with "unexpected keyword
                argument 'phase_name'".

        Returns:
            System prompt for the agent
        """
        if phase_name == "Feature Architect":
            from src.prompts.loader import get_feature_architect_system_prompt

            memory_context = "\n".join([f"- {mem.get('content', '')[:200]}" for mem in memories[:10]])
            return get_feature_architect_system_prompt(
                agent_id=task.get("agent_id", "unknown"),
                task_id=task.get("id", "unknown"),
                memory_context=memory_context,
                project_context=project_context,
            )

        model = self._get_model_for_component(ComponentType.AGENT_PROMPTS)
        if not model:
            return self._default_agent_prompt(task, memories, project_context)

        # For prompt generation, we can directly return the formatted prompt
        # without needing LLM generation
        return self._default_agent_prompt(task, memories, project_context)

    async def analyze_agent_trajectory(
        self,
        agent_output: str,
        accumulated_context: Dict[str, Any],
        past_summaries: List[Dict[str, Any]],
        task_info: Dict[str, Any],
        last_message_marker: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze agent using trajectory thinking.

        Args:
            agent_output: Recent agent output
            accumulated_context: Full accumulated context
            past_summaries: Previous Guardian summaries
            task_info: Current task information
            last_message_marker: Optional marker from previous cycle

        Returns:
            Dictionary with trajectory analysis
        """
        assignment = self.config.model_assignments.get("guardian_analysis")
        logger.info(f"🔵 [LLM CALL] Guardian analyze_agent_trajectory | Provider: {assignment.provider} | Model: {assignment.model}")

        model = self._get_model_for_component(ComponentType.GUARDIAN_ANALYSIS)
        if not model:
            logger.warning("⚠️ [LLM CALL] No model available for guardian_analysis, using fallback")
            return self._default_trajectory_analysis()

        from src.monitoring.prompt_loader import prompt_loader

        prompt = prompt_loader.format_guardian_prompt(
            accumulated_context=accumulated_context,
            past_summaries=past_summaries,
            task_info=task_info,
            agent_output=agent_output,
            last_message_marker=last_message_marker,  # NEW
        )

        messages = [
            SystemMessage(content="You are a trajectory analysis expert using accumulated context thinking."),
            HumanMessage(content=prompt),
        ]

        for attempt in range(3):
            try:
                response = await self._invoke_and_record(model, messages, component="guardian_analysis", task_id=task_id)

                # Parse the response as structured output
                parser = JsonOutputParser()
                result = parser.parse(response.content)

                logger.info(f"✅ [LLM CALL] Guardian analyze_agent_trajectory completed | Provider: {assignment.provider} | Model: {assignment.model}")
                return result

            except Exception as e:
                logger.error(f"❌ [LLM CALL] Guardian analyze_agent_trajectory failed (attempt {attempt + 1}/3) | Provider: {assignment.provider} | Model: {assignment.model} | Error: {e}")
                if attempt == 2:
                    logger.warning("⚠️ [LLM CALL] Guardian analyze_agent_trajectory exhausted retries, using fallback")
                    return self._default_trajectory_analysis()
                await asyncio.sleep(1)

    async def analyze_system_coherence(
        self,
        guardian_summaries: List[Dict[str, Any]],
        system_goals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze system-wide coherence.

        Args:
            guardian_summaries: All Guardian analysis results
            system_goals: Overall system goals

        Returns:
            Dictionary with coherence analysis
        """
        assignment = self.config.model_assignments.get("conductor_analysis")
        logger.info(f"🔵 [LLM CALL] Conductor analyze_system_coherence | Provider: {assignment.provider} | Model: {assignment.model}")

        model = self._get_model_for_component(ComponentType.CONDUCTOR_ANALYSIS)
        if not model:
            logger.warning("⚠️ [LLM CALL] No model available for conductor_analysis, using fallback")
            return self._default_coherence_analysis()

        from src.monitoring.prompt_loader import prompt_loader

        prompt = prompt_loader.format_conductor_prompt(
            guardian_summaries=guardian_summaries,
            system_goals=system_goals,
        )

        messages = [
            SystemMessage(content="You are a system orchestration expert analyzing multi-agent coherence."),
            HumanMessage(content=prompt),
        ]

        # Hard timeout so a slow/over-streaming model (mimo can stream a reasoning
        # trace for minutes and still fail to parse) can NEVER freeze the monitor
        # loop -- this call runs inside MonitoringLoop's single shared cycle, so an
        # unbounded await here blocks the entire loop's heartbeat and every other
        # agent's recovery, not just this one call. Same pattern as Guardian's
        # GUARDIAN_LLM_TIMEOUT (guardian.py).
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(
                    self._invoke_and_record(model, messages, component="conductor_analysis"),
                    timeout=CONDUCTOR_LLM_TIMEOUT,
                )

                # Parse the response as structured output
                parser = JsonOutputParser()
                result = parser.parse(response.content)

                logger.info(f"✅ [LLM CALL] Conductor analyze_system_coherence completed | Provider: {assignment.provider} | Model: {assignment.model}")
                return result

            except Exception as e:
                logger.error(f"❌ [LLM CALL] Conductor analyze_system_coherence failed (attempt {attempt + 1}/3) | Provider: {assignment.provider} | Model: {assignment.model} | Error: {e}")
                if attempt == 2:
                    logger.warning("⚠️ [LLM CALL] Conductor analyze_system_coherence exhausted retries, using fallback")
                    return self._default_coherence_analysis()
                await asyncio.sleep(1)

    def get_model_name(self, component: ComponentType) -> str:
        """Get the name of the model being used for a component.

        Args:
            component: Component type

        Returns:
            Model name or "unknown"
        """
        assignment = self.config.model_assignments.get(component.value)
        if assignment:
            # Return the model name with provider info for clarity
            if assignment.openrouter_provider:
                return f"{assignment.model} (via {assignment.openrouter_provider})"
            return assignment.model
        return "unknown"

    # Helper methods for building prompts
    def _build_task_enrichment_prompt(
        self,
        task_description: str,
        done_definition: str,
        context: List[str],
        phase_context: Optional[str] = None,
    ) -> str:
        """Build prompt for task enrichment."""
        return get_prompt(
            "task_enrichment",
            {
                "task_description": task_description,
                "done_definition": done_definition,
                "context": " ".join(context[:10]),
                "phase_context_section": f"\n\nPhase Context:\n{phase_context}" if phase_context else "",
                "phase_context_hint": ("\nConsider the phase context when determining complexity and requirements." if phase_context else ""),
            },
        )

    def _build_ticket_clarification_prompt(
        self,
        ticket_id: str,
        conflict_description: str,
        context: str,
        potential_solutions: List[str],
        ticket_details: Dict[str, Any],
        related_tickets: List[Dict[str, Any]],
        active_tasks: List[Dict[str, Any]],
    ) -> str:
        """Build prompt for ticket clarification using structured template."""
        from pathlib import Path

        # Load template from src/prompts/ticket_clarification_prompt.md
        template_path = Path(__file__).parent.parent / "prompts" / "ticket_clarification_prompt.md"

        try:
            with open(template_path, "r") as f:
                template = f.read()
        except FileNotFoundError:
            logger.error(f"Ticket clarification prompt template not found at {template_path}")
            # Fallback to a basic prompt
            return self._build_fallback_clarification_prompt(
                ticket_id,
                conflict_description,
                context,
                potential_solutions,
                ticket_details,
            )

        # Format related tickets (60 most recent)
        tickets_context = "\n".join(
            [f"[{t['ticket_id'][:12]}] ({t['status']}) {t['priority']} - {t['title']}\n  Type: {t['ticket_type']}\n  Description: {t['description'][:150]}..." for t in related_tickets[:60]]
        )

        if not tickets_context:
            tickets_context = "No other tickets found in the system."

        # Format active tasks (60 most recent)
        tasks_context = "\n".join([f"[{t['id'][:8]}] ({t['status']}) Phase {t.get('phase_id', 'N/A')} - {t['description'][:150]}..." for t in active_tasks[:60]])

        if not tasks_context:
            tasks_context = "No active tasks found in the system."

        # Format potential solutions with numbering
        if potential_solutions:
            solutions_text = "\n".join([f"{i + 1}. {sol}" for i, sol in enumerate(potential_solutions)])
        else:
            solutions_text = "(Agent did not provide potential solutions)"

        # Fill template with all context
        try:
            prompt = template.format(
                ticket_id=ticket_id,
                ticket_title=ticket_details.get("title", "Unknown"),
                ticket_description=ticket_details.get("description", "No description provided"),
                ticket_status=ticket_details.get("status", "unknown"),
                ticket_priority=ticket_details.get("priority", "unknown"),
                agent_id=ticket_details.get("assigned_agent_id", "unassigned"),
                conflict_description=conflict_description,
                context=context if context else "(No additional context provided)",
                potential_solutions=solutions_text,
                related_tickets=tickets_context,
                active_tasks=tasks_context,
            )
            return prompt
        except Exception as e:
            logger.error(f"Failed to format ticket clarification template: {e}")
            return self._build_fallback_clarification_prompt(
                ticket_id,
                conflict_description,
                context,
                potential_solutions,
                ticket_details,
            )

    def _build_fallback_clarification_prompt(
        self,
        ticket_id: str,
        conflict_description: str,
        context: str,
        potential_solutions: List[str],
        ticket_details: Dict[str, Any],
    ) -> str:
        """Fallback prompt when template is unavailable."""
        solutions_text = "\n".join([f"{i + 1}. {sol}" for i, sol in enumerate(potential_solutions)]) if potential_solutions else "No solutions provided"

        return get_prompt(
            "fallback_ticket_clarification",
            {
                "ticket_id": ticket_id,
                "ticket_title": ticket_details.get("title", "Unknown"),
                "ticket_description": ticket_details.get("description", "No description"),
                "conflict_description": conflict_description,
                "context": context if context else "None provided",
                "solutions_text": solutions_text,
            },
        )

    # Default/fallback methods
    def _default_task_enrichment(self, task_description: str, done_definition: str) -> Dict[str, Any]:
        """Default task enrichment when model unavailable."""
        return {
            "enriched_description": task_description,
            "completion_criteria": [done_definition],
            "agent_prompt": f"Complete this task: {task_description}",
            "required_capabilities": ["general"],
            "estimated_complexity": 5,
        }

    def _default_agent_prompt(self, task: Dict[str, Any], memories: List[Dict[str, Any]], project_context: str) -> str:
        """Generate default agent prompt."""
        memory_context = "\n".join([f"- {mem.get('content', '')[:200]}" for mem in memories[:10]])

        return get_base_system_prompt(
            agent_id=task.get("agent_id", "unknown"),
            task_id=task.get("id", "unknown"),
            memory_context=memory_context,
            project_context=project_context,
        )

    def _default_trajectory_analysis(self) -> Dict[str, Any]:
        """Default trajectory analysis when model unavailable."""
        return {
            "current_phase": "unknown",
            "trajectory_aligned": True,
            "alignment_score": 0.5,
            "alignment_issues": [],
            "needs_steering": False,
            "steering_type": None,
            "steering_recommendation": None,
            "trajectory_summary": "Analysis unavailable",
        }

    def _default_coherence_analysis(self) -> Dict[str, Any]:
        """Default coherence analysis when model unavailable."""
        return {
            "coherence_score": 0.7,
            "duplicates": [],
            "alignment_issues": [],
            "termination_recommendations": [],
            "coordination_needs": [],
            "system_summary": "Analysis unavailable",
        }

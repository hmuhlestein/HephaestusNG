"""Abstract interface for LLM providers."""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.monitoring.models import ConductorSystemAnalysis, GuardianTrajectoryAnalysis
from src.prompts.loader import get_base_system_prompt

logger = logging.getLogger(__name__)


class LLMProviderInterface(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def enrich_task(
        self,
        task_description: str,
        done_definition: str,
        context: List[str],
        phase_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enrich a task with LLM analysis.

        Args:
            task_description: Raw task description
            done_definition: What constitutes task completion
            context: Relevant context from memory
            phase_context: Optional phase context for workflow-based tasks

        Returns:
            Dictionary containing:
                - enriched_description: Clear, unambiguous task description
                - completion_criteria: Specific completion criteria
                - agent_prompt: Suggested system prompt for agent
                - required_capabilities: List of required capabilities
                - estimated_complexity: Complexity score (1-10)
        """
        pass

    @abstractmethod
    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding vector for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        pass

    @abstractmethod
    async def generate_agent_prompt(
        self,
        task: Dict[str, Any],
        memories: List[Dict[str, Any]],
        project_context: str,
        phase_name: str = None,
    ) -> str:
        """Generate agent system prompt.

        Task description and completion criteria are intentionally omitted —
        they arrive in the initial user-turn message with concrete IDs and
        worktree path already interpolated.  Repeating them here wastes
        context tokens and creates two sources of truth.
        """
        memory_context = "\n".join(
            [f"- {mem.get('content', '')[:200]}" for mem in memories[:10]]
        )

        return get_base_system_prompt(
            agent_id=task.get("agent_id", "unknown"),
            task_id=task.get("id", "unknown"),
            memory_context=memory_context,
            project_context=project_context,
        )

    def get_model_name(self) -> str:
        """Get the name of the model being used.

        Returns:
            Model name
        """
        pass

    @abstractmethod
    async def analyze_agent_trajectory(
        self,
        agent_output: str,
        accumulated_context: Dict[str, Any],
        past_summaries: List[Dict[str, Any]],
        task_info: Dict[str, Any],
        last_message_marker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze agent using trajectory thinking.

        This method implements trajectory thinking from the tamagotchi system:
        - Builds understanding from ENTIRE conversation
        - Tracks persistent constraints and goals
        - Detects trajectory alignment
        - Provides targeted steering recommendations

        Args:
            agent_output: Recent agent output
            accumulated_context: Full accumulated context from conversation
            past_summaries: Previous Guardian summaries
            task_info: Current task information
            last_message_marker: Optional marker from previous cycle to identify new content

        Returns:
            Dictionary containing:
                - current_phase: Current work phase
                - trajectory_aligned: Whether agent is on track
                - alignment_issues: List of alignment problems
                - steering_recommendation: Steering message if needed
                - progress_estimate: Estimated progress percentage
                - last_claude_message_marker: Marker for next cycle
        """
        pass

    @abstractmethod
    async def analyze_system_coherence(
        self,
        guardian_summaries: List[Dict[str, Any]],
        system_goals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze system-wide coherence from Guardian summaries.

        This method is used by the Conductor to:
        - Detect duplicate work across agents
        - Check collective progress
        - Identify resource conflicts
        - Ensure system coherence

        Args:
            guardian_summaries: All Guardian analysis results
            system_goals: Overall system goals

        Returns:
            Dictionary containing:
                - duplicates: List of duplicate work pairs
                - coherence_score: System coherence score (0-1)
                - termination_recommendations: Agents to terminate
                - coordination_needs: Resource coordination requirements
        """
        pass


class OpenAIProvider(LLMProviderInterface):
    """OpenAI GPT implementation."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4-turbo-preview",
        embedding_model: str = "text-embedding-ada-002",
    ):
        """Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key
            model: Model to use for completions
            embedding_model: Model to use for embeddings
        """
        import httpx
        import openai

        # Create client with explicit httpx client to avoid proxy parameter issues
        self.client = openai.AsyncOpenAI(
            api_key=api_key, http_client=httpx.AsyncClient()
        )
        self.model = model
        self.embedding_model = embedding_model

    async def enrich_task(
        self,
        task_description: str,
        done_definition: str,
        context: List[str],
        phase_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enrich task using GPT."""
        prompt = f"""Given this task request, analyze and enrich it with clear specifications.

Task: {task_description}
Done Definition: {done_definition}
Context: {" ".join(context[:10])}  # Limit context to avoid token overflow"""

        if phase_context:
            prompt += f"""

# Phase info
{phase_context}"""

        prompt += """

Generate a JSON response with:
1. "enriched_description": A clear, unambiguous task description
2. "completion_criteria": Specific, measurable completion criteria (list)
3. "agent_prompt": A focused system prompt for the agent executing this task
4. "required_capabilities": List of required capabilities (e.g., "file_editing", "code_analysis")
5. "estimated_complexity": Integer 1-10 indicating task complexity

Ensure the enriched description is actionable and the completion criteria are specific and verifiable."""

        if phase_context:
            prompt += "\nConsider the phase context when determining complexity and requirements."

        try:
            # Build kwargs based on model type
            kwargs = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a task analysis expert for an AI orchestration system.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }

            # Use max_completion_tokens for newer models, max_tokens for older ones
            if "gpt-4o" in self.model or "gpt-5" in self.model or "o1" in self.model:
                kwargs["max_completion_tokens"] = 16000
            else:
                kwargs["max_tokens"] = 16000

            response = await self.client.chat.completions.create(**kwargs)

            result = json.loads(response.choices[0].message.content)
            logger.debug(
                f"Task enriched successfully: {result.get('enriched_description', '')[:100]}..."
            )
            return result

        except Exception as e:
            logger.error(f"Failed to enrich task: {e}")
            # Return a basic enrichment as fallback
            return {
                "enriched_description": task_description,
                "completion_criteria": [done_definition],
                "agent_prompt": f"Complete this task: {task_description}",
                "required_capabilities": ["general"],
                "estimated_complexity": 5,
            }

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding using OpenAI."""
        try:
            response = await self.client.embeddings.create(
                model=self.embedding_model,
                input=text[:8000],  # Limit input length
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            # Return zero vector as fallback (3072 for text-embedding-3-large)
            return [0.0] * 3072

    async def generate_agent_prompt(
        self,
        task: Dict[str, Any],
        memories: List[Dict[str, Any]],
        project_context: str,
        phase_name: str = None,
    ) -> str:
        """Generate agent system prompt.

        Task description and completion criteria are intentionally omitted —
        they arrive in the initial user-turn message with concrete IDs and
        worktree path already interpolated.  Repeating them here wastes
        context tokens and creates two sources of truth.
        """
        from src.prompts.loader import get_phase_system_prompt

        memory_context = "\n".join(
            [f"- {mem.get('content', '')[:200]}" for mem in memories[:10]]
        )

        specialized = get_phase_system_prompt(
            phase_name,
            agent_id=task.get("agent_id", "unknown"),
            task_id=task.get("id", "unknown"),
            memory_context=memory_context,
            project_context=project_context,
        )
        if specialized:
            return specialized

        return get_base_system_prompt(
            agent_id=task.get("agent_id", "unknown"),
            task_id=task.get("id", "unknown"),
            memory_context=memory_context,
            project_context=project_context,
        )

    async def analyze_agent_trajectory(
        self,
        agent_output: str,
        accumulated_context: Dict[str, Any],
        past_summaries: List[Dict[str, Any]],
        task_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze agent using trajectory thinking with structured prompts."""
        # Use the prompt loader to get properly formatted prompt
        from src.monitoring.prompt_loader import prompt_loader

        prompt = prompt_loader.format_guardian_prompt(
            accumulated_context=accumulated_context,
            past_summaries=past_summaries,
            task_info=task_info,
            agent_output=agent_output,
        )

        for attempt in range(3):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a trajectory analysis expert using accumulated context thinking.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": GuardianTrajectoryAnalysis,
                }

                if (
                    "gpt-4o" in self.model
                    or "gpt-5" in self.model
                    or "o1" in self.model
                ):
                    kwargs["max_completion_tokens"] = 16000
                else:
                    kwargs["max_tokens"] = 16000

                response = await self.client.beta.chat.completions.parse(**kwargs)

                # parsed is Optional -- the SDK leaves it None when the model
                # refuses or the response doesn't validate. Calling
                # .model_dump() on that raised AttributeError, which the
                # handler below reported as "'NoneType' object has no
                # attribute 'model_dump'" three times over before falling
                # back, hiding what actually happened.
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError(
                        "model returned no parsed structured output "
                        f"(refusal: {response.choices[0].message.refusal!r})"
                    )
                return parsed.model_dump()

            except Exception as e:
                logger.error(
                    f"Failed to analyze trajectory (attempt {attempt + 1}/3): {e}"
                )
                if attempt == 2:  # Last attempt
                    # Return fallback with proper structure
                    fallback = GuardianTrajectoryAnalysis(
                        current_phase="unknown",
                        trajectory_aligned=True,
                        alignment_score=0.5,
                        alignment_issues=[],
                        needs_steering=False,
                        steering_type=None,
                        steering_recommendation=None,
                        trajectory_summary="Analysis failed after 3 attempts",
                    )
                    return fallback.model_dump()
                await asyncio.sleep(1)  # Brief delay before retry

    async def analyze_system_coherence(
        self,
        guardian_summaries: List[Dict[str, Any]],
        system_goals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze system-wide coherence from Guardian summaries with structured prompts."""
        # Use the prompt loader to get properly formatted prompt
        from src.monitoring.prompt_loader import prompt_loader

        prompt = prompt_loader.format_conductor_prompt(
            guardian_summaries=guardian_summaries,
            system_goals=system_goals,
        )

        for attempt in range(3):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a system orchestration expert analyzing multi-agent coherence.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": ConductorSystemAnalysis,
                }

                if (
                    "gpt-4o" in self.model
                    or "gpt-5" in self.model
                    or "o1" in self.model
                ):
                    kwargs["max_completion_tokens"] = 16000
                else:
                    kwargs["max_tokens"] = 16000

                response = await self.client.beta.chat.completions.parse(**kwargs)

                # parsed is Optional -- the SDK leaves it None when the model
                # refuses or the response doesn't validate. Calling
                # .model_dump() on that raised AttributeError, which the
                # handler below reported as "'NoneType' object has no
                # attribute 'model_dump'" three times over before falling
                # back, hiding what actually happened.
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError(
                        "model returned no parsed structured output "
                        f"(refusal: {response.choices[0].message.refusal!r})"
                    )
                return parsed.model_dump()

            except Exception as e:
                logger.error(
                    f"Failed to analyze system coherence (attempt {attempt + 1}/3): {e}"
                )
                if attempt == 2:  # Last attempt
                    # Return fallback with proper structure
                    fallback = ConductorSystemAnalysis(
                        coherence_score=0.7,
                        duplicates=[],
                        alignment_issues=[],
                        termination_recommendations=[],
                        coordination_needs=[],
                        system_summary="Analysis failed after 3 attempts - assuming moderate coherence",
                    )
                    return fallback.model_dump()
                await asyncio.sleep(1)  # Brief delay before retry

    def get_model_name(self) -> str:
        """Get model name."""
        return self.model


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter provider (OpenAI-compatible API with custom base URL)."""

    def __init__(
        self,
        api_key: str,
        model: str = "xiaomi/mimo-v2.5",
        embedding_model: str = "text-embedding-ada-002",
    ):
        import httpx
        import openai

        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            http_client=httpx.AsyncClient(),
        )
        self.model = model
        self.embedding_model = embedding_model


# Registry for LLM providers
LLM_PROVIDERS = {
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
}


def get_llm_provider() -> LLMProviderInterface:
    """Get LLM provider instance based on configuration.

    Returns:
        Configured LLM provider instance
    """
    from ..core.llm_config import get_config as get_llm_config

    logger.info("=" * 60)
    logger.info("🔧 Initializing LLM Provider System")
    logger.info("=" * 60)

    # model_assignments (hephaestus_config.yaml) is required -- MultiProviderLLM
    # is the only supported prompt source. This used to silently fall back to
    # single-provider mode (e.g. OpenAIProvider) on any failure
    # here, including a merely-missing config -- those legacy providers carry
    # their own separate copies of every prompt LangChainLLMClient builds, so
    # a silent fallback meant a misconfigured deployment could run for a long
    # time on prompt text that had already drifted out of sync with the
    # actively maintained versions, with nothing surfacing the mismatch.
    # Raising here makes a missing/invalid config a startup-time failure
    # instead of a silent, hard-to-notice divergence.
    llm_config = get_llm_config()
    llm_config.validate(strict=False)

    if not (llm_config._llm_config and llm_config._llm_config.model_assignments):
        raise RuntimeError(
            "No model_assignments found in hephaestus_config.yaml. The "
            "single-provider fallback has been removed -- configure "
            "model_assignments for the multi-provider LLM client."
        )

    from .multi_provider_llm import MultiProviderLLM

    logger.info(
        "✅ Using MULTI-PROVIDER LLM configuration (from hephaestus_config.yaml)"
    )
    logger.info("=" * 60)
    return MultiProviderLLM()

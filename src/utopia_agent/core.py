"""Core Utopia Agent — the main brain orchestrating everything."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from utopia_agent.agent_loop import AgenticLoop, AgentTrace
from utopia_agent.config import AgentConfig
from utopia_agent.llm.base import LLMProvider, LLMResponse, Message, Role, ToolCall
from utopia_agent.llm.factory import create_llm_provider
from utopia_agent.memory.store import ConversationMemory, LongTermMemory
from utopia_agent.memory.advanced import PromptMemory, SessionStore, UserModel
from utopia_agent.personas.loader import PersonaLoader
from utopia_agent.skills.manager import SkillManager
from utopia_agent.scheduler.engine import Scheduler
from utopia_agent.planning.react import ExecutionPlan, PlanBuilder
from utopia_agent.rag.engine import RAGEngine
from utopia_agent.tools.base import ToolResult, ToolStatus
from utopia_agent.tools.code_executor import CodeExecutorTool
from utopia_agent.tools.calculator import CalculatorTool
from utopia_agent.tools.datetime_tool import DateTimeTool
from utopia_agent.tools.file_ops import FileOperationsTool
from utopia_agent.tools.rag_search import RAGSearchTool
from utopia_agent.tools.registry import ToolRegistry
from utopia_agent.tools.web_search import WebSearchTool


class UtopiaAgent:
    """Super-powered Agentic AI.

    Supports any LLM provider, tool calling, RAG, memory, planning,
    and customizable personas.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()

        # Validate config early
        self._validate_config()

        # Initialize LLM provider
        self.llm: LLMProvider = create_llm_provider(self.config.get_llm_config())

        # Initialize subsystems (order matters: RAG before tools)
        self._init_memory()
        self._init_rag()
        self._init_tools()
        self._init_personas()

        # Agentic loop
        self.agent_loop = AgenticLoop(
            llm=self.llm,
            tools=self.tools,
            max_iterations=self.config.max_iterations,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

        # Planning
        self.plan_builder = PlanBuilder()
        self.current_plan: ExecutionPlan | None = None

        # Advanced memory (4-layer)
        data_dir = self.config.data_path
        self.prompt_memory = PromptMemory(data_dir)
        self.session_store = SessionStore(data_dir / "sessions")
        self.user_model = UserModel(data_dir)

        # Skill system (self-improving)
        self.skill_manager = SkillManager(self.config.data_path / "skills")

        # Session ID for this run
        import uuid
        self.session_id = str(uuid.uuid4())[:8]

        # Scheduler (cron-like automations)
        self.scheduler = Scheduler(
            data_dir=self.config.data_path / "scheduler",
            agent_factory=lambda: UtopiaAgent(self.config),
        )

    def _validate_config(self) -> None:
        """Validate configuration and show helpful error messages."""
        from utopia_agent.config import LLMProvider

        llm_config = self.config.get_llm_config()
        provider = llm_config.get("provider", "")
        api_key = llm_config.get("api_key", "")

        if provider in ("openai", "anthropic", "groq") and not api_key:
            env_var = {
                "openai": "UTOPIA_OPENAI_API_KEY",
                "anthropic": "UTOPIA_ANTHROPIC_API_KEY",
                "groq": "UTOPIA_GROQ_API_KEY",
            }[provider]
            raise ValueError(
                f"API key not set for {provider.upper()}!\n\n"
                f"Set it with:\n"
                f"  export {env_var}=your-api-key\n\n"
                f"Get your key from:\n"
                f"  OpenAI:     https://platform.openai.com/api-keys\n"
                f"  Anthropic:  https://console.anthropic.com/\n"
                f"  Groq:       https://console.groq.com/\n"
                f"  OpenRouter: https://openrouter.ai/keys\n\n"
                f"Or use Ollama (free, local):\n"
                f"  export UTOPIA_LLM_PROVIDER=ollama\n"
                f"  export UTOPIA_OLLAMA_MODEL=llama3.1"
            )
        elif provider == "custom" and not llm_config.get("base_url"):
            raise ValueError(
                "Custom provider URL not set!\n\n"
                "Set it with:\n"
                "  export UTOPIA_CUSTOM_BASE_URL=https://openrouter.ai/api/v1\n"
                "  export UTOPIA_CUSTOM_API_KEY=your-key\n"
                "  export UTOPIA_CUSTOM_MODEL=model-name"
            )

    def _init_memory(self) -> None:
        """Initialize memory systems."""
        data_dir = self.config.data_path / "memory"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.conversation = ConversationMemory(
            max_messages=self.config.memory_max_messages,
            persist_path=str(data_dir / "conversation.json"),
        )
        self.long_term = LongTermMemory(
            persist_path=str(data_dir / "longterm.json"),
        )

    def _init_tools(self) -> None:
        """Initialize and register all tools."""
        self.tools = ToolRegistry()

        # Always available tools
        self.tools.register(CalculatorTool())
        self.tools.register(DateTimeTool())

        # Conditionally available tools
        if self.config.web_search_enabled:
            self.tools.register(WebSearchTool())

        if self.config.code_execution_enabled:
            self.tools.register(CodeExecutorTool())

        if self.config.file_operations_enabled:
            self.tools.register(FileOperationsTool())

        if self.config.shell_enabled:
            from utopia_agent.tools.shell import ShellTool

            self.tools.register(ShellTool())

        # API, DB, and Web Scraper tools
        if self.config.api_call_enabled:
            from utopia_agent.tools.api_caller import APICallerTool
            self.tools.register(APICallerTool())

        if self.config.db_query_enabled:
            from utopia_agent.tools.db_query import DatabaseQueryTool
            self.tools.register(DatabaseQueryTool())

        if self.config.web_scraper_enabled:
            from utopia_agent.tools.web_scraper import WebScraperTool
            self.tools.register(WebScraperTool())

        # Coding tools (agentic coding)
        from utopia_agent.tools.coding import CodebaseTool, MultiFileEditorTool, GitTool, TestRunnerTool
        self.tools.register(CodebaseTool())
        self.tools.register(MultiFileEditorTool())
        self.tools.register(GitTool())
        self.tools.register(TestRunnerTool())

        # RAG search tool
        if self.config.rag_enabled and self._rag_engine:
            self.tools.register(RAGSearchTool(rag_engine=self._rag_engine))

    def _init_rag(self) -> None:
        """Initialize RAG engine."""
        self._rag_engine = None
        if self.config.rag_enabled:
            self._rag_engine = RAGEngine(
                persist_dir=self.config.vector_store_path,
                chunk_size=self.config.rag_chunk_size,
                chunk_overlap=self.config.rag_chunk_overlap,
            )

    def _init_personas(self) -> None:
        """Initialize persona system."""
        self.persona_loader = PersonaLoader(personas_dir=self.config.personas_dir)

    def _build_system_prompt(self) -> str:
        """Build the system prompt from persona + context + advanced memory."""
        # Get persona prompt
        if self.config.system_prompt:
            system_prompt = self.config.system_prompt
        else:
            system_prompt = self.persona_loader.get(self.config.persona)

        # Add tool usage instructions
        tool_names = [t.name for t in self.tools.list_tools()]
        if tool_names:
            system_prompt += (
                f"\n\nYou have access to the following tools: {', '.join(tool_names)}. "
                "Use tools when they would help answer the user's question. "
                "To use a tool, respond with a tool call in the appropriate format. "
                "Always think step by step before acting."
            )

        # Add RAG context instruction
        if self._rag_engine:
            system_prompt += (
                "\n\nYou have a document knowledge base. Use the rag_search tool "
                "to look up information from ingested documents when relevant."
            )

        # Layer 1: Prompt Memory (always loaded)
        prompt_mem = self.prompt_memory.get_combined()
        if prompt_mem:
            system_prompt += f"\n\n{prompt_mem}"

        # Layer 3: Skill summaries (progressive disclosure)
        skill_summaries = self.skill_manager.list_summaries()
        if skill_summaries:
            skill_list = "\n".join(
                f"  - {s['name']}: {s['description']}" for s in skill_summaries[:20]
            )
            system_prompt += (
                f"\n\nYou have learned {len(skill_summaries)} reusable skills:\n{skill_list}\n"
                "Load a skill's full steps when you encounter a similar task."
            )

        # Layer 4: User model context
        user_ctx = self.user_model.get_context()
        if user_ctx:
            system_prompt += f"\n\n{user_ctx}"

        # Conversation summary
        summary = self.conversation.summary
        if summary:
            system_prompt += f"\n\n[Conversation history summary]: {summary[:2000]}"

        return system_prompt

    async def chat(self, user_message: str) -> str:
        """Send a message and get a response (non-streaming)."""
        # Add user message to memory
        self.conversation.add(Message(role=Role.USER, content=user_message))

        # Build messages for agentic loop
        system_prompt = self._build_system_prompt()
        messages = list(self.conversation.get_context())

        # Get tool schemas
        tool_schemas = self.tools.list_schemas() or None

        # Run enhanced agentic loop
        response, trace = await self.agent_loop.run(
            messages=messages,
            system_prompt=system_prompt,
            tool_schemas=tool_schemas,
        )

        # Save to memory
        self.conversation.add(Message(role=Role.ASSISTANT, content=response))

        # Layer 2: Save to session store
        self.session_store.save_message(self.session_id, "user", user_message)
        self.session_store.save_message(self.session_id, "assistant", response)

        # Auto-create skill if task was complex
        self._maybe_create_skill(user_message, response, trace)

        return response

    async def chat_stream(self, user_message: str) -> AsyncIterator[str]:
        """Send a message and stream the response."""
        self.conversation.add(Message(role=Role.USER, content=user_message))

        system_prompt = self._build_system_prompt()
        messages = list(self.conversation.get_context())
        tool_schemas = self.tools.list_schemas() or None

        # First check if tools are needed
        response = await self.llm.chat(
            messages=[Message(role=Role.SYSTEM, content=system_prompt)] + messages,
            tools=tool_schemas,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

        if response.has_tool_calls:
            # Execute tools, show status, then stream final answer
            tool_calls_dicts = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in response.tool_calls
            ]
            messages.append(
                Message(role=Role.ASSISTANT, content=response.content or "", tool_calls=tool_calls_dicts)
            )
            for tc in response.tool_calls:
                result = await self.tools.execute(tc.name, tc.arguments)
                status = "✅" if result.is_success else "❌"
                yield f"\n🔧 {tc.name}: {status}\n"
                messages.append(
                    Message(role=Role.TOOL, content=result.to_llm_message(), tool_call_id=tc.name, name=tc.name)
                )

            full_response = ""
            async for chunk in self.llm.stream_chat(
                messages=[Message(role=Role.SYSTEM, content=system_prompt)] + messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            ):
                full_response += chunk
                yield chunk
            self.conversation.add(Message(role=Role.ASSISTANT, content=full_response))
        else:
            full_response = ""
            async for chunk in self.llm.stream_chat(
                messages=[Message(role=Role.SYSTEM, content=system_prompt)] + messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            ):
                full_response += chunk
                yield chunk
            self.conversation.add(Message(role=Role.ASSISTANT, content=full_response))

    async def chat_with_trace(self, user_message: str) -> tuple[str, AgentTrace]:
        """Chat with full execution trace for debugging."""
        self.conversation.add(Message(role=Role.USER, content=user_message))
        system_prompt = self._build_system_prompt()
        messages = list(self.conversation.get_context())
        tool_schemas = self.tools.list_schemas() or None

        response, trace = await self.agent_loop.run(
            messages=messages,
            system_prompt=system_prompt,
            tool_schemas=tool_schemas,
        )
        self.conversation.add(Message(role=Role.ASSISTANT, content=response))
        return response, trace

    async def plan_and_execute(self, goal: str) -> ExecutionPlan:
        """Create a plan for a goal and execute it step by step."""
        tool_names = [t.name for t in self.tools.list_tools()]

        # Generate plan
        planning_prompt = self.plan_builder.build_planning_prompt(goal, tool_names)
        response = await self.llm.chat(
            messages=[
                Message(role=Role.SYSTEM, content="You are a planning assistant. Respond with JSON only."),
                Message(role=Role.USER, content=planning_prompt),
            ],
            temperature=0.3,
        )

        plan = self.plan_builder.parse_plan(response.content or "", goal)
        self.current_plan = plan
        plan.status = "running"

        # Execute each step
        for step in plan.steps:
            step.status = "running"

            if step.action:
                # Execute tool
                result = await self.tools.execute(step.action, step.action_input or {})
                step.observation = result.to_llm_message()
                step.status = "completed" if result.is_success else "failed"
            else:
                # Reasoning step
                reason_response = await self.llm.chat(
                    messages=[
                        Message(role=Role.SYSTEM, content=self._build_system_prompt()),
                        Message(role=Role.USER, content=f"Execute this step: {step.thought}"),
                    ],
                    temperature=self.config.temperature,
                )
                step.observation = reason_response.content or ""
                step.status = "completed"

        plan.status = "completed"
        return plan

    # === Public API for RAG ===
    async def ingest_document(self, file_path: str) -> int:
        """Ingest a document into the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.ingest_file(file_path)

    async def ingest_text(self, text: str, source: str = "user_input") -> int:
        """Ingest raw text into the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.ingest_text(text, source=source)

    async def search_knowledge(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Search the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.search(query, top_k=top_k)

    # === Public API for Memory ===
    def remember(self, category: str, fact: str) -> None:
        """Store a fact in long-term memory."""
        self.long_term.store(category, fact)

    def recall(self, query: str) -> list[dict[str, str]]:
        """Search long-term memory."""
        return self.long_term.search(query)

    def clear_memory(self) -> None:
        """Clear conversation history."""
        self.conversation.clear()

    # === Public API for Tools ===
    def list_tools(self) -> list[str]:
        """List available tool names."""
        return [t.name for t in self.tools.list_tools()]

    # === Public API for Personas ===
    def set_persona(self, name: str) -> None:
        """Switch to a different persona."""
        self.config.persona = name

    def list_personas(self) -> list[str]:
        """List available personas."""
        return self.persona_loader.list_available()

    def create_persona(self, name: str, prompt: str) -> None:
        """Create a custom persona."""
        self.persona_loader.save(name, prompt)

    def get_provider_info(self) -> dict[str, Any]:
        """Get current LLM provider information."""
        return {
            "provider": self.config.llm_provider.value,
            "model": self.config.get_llm_config().get("model", "unknown"),
        }

    # === Public API for Skills ===
    def list_skills(self) -> list[dict[str, str]]:
        """List all learned skills."""
        return self.skill_manager.list_summaries()

    def get_skill(self, name: str) -> str | None:
        """Get full skill content."""
        skill = self.skill_manager.get(name)
        return skill.to_prompt() if skill else None

    def search_skills(self, query: str) -> list[dict[str, str]]:
        """Search skills by query."""
        results = self.skill_manager.search(query)
        return [{"name": s.name, "description": s.description} for s in results]

    def _maybe_create_skill(self, task: str, response: str, trace: AgentTrace) -> None:
        """Auto-create skill if task was complex enough."""
        if not self.skill_manager.should_create_skill(
            tool_call_count=trace.total_tool_calls,
            had_error_recovery=False,
            had_user_correction=False,
        ):
            return

        # Use LLM to generate skill steps
        try:
            import asyncio

            async def _gen_skill():
                prompt = (
                    f"The user asked: {task}\n"
                    f"The agent completed the task using {trace.total_tool_calls} tool calls.\n"
                    f"Final response: {response[:500]}\n\n"
                    f"Create a reusable skill for this type of task.\n"
                    f"Respond in JSON: {{\"name\": \"...\", \"description\": \"...\", \"steps\": [\"step1\", ...]}}"
                )
                return await self.llm.chat(
                    messages=[
                        Message(role=Role.SYSTEM, content="You are a skill creator. Respond with JSON only."),
                        Message(role=Role.USER, content=prompt),
                    ],
                    temperature=0.3,
                    max_tokens=500,
                )

            resp = asyncio.run(_gen_skill())
            json_start = (resp.content or "").find("{")
            json_end = (resp.content or "").rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                import json
                skill_data = json.loads(resp.content[json_start:json_end])
                self.skill_manager.create_skill(
                    name=skill_data.get("name", f"skill_{len(self.skill_manager.list_skills()) + 1}"),
                    description=skill_data.get("description", "Auto-generated skill"),
                    steps=skill_data.get("steps", []),
                    tags=["auto-generated"],
                )
        except Exception:
            pass  # Don't break chat if skill creation fails

    # === Public API for Sessions ===
    def search_sessions(self, query: str) -> list[dict[str, Any]]:
        """Search past sessions."""
        return self.session_store.search(query)

    def get_session_stats(self) -> dict[str, Any]:
        """Get session statistics."""
        return self.session_store.get_stats()

    def learn_preference(self, key: str, value: str) -> None:
        """Teach the agent a preference."""
        self.user_model.learn_preference(key, value)
        self.prompt_memory.add_user_info(f"{key}: {value}")

    # === Public API for Scheduler ===
    def add_scheduled_task(
        self,
        name: str,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        persona: str = "default",
        timeout: int = 120,
    ) -> dict[str, str]:
        """Add a scheduled task."""
        task = self.scheduler.add_task(
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            persona=persona,
            timeout=timeout,
        )
        return task.to_dict()

    def remove_scheduled_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        return self.scheduler.remove_task(task_id)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """List all scheduled tasks."""
        return [t.to_dict() for t in self.scheduler.list_tasks()]

    def toggle_scheduled_task(self, task_id: str, enabled: bool | None = None) -> bool:
        """Enable/disable a scheduled task."""
        return self.scheduler.toggle_task(task_id, enabled)

    async def run_scheduled_task_now(self, task_id: str) -> str:
        """Run a scheduled task immediately."""
        task = self.scheduler.get_task(task_id)
        if not task:
            return f"Task {task_id} not found"
        return await self.scheduler.run_task(task)

    async def run_due_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Run all due scheduled tasks."""
        return await self.scheduler.run_due_tasks()

    async def start_scheduler(self, check_interval: int = 60) -> None:
        """Start the background scheduler loop."""
        await self.scheduler.start_background_loop(check_interval)

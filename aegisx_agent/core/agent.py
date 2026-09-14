"""Core AegisX Agent — the main brain orchestrating everything."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from aegisx_agent.core.config import AgentConfig, missing_credentials
from aegisx_agent.core.loop import AgenticLoop, AgentTrace
from aegisx_agent.core.memory_api import MemoryAPI
from aegisx_agent.core.rag_api import RAGAPI
from aegisx_agent.core.scheduler_api import SchedulerAPI
from aegisx_agent.llm.base import LLMProvider, Message, Role
from aegisx_agent.llm.factory import create_llm_provider
from aegisx_agent.mcp import MCPManager, load_mcp_config
from aegisx_agent.memory.advanced import PromptMemory, SessionStore, UserModel
from aegisx_agent.memory.store import ConversationMemory, LongTermMemory
from aegisx_agent.observability.usage import USAGE_FILE, UsageTracker
from aegisx_agent.personas.loader import PersonaLoader
from aegisx_agent.planning.react import ExecutionPlan, PlanBuilder
from aegisx_agent.plugins import PluginDefinition, PluginRegistry
from aegisx_agent.project import ProjectContext, detect_project
from aegisx_agent.rag.engine import RAGEngine
from aegisx_agent.scheduler.engine import Scheduler
from aegisx_agent.security.audit import AuditLog
from aegisx_agent.security.permissions import PermissionGate, PermissionMode, PrompterFunc
from aegisx_agent.skills.manager import SkillManager
from aegisx_agent.tools.calculator import CalculatorTool
from aegisx_agent.tools.code_executor import CodeExecutorTool
from aegisx_agent.tools.datetime_tool import DateTimeTool
from aegisx_agent.tools.file_ops import FileOperationsTool
from aegisx_agent.tools.rag_search import RAGSearchTool
from aegisx_agent.tools.registry import ToolRegistry
from aegisx_agent.tools.skill_tool import SkillTool
from aegisx_agent.tools.web_search import WebSearchTool


class AegisXAgent(RAGAPI, MemoryAPI, SchedulerAPI):
    """Super-powered Agentic AI.

    Supports any LLM provider, tool calling, RAG, memory, planning,
    and customizable personas.
    """

    def __init__(
        self,
        config: AgentConfig | None = None,
        interactive: bool = True,
        prompter: PrompterFunc | None = None,
        project: ProjectContext | None = None,
    ) -> None:
        self.config = config or AgentConfig()

        # Workspace awareness, detected lazily so an agent that never chats
        # pays nothing for it.
        self._project = project
        self._project_detected = project is not None

        # Validate config early
        self._validate_config()

        # Initialize LLM provider
        self.llm: LLMProvider = create_llm_provider(self.config.get_llm_config())

        # Permission gate must exist before tools are registered
        self.permission_gate = PermissionGate(
            mode=self.config.permission_mode,
            allowed=self.config.allowed_tool_names,
            denied=self.config.denied_tool_names,
            interactive=interactive,
            prompter=prompter,
            audit=AuditLog(
                self.config.audit_log_path,
                enabled=self.config.audit_log_enabled,
            ),
        )

        # Initialize subsystems (order matters: RAG and skills before tools)
        self._init_memory()
        self._init_rag()
        self._init_skills()
        self.plugin_registry = PluginRegistry()
        self._init_tools()
        self._init_personas()

        # MCP servers (Model Context Protocol): external tool catalogs that
        # are bridged through the plugin registry and the permission gate.
        self.mcp = MCPManager(
            data_path=self.config.data_path,
            plugin_registry=self.plugin_registry,
            tool_registry=self.tools,
        )

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

        # Session ID for this run
        import uuid
        self.session_id = str(uuid.uuid4())[:8]

        # Usage tracking wraps the provider so every path (chat, streaming,
        # plan execution, scheduled runs) records tokens under one run id.
        self.usage = UsageTracker(self.llm, self.config.data_path / USAGE_FILE)
        self.usage.run_id = self.session_id
        self.llm = self.usage  # type: ignore[assignment]
        self.agent_loop.llm = self.llm

        # Scheduler (cron-like automations).
        # Scheduled runs are unattended: the factory builds an agent that cannot
        # prompt, so a dangerous tool is denied instead of silently allowed.
        self.scheduler = Scheduler(
            data_dir=self.config.data_path / "scheduler",
            agent_factory=lambda: AegisXAgent(
                self.config, interactive=False, project=self._project
            ),
        )

    def _validate_config(self) -> None:
        """Validate configuration and show helpful error messages."""

        provider = missing_credentials(self.config)
        if not provider:
            return

        if provider in ("openai", "anthropic", "groq"):
            env_var = {
                "openai": "AEGISX_OPENAI_API_KEY",
                "anthropic": "AEGISX_ANTHROPIC_API_KEY",
                "groq": "AEGISX_GROQ_API_KEY",
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
                f"  export AEGISX_LLM_PROVIDER=ollama\n"
                f"  export AEGISX_OLLAMA_MODEL=llama3.1"
            )
        raise ValueError(
            f"No endpoint configured for the '{provider}' provider!\n\n"
            "Set it with:\n"
            "  export AEGISX_CUSTOM_BASE_URL=https://openrouter.ai/api/v1\n"
            "  export AEGISX_CUSTOM_API_KEY=your-key\n"
            "  export AEGISX_CUSTOM_MODEL=model-name"
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
        self.tools = ToolRegistry(gate=self.permission_gate)

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
            from aegisx_agent.tools.shell import ShellTool

            self.tools.register(ShellTool())

        # API, DB, and Web Scraper tools
        if self.config.api_call_enabled:
            from aegisx_agent.tools.api_caller import APICallerTool
            self.tools.register(APICallerTool())

        if self.config.db_query_enabled:
            from aegisx_agent.tools.db_query import DatabaseQueryTool
            self.tools.register(DatabaseQueryTool())

        if self.config.web_scraper_enabled:
            from aegisx_agent.tools.web_scraper import WebScraperTool
            self.tools.register(WebScraperTool())

        # Coding tools (agentic coding)
        from aegisx_agent.tools.coding import (
            CodebaseTool,
            GitTool,
            MultiFileEditorTool,
            TestRunnerTool,
        )
        self.tools.register(CodebaseTool())
        self.tools.register(MultiFileEditorTool())
        self.tools.register(GitTool())
        self.tools.register(TestRunnerTool())

        # Skill library (progressive disclosure)
        self.tools.register(SkillTool(self.skill_manager))

        # RAG search tool
        if self.config.rag_enabled and self._rag_engine:
            self.tools.register(RAGSearchTool(rag_engine=self._rag_engine))

    def register_plugin(self, definition: PluginDefinition) -> str:
        """Validate and register one explicit plugin as a gated tool."""
        plugin_tool = self.plugin_registry.register(definition)
        self.tools.register(plugin_tool)
        return plugin_tool.name

    def load_plugin_module(self, module: str) -> list[str]:
        """Load and register plugin definitions from an importable module."""
        definitions = self.plugin_registry.load_module(module)
        return [self.register_plugin(definition) for definition in definitions]

    def load_plugin_path(self, path: str) -> list[str]:
        """Load and register plugin definitions from an explicit Python file."""
        definitions = self.plugin_registry.load_path(path)
        return [self.register_plugin(definition) for definition in definitions]

    def unload_plugin(self, plugin_id: str) -> bool:
        """Unload a plugin and remove every tool it brought from the registry."""
        definitions = self.plugin_registry.unregister(plugin_id)
        if definitions is None:
            return False
        for definition in definitions:
            self.tools.unregister(definition.manifest.qualified_tool_name)
        return True

    # ------------------------------------------------------------------ #
    # MCP (Model Context Protocol) servers
    # ------------------------------------------------------------------ #

    async def connect_mcp_server(
        self, server_id: str, server_config: dict[str, Any] | None = None
    ) -> list[str]:
        """Connect to an MCP server and register its tools as gated plugins.

        Without ``server_config`` the persisted config from
        ``<data_dir>/mcp_servers.json`` is used. Returns the qualified tool
        names (``mcp_<server>_<tool>``).
        """
        if server_config is None:
            server_config = self.mcp.get_server_config(server_id)
        return await self.mcp.connect_server(server_id, server_config)

    async def disconnect_mcp_server(self, server_id: str) -> bool:
        """Disconnect an MCP server and remove every tool it brought."""
        return await self.mcp.disconnect_server(server_id)

    def list_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Configured MCP servers, each annotated with connection state."""
        servers = load_mcp_config(self.mcp.config_path)
        return {
            server_id: {**server_config, "connected": self.mcp.is_connected(server_id)}
            for server_id, server_config in servers.items()
        }

    async def close_mcp_connections(self) -> None:
        """Tear down every MCP session (used at shutdown)."""
        await self.mcp.close_all()

    def _init_rag(self) -> None:
        """Initialize RAG engine."""
        self._rag_engine = None
        if self.config.rag_enabled:
            self._rag_engine = RAGEngine(
                persist_dir=self.config.vector_store_path,
                chunk_size=self.config.rag_chunk_size,
                chunk_overlap=self.config.rag_chunk_overlap,
            )

    def _init_skills(self) -> None:
        """Initialize the self-improving skill library."""
        self.skill_manager = SkillManager(self.config.data_path / "skills")
        self.last_skill_error = ""

    def _init_personas(self) -> None:
        """Initialize persona system."""
        self.persona_loader = PersonaLoader(personas_dir=self.config.personas_dir)

    def get_project(self) -> ProjectContext | None:
        """The workspace this agent is working in (detected on first use)."""
        if not self.config.project_context_enabled:
            return None
        if not self._project_detected:
            self._project = detect_project()
            self._project_detected = True
        return self._project

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

        # Workspace awareness (where am I, what is this project)
        project = self.get_project()
        if project is not None:
            system_prompt += f"\n\n{project.to_prompt()}"

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

        # Permission policy — telling the model up front avoids it burning turns
        # on tools the gate will refuse anyway.
        mode = self.permission_gate.mode
        if mode is PermissionMode.ALLOW_ALL:
            pass
        elif mode is PermissionMode.READ_ONLY:
            system_prompt += (
                "\n\n[Permissions]: read-only mode is active. Tools that write, delete, "
                "execute code, or run commands will be refused. Read, search, and "
                "calculate freely, and tell the user what change you would make."
            )
        else:
            system_prompt += (
                "\n\n[Permissions]: tools that execute code, run commands, delete files, "
                "or rewrite git state need explicit approval. Other tools run without "
                "asking. If a call is refused, stop and explain what you need instead "
                "of retrying it."
            )

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
        await self._maybe_create_skill(user_message, response, trace)

        return response

    async def chat_stream(self, user_message: str) -> AsyncIterator[str]:
        """Send a message and stream the response — ONE LLM call per turn.

        Tool calls are parsed from the stream itself, so a toolless turn costs
        exactly one request instead of a full non-streaming probe plus the
        real request. Text deltas are yielded live, tool executions emit a
        status line, and a completed turn is finished exactly like ``chat()``:
        memory, session store, and skill capture all run on the final trace.
        """
        self.conversation.add(Message(role=Role.USER, content=user_message))

        system_prompt = self._build_system_prompt()
        messages = list(self.conversation.get_context())
        tool_schemas = self.tools.list_schemas() or None

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _drive() -> None:
            def _on_chunk(piece: str) -> None:
                queue.put_nowait(piece)

            def _on_tool_result(name: str, success: bool) -> None:
                queue.put_nowait(f"\n🔧 {name}: {'✅' if success else '❌'}\n")

            try:
                response, trace = await self.agent_loop.run_streaming(
                    messages=messages,
                    system_prompt=system_prompt,
                    tool_schemas=tool_schemas,
                    on_chunk=_on_chunk,
                    on_tool_result=_on_tool_result,
                )
            finally:
                queue.put_nowait(None)

            # The loop only reaches here on a completed turn, so the same
            # bookkeeping as chat() applies.
            self.conversation.add(Message(role=Role.ASSISTANT, content=response))
            self.session_store.save_message(self.session_id, "user", user_message)
            self.session_store.save_message(self.session_id, "assistant", response)
            await self._maybe_create_skill(user_message, response, trace)

        task = asyncio.create_task(_drive())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            # Propagate loop failures (connection errors, bad config, …).
            await task
        finally:
            # Consumer walked away mid-stream: stop the turn behind it.
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

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
                Message(
                    role=Role.SYSTEM,
                    content="You are a planning assistant. Respond with JSON only.",
                ),
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

    def set_model(self, model: str) -> None:
        """Switch the active model and rebuild the LLM client."""
        attribute = {
            "openai": "openai_model",
            "anthropic": "anthropic_model",
            "ollama": "ollama_model",
            "groq": "groq_model",
            "custom": "custom_model",
        }[self.config.llm_provider.value]
        setattr(self.config, attribute, model)
        self._rebuild_llm()

    def set_provider(self, provider: str) -> None:
        """Switch the LLM provider and rebuild the LLM client."""
        from aegisx_agent.core.config import LLMProvider

        self.config.llm_provider = LLMProvider(provider)
        self._rebuild_llm()

    def _rebuild_llm(self) -> None:
        """Recreate the LLM client so config changes take effect immediately."""
        inner = create_llm_provider(self.config.get_llm_config())
        # Re-wrap so usage tracking survives provider/model switches.
        self.usage = UsageTracker(inner, self.config.data_path / USAGE_FILE)
        self.usage.run_id = self.session_id
        self.llm = self.usage  # type: ignore[assignment]
        self.agent_loop.llm = self.llm

    # === Public API for permissions ===

    def get_permission_info(self) -> dict[str, Any]:
        """Describe the active permission policy, for ``/permissions``."""
        gate = self.permission_gate
        return {
            "mode": gate.mode.value,
            "interactive": gate.interactive,
            "allowed": sorted(gate.allowed_tools),
            "denied": sorted(gate.denied_tools),
            "audit_log": str(gate.audit.path) if gate.audit.path else "(disabled)",
            "audit_errors": gate.audit.last_error,
        }

    def set_permission_mode(self, mode: str | PermissionMode) -> None:
        """Switch the permission mode at runtime and remember it."""
        self.permission_gate.set_mode(mode)
        self.config.permission_mode = self.permission_gate.mode

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

    async def _maybe_create_skill(self, task: str, response: str, trace: AgentTrace) -> None:
        """Auto-create a reusable skill when a task was complex enough.

        Async on purpose: it reuses the running event loop instead of spinning
        up a nested one, which is what previously made skill creation dead code.
        """
        if not self.skill_manager.should_create_skill(
            tool_call_count=trace.total_tool_calls,
            had_error_recovery=False,
            had_user_correction=False,
        ):
            return

        prompt = (
            f"The user asked: {task}\n"
            f"The agent completed the task using {trace.total_tool_calls} tool calls.\n"
            f"Final response: {response[:500]}\n\n"
            "Create a reusable skill for this type of task.\n"
            'Respond in JSON: {"name": "...", "description": "...", "steps": ["step1", ...]}'
        )

        try:
            resp = await self.llm.chat(
                messages=[
                    Message(
                        role=Role.SYSTEM,
                        content="You are a skill creator. Respond with JSON only.",
                    ),
                    Message(role=Role.USER, content=prompt),
                ],
                temperature=0.3,
                max_tokens=500,
            )

            content = resp.content or ""
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start < 0 or json_end <= json_start:
                self.last_skill_error = "skill creator returned no JSON object"
                return

            skill_data = json.loads(content[json_start:json_end])
            steps = skill_data.get("steps") or []
            if not steps:
                self.last_skill_error = "skill creator returned no steps"
                return

            self.skill_manager.create_skill(
                name=skill_data.get("name")
                or f"skill_{len(self.skill_manager.list_skills()) + 1}",
                description=skill_data.get("description", "Auto-generated skill"),
                steps=steps,
                tags=["auto-generated"],
            )
            self.last_skill_error = ""
        except Exception as exc:  # noqa: BLE001 - never break a chat because of skill capture
            self.last_skill_error = f"{type(exc).__name__}: {exc}"

    # === Public API for Skills ===

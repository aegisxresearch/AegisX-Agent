# 🤖 Utopia Agent

**Super-powered Agentic AI** — Support ANY LLM provider, with tool calling, RAG, memory, planning, and customizable personas.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔌 **Multi-Provider LLM** | OpenAI, Anthropic, Ollama (local), Groq, or ANY OpenAI-compatible endpoint |
| 🔧 **Tool Calling** | Web search, code execution, file ops, shell, calculator, datetime |
| 📚 **RAG** | Ingest documents, search knowledge base with ChromaDB |
| 🧠 **Memory** | Conversation history + long-term fact storage |
| 📋 **Planning** | Multi-step reasoning with ReAct pattern |
| 🎭 **Personas** | Built-in personas + custom persona creation |
| 🖥️ **Rich CLI** | Beautiful terminal interface with streaming |

## 🚀 Quick Start

### Install

```bash
pip install -e .
```

### Set up your LLM provider

```bash
# Option 1: OpenAI
export UTOPIA_OPENAI_API_KEY="sk-..."

# Option 2: Anthropic
export UTOPIA_LLM_PROVIDER=anthropic
export UTOPIA_ANTHROPIC_API_KEY="sk-ant-..."

# Option 3: Ollama (local, free!)
export UTOPIA_LLM_PROVIDER=ollama
export UTOPIA_OLLAMA_MODEL=llama3.1

# Option 4: Groq (fast inference)
export UTOPIA_LLM_PROVIDER=groq
export UTOPIA_GROQ_API_KEY="gsk_..."

# Option 5: ANY custom OpenAI-compatible endpoint
export UTOPIA_LLM_PROVIDER=custom
export UTOPIA_CUSTOM_BASE_URL="https://api.together.xyz/v1"
export UTOPIA_CUSTOM_API_KEY="your-key"
export UTOPIA_CUSTOM_MODEL="meta-llama/Llama-3-70b-chat-hf"
```

### Chat!

```bash
# Interactive chat (default)
utopia

# Use specific provider
utopia --provider ollama --model llama3.1
utopia -p openai -m gpt-4o

# Use custom endpoint
utopia -p custom --url https://api.together.xyz/v1 -k your-key -m meta-llama/Llama-3-70b-chat-hf
```

## 📖 Commands

```bash
utopia                    # Start interactive chat
utopia chat               # Same as above
utopia plan "goal"        # Plan and execute a multi-step goal
utopia ingest ./docs/     # Ingest documents into knowledge base
utopia search "query"     # Search the knowledge base
utopia personas           # List available personas
utopia tools              # List available tools
utopia config             # Show current configuration
```

## 🔌 Supported Providers

| Provider | Setup | Free? |
|----------|-------|-------|
| **OpenAI** | `UTOPIA_OPENAI_API_KEY` | ❌ (paid) |
| **Anthropic** | `UTOPIA_ANTHROPIC_API_KEY` | ❌ (paid) |
| **Ollama** | Install ollama, pull a model | ✅ (local) |
| **Groq** | `UTOPIA_GROQ_API_KEY` | ✅ (free tier) |
| **Custom** | `UTOPIA_CUSTOM_BASE_URL` | Varies |

### Custom Provider Examples

Works with ANY OpenAI-compatible API:

```bash
# Together AI
utopia -p custom -u https://api.together.xyz/v1 -k $TOGETHER_KEY -m meta-llama/Llama-3-70b-chat-hf

# OpenRouter
utopia -p custom -u https://openrouter.ai/api/v1 -k $OPENROUTER_KEY -m anthropic/claude-3.5-sonnet

# Local LM Studio
utopia -p custom -u http://localhost:1234/v1 -m local-model

# vLLM
utopia -p custom -u http://localhost:8000/v1 -m model-name

# Text Generation WebUI
utopia -p custom -u http://localhost:5000/v1 -m model-name
```

## 🎭 Personas

```bash
# List personas
utopia personas

# Use a persona
utopia --persona coder
utopia --persona researcher

# Create custom persona
# Save to ~/.utopia/personas/my_persona.txt
```

Built-in personas: `default`, `coder`, `researcher`, `analyst`, `creative`, `hacker`, `scientist`

## 🔧 Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the internet via DuckDuckGo |
| `execute_code` | Run Python code safely |
| `file_ops` | Read, write, list, search files |
| `shell` | Execute shell commands (opt-in) |
| `calculator` | Evaluate math expressions |
| `datetime` | Date/time utilities |
| `rag_search` | Search document knowledge base |

## 📚 RAG (Document Ingestion)

```bash
# Ingest a file
utopia ingest ./document.pdf

# Ingest a directory
utopia ingest ./docs/

# Search knowledge base
utopia search "what is the API rate limit?"
```

Supported formats: `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`, `.yaml`, `.toml`, `.pdf`

## ⚙️ Configuration

All settings can be configured via environment variables (prefix `UTOPIA_`):

```env
# .env file
UTOPIA_LLM_PROVIDER=custom
UTOPIA_CUSTOM_BASE_URL=https://api.together.xyz/v1
UTOPIA_CUSTOM_API_KEY=your-key
UTOPIA_CUSTOM_MODEL=meta-llama/Llama-3-70b-chat-hf
UTOPIA_TEMPERATURE=0.7
UTOPIA_MAX_ITERATIONS=15
UTOPIA_MEMORY_ENABLED=true
UTOPIA_RAG_ENABLED=true
UTOPIA_SHELL_ENABLED=false
```

## 🏗️ Architecture

```
utopia_agent/
├── core.py              # Main agent orchestrator
├── config.py            # Configuration (pydantic-settings)
├── cli.py               # Rich terminal CLI
├── llm/                 # Multi-provider LLM support
│   ├── base.py          # Base abstractions
│   ├── factory.py       # Provider factory
│   ├── openai_provider.py
│   ├── anthropic_provider.py
│   ├── ollama_provider.py
│   ├── groq_provider.py
│   └── custom_provider.py  # ANY OpenAI-compatible endpoint
├── tools/               # Tool system
│   ├── base.py          # Tool abstractions
│   ├── registry.py      # Tool registry
│   ├── web_search.py
│   ├── code_executor.py
│   ├── file_ops.py
│   ├── shell.py
│   ├── calculator.py
│   ├── datetime_tool.py
│   └── rag_search.py
├── memory/              # Memory system
│   └── store.py         # Conversation + long-term memory
├── rag/                 # RAG system
│   └── engine.py        # ChromaDB vector store
├── planning/            # Planning system
│   └── react.py         # ReAct reasoning loop
└── personas/            # Persona system
    └── loader.py        # Custom persona loader
```

## 📄 License

MIT

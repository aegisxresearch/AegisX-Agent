"""Find an LLM that is already running on this machine.

The whole point is that ``aegisx`` should start without a setup wizard when
there is a local model server available. Ollama speaks HTTP on a known port and
can list what is installed, so it is the one worth probing.
"""

from __future__ import annotations

import httpx

DEFAULT_OLLAMA_URL = "http://localhost:11434"

#: Probing at startup must not feel like a hang, so this is deliberately short.
PROBE_TIMEOUT = 0.6

#: Substrings that mark a model as embedding-only; it cannot hold a conversation.
_NON_CHAT_MARKERS = ("embed", "bge-", "e5-", "minilm", "rerank")


def ollama_reachable(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether an Ollama server answers at ``base_url``."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
    except Exception:  # noqa: BLE001 - any network failure means "not available"
        return False
    return response.status_code == 200


def installed_models(
    base_url: str = DEFAULT_OLLAMA_URL, timeout: float = PROBE_TIMEOUT
) -> list[str]:
    """Names of the models installed on the Ollama server (may be empty)."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - treated as "nothing installed"
        return []

    models: list[str] = []
    for entry in payload.get("models", []) or []:
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if name:
            models.append(name)
    return models


def chat_model(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = PROBE_TIMEOUT) -> str | None:
    """A chat-capable model that is actually installed, or ``None``.

    Picking a model that exists matters: defaulting to ``llama3.1`` when the
    user has only pulled something else produces a confusing 404 on the first
    message instead of a working session.
    """
    candidates = [
        name
        for name in installed_models(base_url, timeout)
        if not any(marker in name.lower() for marker in _NON_CHAT_MARKERS)
    ]
    if not candidates:
        return None

    # Prefer something small and fast for a first run; a 70b model on a laptop
    # is a bad first impression.
    preferred = ("llama3.2", "llama3.1", "llama3", "qwen", "mistral", "gemma", "phi")
    for prefix in preferred:
        for name in candidates:
            if name.lower().startswith(prefix):
                return name
    return candidates[0]


def detect_local_provider(base_url: str = DEFAULT_OLLAMA_URL) -> tuple[str, str] | None:
    """Return ``(base_url, model)`` for a usable local server, or ``None``."""
    if not ollama_reachable(base_url):
        return None
    model = chat_model(base_url)
    if not model:
        return None
    return base_url, model

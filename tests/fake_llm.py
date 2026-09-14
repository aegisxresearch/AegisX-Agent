"""An in-process OpenAI/Anthropic-compatible server for integration tests.

The unit tests drive a scripted ``LLMProvider`` directly, which never proves
that the agent survives a real HTTP round trip. This server speaks the actual
wire protocols (request bodies, SSE frames, 429 retries) so the agent can be
exercised end to end without a network or API key.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def openai_text_response(text: str) -> dict[str, Any]:
    """A non-streaming OpenAI chat completion carrying plain text."""
    return {
        "id": "chatcmpl-text",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def openai_tool_call_response(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """A non-streaming OpenAI chat completion requesting a tool call."""
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    }


def anthropic_tool_use_response(
    tool_use_id: str, name: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """An Anthropic message asking for a tool call."""
    return {
        "id": "msg-tool",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


def anthropic_text_response(text: str) -> dict[str, Any]:
    """An Anthropic message carrying plain text."""
    return {
        "id": "msg-text",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


def sse_text(pieces: list[str]) -> dict[str, Any]:
    """A streaming OpenAI reply that emits ``pieces`` then ``[DONE]``."""
    frames = "".join(
        "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n"
        for piece in pieces
    )
    return {"sse": frames + "data: [DONE]\n\n"}


def sse_tool_call(
    call_id: str, name: str, arguments: str, content: str | None = None
) -> dict[str, Any]:
    """A streaming OpenAI reply requesting a tool call, the way real servers do.

    The call arrives as a ``tool_calls`` delta (name, then argument chunks);
    an optional ``content`` piece precedes it, and the stream closes with a
    ``finish_reason: tool_calls`` chunk before ``[DONE]``.
    """
    frames = ""
    if content:
        frames += (
            "data: "
            + json.dumps({"choices": [{"delta": {"content": content}}]})
            + "\n\n"
        )
    frames += (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            }
        )
        + "\n\n"
    )
    frames += (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            }
        )
        + "\n\n"
    )
    frames += (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        )
        + "\n\n"
    )
    return {"sse": frames + "data: [DONE]\n\n"}


def anthropic_sse_text(pieces: list[str]) -> dict[str, Any]:
    """A streaming Anthropic reply emitting ``pieces`` as text blocks."""
    frames = ""
    for index, piece in enumerate(pieces):
        frames += (
            "data: "
            + json.dumps(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            + "\n\n"
        )
        frames += (
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": piece},
                }
            )
            + "\n\n"
        )
        frames += (
            "data: "
            + json.dumps({"type": "content_block_stop", "index": index})
            + "\n\n"
        )
    frames += (
        "data: "
        + json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            }
        )
        + "\n\n"
    )
    frames += 'data: {"type":"message_stop"}\n\n'
    return {"sse": frames}


def too_many_requests(retry_after: str = "0") -> dict[str, Any]:
    """A 429 entry; ``retry-after: 0`` keeps the retry test fast."""
    return {"status": 429, "headers": {"retry-after": retry_after}, "body": {"error": "slow down"}}


def ollama_tags(models: list[str]) -> dict[str, Any]:
    """Body of Ollama's ``GET /api/tags``, listing the installed models."""
    return {
        "models": [
            {"name": name, "model": name, "size": 1024, "details": {"family": "llama"}}
            for name in models
        ]
    }


class FakeLLMServer:
    """Serves a scripted sequence of responses over real HTTP."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.get_requests: list[str] = []
        self.get_responses: dict[str, dict[str, Any]] = {}
        self._script: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def root_url(self) -> str:
        """Origin with no path — what a local server such as Ollama serves from."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def base_url(self) -> str:
        return f"{self.root_url}/v1"

    def serve_get(self, path: str, entry: dict[str, Any]) -> None:
        """Script the response for a GET request to ``path``."""
        with self._lock:
            self.get_responses[path] = entry

    def script(self, *entries: dict[str, Any]) -> None:
        """Replace the scripted response queue."""
        with self._lock:
            self._script = list(entries)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def _get_entry(self, path: str) -> dict[str, Any]:
        with self._lock:
            entry = self.get_responses.get(path)
        return entry if entry is not None else {
            "status": 404,
            "body": {"error": f"fake server has no GET route for {path}"},
        }

    def _next_entry(self) -> dict[str, Any]:
        with self._lock:
            if not self._script:
                return {
                    "status": 500,
                    "body": {"error": "fake server script exhausted"},
                }
            return self._script.pop(0)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                """Keep pytest output clean."""
                return

            def _respond(self, entry: dict[str, Any]) -> None:
                status = entry.get("status", 200)

                if "sse" in entry:
                    body = entry["sse"]
                    content_type = "text/event-stream"
                elif "raw" in entry:
                    # Exact bytes for non-JSON payloads (HTML pages, plain text).
                    body = entry["raw"]
                    content_type = entry.get("content_type", "text/plain")
                else:
                    # A scripted entry is either an envelope (``status``/``body``)
                    # or a bare response body such as an OpenAI completion.
                    payload = entry["body"] if "body" in entry else entry
                    body = json.dumps(payload)
                    content_type = "application/json"

                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for key, value in entry.get("headers", {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                server.requests.append((self.path, payload))
                server.headers_seen.append(dict(self.headers.items()))
                self._respond(server._next_entry())

            def do_GET(self) -> None:
                server.get_requests.append(self.path)
                server.headers_seen.append(dict(self.headers.items()))
                self._respond(server._get_entry(self.path))

        return Handler

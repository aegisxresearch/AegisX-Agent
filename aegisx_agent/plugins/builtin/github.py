"""GitHub plugin — read-only repository metadata through the REST API.

Loaded explicitly:

    agent.load_plugin_module("aegisx_agent.plugins.builtin.github")

Every call is read-only (``SAFE``) and goes through the standard permission
gate like any other tool. The handler is sync on purpose: ``httpx`` runs the
request, and the registry tolerates sync handlers.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from aegisx_agent.plugins import PluginManifest, PluginPermissionPolicy, define_plugin
from aegisx_agent.tools.base import ToolRisk

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 15.0


def _token() -> str:
    """The token from the environment; an empty value means anonymous access."""
    return os.environ.get("AEGISX_GITHUB_TOKEN", "").strip()


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "aegisx-agent"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(path: str) -> dict[str, Any]:
    """One authenticated GET; errors raise so the registry reports them."""
    response = httpx.get(
        f"{API_ROOT.rstrip('/')}{path}",
        headers=_headers(),
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
    )
    if response.status_code == 404:
        raise ValueError(f"not found on GitHub: {path}")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def _repo_slug(repo: str) -> str:
    """Accept ``owner/name`` or a full https://github.com URL."""
    slug = repo.strip().rstrip("/")
    if slug.startswith("http"):
        slug = slug.split("github.com/", 1)[-1]
    if slug.count("/") != 1 or not all(slug.split("/")):
        raise ValueError(f"repo must be 'owner/name' or a GitHub URL, got: {repo!r}")
    return slug


REPO_MANIFEST = PluginManifest(
    plugin_id="github",
    version="1.0.0",
    tool_name="repo_info",
    description=(
        "Fetch GitHub repository metadata: stars, forks, language, open issues, "
        "and description. Pass 'owner/name' or a full GitHub URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Repository as 'owner/name' or a github.com URL",
            },
        },
        "required": ["repo"],
    },
    risk=ToolRisk.SAFE,
    permission=PluginPermissionPolicy(allow_in_read_only=True),
)


@define_plugin(REPO_MANIFEST)
def repo_info(repo: str) -> str:
    slug = _repo_slug(repo)
    data = _get(f"/repos/{slug}")
    license_name = (data.get("license") or {}).get("spdx_id") or "none"
    return (
        f"{data.get('full_name', slug)} — ⭐ {data.get('stargazers_count', 0)} | "
        f"forks {data.get('forks_count', 0)} | open issues {data.get('open_issues_count', 0)} | "
        f"language {data.get('language') or 'unknown'} | license {license_name} | "
        f"{(data.get('description') or 'no description').strip()}"
    )


ISSUES_MANIFEST = PluginManifest(
    plugin_id="github",
    version="1.0.0",
    tool_name="list_issues",
    description=(
        "List the most recently updated open issues of a GitHub repository. "
        "Pass 'owner/name' or a full GitHub URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Repository as 'owner/name' or a github.com URL",
            },
            "limit": {
                "type": "integer",
                "description": "How many issues to list (1-10, default 5)",
                "default": 5,
            },
        },
        "required": ["repo"],
    },
    risk=ToolRisk.SAFE,
    permission=PluginPermissionPolicy(allow_in_read_only=True),
)


@define_plugin(ISSUES_MANIFEST)
def list_issues(repo: str, limit: int = 5) -> str:
    slug = _repo_slug(repo)
    limit = max(1, min(int(limit), 10))
    payload = _get(f"/repos/{slug}/issues?state=open&per_page={limit}")
    items: list[dict[str, Any]] = payload if isinstance(payload, list) else []
    issues = [item for item in items if "pull_request" not in item]
    if not issues:
        return f"No open issues in {slug}."
    lines = [f"Open issues in {slug}:"]
    for item in issues:
        lines.append(f"  #{item.get('number')} — {item.get('title', 'untitled')}")
    return "\n".join(lines)


PLUGIN = repo_info
PLUGINS = (repo_info, list_issues)

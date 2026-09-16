"""Tool permission gate — the checkpoint between a decision and a side effect.

The agent decides to call a tool; this gate decides whether that call is allowed
to touch the system. It is deliberately fail-closed:

- the tool's own declared risk decides how strict the check is, so nothing has
  to guess from a tool's name;
- a call that needs human approval is *withheld* until someone approves it;
- when there is nobody to ask (a scheduled, unattended run), such a call is
  denied rather than allowed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from aegisx_agent.security.audit import AuditLog, redact
from aegisx_agent.tools.base import Tool, ToolRisk


class PermissionMode(str, Enum):
    """How strictly tool calls are gated."""

    ALLOW_ALL = "allow-all"  # nothing is gated, every call is still audited
    ASK = "ask"  # default: safe/caution run freely, dangerous needs approval
    READ_ONLY = "read-only"  # only safe tools run, everything else is refused


@dataclass(frozen=True)
class PermissionScopeRule:
    """One scoped allow rule: ``tool`` may run when its scope matches."""

    tool: str
    pattern: str  # fnmatch pattern against the tool's scope value


@dataclass(frozen=True)
class PermissionRequest:
    """A call that needs a decision, shown to a human before approval."""

    tool: str
    risk: ToolRisk
    summary: str
    arguments: dict[str, Any]

    def describe(self) -> str:
        """One-line description for a prompt or a log line."""
        return f"{self.tool} [{self.risk.value}] {self.summary}".strip()


@dataclass(frozen=True)
class PermissionDecision:
    """The gate's verdict on one tool call."""

    allowed: bool
    reason: str
    risk: ToolRisk
    mode: PermissionMode
    #: How the verdict was reached: policy, allowlist, denylist, user, unattended.
    decided_by: str
    #: True while the call is withheld pending human approval.
    requires_prompt: bool = False


#: Called to ask a human to approve a request. Returning False denies it.
PrompterFunc = Callable[[PermissionRequest], Awaitable[bool]]

#: Argument names used to describe a call in prompts and logs, in reading order.
_SUMMARY_KEYS = (
    "action",
    "command",
    "code",
    "expression",
    "query",
    "url",
    "path",
    "database",
    "message",
)


def summarise(arguments: dict[str, Any], limit: int = 160) -> str:
    """Build a short, secret-free description of a tool call's arguments."""
    parts: list[str] = []
    for key in _SUMMARY_KEYS:
        if key in arguments and arguments[key] not in (None, "", [], {}):
            parts.append(f"{key}={redact(arguments[key], key)}")
    if not parts:
        parts.append(json.dumps(redact(arguments), ensure_ascii=False, default=str))

    text = " ".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


class PermissionGate:
    """Decides whether a tool call may run, and records every decision."""

    def __init__(
        self,
        mode: PermissionMode | str = PermissionMode.ASK,
        allowed: Iterable[str] = (),
        denied: Iterable[str] = (),
        interactive: bool = True,
        prompter: PrompterFunc | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.mode = PermissionMode(mode)
        self.interactive = interactive
        self.prompter = prompter
        self.audit = audit if audit is not None else AuditLog(None, enabled=False)
        self._allowed = {name.strip() for name in allowed if name and name.strip()}
        self._denied = {name.strip() for name in denied if name and name.strip()}
        self._scoped: dict[str, str] = {}

    # === Scoped allow rules ===

    #: Which single argument defines a call's blast radius per tool.
    _SCOPE_KEYS = {"file_ops": "path", "editor": "path", "shell": "command", "git": "action"}

    def allow_scoped(self, tool: str, pattern: str) -> None:
        """Permit ``tool`` when its scope value matches ``pattern`` (fnmatch).

        E.g. ``allow_scoped("editor", "login-register/**")`` lets the agent
        rewrite anything under that folder without asking, while the rest of
        the tree still needs approval.
        """
        self._scoped[tool] = pattern
        self._allowed.discard(tool)

    def scoped_rules(self) -> dict[str, str]:
        return dict(self._scoped)

    def _scope_value(self, tool: str, arguments: dict[str, Any]) -> str:
        key = self._SCOPE_KEYS.get(tool)
        if not key:
            return ""
        value = arguments.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _matches_scoped(self, tool: str, arguments: dict[str, Any]) -> bool:
        import fnmatch

        pattern = self._scoped.get(tool)
        if not pattern:
            return False
        value = self._scope_value(tool, arguments)
        return bool(value) and fnmatch.fnmatch(value, pattern)

    def default_scope_for(self, tool: str, arguments: dict[str, Any]) -> str | None:
        """The sensible scoped-allow pattern for this call, if one exists.

        For file writes: a sibling folder rule (``<dir>/**``) so a whole
        feature folder can be approved at once. For shell: the program name
        with a trailing ``*`` so ``pytest -q`` approves ``pytest -x`` too.
        ``None`` when the tool has no scopeable argument.
        """
        value = self._scope_value(tool, arguments)
        if not value:
            return None
        if tool in ("file_ops", "editor"):
            parent = value.rstrip("/").rsplit("/", 1)[0]
            return f"{parent}/**" if parent else "**"
        if tool == "shell":
            program = value.split()[0] if value.split() else value
            return f"{program}*"
        if tool == "git":
            return value
        return None

    # === Policy inspection / mutation ===

    @property
    def allowed_tools(self) -> set[str]:
        return set(self._allowed)

    @property
    def denied_tools(self) -> set[str]:
        return set(self._denied)

    def set_mode(self, mode: PermissionMode | str) -> None:
        """Switch the active mode at runtime (used by ``/permissions``)."""
        self.mode = PermissionMode(mode)

    def allow(self, tool: str) -> None:
        """Always run ``tool`` without asking, overriding the mode."""
        self._denied.discard(tool)
        self._allowed.add(tool)

    def deny(self, tool: str) -> None:
        """Always refuse ``tool``, overriding the mode."""
        self._allowed.discard(tool)
        self._denied.add(tool)

    def clear(self, tool: str) -> None:
        """Drop ``tool`` from both lists so the mode decides again."""
        self._allowed.discard(tool)
        self._denied.discard(tool)

    # === Evaluation ===

    @staticmethod
    def classify(tool: Tool, arguments: dict[str, Any]) -> ToolRisk:
        """Ask the tool how risky this particular call is."""
        return tool.risk_for(arguments)

    def evaluate(
        self,
        tool: str,
        risk: ToolRisk,
        arguments: dict[str, Any],
    ) -> PermissionDecision:
        """Apply policy without doing any I/O.

        ``requires_prompt`` marks a call that is withheld until a human answers;
        :meth:`check` is what resolves it. Pure so the rules stay testable and
        so callers can preview a decision without prompting.
        """
        if tool in self._denied:
            return PermissionDecision(
                False, f"'{tool}' is on the deny list", risk, self.mode, "denylist"
            )
        if tool in self._allowed:
            return PermissionDecision(
                True, f"'{tool}' is allow-listed", risk, self.mode, "allowlist"
            )
        if self._matches_scoped(tool, arguments):
            return PermissionDecision(
                True,
                f"'{tool}' matches scoped allow '{self._scoped.get(tool, '')}'",
                risk,
                self.mode,
                "scoped-allowlist",
            )

        if self.mode is PermissionMode.ALLOW_ALL:
            return PermissionDecision(True, "mode is allow-all", risk, self.mode, "policy")

        if self.mode is PermissionMode.READ_ONLY:
            if risk is ToolRisk.SAFE:
                return PermissionDecision(
                    True, "read-only mode allows safe tools", risk, self.mode, "policy"
                )
            return PermissionDecision(
                False,
                f"read-only mode refuses {risk.value} tools",
                risk,
                self.mode,
                "policy",
            )

        # PermissionMode.ASK — only dangerous calls need a human.
        if risk is ToolRisk.DANGEROUS:
            return PermissionDecision(
                False,
                "dangerous tool requires approval",
                risk,
                self.mode,
                "policy",
                requires_prompt=True,
            )
        return PermissionDecision(
            True, f"{risk.value} tool runs without asking", risk, self.mode, "policy"
        )

    async def check(self, tool: Tool, arguments: dict[str, Any]) -> PermissionDecision:
        """Resolve one call into a final allow/deny decision, and audit it."""
        risk = self.classify(tool, arguments)
        decision = self.evaluate(tool.name, risk, arguments)

        request = PermissionRequest(
            tool=tool.name,
            risk=risk,
            summary=summarise(arguments),
            arguments=arguments,
        )
        if decision.requires_prompt:
            decision = await self._request_approval(request)

        self._audit(request, decision)
        return decision

    async def _request_approval(self, request: PermissionRequest) -> PermissionDecision:
        """Ask a human, or refuse when there is nobody to ask."""
        if not self.interactive or self.prompter is None:
            return PermissionDecision(
                False,
                (
                    "unattended run: this tool is dangerous and nobody can approve it "
                    f"here. Add '{request.tool}' to AEGISX_ALLOWED_TOOLS to permit it."
                ),
                request.risk,
                self.mode,
                "unattended",
            )

        try:
            approved = await self.prompter(request)
        except Exception as exc:  # a broken prompt must deny, never allow
            return PermissionDecision(
                False,
                f"approval prompt failed: {type(exc).__name__}: {exc}",
                request.risk,
                self.mode,
                "user",
            )

        if approved:
            return PermissionDecision(
                True, "approved by user", request.risk, self.mode, "user"
            )
        return PermissionDecision(False, "denied by user", request.risk, self.mode, "user")

    def _audit(self, request: PermissionRequest, decision: PermissionDecision) -> None:
        self.audit.record(
            "tool_call",
            tool=request.tool,
            risk=decision.risk.value,
            mode=decision.mode.value,
            allowed=decision.allowed,
            decided_by=decision.decided_by,
            reason=decision.reason,
            arguments=request.arguments,
        )

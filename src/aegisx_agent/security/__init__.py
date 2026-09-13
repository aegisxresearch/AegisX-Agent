"""Safety controls that sit between the agent's decisions and the system.

- :mod:`aegisx_agent.security.permissions` — the gate every tool call passes.
- :mod:`aegisx_agent.security.audit` — the append-only record of those decisions.
"""

from aegisx_agent.security.audit import REDACTED, AuditLog, redact
from aegisx_agent.security.permissions import (
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionRequest,
    PrompterFunc,
    summarise,
)

__all__ = [
    "AuditLog",
    "PermissionDecision",
    "PermissionGate",
    "PermissionMode",
    "PermissionRequest",
    "PrompterFunc",
    "REDACTED",
    "redact",
    "summarise",
]

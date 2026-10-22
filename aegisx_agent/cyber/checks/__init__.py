"""Check framework — the contract every individual check implements.

A "check" is one narrow question asked of a target: *are the security
headers present?*, *does this parameter reflect input unescaped?*, *is
CORS wide open?*.

Keeping each check narrow has two payoffs. Each one is unit-testable in
isolation, and a check that finds nothing is cheap to skip on the next
host — the framework can run the expensive ones only where they apply.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from aegisx_agent.cyber.models import Category, Confidence, Finding, Severity

if TYPE_CHECKING:  # avoids a runtime import cycle
    from aegisx_agent.cyber.prober import Prober, Response


@dataclass
class CheckContext:
    """Everything a check is allowed to touch.

    A check receives a prober, the findings collected so far, and the
    endpoints discovered so far. It may not reach outside this object —
    that is what keeps a check from becoming an unscoped request machine.
    """

    base_url: str
    host: str
    prober: Prober
    endpoints: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def add(self, finding: Finding) -> None:
        """Record a finding and remember it on the context."""
        self.findings.append(finding)

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    async def fetch(self, url: str, **kwargs: Any) -> Response:
        """Proceed with a scoped request (convenience for checks)."""
        return await self.prober.request(url, **kwargs)


@runtime_checkable
class Check(Protocol):
    """The protocol every check satisfies."""

    #: Stable identifier, used in reports and to select checks.
    name: str
    #: The finding category this check produces.
    category: Category
    #: How expensive the check is: ``cheap`` checks always run, ``deep``
    #: checks only run on targets that look worth them.
    cost: str

    async def run(self, context: CheckContext) -> list[Finding]:
        """Inspect the target and return whatever it found."""
        ...


def finding(
    *,
    title: str,
    severity: Severity,
    category: Category,
    confidence: Confidence = Confidence.PROBABLE,
    context: CheckContext | None = None,
    description: str = "",
    impact: str = "",
    remediation: str = "",
    cwe: str = "",
    owasp: str = "",
    cvss: float | None = None,
    references: list[str] | None = None,
    endpoint: str = "",
    evidence: list[Any] | None = None,
    source: str = "",
    tags: list[str] | None = None,
) -> Finding:
    """Build a finding, filling in target/host from the context."""
    return Finding(
        title=title,
        severity=severity,
        category=category,
        confidence=confidence,
        target=context.host if context else "",
        endpoint=endpoint or (context.base_url if context else ""),
        description=description,
        impact=impact,
        remediation=remediation,
        cwe=cwe,
        owasp=owasp,
        cvss=cvss,
        references=references or [],
        evidence=list(evidence or []),
        source=source,
        tags=tags or [],
    )


async def run_check(check: Check, context: CheckContext) -> list[Finding]:
    """Run one check, converting any failure into an empty result.

    A check that crashes must not take the scan down, and it must not
    report a false negative silently either — the error is appended to
    the context so the scan can surface it in its stats.
    """
    try:
        produced = await check.run(context)
    except Exception as exc:  # noqa: BLE001 - one broken check must not end the scan
        context.options.setdefault("errors", []).append(
            f"{check.name}: {type(exc).__name__}: {exc}"
        )
        return []
    return list(produced or [])


async def run_checks(checks: list[Check], context: CheckContext) -> list[Finding]:
    """Run every check concurrently and collect their findings."""
    batches = await asyncio.gather(
        *(run_check(check, context) for check in checks), return_exceptions=True
    )
    collected: list[Finding] = []
    for check, batch in zip(checks, batches, strict=False):
        if isinstance(batch, BaseException):
            context.options.setdefault("errors", []).append(
                f"{check.name}: {type(batch).__name__}: {batch}"
            )
            continue
        collected.extend(batch)
    return collected

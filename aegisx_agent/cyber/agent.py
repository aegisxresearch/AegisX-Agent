"""The autonomous cyber agent — plan, execute, verify, report.

Everything below the scanner is deterministic. That is deliberate: a
finding a human cannot reproduce is a finding nobody will act on. But
determinism alone is not an assessment. A good operator chooses *where
to look* based on what the target turns out to be, and throws away the
signals that do not survive scrutiny.

This module is that judgement, made explicit and inspectable:

1. **Survey.** Reconnaissance first, with no LLM involved. The model
   sees what the target actually is — its technologies, its endpoints,
   its surface — not what the operator assumed it was.
2. **Plan.** The model chooses *which lines of attack to run*, within a
   catalogue of capabilities the engine already implements. The model
   cannot invent a technique; it can only choose from the menu and
   describe the intent. The menu maps one-to-one onto check
   implementations, so a plan is always executable.
3. **Execute.** The swarm runs the chosen lines concurrently, each with
   the capability grant the plan asked for. The scope guard still
   refuses out-of-scope traffic; a plan cannot widen the scope.
4. **Escalate.** The findings of the first wave are fed back to the
   model, which may plan a second, narrower wave that follows up where
   something was actually found. It may also decline: a second sweep
   that repeats the first is noise, not coverage.
5. **Verify.** The model reviews the findings and marks the ones it
   believes are false positives. A dismissal needs a reason, and the
   reason is kept. A model that dismisses everything gets ignored —
   the verification is bounded by a review budget and a retention floor.
6. **Report.** The survivors are rendered as SARIF, Markdown, and HTML.

The verification stage is the part that makes this an agent rather than
a scanner with a language model stapled on. A scanner that reports
every ``500`` as an information-disclosure finding is a scanner nobody
reads. Here the model has to argue for a dismissal, and the argument
is recorded next to the finding.

The whole pipeline is *bounded* in ways an operator can see: a request
budget, a deadline, a plan token budget, a wave count, a review budget,
and a cap on how much of the finding set the model is allowed to touch.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    ScanResult,
    ScanStats,
    Severity,
)
from aegisx_agent.cyber.prober import Prober
from aegisx_agent.cyber.recon import Crawler, Surface, fingerprint
from aegisx_agent.cyber.scope import ScopeGuard, ScopePolicy, scope_from_targets
from aegisx_agent.cyber.store import FindingStore
from aegisx_agent.cyber.swarm import (
    CAPABILITIES,
    LineOfAttack,
    SwarmPlan,
    SwarmResult,
    SwarmRunner,
    ToolGrant,
    default_lines,
)

# --------------------------------------------------------------------------
# The capability catalogue
# --------------------------------------------------------------------------

#: What each capability means, and which finding categories it produces.
#: The model plans against this catalogue; the engine dispatches against
#: the same one. A capability the engine does not implement is not in
#: the catalogue, so a plan can never name a technique that does not
#: exist.
CAPABILITY_CATALOGUE: dict[str, tuple[str, tuple[Category, ...]]] = {
    "crawl": (
        "Map the attack surface: follow links, read robots.txt and sitemaps, "
        "and list every reachable endpoint.",
        (Category.RECON, Category.EXPOSURE),
    ),
    "fingerprint": (
        "Identify the server, framework, and library versions in use so later "
        "checks know what the target is made of.",
        (Category.RECON,),
    ),
    "port-scan": (
        "Probe a bounded list of common TCP ports to find services that are "
        "reachable from outside.",
        (Category.RECON,),
    ),
    "subdomain-enum": (
        "Resolve common subdomain names for the target's domain and report the "
        "ones that exist but sit outside the authorised scope.",
        (Category.RECON,),
    ),
    "inject": (
        "Test for SQL injection, cross-site scripting, template injection, and "
        "path traversal using differential payloads that prove the flaw "
        "instead of assuming it.",
        (Category.INJECTION,),
    ),
    "ssrf": (
        "Test whether the server fetches URLs the client supplies: loopback, "
        "reserved-TLD, file-scheme, and cloud-metadata probes that prove the "
        "server-side request without touching a third party.",
        (Category.INJECTION,),
    ),
    "xxe": (
        "Test whether the XML parser expands entities, using an internal "
        "marker and an external entity that points at a reserved host.",
        (Category.INJECTION,),
    ),
    "command-injection": (
        "Test whether user input reaches a shell, by echoing a marker and — "
        "only where the echo is filtered — by timing a sleep.",
        (Category.INJECTION,),
    ),
    "nosql": (
        "Test whether a lookup parameter is treated as a query operator "
        "rather than as a value, which turns authentication into a tautology.",
        (Category.INJECTION,),
    ),
    "token": (
        "Audit the tokens the service issues: unsigned JWTs, tokens that never "
        "expire, authorisation decisions carried in the client, credentials "
        "passed in URLs, and low-entropy session identifiers.",
        (Category.AUTHZ, Category.CONFIG),
    ),
    "protocol": (
        "Test the HTTP layer itself: response splitting through unsanitised "
        "headers, and host header injection into generated links.",
        (Category.INJECTION,),
    ),
    "api-surface": (
        "Look for administration panels, debug endpoints, and GraphQL schemas "
        "that are reachable without authentication.",
        (Category.EXPOSURE,),
    ),
    "dns": (
        "Check whether a CNAME points at a hosting provider where the target "
        "no longer exists, which makes the name claimable.",
        (Category.RECON,),
    ),
    "cloud": (
        "When the target is itself a storage host, check whether its "
        "container lists itself to anyone.",
        (Category.EXPOSURE,),
    ),
    "authz": (
        "Test access control: object identifiers that are not bound to the "
        "caller, dangerous HTTP methods, and open redirects.",
        (Category.AUTHZ, Category.CONFIG),
    ),
    "exposure": (
        "Look for exposed sensitive files, directory listings, verbose error "
        "pages, and permissive CORS policy.",
        (Category.EXPOSURE,),
    ),
    "headers": (
        "Audit the browser-facing security headers and the attributes on the "
        "cookies the server sets.",
        (Category.HEADERS, Category.COOKIE),
    ),
    "tls": (
        "Audit the transport configuration: certificate validity, protocol "
        "versions, cipher strength, and HSTS.",
        (Category.TRANSPORT,),
    ),
    "cors": (
        "Test whether the CORS policy lets an arbitrary origin read authenticated responses.",
        (Category.CORS,),
    ),
    "secrets": (
        "Search the source tree for committed credentials and API keys.",
        (Category.SECRETS,),
    ),
    "static-analysis": (
        "Analyse the source tree for dangerous call sites, weak cryptography, "
        "and framework misconfiguration.",
        (Category.CODE, Category.CRYPTO, Category.CONFIG),
    ),
}


class AgentPhase(str, Enum):
    """The stages of an autonomous run, in order."""

    SURVEY = "survey"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    REPORT = "report"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class CyberAgentConfig:
    """Everything an operator can tune about the autonomous agent."""

    #: Target URLs. Only these hosts (and the hosts in ``extra_hosts``)
    #: may be contacted.
    targets: list[str] = field(default_factory=list)
    extra_hosts: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    #: The request budget for the whole run.
    max_requests: int = 2000
    requests_per_second: float = 10.0
    max_concurrency: int = 6
    #: Wall-clock ceiling for the run.
    timeout_seconds: float = 1800.0
    #: Crawl depth and page budget for the survey stage.
    max_crawl_pages: int = 150
    max_crawl_depth: int = 3
    #: How many lines of attack the plan may contain.
    max_lines: int = 6
    #: How many waves one run may execute. Every wave after the first is
    #: planned from what the previous one found, so a confirmed flaw can
    #: be followed up where it appeared instead of being left as a note.
    max_waves: int = 2
    #: How many lines an escalation wave may contain.
    escalation_max_lines: int = 3
    #: Token budget for the escalation call.
    escalation_max_tokens: int = 900
    #: How many lines the plan may run *concurrently*.
    max_parallel_lines: int = 4
    #: Token budget for the planning call.
    plan_max_tokens: int = 1200
    #: Token budget for the verification call.
    verify_max_tokens: int = 2000
    #: Temperature for the planning call. Low: a plan should be reproducible.
    plan_temperature: float = 0.2
    #: Temperature for the verification call. A dismissal is a judgement
    #: call, so this is allowed to be a little more exploratory.
    verify_temperature: float = 0.3
    #: How many findings the model may review in one pass.
    max_review_findings: int = 60
    #: The model may not dismiss findings below this severity, no matter
    #: how confident it is. A low-severity observation is cheap to keep.
    protected_severity: Severity = Severity.LOW
    #: The model may not dismiss more than this fraction of the findings
    #: it reviews. A model that dismisses half the report has stopped
    #: reviewing and started erasing.
    max_dismissal_rate: float = 0.5
    #: Skip the verification stage entirely.
    skip_verification: bool = False
    #: Allow the plan to include a source-tree line of attack.
    allow_source_analysis: bool = False
    #: The source tree to analyse when ``allow_source_analysis`` is set.
    source_path: str = ""

    def to_policy(self) -> ScopePolicy:
        """Build the scope policy this configuration describes."""
        return scope_from_targets(
            self.targets,
            extra_hosts=self.extra_hosts,
            allowed_paths=self.allowed_paths,
            max_requests=self.max_requests,
            requests_per_second=self.requests_per_second,
            max_concurrency=self.max_concurrency,
            timeout_seconds=self.timeout_seconds,
        )


# --------------------------------------------------------------------------
# The survey — what the model is allowed to see
# --------------------------------------------------------------------------


@dataclass
class Survey:
    """What reconnaissance learned, in the shape the model receives it."""

    target: str
    base_url: str
    host: str
    technologies: dict[str, str] = field(default_factory=dict)
    endpoints: list[str] = field(default_factory=list)
    status: int | None = None
    title: str = ""
    server: str = ""
    robots_rules: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Render the survey for the planning prompt."""
        tech = (
            ", ".join(f"{name} {version}".strip() for name, version in self.technologies.items())
            or "unknown"
        )
        endpoints = "\n".join(f"  - {endpoint}" for endpoint in self.endpoints[:60]) or "  (none)"
        robots = "\n".join(f"  - {rule}" for rule in self.robots_rules[:20]) or "  (none)"
        notes = "\n".join(f"  - {note}" for note in self.notes[:20]) or "  (none)"
        return (
            f"Target: {self.target}\n"
            f"Base URL: {self.base_url}\n"
            f"HTTP status: {self.status if self.status is not None else 'no response'}\n"
            f"Server: {self.server or 'unknown'}\n"
            f"Page title: {self.title or '(none)'}\n"
            f"Technologies: {tech}\n"
            f"Endpoints discovered ({len(self.endpoints)}):\n{endpoints}\n"
            f"robots.txt rules:\n{robots}\n"
            f"Other notes:\n{notes}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "base_url": self.base_url,
            "host": self.host,
            "technologies": dict(self.technologies),
            "endpoints": list(self.endpoints),
            "status": self.status,
            "title": self.title,
            "server": self.server,
            "robots_rules": list(self.robots_rules),
            "sitemap_urls": list(self.sitemap_urls),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass
class PlanLine:
    """One line of attack the model asked for."""

    name: str
    intent: str
    capabilities: tuple[str, ...]
    focus: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intent": self.intent,
            "capabilities": list(self.capabilities),
            "focus": self.focus,
        }


@dataclass
class Plan:
    """The model's assessment of what to attack and how."""

    lines: list[PlanLine] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Set when the plan had to be replaced by the default plan.
    fallback: bool = False
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "hypotheses": list(self.hypotheses),
            "notes": list(self.notes),
            "fallback": self.fallback,
        }

    def to_swarm_plan(self, base_url: str, target: str, max_concurrency: int) -> SwarmPlan:
        """Convert the plan into a swarm plan the engine can run.

        Every requested capability is checked against the catalogue. A
        capability the engine cannot execute is dropped, and a line that
        ends up with no capabilities at all is dropped with it — an empty
        line of attack is not a line of attack.
        """
        swarm_plan = SwarmPlan(
            target=target,
            base_url=base_url,
            max_concurrency=max_concurrency,
        )
        for line in self.lines:
            capabilities = frozenset(
                capability for capability in line.capabilities if capability in CAPABILITIES
            )
            if not capabilities:
                continue
            categories: list[Category] = []
            for capability in sorted(capabilities):
                for category in CAPABILITY_CATALOGUE[capability][1]:
                    if category not in categories:
                        categories.append(category)
            swarm_plan.add_line(
                LineOfAttack(
                    name=line.name,
                    brief=line.intent,
                    grant=ToolGrant(capabilities),
                    categories=tuple(categories),
                )
            )
        return swarm_plan


# --------------------------------------------------------------------------
# The verification
# --------------------------------------------------------------------------


@dataclass
class Dismissal:
    """A finding the model rejected, and the reason it gave."""

    fingerprint: str
    title: str
    reason: str
    severity: Severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "title": self.title,
            "reason": self.reason,
            "severity": self.severity.value,
        }


@dataclass
class Wave:
    """One round of lines of attack, and what it was planned from."""

    index: int
    plan: Plan
    lines_run: int = 0
    findings_total: int = 0
    new_findings: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "plan": self.plan.to_dict(),
            "lines_run": self.lines_run,
            "findings_total": self.findings_total,
            "new_findings": self.new_findings,
            "notes": list(self.notes),
            "errors": list(self.errors),
        }


@dataclass
class Verification:
    """The outcome of the review stage."""

    reviewed: int = 0
    dismissed: list[Dismissal] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Set when the review was skipped or the model failed to answer.
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed": self.reviewed,
            "dismissed": [item.to_dict() for item in self.dismissed],
            "kept": list(self.kept),
            "notes": list(self.notes),
            "skipped": self.skipped,
        }


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


@dataclass
class AgentRun:
    """Everything one autonomous run produced."""

    target: str
    scope: str
    survey: Survey
    plan: Plan
    findings: list[Finding] = field(default_factory=list)
    verification: Verification = field(default_factory=Verification)
    waves: list[Wave] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    report_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "scope": self.scope,
            "survey": self.survey.to_dict(),
            "plan": self.plan.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "verification": self.verification.to_dict(),
            "waves": [wave.to_dict() for wave in self.waves],
            "stats": self.stats.to_dict(),
            "duration_seconds": self.duration_seconds,
            "errors": list(self.errors),
            "report_files": dict(self.report_files),
        }

    def to_scan_result(self) -> ScanResult:
        """Render the run as a scan result, for the reporters."""
        result = ScanResult(target=self.target, scope=self.scope)
        result.findings = list(self.findings)
        result.stats = self.stats
        result.finalize()
        return result


# --------------------------------------------------------------------------
# The prompts
# --------------------------------------------------------------------------

PLANNING_SYSTEM_PROMPT = """\
You are the planner of an authorised penetration test.

You have surveyed the target. Now you choose which lines of attack to run.

Rules:
- Choose only from the capability list. Each capability maps to a check \
implementation that already exists. You cannot invent techniques.
- Every line needs a name, an intent, and at least one capability.
- Prefer fewer, better-targeted lines. A line that targets something \
specific to this target beats a generic sweep.
- The `focus` field says what the line should look at. Be concrete: name \
endpoints, parameters, or technologies you actually saw.
- List your working hypotheses about where the weaknesses are.

Respond with JSON only, in exactly this shape:
{
  "hypotheses": ["what you expect to find and why"],
  "lines": [
    {
      "name": "short-kebab-case-name",
      "intent": "what this line of attack does",
      "capabilities": ["crawl", "inject"],
      "focus": "the specific endpoints or behaviours to examine"
    }
  ]
}
"""

VERIFICATION_SYSTEM_PROMPT = """\
You are the reviewer of an authorised penetration test's findings.

You are given findings that a deterministic scanner produced. For each one, \
decide whether it is a real, reportable problem on this target or a false \
positive.

Rules:
- A dismissal needs a reason. "Looks wrong" is not a reason.
- Dismiss only what the evidence does not support. A weak finding that is \
merely uninteresting is not a false positive — keep it and let the \
severity speak.
- Do not invent new findings. You are reviewing, not extending.
- Do not raise severities. Your job is to remove noise, not to add noise.
- If a finding is real but its evidence is thin, keep it.

Respond with JSON only, in exactly this shape:
{
  "verdicts": [
    {
      "fingerprint": "the fingerprint given to you",
      "verdict": "real" | "false-positive",
      "reason": "one sentence, required when the verdict is false-positive"
    }
  ]
}
"""


ESCALATION_SYSTEM_PROMPT = """\
You are the planner of the second wave of an authorised penetration test.

The first wave has run. You are given what it found. Decide whether any \
follow-up line of attack is worth running, and if so which one.

Rules:
- Escalation is not a second sweep. A line earns its place only when a \
first-wave finding makes it specifically worth running: an exposed admin \
path, a parameter that answered to a payload, a token with a weak claim.
- If the findings do not justify a follow-up, return an empty \
"lines" list. That is a real answer, not a failure.
- Choose only from the capability list, and do not repeat a capability \
combination that has already run against this target unless the new \
endpoint is the point.
- The `focus` field must name the endpoint, parameter, or finding the \
line follows up on.

Respond with JSON only, in exactly this shape:
{
  "hypotheses": ["what the first wave suggests and why"],
  "lines": [
    {
      "name": "short-kebab-case-name",
      "intent": "what this line follows up on",
      "capabilities": ["authz"],
      "focus": "the endpoint or finding that motivated it"
    }
  ]
}
"""


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class CyberAgent:
    """The autonomous assessment loop.

    The agent is given a language model (a callable that takes a system
    prompt and a user prompt and returns text) rather than a provider
    object, so the same loop can be driven by any provider, by a
    recorded fixture in a test, or by a human typing the answer.
    """

    def __init__(
        self,
        config: CyberAgentConfig,
        *,
        model: Any = None,
        on_progress: Any = None,
    ) -> None:
        self.config = config
        #: A callable ``(system_prompt, user_prompt, max_tokens, temperature)``
        #: returning a string. ``None`` means "no model" — the agent then
        #: falls back to the default plan and skips verification, which
        #: makes the deterministic path testable on its own.
        self.model = model
        self._on_progress = on_progress
        self.store = FindingStore()

    # === Progress ===================================================== #

    def _emit(self, phase: str, message: str) -> None:
        """Notify the progress listener, if there is one."""
        if self._on_progress is None:
            return
        try:
            self._on_progress(phase, message)
        except Exception:  # noqa: BLE001 - a bad listener must not stop a scan
            pass

    # === The run ====================================================== #

    async def run(self) -> AgentRun:
        """Run the full autonomous assessment."""
        started = time.monotonic()
        target = ", ".join(self.config.targets)
        policy = self.config.to_policy()
        guard = ScopeGuard(policy)

        survey = await self._survey(guard)
        plan = await self._plan(survey)
        swarm_result = await self._execute(guard, plan, survey)
        waves = [self._record_wave(1, plan, swarm_result, before=0)]

        # Second and later waves: the plan for each one comes from what the
        # previous wave found. A confirmed flaw is where the next question
        # is, and a run that stops at the first wave leaves it unasked.
        while len(waves) < max(1, self.config.max_waves):
            if not self.store.all():
                break
            if guard.requests_made >= self.config.max_requests:
                waves[-1].notes.append(
                    "No escalation: the request budget was exhausted by the previous wave."
                )
                break

            escalation = await self._escalate(survey, waves)
            if not escalation.lines:
                waves[-1].notes.extend(escalation.notes)
                break

            before = len(self.store.findings)
            wave_result = await self._execute(guard, escalation, survey)
            waves.append(
                self._record_wave(len(waves) + 1, escalation, wave_result, before=before)
            )
            swarm_result = wave_result

        verification = Verification()
        if not self.config.skip_verification:
            verification = await self._verify(swarm_result.findings)
        else:
            verification.skipped = True
            verification.notes.append("Verification was skipped by configuration.")

        dismissed = {item.fingerprint for item in verification.dismissed}
        kept = [item for item in swarm_result.findings if item.fingerprint not in dismissed]
        for item in verification.dismissed:
            kept.append(self._dismissal_finding(item))

        store = self.store
        store.extend(kept)

        result = ScanResult(target=target, scope=self._scope_description(policy))
        result.findings = store.all()
        result.stats = swarm_result.stats
        result.stats.requests_sent = guard.requests_made
        result.stats.findings_total = len(result.findings)
        result.stats.duration_seconds = round(time.monotonic() - started, 2)
        result.finalize()

        run = AgentRun(
            target=target,
            scope=result.scope,
            survey=survey,
            plan=plan,
            findings=result.findings,
            verification=verification,
            waves=waves,
            stats=result.stats,
            duration_seconds=result.stats.duration_seconds,
        )
        self._emit("report", f"{len(result.findings)} findings after verification")
        return run

    # === Stage 1: survey ============================================== #

    async def _survey(self, guard: ScopeGuard) -> Survey:
        """Map the target without asking the model anything.

        The model never sees a target it has not been given, and it
        never gets to widen one: the survey runs entirely inside the
        scope guard.
        """
        if not self.config.targets:
            raise ValueError("no targets were supplied")

        base_url = self.config.targets[0]
        if "://" not in base_url:
            base_url = f"https://{base_url}"

        survey = Survey(target=", ".join(self.config.targets), base_url=base_url, host="")

        async with Prober(guard) as prober:
            response = await prober.request(base_url)
            survey.status = response.status or None
            survey.server = response.header("server", "")
            survey.title = _extract_title(response.body)
            survey.technologies = fingerprint(response) if not response.error else {}
            survey.host = response.url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]

            crawler = Crawler(
                prober,
                guard,
                max_pages=self.config.max_crawl_pages,
                max_depth=self.config.max_crawl_depth,
            )
            surface: Surface = await crawler.crawl(base_url)
            survey.endpoints = list(surface.endpoints)
            survey.robots_rules = list(surface.robots_rules)
            survey.sitemap_urls = list(surface.sitemap_urls)
            if surface.technologies:
                survey.technologies = dict(surface.technologies)
            survey.notes.append(f"{len(surface.endpoints)} endpoints crawled")
            if surface.open_ports:
                survey.notes.append(f"open ports: {', '.join(str(p) for p in surface.open_ports)}")
            if surface.subdomains:
                survey.notes.append(f"subdomains: {', '.join(surface.subdomains[:20])}")

        self._emit("survey", f"Surveyed {base_url}: {len(survey.endpoints)} endpoints")
        return survey

    # === Stage 2: plan ================================================= #

    async def _plan(self, survey: Survey) -> Plan:
        """Ask the model which lines of attack to run.

        A model that cannot be reached, or that answers with something
        other than JSON, gets the default plan instead of an empty
        assessment. The default plan is not a consolation prize — it is
        the plan the engine would have run anyway.
        """
        if self.model is None:
            plan = self._default_plan(survey)
            plan.fallback = True
            plan.notes.append("No model was supplied; the default plan was used.")
            self._emit("plan", "Default plan (no model)")
            return plan

        catalogue = "\n".join(
            f"- {name}: {description}"
            for name, (description, _categories) in sorted(CAPABILITY_CATALOGUE.items())
        )
        prompt = (
            f"Available capabilities:\n{catalogue}\n\n"
            f"Survey of the target:\n{survey.to_prompt()}\n"
            "Produce the plan as JSON."
        )

        try:
            raw = await self._call_model(
                PLANNING_SYSTEM_PROMPT,
                prompt,
                max_tokens=self.config.plan_max_tokens,
                temperature=self.config.plan_temperature,
            )
        except Exception as exc:  # noqa: BLE001 - a model failure must not end the run
            plan = self._default_plan(survey)
            plan.fallback = True
            plan.notes.append(f"The planner failed: {type(exc).__name__}: {exc}")
            self._emit("plan", f"Planner failed, using default plan: {exc}")
            return plan

        plan = self._parse_plan(raw, survey)
        if not plan.lines:
            plan = self._default_plan(survey)
            plan.fallback = True
            plan.notes.append("The planner produced no usable lines; the default plan was used.")
        self._emit("plan", f"Planned {len(plan.lines)} lines of attack")
        return plan

    def _parse_plan(self, raw: str, survey: Survey, *, limit: int | None = None) -> Plan:
        """Read the model's plan out of its answer.

        The parser is forgiving about shape and strict about substance:
        a line with a capability the engine cannot run is dropped, and
        the plan never exceeds the configured line budget.
        """
        plan = Plan(raw=raw[:4000])
        payload = _extract_json(raw)
        if not isinstance(payload, dict):
            plan.notes.append("The planner's answer was not a JSON object.")
            return plan

        hypotheses = payload.get("hypotheses")
        if isinstance(hypotheses, list):
            plan.hypotheses = [str(item)[:300] for item in hypotheses if isinstance(item, str)][:10]

        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list):
            plan.notes.append("The planner's answer had no 'lines' list.")
            return plan

        for entry in raw_lines[: (limit or self.config.max_lines)]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            intent = str(entry.get("intent", "")).strip()
            if not intent:
                continue
            requested = entry.get("capabilities")
            if not isinstance(requested, list):
                continue
            capabilities = tuple(
                sorted(
                    {
                        str(capability).strip()
                        for capability in requested
                        if isinstance(capability, str) and str(capability).strip() in CAPABILITIES
                    }
                )
            )
            if not capabilities:
                plan.notes.append(f"Line '{name}' named no runnable capability and was dropped.")
                continue
            plan.lines.append(
                PlanLine(
                    name=name[:60],
                    intent=intent[:600],
                    capabilities=capabilities,
                    focus=str(entry.get("focus", ""))[:600],
                )
            )
        return plan

    def _default_plan(self, survey: Survey) -> Plan:
        """The plan used when the model is unavailable or unhelpful."""
        plan = Plan()
        for line in default_lines(survey.base_url):
            plan.lines.append(
                PlanLine(
                    name=line.name,
                    intent=line.brief,
                    capabilities=tuple(sorted(line.grant.capabilities)),
                )
            )
        return plan

    # === Stage 3: execute ============================================= #

    async def _execute(self, guard: ScopeGuard, plan: Plan, survey: Survey) -> SwarmResult:
        """Run the planned lines of attack."""
        swarm_plan = plan.to_swarm_plan(
            survey.base_url,
            survey.target,
            self.config.max_parallel_lines,
        )
        if not swarm_plan.lines:
            # The plan named nothing runnable. Fall back to the default
            # lines so the run still produces an assessment.
            fallback = self._default_plan(survey)
            swarm_plan = fallback.to_swarm_plan(
                survey.base_url, survey.target, self.config.max_parallel_lines
            )
            plan.notes.append("No runnable lines remained; the default plan was executed.")

        runner = SwarmRunner(swarm_plan, guard=guard, store=self.store)
        self._emit("execute", f"Running {len(swarm_plan.lines)} lines of attack")
        result = await runner.run()
        for line in result.lines:
            if line.error:
                self._emit("execute", f"Line '{line.name}' failed: {line.error}")
        return result

    # === Stage 3b: escalate =========================================== #

    async def _escalate(self, survey: Survey, waves: list[Wave]) -> Plan:
        """Plan the next wave from the findings the previous ones produced.

        The model is given the findings and the lines already run. It may
        answer with no lines at all, and that is treated as the correct
        answer rather than a failure — a second wave that only repeats the
        first is how a scanner teaches its reader to skip the report.
        """
        findings = self.store.all()[: self.config.max_review_findings]
        if not findings:
            return Plan(notes=["No escalation: the previous wave found nothing to follow up."])
        if self.model is None:
            return Plan(notes=["No escalation: no model was supplied."])

        catalogue = "\n".join(
            f"- {name}: {description}"
            for name, (description, _categories) in sorted(CAPABILITY_CATALOGUE.items())
        )
        finding_lines = "\n".join(
            f"- [{finding.severity.value}/{finding.confidence.value}] "
            f"{finding.title} at {finding.endpoint or survey.base_url}"
            for finding in findings
        )
        already_run = ", ".join(
            f"{line.name} ({', '.join(line.capabilities)})"
            for wave in waves
            for line in wave.plan.lines
        )
        prompt = (
            f"Available capabilities:\n{catalogue}\n\n"
            f"Survey of the target:\n{survey.to_prompt()}\n"
            f"Findings from the previous wave ({len(findings)}):\n{finding_lines}\n\n"
            f"Lines of attack already run: {already_run or '(none)'}\n"
            "Produce the follow-up plan as JSON, or an empty 'lines' list."
        )

        try:
            raw = await self._call_model(
                ESCALATION_SYSTEM_PROMPT,
                prompt,
                max_tokens=self.config.escalation_max_tokens,
                temperature=self.config.plan_temperature,
            )
        except Exception as exc:  # noqa: BLE001 - a failed escalation ends the run cleanly
            return Plan(notes=[f"The escalation planner failed: {type(exc).__name__}: {exc}"])

        plan = self._parse_plan(raw, survey, limit=self.config.escalation_max_lines)
        seen = {
            line.name
            for wave in waves
            for line in wave.plan.lines
        }
        repeated = [line.name for line in plan.lines if line.name in seen]
        plan.lines = [line for line in plan.lines if line.name not in seen]
        for name in repeated:
            plan.notes.append(f"Escalation line '{name}' repeated an earlier line and was dropped.")

        if plan.lines:
            self._emit(
                "escalate",
                f"Wave {len(waves) + 1}: {len(plan.lines)} follow-up lines of attack",
            )
        else:
            plan.notes.append(
                "The escalation planner proposed no follow-up lines; the run ended "
                "after the previous wave."
            )
        return plan

    def _record_wave(
        self, index: int, plan: Plan, result: SwarmResult, *, before: int
    ) -> Wave:
        """Summarise one wave for the run record."""
        return Wave(
            index=index,
            plan=plan,
            lines_run=len(result.lines),
            findings_total=len(result.findings),
            new_findings=max(0, len(self.store.findings) - before),
            errors=[line.error for line in result.lines if line.error],
        )

    # === Stage 4: verify ============================================== #

    async def _verify(self, findings: list[Finding]) -> Verification:
        """Let the model dismiss findings it believes are false positives.

        The review is bounded twice over. The model may not dismiss
        findings at or below the protected severity, and it may not
        dismiss more than a set fraction of what it reviewed. Either
        limit being hit ends the review and the rest of the findings
        are kept.
        """
        verification = Verification()
        if self.model is None:
            verification.skipped = True
            verification.notes.append("No model was supplied; no finding was dismissed.")
            return verification

        reviewable = [
            finding
            for finding in findings
            if finding.severity.weight > self.config.protected_severity.weight
        ]
        if not reviewable:
            verification.notes.append("Nothing was above the protected severity.")
            verification.skipped = True
            return verification

        reviewable = reviewable[: self.config.max_review_findings]
        verification.reviewed = len(reviewable)

        prompt = self._verification_prompt(reviewable)
        try:
            raw = await self._call_model(
                VERIFICATION_SYSTEM_PROMPT,
                prompt,
                max_tokens=self.config.verify_max_tokens,
                temperature=self.config.verify_temperature,
            )
        except Exception as exc:  # noqa: BLE001 - a model failure keeps every finding
            verification.notes.append(f"The reviewer failed: {type(exc).__name__}: {exc}")
            verification.skipped = True
            return verification

        payload = _extract_json(raw)
        verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
        if not isinstance(verdicts, list):
            verification.notes.append("The reviewer's answer was not a JSON object.")
            verification.skipped = True
            return verification

        by_fingerprint = {finding.fingerprint: finding for finding in reviewable}
        dismissed_count = 0
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            fingerprint = str(verdict.get("fingerprint", "")).strip()
            if fingerprint not in by_fingerprint:
                continue
            if str(verdict.get("verdict", "")).strip().lower() != "false-positive":
                verification.kept.append(fingerprint)
                continue
            reason = str(verdict.get("reason", "")).strip()
            if not reason:
                dismissed_title = by_fingerprint[fingerprint].title
                verification.notes.append(
                    f"A dismissal of '{dismissed_title}' was ignored: no reason given."
                )
                verification.kept.append(fingerprint)
                continue
            finding = by_fingerprint[fingerprint]
            verification.dismissed.append(
                Dismissal(
                    fingerprint=fingerprint,
                    title=finding.title,
                    reason=reason[:600],
                    severity=finding.severity,
                )
            )
            dismissed_count += 1

        # The retention floor: a review that removes most of the report is
        # not a review.
        if verification.reviewed and dismissed_count / verification.reviewed > (
            self.config.max_dismissal_rate
        ):
            verification.notes.append(
                f"The review dismissed {dismissed_count} of {verification.reviewed} findings, "
                f"above the {self.config.max_dismissal_rate:.0%} limit. The dismissals were "
                "reverted and every finding was kept."
            )
            verification.dismissed.clear()
            verification.kept = [finding.fingerprint for finding in reviewable]

        verification.notes.append(
            f"{len(verification.dismissed)} dismissed, {len(verification.kept)} kept of "
            f"{verification.reviewed} reviewed."
        )
        self._emit(
            "verify",
            f"Reviewed {verification.reviewed} findings, dismissed {len(verification.dismissed)}",
        )
        return verification

    def _verification_prompt(self, findings: list[Finding]) -> str:
        """Render the findings for the reviewer."""
        payload = [
            {
                "fingerprint": finding.fingerprint,
                "title": finding.title,
                "severity": finding.severity.value,
                "confidence": finding.confidence.value,
                "category": finding.category.value,
                "endpoint": finding.endpoint,
                "description": finding.description[:400],
                "evidence": [item.summary[:200] for item in finding.evidence[:3]],
            }
            for finding in findings
        ]
        return "Review these findings. Respond with a 'verdicts' list.\n\n" + json.dumps(
            payload, indent=2, ensure_ascii=False
        )

    def _dismissal_finding(self, dismissal: Dismissal) -> Finding:
        """Record a dismissal as a finding, so the report stays honest.

        The dismissed finding is kept at info severity with the reason
        attached. Hiding it entirely would make the report look cleaner
        than the assessment was.
        """
        return Finding(
            title=f"Dismissed: {dismissal.title}",
            severity=Severity.INFO,
            category=Category.INFO,
            confidence=Confidence.TENTATIVE,
            target="",
            description=dismissal.reason,
            impact=(
                "The reviewer judged this finding to be a false positive. It is "
                "listed here so the dismissal is visible and can be challenged."
            ),
            remediation="No action required unless you disagree with the reviewer's reasoning.",
            source="agent-verification",
            tags=["dismissed", "false-positive"],
            evidence=[
                Evidence(
                    kind=EvidenceKind.MATCH,
                    summary=f"Dismissed during automated verification: {dismissal.reason}",
                )
            ],
        )

    # === The model ==================================================== #

    async def _call_model(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call the model and return its text.

        The model may be a callable taking the two prompts, or an object
        with a ``chat`` coroutine — the second shape is what an AegisX
        provider looks like, and accepting it here means the cyber
        agent can be driven by the same provider as the rest of the
        project without an adapter.
        """
        model = self.model
        if model is None:
            raise RuntimeError("no model was supplied")

        chat = getattr(model, "chat", None)
        if chat is not None:
            from aegisx_agent.llm.base import Message, Role

            response = await chat(
                messages=[
                    Message(role=Role.SYSTEM, content=system_prompt),
                    Message(role=Role.USER, content=user_prompt),
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.content or ""

        outcome = model(system_prompt, user_prompt, max_tokens, temperature)
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        return str(outcome)

    @staticmethod
    def _scope_description(policy: ScopePolicy) -> str:
        """A human-readable description of what was authorised."""
        hosts = ", ".join(policy.allowed_hosts)
        if policy.allowed_paths:
            return f"{hosts} (paths: {', '.join(policy.allowed_paths)})"
        return hosts


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _extract_json(raw: str) -> Any:
    """Pull the first JSON object out of a model's answer.

    Models wrap JSON in prose, in a code fence, or both. This finds the
    outermost brace-balanced object and parses it, returning ``None``
    when there is no JSON to be had.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_title(body: str) -> str:
    """The ``<title>`` of an HTML page, if it has one."""
    import re

    match = re.search(r"<title[^>]*>(.*?)</title>", body[:20_000], re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return " ".join(match.group(1).split())[:200]

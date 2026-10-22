"""Built-in penetration-testing playbooks.

A playbook is a *procedure*, not a capability. The engine's checks
answer "is this flaw present"; a playbook answers "given that this
technology is in use, what do I do next, in what order, and what
counts as proof".

They are shipped with the engine rather than written by the agent so
that every run starts from the same procedure — an assessment whose
method depends on what the model happened to remember is not
reproducible, and a report that cannot be reproduced is not evidence.

Each playbook lists the steps in the order they should be taken, and
names the engine tool that carries out each one. Installing them into
the skill library makes them available to the agent the same way a
learned skill is, so a run can grow beyond them without throwing them
away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoids a runtime import cycle
    from aegisx_agent.skills.manager import SkillManager
    from aegisx_agent.skills.skill import Skill


@dataclass(frozen=True)
class Playbook:
    """One named procedure for a class of assessment."""

    name: str
    description: str
    steps: tuple[str, ...]
    tools: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_skill(self) -> Skill:
        """Render the playbook as a skill the skill library can hold."""
        from aegisx_agent.skills.skill import Skill

        return Skill(
            name=self.name,
            description=self.description,
            steps=list(self.steps),
            tool_calls=[{"name": tool, "args": {}} for tool in self.tools],
            tags=list(self.tags),
            category="pentest",
        )

    def to_prompt(self) -> str:
        """Render the playbook for a system prompt."""
        lines = [f"### {self.name} — {self.description}"]
        for index, step in enumerate(self.steps, 1):
            lines.append(f"{index}. {step}")
        if self.tools:
            lines.append(f"Tools: {', '.join(self.tools)}")
        return "\n".join(lines)


PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        name="recon-survey",
        description=(
            "Map the target before touching it, so every later step targets "
            "something real."
        ),
        steps=(
            "Read robots.txt and sitemap.xml first — they name paths the application "
            "prefers to hide from a crawler.",
            "Crawl from the root, follow links and forms, and collect every endpoint "
            "with its parameters. The parameter names are the attack surface.",
            "Fingerprint the stack: server header, framework markers, JavaScript "
            "bundle names, and the version strings that come with them.",
            "Check which common ports answer, so the review covers the admin host "
            "and not only the public one.",
            "Resolve common subdomains. A name that resolves but is out of scope is "
            "reported as an observation, never scanned.",
            "Write down what the target *is* before deciding what to attack. A "
            "plan built on a guess produces findings about a system that is not there.",
        ),
        tools=("cyber_scan", "cyber_agent"),
        tags=("recon", "enumeration"),
    ),
    Playbook(
        name="injection-hunt",
        description="Prove injection flaws differentially, without damaging the target.",
        steps=(
            "Take a baseline response for every parameter before probing it. A "
            "difference you cannot explain is not a difference you can report.",
            "Use payloads that prove the flaw by their *effect*: an arithmetic "
            "expression that evaluates, a marker that comes back resolved, an echo "
            "that returns the output of a command.",
            "Never send stacked queries, never write files, never delete, and never "
            "sleep long enough to matter. A scanner that breaks the target is a "
            "liability.",
            "For SQL injection, use a true/false pair plus error signatures — one "
            "of the two catches the loud flaw and the other catches the quiet one.",
            "For SSRF, probe with loopback, the reserved .invalid TLD, and the file "
            "scheme, and confirm the server's *own* error before you report it.",
            "For XXE, use an internal entity that expands to a marker, and an "
            "external entity that points at a host under .invalid, which never "
            "resolves.",
            "For command injection, echo a marker; if the echo is filtered, fall "
            "back to a three-second sleep and compare the two response times.",
            "Report a finding only when the response proves the flaw. A payload "
            "that was accepted is not a payload that was executed.",
        ),
        tools=("cyber_scan",),
        tags=("injection", "sqli", "xss", "ssti", "ssrf", "xxe", "rce"),
    ),
    Playbook(
        name="authn-review",
        description="Audit authentication and session handling from what the server hands out.",
        steps=(
            "Collect every token the service issues: in cookies, in bodies, and in "
            "URLs. Do not replay any of them against an authenticated endpoint — "
            "that would be using somebody else's session.",
            "For a JWT, decode the header and the payload. alg: none means the "
            "signature is decorative. A missing exp means the token is valid "
            "forever.",
            "Read the payload for authorisation claims (role, scope, permissions). A "
            "decision the client can read is a decision the client can influence.",
            "Read the payload for personal data. A token is stored in a browser; "
            "personal data inside it has left the database.",
            "Look for credentials in URLs. URLs end up in access logs, in browser "
            "history, and in the Referer header of every third-party request.",
            "Check the entropy of session identifiers, and check whether the "
            "identifier changes on privilege change.",
        ),
        tools=("cyber_scan",),
        tags=("authn", "jwt", "session"),
    ),
    Playbook(
        name="authz-review",
        description="Test whether the server enforces who may see and do what.",
        steps=(
            "Collect object identifiers from the crawled surface — path segments "
            "that look like ids, and query parameters that carry them.",
            "Ask for an object you did not create, with a value you did not obtain. "
            "A response that returns it anyway is a broken object-level check.",
            "Compare the unauthorised response with the authorised one. A 403 with "
            "the right shape is a control; a 200 with the data is a breach.",
            "Test dangerous HTTP methods (PUT, DELETE, PATCH) on endpoints that "
            "should only accept POST, and note which ones accept them.",
            "Test open redirects on every parameter that takes a URL, because a "
            "redirect from the real origin is a phishing primitive.",
            "Do not guess other people's identifiers. The point is that the server "
            "does not check, not that you found a particular record.",
        ),
        tools=("cyber_scan",),
        tags=("authz", "idor", "bola", "access-control"),
    ),
    Playbook(
        name="api-abuse",
        description="Look for the ways an API trusts the client more than it should.",
        steps=(
            "Read the schema before the endpoints. Introspection, an OpenAPI "
            "document, or the JavaScript bundle will name the routes faster than "
            "crawling will.",
            "Compare the fields the client sends with the fields the client should "
            "be allowed to send. A role, an owner id, or a price in the request "
            "body is a decision the client is making.",
            "Look for the debug surface: /admin, /debug, /actuator, /console, "
            "/phpinfo.php, /metrics, the API explorer. A 200 with real content is "
            "an exposure.",
            "Test CORS with an arbitrary origin and check whether credentials are "
            "allowed with it. Cross-origin readable authenticated responses are "
            "the whole finding.",
            "Check whether the API version is pinned in the path, and whether old "
            "versions are still routed.",
            "Never brute-force a credential, never create an account, and never "
            "send state-changing payloads as part of an abuse test.",
        ),
        tools=("cyber_scan",),
        tags=("api", "graphql", "cors", "mass-assignment"),
    ),
    Playbook(
        name="exposure-hunt",
        description="Find what the deployment left in the open.",
        steps=(
            "Probe the paths that deployments forget: .env, .git, backups, "
            "config files, source maps, and directory listings.",
            "Treat a 200 that returns real content as a finding and a 200 that "
            "returns the site's soft-404 page as nothing. Servers that answer 200 "
            "to everything are common.",
            "Check verbose error pages: stack traces, framework debug pages, and "
            "SQL errors reveal paths, versions, and queries.",
            "Check the transport: certificate validity, protocol versions, cipher "
            "suites, and whether HSTS is set with a real max-age.",
            "Check the browser-facing headers and the attributes on the cookies: "
            "Secure, HttpOnly, SameSite, and a session cookie without them.",
            "Look for a dangling CNAME — a name that resolves to a hosting provider "
            "where the target no longer exists is claimable by anyone.",
        ),
        tools=("cyber_scan",),
        tags=("exposure", "headers", "tls", "takeover"),
    ),
    Playbook(
        name="supply-chain-review",
        description="Check the code and its dependencies for what is already known to be broken.",
        steps=(
            "Read the manifests and lock files, and check every dependency against "
            "the advisory database.",
            "Search the tree for committed credentials: API keys, private keys, "
            "connection strings, and passwords. A committed secret is in every "
            "clone and in the history forever.",
            "Look for the dangerous call sites: command execution with a "
            "concatenated argument, a template rendered from user input, a query "
            "built by string concatenation, and a deserialiser fed by the network.",
            "Check for weak cryptography: MD5 or SHA-1 where a password or a "
            "signature is involved, and ECB mode.",
            "Treat a dependency advisory as a *candidate*. Check whether the "
            "vulnerable code path is actually reachable before reporting it as "
            "exploitable, and say which way you decided.",
        ),
        tools=("cyber_sast", "cyber_cve"),
        tags=("supply-chain", "sast", "secrets"),
    ),
    Playbook(
        name="business-logic",
        description="Test the rules the application states, and the ones it only assumes.",
        steps=(
            "Write the business rules down first: what a price means, who may "
            "approve what, which step may follow which. A logic flaw is a rule "
            "the application does not enforce, not a malformed request.",
            "Look for a value the client supplies that changes what it costs or "
            "what it owns: quantity, price, discount, currency, owner id. A "
            "server that trusts one of those is the finding.",
            "Walk a multi-step workflow out of order: skip the payment step, "
            "replay the final step, or return to an earlier step after the state "
            "has already advanced.",
            "Look for the race: the same coupon, the same balance, the same "
            "stock redeemed twice at once. Report it as probable unless you can "
            "observe the effect twice.",
            "Never place a real order, never move real money, and never leave a "
            "test record behind. Stop at the observation that the rule is not "
            "enforced.",
        ),
        tools=("cyber_scan",),
        tags=("business-logic", "workflow", "race-condition"),
    ),
    Playbook(
        name="cloud-posture",
        description="Read the storage and metadata a deployment exposes from outside.",
        steps=(
            "Check whether the authorised target is itself a storage endpoint. If "
            "it is, ask the listing API whether the container enumerates — a "
            "listing that answers without credentials is the whole finding.",
            "Read the JavaScript bundles for cloud material: access keys, bucket "
            "names, project ids, and signed URLs with an expiry far in the future.",
            "Test whether a user-supplied URL reaches the instance metadata "
            "service, and report it as SSRF together with the credential exposure "
            "it implies. Never fetch the credentials themselves.",
            "Check a signed URL's expiry against the object it names. A link that "
            "stays valid for a year is a public link with extra steps.",
            "Stay on the authorised host. A bucket that belongs to a third party "
            "is out of scope, and being linked from the page does not make it a "
            "target.",
        ),
        tools=("cyber_scan", "cyber_sast"),
        tags=("cloud", "storage", "ssrf", "metadata"),
    ),
    Playbook(
        name="finding-discipline",
        description="Decide what is worth reporting, and write it so a reader can act.",
        steps=(
            "Severity is about impact, not excitement. A missing header is low; "
            "remote code execution is critical.",
            "Confidence is a separate axis. Say which one you have, and never "
            "collapse the two into one number.",
            "Every finding carries its evidence: the request, the response, the "
            "source line. A finding nobody can reproduce is a finding nobody will fix.",
            "Prefer under-reporting. A reader who is trained to ignore you stops "
            "reading you entirely.",
            "Never speculate about a vulnerability you did not observe. 'This might "
            "be exploitable' is not a finding; it is a suggestion for a test.",
            "Lead with what an attacker would do with it, then how it happens, then "
            "the fix. Not the other way round.",
            "State the scope of the assessment and what was not covered. A reader "
            "who knows the gaps can decide what the gaps are worth.",
        ),
        tools=("cyber_report",),
        tags=("reporting", "quality"),
    ),
)


def install_playbooks(manager: SkillManager, *, overwrite: bool = False) -> list[Skill]:
    """Install the built-in playbooks into a skill library.

    Existing skills of the same name are left alone unless
    ``overwrite`` is set, so an operator's own edits to a playbook
    survive an upgrade.
    """
    installed: list[Skill] = []
    for playbook in PLAYBOOKS:
        if not overwrite and manager.get(playbook.name) is not None:
            continue
        installed.append(manager.create_skill(
            name=playbook.name,
            description=playbook.description,
            steps=list(playbook.steps),
            tool_calls=[{"name": tool, "args": {}} for tool in playbook.tools],
            tags=list(playbook.tags),
            category="pentest",
        ))
    return installed


def playbooks_prompt(names: list[str] | None = None) -> str:
    """Render the playbooks for a system prompt.

    ``names`` selects a subset; ``None`` renders all of them.
    """
    selected = PLAYBOOKS
    if names:
        wanted = set(names)
        selected = tuple(playbook for playbook in PLAYBOOKS if playbook.name in wanted)
    if not selected:
        return ""
    body = "\n\n".join(playbook.to_prompt() for playbook in selected)
    return (
        "[Pentest playbooks]\n"
        "Follow these procedures when the assessment reaches their subject. "
        "They describe the order to work in and what counts as proof.\n\n"
        f"{body}"
    )


def playbook_names() -> list[str]:
    """The names of every built-in playbook."""
    return [playbook.name for playbook in PLAYBOOKS]


def describe() -> list[dict[str, Any]]:
    """A machine-readable summary of the playbook library."""
    return [
        {
            "name": playbook.name,
            "description": playbook.description,
            "steps": len(playbook.steps),
            "tools": list(playbook.tools),
            "tags": list(playbook.tags),
        }
        for playbook in PLAYBOOKS
    ]

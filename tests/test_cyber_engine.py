"""The engines that drive the checks: recon, the swarm, the agent, the scanner.

The agent tests are the important ones here. They prove the two things a
model-driven scanner has to get right: a plan can only name a capability
the engine implements, and the second wave has to be justified by what the
first one found rather than being a second sweep.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import pytest

from aegisx_agent.cyber import agent as agent_module
from aegisx_agent.cyber import recon, scanner
from aegisx_agent.cyber import swarm as swarm_module
from aegisx_agent.cyber.agent import (
    CAPABILITY_CATALOGUE,
    CyberAgent,
    CyberAgentConfig,
    Plan,
    _extract_json,
)
from aegisx_agent.cyber.models import Category, Confidence, Severity
from aegisx_agent.cyber.prober import Response
from aegisx_agent.cyber.scope import ScopeGuard, scope_from_targets
from aegisx_agent.cyber.swarm import (
    CAPABILITIES,
    LineOfAttack,
    SwarmPlan,
    SwarmRunner,
    ToolGrant,
    default_lines,
)

BASE = "https://app.example.com"


def response(
    url: str,
    status: int = 200,
    body: str = "",
    *,
    headers: dict[str, str] | None = None,
    content_type: str = "text/html",
) -> Response:
    return Response(
        url=url,
        status=status,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        body=body,
        content_type=content_type,
    )


class FakeProber:
    """A network-free prober that answers from a routing function."""

    def __init__(self, handler: Any = None, *, guard: Any = None, **_: Any) -> None:
        self.handler = handler or (lambda url, method: response(url, 404, "not found"))
        self.guard = guard
        self.calls: list[tuple[str, str]] = []

    async def __aenter__(self) -> FakeProber:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def close(self) -> None:
        return None

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
        params: Any = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> Response:
        self.calls.append((method, url))
        if self.guard is not None:
            self.guard.record_request(url)
        return self.handler(url, method)


def install_fake_prober(monkeypatch: pytest.MonkeyPatch, handler: Any, **kwargs: Any) -> None:
    """Point every engine at the same fake prober factory."""

    def factory(guard: Any = None, **_kwargs: Any) -> FakeProber:
        return FakeProber(handler, guard=guard, **kwargs)

    monkeypatch.setattr(agent_module, "Prober", factory)
    monkeypatch.setattr(swarm_module, "Prober", factory)
    monkeypatch.setattr(scanner, "Prober", factory)


def site(url: str, method: str = "GET") -> Response:
    """A small, well-behaved site with one admin panel and one id endpoint."""
    if method != "GET":
        return response(url, 405, "method not allowed")
    path = urlparse(url).path or "/"
    if path == "/robots.txt":
        return response(url, body="Disallow: /private\n", content_type="text/plain")
    if path in ("/", "/home"):
        return response(
            url,
            body=(
                "<html><head><title>App</title></head><body>"
                "<a href='/api/users/1'>user</a>"
                "</body></html>"
            ),
        )
    if path == "/api/users/1":
        return response(url, body='{"id":1,"email":"a@example.com"}')
    if path == "/api/users/2":
        return response(url, body='{"id":2,"email":"b@example.com"}')
    if path == "/admin":
        return response(url, body="<h1>Admin dashboard</h1>")
    return response(url, 404, "not found")


# --------------------------------------------------------------------------
# Recon
# --------------------------------------------------------------------------


def test_fingerprint_reads_versions_from_headers_and_bodies() -> None:
    captured = response(
        BASE,
        body="<div data-reactroot></div> /wp-content/uploads/x",
        headers={"Server": "nginx/1.24.0", "X-Powered-By": "Express/4.18.2"},
    )

    technologies = recon.fingerprint(captured)

    assert technologies["nginx"] == "1.24.0"
    assert technologies["express"] == "4.18.2"
    assert technologies["react"] == ""
    assert technologies["wordpress"] == ""


@pytest.mark.asyncio()
async def test_the_crawler_stays_on_the_host_and_records_what_it_refused() -> None:
    pages = {
        "/": (
            "<a href='/about'>about</a>"
            "<a href='https://evil.example.net/x'>out</a>"
            "<a href='/static/app.css'>css</a>"
            "<form action='/login' method='post'></form>"
        ),
        "/about": "<a href='/contact'>contact</a>",
        "/contact": "<p>contact us</p>",
    }

    def handler(url: str, _method: str = "GET") -> Response:
        return response(url, body=pages.get(urlparse(url).path or "/", "not found"), status=200)

    guard = ScopeGuard(scope_from_targets([BASE]))
    crawler = recon.Crawler(FakeProber(handler), guard, max_pages=10, max_depth=3)  # type: ignore[arg-type]

    surface = await crawler.crawl(BASE)

    paths = {(urlparse(endpoint).path or "/") for endpoint in surface.endpoints}
    assert paths == {"/", "/about", "/contact", "/login"}
    assert surface.subdomains == ["evil.example.net"]
    assert surface.forms == [f"{BASE}/login"]
    assert crawler.stats.pages_fetched == 4


@pytest.mark.asyncio()
async def test_the_crawler_respects_the_depth_limit() -> None:
    pages = {"/": "<a href='/about'>about</a>", "/about": "<a href='/contact'>c</a>"}

    def handler(url: str, _method: str = "GET") -> Response:
        return response(url, body=pages.get(urlparse(url).path or "/", ""))

    guard = ScopeGuard(scope_from_targets([BASE]))
    crawler = recon.Crawler(FakeProber(handler), guard, max_pages=10, max_depth=1)  # type: ignore[arg-type]

    surface = await crawler.crawl(BASE)

    assert [(urlparse(endpoint).path or "/") for endpoint in surface.endpoints] == [
        "/",
        "/about",
    ]


@pytest.mark.asyncio()
async def test_the_crawler_stops_at_its_page_budget() -> None:
    def handler(url: str, _method: str = "GET") -> Response:
        return response(url, body="<a href='/a'>a</a><a href='/b'>b</a>")

    guard = ScopeGuard(scope_from_targets([BASE]))
    crawler = recon.Crawler(FakeProber(handler), guard, max_pages=1)  # type: ignore[arg-type]

    await crawler.crawl(BASE)

    assert crawler.stats.truncated is True
    assert crawler.stats.pages_fetched == 1


@pytest.mark.asyncio()
async def test_robots_txt_is_read_for_paths_and_sitemaps() -> None:
    def handler(url: str, _method: str = "GET") -> Response:
        return response(
            url,
            body="User-agent: *\nDisallow: /admin\nSitemap: https://app.example.com/sitemap.xml\n",
            content_type="text/plain",
        )

    rules, sitemaps = await recon.fetch_robots(FakeProber(handler), BASE)  # type: ignore[arg-type]

    assert rules == ["/admin"]
    assert sitemaps == ["https://app.example.com/sitemap.xml"]


# --------------------------------------------------------------------------
# Swarm
# --------------------------------------------------------------------------


def test_a_grant_refuses_a_capability_it_does_not_hold() -> None:
    grant = ToolGrant(frozenset({"crawl"}))

    assert grant.permits("crawl")
    with pytest.raises(PermissionError, match="not granted"):
        grant.require("inject")


def test_the_default_plan_only_uses_capabilities_the_engine_can_run() -> None:
    for line in default_lines(BASE):
        assert line.grant.capabilities <= CAPABILITIES
        assert line.name
        assert line.brief


@pytest.mark.asyncio()
async def test_two_lines_that_find_the_same_problem_corroborate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_prober(monkeypatch, site)
    guard = ScopeGuard(scope_from_targets([BASE]))
    plan = SwarmPlan(target="app.example.com", base_url=BASE, max_concurrency=2)
    plan.add_line(
        LineOfAttack(
            name="surface-mapper",
            brief="map",
            grant=ToolGrant(frozenset({"crawl", "fingerprint"})),
            categories=(Category.RECON,),
        )
    )
    for name in ("access-a", "access-b"):
        plan.add_line(
            LineOfAttack(
                name=name,
                brief="audit access control",
                grant=ToolGrant(frozenset({"authz"})),
                categories=(Category.AUTHZ,),
            )
        )

    result = await SwarmRunner(plan, guard=guard).run()

    assert len(result.findings) == 1
    assert result.findings[0].title.startswith("Object identifier")
    assert result.corroborated == [result.findings[0].fingerprint]
    assert result.findings[0].confidence is Confidence.CONFIRMED


@pytest.mark.asyncio()
async def test_a_line_runs_only_the_checks_its_grant_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_prober(monkeypatch, site)
    guard = ScopeGuard(scope_from_targets([BASE]))
    plan = SwarmPlan(target="app.example.com", base_url=BASE, max_concurrency=1)
    plan.add_line(
        LineOfAttack(name="crawl-only", brief="map", grant=ToolGrant(frozenset({"crawl"})))
    )

    result = await SwarmRunner(plan, guard=guard).run()

    assert result.findings == []
    assert result.lines[0].name == "crawl-only"


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class ScriptedModel:
    """A model that answers from a function and records the prompts."""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.prompts: list[str] = []

    def __call__(
        self, system_prompt: str, user_prompt: str, max_tokens: int, temperature: float
    ) -> str:
        self.prompts.append(user_prompt)
        return str(self.responder(system_prompt, user_prompt))


def plan_json(capabilities: list[str], name: str = "config-audit") -> str:
    return json.dumps(
        {
            "hypotheses": ["the deployment looks like a default configuration"],
            "lines": [
                {
                    "name": name,
                    "intent": "check the configuration the deployment shipped with",
                    "capabilities": capabilities,
                    "focus": "/",
                }
            ],
        }
    )


def first_fingerprint(prompt: str) -> str:
    return re.search(r'"fingerprint":\s*"([^"]+)"', prompt).group(1)  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_the_agent_escalates_from_what_the_first_wave_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_prober(monkeypatch, site)

    def responder(system: str, user: str) -> str:
        if "second wave" in system:
            assert "Missing security header" in user
            return plan_json(["api-surface"], name="follow-the-admin-panel")
        if "reviewer" in system:
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "fingerprint": first_fingerprint(user),
                            "verdict": "false-positive",
                            "reason": "The page is served by a static host with no user data.",
                        }
                    ]
                }
            )
        return plan_json(["headers"])

    model = ScriptedModel(responder)
    config = CyberAgentConfig(targets=[BASE], max_waves=2)
    run = await CyberAgent(config, model=model).run()

    assert len(run.waves) == 2
    assert run.waves[0].index == 1
    assert run.waves[1].index == 2
    assert run.waves[1].new_findings >= 1
    assert any("admin" in line.name for line in run.waves[1].plan.lines)
    assert any(finding.title.startswith("Exposed administration panel") for finding in run.findings)
    assert len(run.verification.dismissed) == 1
    assert any(finding.title.startswith("Dismissed:") for finding in run.findings)
    assert len(model.prompts) == 3


@pytest.mark.asyncio()
async def test_an_escalation_that_answers_nothing_ends_the_run_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_prober(monkeypatch, site)

    def responder(system: str, user: str) -> str:
        if "second wave" in system:
            return json.dumps({"hypotheses": [], "lines": []})
        if "reviewer" in system:
            return json.dumps({"verdicts": []})
        return plan_json(["headers"])

    config = CyberAgentConfig(targets=[BASE], max_waves=3)
    run = await CyberAgent(config, model=ScriptedModel(responder)).run()

    assert len(run.waves) == 1
    assert run.waves[0].notes
    assert "no follow-up lines" in " ".join(run.waves[0].notes)


@pytest.mark.asyncio()
async def test_without_a_model_the_agent_runs_the_default_plan_and_skips_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_prober(monkeypatch, site)
    config = CyberAgentConfig(targets=[BASE], max_waves=1)

    run = await CyberAgent(config, model=None).run()

    assert run.plan.fallback is True
    assert "No model was supplied" in " ".join(run.plan.notes)
    assert run.verification.skipped is True
    assert len(run.waves) == 1
    assert run.findings


@pytest.mark.asyncio()
async def test_the_review_cannot_erase_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_prober(monkeypatch, site)

    def responder(system: str, user: str) -> str:
        if "reviewer" in system:
            fingerprints = re.findall(r'"fingerprint":\s*"([^"]+)"', user)
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "fingerprint": fingerprint,
                            "verdict": "false-positive",
                            "reason": "Not interesting enough to report.",
                        }
                        for fingerprint in fingerprints
                    ]
                }
            )
        return plan_json(["headers"])

    run = await CyberAgent(
        CyberAgentConfig(targets=[BASE], max_waves=1), model=ScriptedModel(responder)
    ).run()

    assert run.verification.dismissed == []
    assert any("above the" in note for note in run.verification.notes)
    assert not any(finding.title.startswith("Dismissed:") for finding in run.findings)


@pytest.mark.asyncio()
async def test_a_dismissal_without_a_reason_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_prober(monkeypatch, site)

    def responder(system: str, user: str) -> str:
        if "reviewer" in system:
            return json.dumps(
                {
                    "verdicts": [
                        {"fingerprint": first_fingerprint(user), "verdict": "false-positive"}
                    ]
                }
            )
        return plan_json(["headers"])

    run = await CyberAgent(
        CyberAgentConfig(targets=[BASE], max_waves=1), model=ScriptedModel(responder)
    ).run()

    assert run.verification.dismissed == []
    assert any("no reason" in note for note in run.verification.notes)


def test_the_plan_parser_drops_capabilities_the_engine_cannot_run() -> None:
    survey = agent_module.Survey(target=BASE, base_url=BASE, host="app.example.com")
    raw = json.dumps(
        {
            "lines": [
                {"name": "ok", "intent": "run inject", "capabilities": ["inject", "telepathy"]},
                {"name": "empty", "intent": "nothing", "capabilities": ["telepathy"]},
                {"name": "no-intent", "capabilities": ["crawl"]},
            ]
        }
    )

    plan = CyberAgent(CyberAgentConfig(targets=[BASE]))._parse_plan(raw, survey)

    assert [line.name for line in plan.lines] == ["ok"]
    assert plan.lines[0].capabilities == ("inject",)
    assert any("was dropped" in note for note in plan.notes)


def test_the_plan_parser_respects_the_line_budget() -> None:
    survey = agent_module.Survey(target=BASE, base_url=BASE, host="app.example.com")
    raw = json.dumps(
        {
            "lines": [
                {"name": f"line-{index}", "intent": "x", "capabilities": ["crawl"]}
                for index in range(10)
            ]
        }
    )

    plan = CyberAgent(CyberAgentConfig(targets=[BASE], max_lines=3))._parse_plan(raw, survey)

    assert len(plan.lines) == 3


def test_extract_json_finds_the_object_inside_prose_and_fences() -> None:
    assert _extract_json('Here you go:\n```json\n{"lines": []}\n```') == {"lines": []}
    assert _extract_json("no json here") is None
    assert _extract_json('{"a": 1} trailing') == {"a": 1}


def test_every_capability_in_the_catalogue_is_runnable() -> None:
    assert set(CAPABILITY_CATALOGUE) <= CAPABILITIES


def test_the_agent_config_becomes_a_scope_policy() -> None:
    policy = CyberAgentConfig(targets=[BASE], max_requests=42).to_policy()

    assert policy.allowed_hosts == ["app.example.com"]
    assert policy.max_requests == 42


def test_an_empty_plan_is_a_plan_and_not_an_error() -> None:
    plan = Plan()

    assert plan.to_dict()["lines"] == []
    assert plan.to_swarm_plan(BASE, BASE, 2).lines == []


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_the_scanner_maps_checks_and_summarises(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_prober(monkeypatch, site)
    progress: list[str] = []
    config = scanner.ScanConfig(targets=[BASE], deep=True)
    scan = scanner.Scanner(config)
    scan.on_progress(lambda event: progress.append(event.phase))

    result = await scan.run()

    assert result.target == BASE
    assert result.hosts[0].host == "app.example.com"
    assert result.stats.requests_sent > 0
    assert result.stats.checks_run > 0
    assert any(finding.category is Category.HEADERS for finding in result.findings)
    assert "recon" in progress
    assert progress[-1] == "done"
    assert "findings" in scanner.summarise(result)


@pytest.mark.asyncio()
async def test_a_scan_without_targets_reports_the_error_instead_of_crashing() -> None:
    result = await scanner.Scanner(scanner.ScanConfig()).run()

    assert result.findings == []
    assert result.stats.errors == ["no targets were supplied"]


def test_cyber_mode_is_opt_in_and_a_default_session_stays_plain(tmp_path: Any) -> None:
    """A plain chat session pays nothing for the assessment engine.

    The cyber tools are not registered, the playbooks do not travel with
    the prompt, and the persona is the general-purpose assistant — not the
    pentester doctrine, which would otherwise colour every answer about
    ordinary work.
    """
    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent

    agent = AegisXAgent(AgentConfig(llm_provider=LLMProvider.OLLAMA, data_dir=str(tmp_path)))

    registered = {tool.name for tool in agent.tools.list_tools()}
    assert not any(name.startswith("cyber_") for name in registered)
    assert agent.config.persona == "default"
    assert not agent.persona_loader.get(agent.config.persona).startswith(
        "You are AegisX Pentester"
    )

    prompt = agent._build_system_prompt()
    assert "cyber_scan" not in prompt
    assert "[Pentest playbooks]" not in prompt


def test_cyber_mode_can_be_turned_on(tmp_path: Any) -> None:
    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent

    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            cyber_enabled=True,
            persona="pentester",
        )
    )

    registered = {tool.name for tool in agent.tools.list_tools()}
    assert {"cyber_scan", "cyber_sast", "cyber_cve", "cyber_report"} <= registered
    assert agent.persona_loader.get(agent.config.persona).startswith("You are AegisX Pentester")

    prompt = agent._build_system_prompt()
    assert "cyber_scan" in prompt
    assert "[Pentest playbooks]" in prompt
    assert "business-logic" in prompt
    # Doctrine goes into the prompt, not into the operator's learned skills.
    assert agent.skill_manager.list_skills() == []


def test_filter_findings_applies_severity_and_category() -> None:
    from aegisx_agent.cyber.models import Finding

    findings = [
        Finding(title="high", severity=Severity.HIGH, category=Category.INJECTION),
        Finding(title="low", severity=Severity.LOW, category=Category.HEADERS),
    ]

    filtered = scanner.filter_findings(findings, minimum_severity=Severity.MEDIUM)
    assert [finding.title for finding in filtered] == ["high"]
    assert [
        f.title for f in scanner.filter_findings(findings, categories=["headers"])
    ] == ["low"]

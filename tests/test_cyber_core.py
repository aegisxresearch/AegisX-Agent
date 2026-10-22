"""The cyber layer's foundations: findings, scope, storage, and the prober.

These are the parts every check and every reporter depend on, so the
tests here are about invariants rather than about a target: a finding's
identity must not move when its wording does, a refusal must name its
reason, and no code path may send a request the scope guard refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    ScanResult,
    Severity,
)
from aegisx_agent.cyber.playbooks import (
    PLAYBOOKS,
    describe,
    install_playbooks,
    playbook_names,
    playbooks_prompt,
)
from aegisx_agent.cyber.prober import Prober, Response
from aegisx_agent.cyber.scope import (
    ScopeDecision,
    ScopeError,
    ScopeGuard,
    ScopePolicy,
    ScopeViolation,
    scope_from_targets,
)
from aegisx_agent.cyber.store import FindingStore
from aegisx_agent.skills.manager import SkillManager


def make_finding(**overrides: Any) -> Finding:
    """A finding with everything a reporter expects, overridable per test."""
    payload: dict[str, Any] = {
        "title": "Missing security header: Content-Security-Policy",
        "severity": Severity.MEDIUM,
        "category": Category.HEADERS,
        "confidence": Confidence.CONFIRMED,
        "target": "app.example.com",
        "endpoint": "https://app.example.com/",
        "description": "The response does not set Content-Security-Policy.",
        "remediation": "Set it on every response.",
    }
    payload.update(overrides)
    return Finding(**payload)


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


def test_severity_is_ordered_from_info_to_critical() -> None:
    ordered = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    weights = [severity.weight for severity in ordered]
    assert weights == sorted(weights)
    assert Severity.CRITICAL.label == "CRITICAL"


def test_fingerprint_ignores_case_and_whitespace_but_tracks_endpoint() -> None:
    first = make_finding(title="Missing  Header")
    second = make_finding(title="missing header")
    third = make_finding(endpoint="https://app.example.com/login")

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint
    assert len(first.fingerprint) == 16


def test_merge_raises_severity_and_confidence_but_never_lowers_them() -> None:
    stored = make_finding(severity=Severity.HIGH, confidence=Confidence.PROBABLE)
    newer = make_finding(severity=Severity.LOW, confidence=Confidence.CONFIRMED)

    stored.merge(newer)

    assert stored.severity is Severity.HIGH
    assert stored.confidence is Confidence.CONFIRMED


def test_merge_appends_evidence_and_fills_empty_fields_once() -> None:
    stored = make_finding(evidence=[Evidence(kind=EvidenceKind.MATCH, summary="first")])
    stored.remediation = ""
    newer = make_finding(
        evidence=[
            Evidence(kind=EvidenceKind.MATCH, summary="first"),
            Evidence(kind=EvidenceKind.RESPONSE, summary="second"),
        ],
        remediation="Upgrade.",
    )

    stored.merge(newer)

    assert [item.summary for item in stored.evidence] == ["first", "second"]
    assert stored.remediation == "Upgrade."


def test_finding_survives_a_dict_round_trip() -> None:
    original = make_finding(
        severity=Severity.HIGH,
        evidence=[Evidence(kind=EvidenceKind.REQUEST, summary="GET /", request="GET / HTTP/1.1")],
    )

    restored = Finding.from_dict(json.loads(original.to_json()))

    assert restored.fingerprint == original.fingerprint
    assert restored.severity is Severity.HIGH
    assert restored.evidence[0].request == "GET / HTTP/1.1"


def test_evidence_renders_its_request_and_snippet() -> None:
    evidence = Evidence(
        kind=EvidenceKind.RESPONSE,
        summary="status 200",
        request="GET / HTTP/1.1",
        snippet="<html>",
    )

    rendered = evidence.render()

    assert "**response** — status 200" in rendered
    assert "GET / HTTP/1.1" in rendered
    assert "<html>" in rendered


def test_scan_result_finalize_builds_the_severity_histogram() -> None:
    result = ScanResult(target="app.example.com")
    result.findings = [
        make_finding(severity=Severity.HIGH),
        make_finding(severity=Severity.HIGH, title="Other"),
        make_finding(severity=Severity.LOW, title="Third"),
    ]

    result.finalize()

    assert result.stats.findings_total == 3
    assert result.stats.findings_by_severity == {"high": 2, "low": 1}
    assert result.finished_at


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_scope_from_targets_records_hosts_and_ports() -> None:
    policy = scope_from_targets(
        ["https://app.example.com", "http://internal.example.com:8080/path"]
    )

    assert policy.allowed_hosts == ["app.example.com", "internal.example.com"]
    assert policy.allowed_ports == [443, 8080]


def test_scope_from_targets_rejects_a_malformed_host() -> None:
    with pytest.raises(ValueError, match="not a valid host"):
        scope_from_targets(["https://exa mple.com"])


def test_wildcard_hosts_match_the_domain_and_its_children_only() -> None:
    guard = ScopeGuard(ScopePolicy(allowed_hosts=["*.example.com"]))

    assert guard.host_allowed("api.example.com")
    assert guard.host_allowed("example.com")
    assert not guard.host_allowed("notexample.com")
    assert not guard.host_allowed("example.com.evil.test")


def test_a_url_outside_the_scope_is_refused_with_its_reason() -> None:
    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))

    decision = guard.check("https://other.example.net/")

    assert not decision.allowed
    assert decision.violation is ScopeViolation.NOT_IN_SCOPE
    assert "not in the authorised scope" in decision.reason
    assert guard.violations == [("https://other.example.net/", "not-in-scope")]


@pytest.mark.parametrize(
    ("url", "violation"),
    [
        ("file://app.example.com/etc/passwd", ScopeViolation.SCHEME_NOT_ALLOWED),
        ("https://app.example.com:2222/", ScopeViolation.PORT_NOT_ALLOWED),
        ("https://app.example.com/private", ScopeViolation.PATH_NOT_ALLOWED),
        ("http://127.0.0.1/public", ScopeViolation.PRIVATE_ADDRESS),
        ("http://169.254.169.254/public/meta-data", ScopeViolation.PRIVATE_ADDRESS),
    ],
)
def test_each_unsafe_quality_of_a_request_has_its_own_refusal(
    url: str, violation: ScopeViolation
) -> None:
    policy = scope_from_targets(
        [
            "https://app.example.com",
            "http://127.0.0.1",
            "http://169.254.169.254",
        ],
        allowed_paths=["/public"],
    )
    guard = ScopeGuard(policy)

    decision = guard.check(url)

    assert not decision.allowed
    assert decision.violation is violation


def test_private_addresses_can_be_opted_into_explicitly() -> None:
    policy = scope_from_targets(["http://127.0.0.1:8000"], allow_private_addresses=True)
    guard = ScopeGuard(policy)

    assert guard.check("http://127.0.0.1:8000/").allowed


def test_an_exhausted_budget_refuses_the_request_before_it_is_sent() -> None:
    policy = scope_from_targets(["https://app.example.com"], max_requests=1)
    guard = ScopeGuard(policy)
    guard.record_request("https://app.example.com/")

    decision = guard.check("https://app.example.com/")

    assert decision.violation is ScopeViolation.BUDGET_EXHAUSTED
    assert guard.remaining_requests == 0


def test_a_passed_deadline_refuses_the_request() -> None:
    guard = ScopeGuard(scope_from_targets(["https://app.example.com"], timeout_seconds=0))

    decision = guard.check("https://app.example.com/")

    assert decision.violation is ScopeViolation.DEADLINE_PASSED


def test_ensure_allowed_raises_scope_error_and_a_decision_is_truthy() -> None:
    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))

    allowed: ScopeDecision = guard.check("https://app.example.com/")
    assert allowed

    with pytest.raises(ScopeError) as excinfo:
        guard.ensure_allowed("https://evil.example.net/")
    assert excinfo.value.violation is ScopeViolation.NOT_IN_SCOPE


def test_policy_rejects_nonsense_limits() -> None:
    with pytest.raises(ValueError, match="requests_per_second"):
        ScopePolicy(requests_per_second=0)
    with pytest.raises(ValueError, match="max_concurrency"):
        ScopePolicy(max_concurrency=0)
    with pytest.raises(ValueError, match="max_requests"):
        ScopePolicy(max_requests=0)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_store_deduplicates_by_fingerprint_and_counts_the_duplicate() -> None:
    store = FindingStore()

    store.add(make_finding())
    stored = store.add(make_finding(confidence=Confidence.TENTATIVE))

    assert len(store.all()) == 1
    assert store.stats.added == 1
    assert store.stats.duplicates == 1
    assert stored.confidence is Confidence.CONFIRMED


def test_store_sorts_worst_first_and_summarises() -> None:
    store = FindingStore()
    store.add(make_finding(severity=Severity.LOW))
    store.add(make_finding(severity=Severity.CRITICAL, title="Remote code execution"))

    assert store.all()[0].title == "Remote code execution"
    assert "2 findings" in store.summary()
    assert store.by_severity(Severity.HIGH)[0].severity is Severity.CRITICAL


def test_store_detects_a_regression_from_the_baseline(tmp_path: Path) -> None:
    store = FindingStore()
    finding = make_finding(severity=Severity.HIGH)
    snapshot = tmp_path / "baseline.json"
    snapshot.write_text(json.dumps({"findings": [finding.to_dict()]}), encoding="utf-8")

    assert store.load_baseline(snapshot) == 1
    store.add(make_finding(severity=Severity.HIGH))

    assert store.detect_regressions() == [finding.fingerprint]
    assert store.stats.regressions == 1


def test_store_appends_jsonl_so_a_scan_stays_evidence(tmp_path: Path) -> None:
    log = tmp_path / "findings.jsonl"
    store = FindingStore(path=log)

    store.add(make_finding())
    store.add(make_finding())
    store.save(tmp_path / "snapshot.json")

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["severity"] == "medium"
    snapshot = json.loads((tmp_path / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["count_by_severity"] == {"medium": 1}


def test_an_unknown_baseline_file_loads_as_empty(tmp_path: Path) -> None:
    store = FindingStore()

    assert store.load_baseline(tmp_path / "missing.json") == 0
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert store.load_baseline(broken) == 0


# --------------------------------------------------------------------------
# Prober
# --------------------------------------------------------------------------


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport(handler), follow_redirects=False)


@pytest.mark.asyncio()
async def test_prober_captures_the_exchange_as_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Server": "nginx/1.24.0", "Set-Cookie": "a=1"},
            text="hello",
            request=request,
        )

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard, verify_tls=False)
    prober._client = _client(handler)

    response = await prober.request("https://app.example.com/")

    assert response.status == 200
    assert response.ok
    assert response.header("server") == "nginx/1.24.0"
    assert response.header_values("set-cookie") == ["a=1"]
    assert response.body == "hello"
    assert response.request_line() == "GET https://app.example.com/ HTTP/1.1"
    assert guard.requests_made == 1
    assert prober.requests_sent == 1
    await prober.close()


@pytest.mark.asyncio()
async def test_prober_refuses_an_out_of_scope_url_without_touching_the_network() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(str(request.url))
        return httpx.Response(200, text="leaked", request=request)

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard)
    prober._client = _client(handler)

    response = await prober.request("https://evil.example.net/")

    assert calls == []
    assert response.error is not None
    assert "scope violation" in response.error
    assert response.status == 0
    await prober.close()


@pytest.mark.asyncio()
async def test_a_redirect_does_not_widen_the_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "app.example.com":
            return httpx.Response(
                302, headers={"Location": "https://evil.example.net/"}, request=request
            )
        return httpx.Response(200, text="third party", request=request)

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard)
    prober._client = _client(handler)

    response = await prober.request("https://app.example.com/")

    assert response.error is not None
    assert "not-in-scope" in response.error
    assert response.redirects == ["https://evil.example.net/"]
    await prober.close()


@pytest.mark.asyncio()
async def test_a_transport_error_becomes_an_error_response_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard)
    prober._client = _client(handler)

    response = await prober.request("https://app.example.com/")

    assert not response.ok
    assert "ConnectError" in response.error
    assert prober.errors
    await prober.close()


@pytest.mark.asyncio()
async def test_head_or_get_falls_back_when_head_is_rejected() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        status = 405 if request.method == "HEAD" else 200
        return httpx.Response(status, text="body", request=request)

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard)
    prober._client = _client(handler)

    response = await prober.head_or_get("https://app.example.com/")

    assert seen == ["HEAD", "GET"]
    assert response.status == 200
    await prober.close()


@pytest.mark.asyncio()
async def test_probe_many_keeps_the_order_and_reports_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bad":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, text=request.url.path, request=request)

    guard = ScopeGuard(scope_from_targets(["https://app.example.com"]))
    prober = Prober(guard)
    prober._client = _client(handler)

    responses = await prober.probe_many(
        ["https://app.example.com/a", "https://app.example.com/bad"]
    )

    assert isinstance(responses[0], Response)
    assert responses[0].body == "/a"
    assert "ReadTimeout" in responses[1].error
    await prober.close()


def test_same_origin_compares_scheme_host_and_port() -> None:
    assert Prober.same_origin("https://app.example.com/a", "https://app.example.com/b")
    assert not Prober.same_origin("https://app.example.com", "http://app.example.com")
    assert not Prober.same_origin("https://app.example.com", "https://api.example.com")


# --------------------------------------------------------------------------
# Playbooks
# --------------------------------------------------------------------------


def test_the_shipped_playbooks_cover_the_classes_of_assessment() -> None:
    names = playbook_names()

    for expected in (
        "recon-survey",
        "injection-hunt",
        "authn-review",
        "authz-review",
        "api-abuse",
        "business-logic",
        "cloud-posture",
        "supply-chain-review",
        "finding-discipline",
    ):
        assert expected in names
    assert len(names) == len(PLAYBOOKS)


def test_installing_playbooks_is_idempotent_unless_overwrite_is_asked_for(
    tmp_path: Path,
) -> None:
    manager = SkillManager(tmp_path / "skills")

    first = install_playbooks(manager)
    second = install_playbooks(manager)
    assert len(first) == len(PLAYBOOKS)
    assert second == []

    manager.update_skill("recon-survey", steps=["my own step"])
    install_playbooks(manager)
    assert manager.get("recon-survey").steps == ["my own step"]  # type: ignore[union-attr]

    install_playbooks(manager, overwrite=True)
    assert manager.get("recon-survey").steps != ["my own step"]  # type: ignore[union-attr]


def test_a_playbook_prompt_names_steps_and_tools() -> None:
    prompt = playbooks_prompt(["business-logic"])

    assert prompt.startswith("[Pentest playbooks]")
    assert "business-logic" in prompt
    assert "cyber_scan" in prompt
    assert "recon-survey" not in prompt
    assert playbooks_prompt(["does-not-exist"]) == ""


def test_describe_exposes_the_library_as_data() -> None:
    described = describe()

    assert len(described) == len(PLAYBOOKS)
    entry = next(item for item in described if item["name"] == "cloud-posture")
    assert entry["steps"] > 0
    assert "cloud" in entry["tags"]

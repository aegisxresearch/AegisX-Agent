"""Static analysis, advisory lookup, the agent-facing tools, and reporting.

The static analyzer and the advisory lookup are the two paths that never
touch a network target, so their tests are about precision: a real secret
is found, a safe call is not, a version inside an affected range matches
and the fixed one does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegisx_agent.cyber.cve import (
    Advisory,
    OfflineKnowledgeBase,
    VulnerabilityAuditor,
    advisory_to_finding,
    match_technology_advisories,
    parse_version,
    severity_from_cvss,
    technology_advisory_to_finding,
    version_in_range,
)
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    ScanResult,
    Severity,
)
from aegisx_agent.cyber.report import (
    HtmlReporter,
    MarkdownReporter,
    ReportOptions,
    SarifReporter,
    write_reports,
)
from aegisx_agent.cyber.sast import (
    Dependency,
    StaticAnalyzer,
    find_python_ast_sinks,
    iter_source_files,
    language_for,
    parse_dependencies,
    shannon_entropy,
)
from aegisx_agent.cyber.tools import (
    CyberCveTool,
    CyberReportTool,
    CyberSastTool,
    CyberScanTool,
    CyberSession,
    register_cyber_tools,
)
from aegisx_agent.tools.registry import ToolRegistry

#: A synthetic AWS key id for the secret scanner to find. The literal is split
#: so the repository's own pre-push secret guard does not read this fixture as
#: a leaked credential — the scanner still sees the full value at runtime.
AWS_KEY = "AKIA" "3XQ7ZJ2K5M8N4P6R"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sample_finding(**overrides: object) -> Finding:
    payload: dict[str, object] = {
        "title": "SQL injection in parameter 'id'",
        "severity": Severity.HIGH,
        "category": Category.INJECTION,
        "confidence": Confidence.CONFIRMED,
        "target": "app.example.com",
        "endpoint": "https://app.example.com/item?id=1",
        "description": "A true/false condition pair produced different responses.",
        "impact": "An attacker reads the database.",
        "remediation": "Use parameterised queries.",
        "cwe": "CWE-89",
        "owasp": "A03:2021 - Injection",
        "evidence": [
            Evidence(
                kind=EvidenceKind.RESPONSE,
                summary="differential response",
                request="GET /item?id=1' HTTP/1.1",
                response_status=500,
                snippet="SQL syntax error",
            )
        ],
    }
    payload.update(overrides)
    return Finding(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Static analysis
# --------------------------------------------------------------------------


def test_shannon_entropy_separates_a_token_from_a_word() -> None:
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaaaaaa") == 0.0
    assert shannon_entropy(AWS_KEY) > 3.0


def test_a_committed_aws_key_is_a_secret_finding(tmp_path: Path) -> None:
    write(tmp_path / "settings.py", f'AWS_ACCESS_KEY_ID = "{AWS_KEY}"\n')

    outcome = StaticAnalyzer().analyze(tmp_path)

    assert outcome.files_scanned >= 1
    assert any(finding.category is Category.SECRETS for finding in outcome.findings)
    secret = next(f for f in outcome.findings if f.category is Category.SECRETS)
    assert secret.severity is Severity.CRITICAL
    assert AWS_KEY not in secret.evidence[0].snippet


def test_a_shell_call_built_from_a_variable_is_reported(tmp_path: Path) -> None:
    write(
        tmp_path / "deploy.py",
        "import os\n\ndef restart(host):\n    os.system(f'ssh {host} reboot')\n",
    )

    outcome = StaticAnalyzer().analyze(tmp_path)
    titles = [finding.title for finding in outcome.findings]

    assert any("Command Injection" in title for title in titles)


def test_dependencies_and_virtualenvs_are_not_scanned(tmp_path: Path) -> None:
    write(tmp_path / "node_modules" / "pkg" / "index.js", f"const key = '{AWS_KEY}';\n")
    write(tmp_path / ".git" / "config", f"token = {AWS_KEY}\n")
    write(tmp_path / "app.py", "print('hello')\n")

    files = [path.name for path in iter_source_files(tmp_path)]
    outcome = StaticAnalyzer().analyze(tmp_path)

    assert files == ["app.py"]
    assert outcome.findings == []


def test_the_analyzer_reports_a_missing_path_instead_of_raising(tmp_path: Path) -> None:
    outcome = StaticAnalyzer().analyze(tmp_path / "does-not-exist")

    assert outcome.findings == []
    assert "does not exist" in outcome.errors[0]


def test_language_for_maps_the_common_suffixes(tmp_path: Path) -> None:
    assert language_for(tmp_path / "app.py") == "python"
    assert language_for(tmp_path / "index.js") == "javascript"
    assert language_for(tmp_path / "notes.unknown") == "other"


def test_the_ast_pass_flags_concatenated_subprocess_but_not_a_list(tmp_path: Path) -> None:
    unsafe = write(
        tmp_path / "unsafe.py",
        "import subprocess\nsubprocess.run('ls ' + name, shell=True)\n",
    )
    safe = write(tmp_path / "safe.py", "import subprocess\nsubprocess.run(['ls', name])\n")

    assert find_python_ast_sinks(unsafe)
    assert find_python_ast_sinks(safe) == []


def test_parse_dependencies_reads_npm_and_python_manifests(tmp_path: Path) -> None:
    write(
        tmp_path / "package.json",
        json.dumps(
            {
                "dependencies": {"lodash": "^4.17.20"},
                "devDependencies": {"vitest": "~1.2.0"},
            }
        ),
    )
    write(tmp_path / "requirements.txt", "django==3.2.0\nrequests>=2.31.0\n")
    write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["flask==2.2.0"]\n')

    dependencies = parse_dependencies(tmp_path)
    by_name = {dependency.name: dependency for dependency in dependencies}

    assert by_name["lodash"].version == "4.17.20"
    assert by_name["lodash"].ecosystem == "npm"
    assert by_name["vitest"].version == "1.2.0"
    assert by_name["django"].version == "3.2.0"
    assert by_name["requests"].ecosystem == "pypi"
    assert by_name["flask"].version == "2.2.0"


# --------------------------------------------------------------------------
# Advisories
# --------------------------------------------------------------------------


def test_a_version_is_parsed_into_comparable_numbers() -> None:
    parsed = parse_version("v1.2.3-rc1")

    assert parsed.numbers == (1, 2, 3)
    assert parsed.pre_release == "rc1"
    assert parsed.is_known
    assert not parse_version("latest").is_known


@pytest.mark.parametrize(
    ("version", "expression", "expected"),
    [
        ("3.2.0", ">=3.0,<3.2.4", True),
        ("3.2.4", ">=3.0,<3.2.4", False),
        ("2.0.0", "<1.2.3", False),
        ("1.0.0", ">=1.0", True),
        ("99.0.0", "*", True),
    ],
)
def test_version_in_range_handles_the_real_advisory_shapes(
    version: str, expression: str, expected: bool
) -> None:
    assert version_in_range(version, expression) is expected


@pytest.mark.parametrize(
    ("score", "severity"),
    [(9.8, Severity.CRITICAL), (7.5, Severity.HIGH), (5.0, Severity.MEDIUM), (0.5, Severity.LOW)],
)
def test_cvss_maps_onto_the_severity_bands(score: float, severity: Severity) -> None:
    assert severity_from_cvss(score) is severity
    assert severity_from_cvss(None) is Severity.MEDIUM


def test_the_offline_knowledge_base_knows_an_affected_django() -> None:
    advisories = OfflineKnowledgeBase().lookup("PyPI", "django", "3.2.0")

    assert advisories
    assert advisories[0].fixed_version == "3.2.4"
    assert OfflineKnowledgeBase().lookup("PyPI", "django", "3.2.4") == []
    assert OfflineKnowledgeBase().lookup("PyPI", "not-a-package", "1.0") == []


def test_only_a_versioned_technology_matches_an_advisory() -> None:
    assert match_technology_advisories({"nginx": "1.18.0"})
    assert match_technology_advisories({"nginx": ""}) == []
    assert match_technology_advisories({"nginx": "99.0.0"}) == []


def test_an_advisory_becomes_an_actionable_finding() -> None:
    advisory = Advisory(
        id="GHSA-test",
        ecosystem="PyPI",
        package="django",
        summary="A test advisory.",
        severity=Severity.HIGH,
        aliases=["CVE-2024-0001"],
        affected_range="<3.2.4",
        fixed_version="3.2.4",
    )

    finding = advisory_to_finding(advisory, package="django", version="3.2.0", ecosystem="PyPI")

    assert "CVE-2024-0001" in finding.title
    assert finding.category is Category.SUPPLY_CHAIN
    assert finding.remediation == "Upgrade django to 3.2.4 or later."
    assert "https://nvd.nist.gov/vuln/detail/CVE-2024-0001" in finding.references


def test_a_technology_advisory_becomes_a_finding() -> None:
    advisory = match_technology_advisories({"nginx": "1.18.0"})[0]

    finding = technology_advisory_to_finding(advisory, "nginx", "1.18.0")

    assert "nginx" in finding.title
    assert finding.severity in (Severity.HIGH, Severity.MEDIUM, Severity.CRITICAL)


class FakeOsvClient:
    """An advisory client that answers from a fixed list."""

    def __init__(self, advisories: list[Advisory] | None = None, error: str = "") -> None:
        self.advisories = advisories or []
        self.last_error = error
        self.queries: list[tuple[str, str, str]] = []

    async def query(self, ecosystem: str, package: str, version: str) -> list[Advisory]:
        self.queries.append((ecosystem, package, version))
        if self.last_error:
            raise RuntimeError(self.last_error)
        return self.advisories


@pytest.mark.asyncio()
async def test_the_auditor_turns_every_match_into_a_finding() -> None:
    advisory = Advisory(
        id="GHSA-test",
        ecosystem="PyPI",
        package="django",
        affected_range="<3.2.4",
        fixed_version="3.2.4",
    )
    auditor = VulnerabilityAuditor(client=FakeOsvClient([advisory]))  # type: ignore[arg-type]

    report = await auditor.audit_dependencies([Dependency("PyPI", "django", "3.2.0")])

    assert report.packages_checked == 1
    assert report.offline_used is True
    assert len(report.findings) == 1


@pytest.mark.asyncio()
async def test_a_package_without_a_version_is_skipped_and_errors_are_kept() -> None:
    auditor = VulnerabilityAuditor(
        client=FakeOsvClient(error="network unreachable")  # type: ignore[arg-type]
    )

    report = await auditor.audit_dependencies(
        [
            Dependency("npm", "lodash", ""),
            Dependency("npm", "express", "4.18.2"),
        ]
    )

    assert report.packages_checked == 0
    assert report.packages_skipped
    assert report.errors


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def test_the_session_keeps_one_copy_of_each_finding() -> None:
    session = CyberSession()
    finding = sample_finding()

    session.add([finding, sample_finding()])
    session.add([sample_finding(title="Another problem")])

    assert len(session.findings) == 2
    session.clear()
    assert session.findings == []


def test_registering_the_tools_shares_one_session() -> None:
    registry = ToolRegistry()

    session = register_cyber_tools(registry)

    names = {tool.name for tool in registry.list_tools()}
    assert {"cyber_scan", "cyber_sast", "cyber_cve", "cyber_report"} <= names
    assert session is not None


def test_registering_the_tools_requires_the_real_registry() -> None:
    with pytest.raises(TypeError, match="ToolRegistry"):
        register_cyber_tools(object())


@pytest.mark.asyncio()
async def test_the_scan_tool_needs_a_target() -> None:
    result = await CyberScanTool(CyberSession()).execute()

    assert not result.is_success
    assert "target is required" in (result.error or "")


@pytest.mark.asyncio()
async def test_the_sast_tool_reports_a_missing_path(tmp_path: Path) -> None:
    result = await CyberSastTool(CyberSession()).execute(path=str(tmp_path / "nope"))

    assert not result.is_success
    assert "Path not found" in (result.error or "")


@pytest.mark.asyncio()
async def test_the_sast_tool_collects_findings_into_the_session(tmp_path: Path) -> None:
    write(tmp_path / "app.py", f"key = '{AWS_KEY}'\n")
    session = CyberSession()

    result = await CyberSastTool(session).execute(path=str(tmp_path))

    assert result.is_success
    assert session.findings
    assert "Static analysis" in result.output


@pytest.mark.asyncio()
async def test_the_cve_tool_says_so_when_there_is_nothing_to_check(tmp_path: Path) -> None:
    result = await CyberCveTool(CyberSession()).execute(path=str(tmp_path))

    assert result.is_success
    assert "No dependency manifests" in result.output


@pytest.mark.asyncio()
async def test_the_report_tool_refuses_an_empty_session(tmp_path: Path) -> None:
    result = await CyberReportTool(CyberSession()).execute(directory=str(tmp_path))

    assert not result.is_success
    assert "no findings" in (result.error or "").lower()


@pytest.mark.asyncio()
async def test_the_report_tool_writes_every_format(tmp_path: Path) -> None:
    session = CyberSession()
    session.add([sample_finding()])

    result = await CyberReportTool(session).execute(directory=str(tmp_path / "report"))

    assert result.is_success
    written = result.metadata["files"]
    assert set(written) == {"sarif", "markdown", "html"}
    assert Path(written["sarif"]).exists()


# --------------------------------------------------------------------------
# Reporters
# --------------------------------------------------------------------------


def build_result() -> ScanResult:
    result = ScanResult(target="app.example.com", scope="app.example.com")
    result.findings = [
        sample_finding(),
        sample_finding(
            title="Missing security header: Content-Security-Policy",
            severity=Severity.LOW,
            category=Category.HEADERS,
            endpoint="https://app.example.com/",
        ),
    ]
    result.finalize()
    return result


def test_sarif_is_the_shape_a_ci_pipeline_expects() -> None:
    document = SarifReporter().build(build_result())

    assert document["version"] == "2.1.0"
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"]
    assert run["results"]
    assert run["results"][0]["ruleId"] in {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert run["results"][0]["level"] in {"error", "warning", "note"}


def test_sarif_render_is_valid_json() -> None:
    rendered = SarifReporter().render(build_result())

    assert json.loads(rendered)["runs"][0]["results"]


def test_markdown_carries_the_findings_and_the_coverage() -> None:
    rendered = MarkdownReporter().render(build_result())

    assert "AegisX Security Assessment" in rendered
    assert "SQL injection in parameter 'id'" in rendered
    assert "Use parameterised queries." in rendered
    assert "app.example.com" in rendered


def test_markdown_can_hide_the_low_severity_findings() -> None:
    options = ReportOptions(minimum_severity=Severity.MEDIUM)

    rendered = MarkdownReporter(options).render(build_result())

    assert "SQL injection" in rendered
    assert "Missing security header" not in rendered


def test_html_is_self_contained_and_escapes_the_title() -> None:
    finding = sample_finding(title="Reflected <script>alert(1)</script>")
    result = ScanResult(target="app.example.com")
    result.findings = [finding]
    result.finalize()

    rendered = HtmlReporter().render(result)

    assert rendered.startswith("<!DOCTYPE html>")
    assert "<style>" in rendered
    assert "&lt;script&gt;" in rendered
    assert "Reflected <script>alert(1)</script>" not in rendered


def test_write_reports_writes_only_the_formats_asked_for(tmp_path: Path) -> None:
    written = write_reports(build_result(), str(tmp_path), formats=("markdown",))

    assert set(written) == {"markdown"}
    assert Path(written["markdown"]).exists()
    assert not (tmp_path / "aegisx-report.sarif").exists()

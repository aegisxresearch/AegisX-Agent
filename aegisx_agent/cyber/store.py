"""Finding store — the single place findings accumulate, deduplicate, and persist.

Design decisions worth stating:

- **Dedupe by fingerprint, not by title.** A title changes when a wording
  changes; the fingerprint (category + target + endpoint + normalised
  title) does not. Re-scanning a target therefore updates the existing
  finding instead of appending a near-duplicate.
- **Never silently downgrade.** Merging a re-scan may raise a severity or
  upgrade a confidence, but a finding that was critical last week cannot
  quietly become informational today — that is how a real regression gets
  lost. A regression is reported separately, as :attr:`Store.regressions`.
- **Append-only JSONL on disk.** A scan is evidence. Truncating it to the
  newest state would destroy the record of what was true when.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegisx_agent.cyber.models import Category, Confidence, Finding, Severity


@dataclass
class StoreStats:
    """Counters describing what the store did."""

    added: int = 0
    updated: int = 0
    duplicates: int = 0
    regressions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class FindingStore:
    """In-memory index of findings, optionally backed by a JSONL file."""

    path: Path | None = None
    findings: dict[str, Finding] = field(default_factory=dict)
    stats: StoreStats = field(default_factory=StoreStats)
    #: Fingerprints seen in a previous run, with their severity, so a
    #: regression (a fixed finding coming back) can be detected.
    baseline: dict[str, Severity] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)

    # === Population ==================================================== #

    def add(self, finding: Finding) -> Finding:
        """Record a finding, merging it with an existing one if present.

        Returns the stored finding, which may be the pre-existing object
        with updated fields rather than the object passed in.
        """
        existing = self.findings.get(finding.fingerprint)
        if existing is None:
            self.findings[finding.fingerprint] = finding
            self.stats.added += 1
            self._append(finding)
            return finding

        self.stats.duplicates += 1
        previous_severity = existing.severity
        existing.merge(finding)
        if existing.severity.weight > previous_severity.weight:
            self.stats.updated += 1
        self._append(existing)
        return existing

    def extend(self, findings: list[Finding]) -> None:
        for finding in findings:
            self.add(finding)

    def remove(self, fingerprint: str) -> bool:
        """Drop a finding from the store (used when a fix is confirmed)."""
        return self.findings.pop(fingerprint, None) is not None

    # === Queries ======================================================= #

    def all(self) -> list[Finding]:
        """Every finding, worst first."""
        return sorted(
            self.findings.values(),
            key=lambda finding: (
                -finding.severity.weight,
                finding.title.lower(),
            ),
        )

    def by_severity(self, minimum: Severity) -> list[Finding]:
        """Findings at or above a severity threshold."""
        return [finding for finding in self.all() if finding.severity.weight >= minimum.weight]

    def by_category(self, category: Category) -> list[Finding]:
        return [finding for finding in self.all() if finding.category is category]

    def by_confidence(self, minimum: Confidence) -> list[Finding]:
        """Findings at or above a confidence threshold."""
        rank = {
            Confidence.CONFIRMED: 0,
            Confidence.PROBABLE: 1,
            Confidence.TENTATIVE: 2,
        }
        limit = rank[minimum]
        return [finding for finding in self.all() if rank[finding.confidence] <= limit]

    def count_by_severity(self) -> dict[str, int]:
        histogram: dict[str, int] = {}
        for finding in self.findings.values():
            histogram[finding.severity.value] = histogram.get(finding.severity.value, 0) + 1
        return histogram

    def summary(self) -> str:
        """One line describing the store's contents."""
        histogram = self.count_by_severity()
        parts = [
            f"{histogram.get(level, 0)} {level}"
            for level in ("critical", "high", "medium", "low", "info")
        ]
        return f"{len(self.findings)} findings ({', '.join(parts)})"

    # === Persistence =================================================== #

    def _append(self, finding: Finding) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(finding.to_dict(), ensure_ascii=False, default=str) + "\n")

    def save(self, path: Path | None = None) -> Path:
        """Write every finding to a JSON file (a snapshot, not a log)."""
        destination = path or self.path
        if destination is None:
            raise ValueError("no path configured for the finding store")
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "findings": [finding.to_dict() for finding in self.all()],
            "count_by_severity": self.count_by_severity(),
        }
        destination.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return destination

    def load_baseline(self, path: Path) -> int:
        """Load a previous scan's findings as the regression baseline.

        Returns the number of baseline findings loaded. A finding present
        in the baseline but *not* in the current scan is not automatically
        "fixed" — absence of evidence is not evidence of absence, so it is
        simply not reported as a regression either. What *is* reported is
        the opposite: a baseline finding that reappears at a higher
        severity.
        """
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        entries = payload.get("findings", []) if isinstance(payload, dict) else payload
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fingerprint = str(entry.get("fingerprint", ""))
            if not fingerprint:
                continue
            try:
                self.baseline[fingerprint] = Severity(entry.get("severity", "info"))
            except ValueError:
                continue
        return len(self.baseline)

    def detect_regressions(self) -> list[str]:
        """Findings that were present in the baseline and are back now.

        A regression is a baseline finding that is present again *and* at
        least as severe as before. It is called out separately because
        "the bug you fixed last month is back" is the single most
        important thing a re-scan can tell you.
        """
        self.regressions = []
        for fingerprint, finding in self.findings.items():
            previous = self.baseline.get(fingerprint)
            if previous is not None and finding.severity.weight >= previous.weight:
                self.regressions.append(fingerprint)
                self.stats.regressions += 1
        return self.regressions

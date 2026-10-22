"""DNS and cloud-storage checks — dangling records and open buckets.

Two failures that live outside the application's code but decide what
an attacker can do with it:

- **Subdomain takeover.** A CNAME points at a hosting provider, and
  the target no longer exists there. The record still resolves, so the
  name still looks legitimate — and whoever claims it at the provider
  inherits the domain's trust.
- **Open cloud storage.** When the authorised target *is* the bucket
  (``bucket.s3.amazonaws.com``), the listing API answers the same
  question the browser does: is this container readable by anyone?

Both checks stay inside the authorised scope. The provider host a
CNAME points at is never contacted — only the target itself is.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from aegisx_agent.cyber.checks import CheckContext, finding
from aegisx_agent.cyber.models import (
    Category,
    Confidence,
    Evidence,
    EvidenceKind,
    Finding,
    Severity,
)

#: Hosting providers whose dangling records are commonly claimable.
#: The value is what the provider says when the target is gone, which
#: is what separates a dangling record from a live one.
TAKEOVER_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("herokuapp.com", "No such app"),
    ("cloudfront.net", "The specified key does not exist"),
    ("s3.amazonaws.com", "NoSuchBucket"),
    ("elasticbeanstalk.com", "The specified location does not exist"),
    ("azurewebsites.net", "404 - Web site not found"),
    ("netlify.app", "Not Found"),
    ("vercel.app", "404 Not Found"),
    ("github.io", "404 Not Found"),
    ("pages.dev", "404 Not Found"),
    ("firebaseapp.com", "Not Found"),
    ("web.app", "Not Found"),
    ("surge.sh", "project not found"),
    ("myshopify.com", "Sorry, this shop is currently unavailable"),
    ("ghost.io", "The site does not exist"),
    ("pantheonsite.io", "The gods are wise, but do not know of the site"),
    ("wpengine.com", "The requested URL was not found"),
    ("readthedocs.io", "unknown to Read the Docs"),
)

#: Suffixes that mean the target host is itself a storage endpoint.
STORAGE_HOST_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".s3.amazonaws.com", "Amazon S3"),
    (".s3.us-east-1.amazonaws.com", "Amazon S3"),
    (".s3.eu-west-1.amazonaws.com", "Amazon S3"),
    (".blob.core.windows.net", "Azure Blob Storage"),
    ("storage.googleapis.com", "Google Cloud Storage"),
    (".r2.cloudflarestorage.com", "Cloudflare R2"),
    (".digitaloceanspaces.com", "DigitalOcean Spaces"),
)

#: Listing parameters per provider.
_LISTING_QUERY: dict[str, str] = {
    "s3": "list-type=2&max-keys=5",
    "azure": "restype=container&comp=list",
    "gcs": "prefix=",
}


def _storage_kind(host: str) -> str | None:
    """Which storage provider the host belongs to, if any."""
    for suffix, provider in STORAGE_HOST_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            if "s3" in provider or "Spaces" in provider or "R2" in provider:
                return "s3"
            if "Azure" in provider:
                return "azure"
            return "gcs"
    return None


def _canonical_name(host: str) -> str:
    """The canonical name a host resolves to, or ``""``."""
    try:
        name, _aliases, _addresses = socket.gethostbyname_ex(host)
    except (socket.gaierror, OSError):
        return ""
    return name.lower()


@dataclass
class SubdomainTakeoverCheck:
    """Detect a CNAME that points at a provider where the target is gone.

    The check resolves the target's own name, compares the canonical
    name against the provider list, and then asks the *target* whether
    it still serves anything. A provider's own "not found" page is the
    signal that the record is dangling.
    """

    name: str = "subdomain-takeover"
    category: Category = Category.RECON
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        found: list[Finding] = []
        hosts = [urlparse(context.base_url).hostname or context.host]
        for endpoint in context.endpoints[:10]:
            hostname = urlparse(endpoint).hostname
            if hostname and hostname not in hosts:
                hosts.append(hostname)

        for host in hosts[:6]:
            if not host:
                continue
            canonical = await asyncio.to_thread(_canonical_name, host)
            if not canonical:
                continue
            provider = self._provider_for(canonical)
            if provider is None:
                continue

            suffix, gone_message = provider
            response = await context.fetch(f"https://{host}/")
            if response.error:
                continue
            if response.status not in (404, 410):
                continue
            if gone_message.lower() not in response.body.lower():
                continue

            found.append(
                finding(
                    title=f"Dangling DNS record for '{host}' ({suffix})",
                    severity=Severity.HIGH,
                    category=self.category,
                    confidence=Confidence.CONFIRMED,
                    context=context,
                    endpoint=f"https://{host}/",
                    description=(
                        f"'{host}' resolves to '{canonical}', a host under "
                        f"{suffix}, but the provider reports that the target no "
                        "longer exists there. The DNS record is still in place, so "
                        "the name still resolves and still looks legitimate."
                    ),
                    impact=(
                        "Whoever claims the name at the provider inherits a "
                        "hostname under a domain that other systems trust — "
                        "cookie scoping, certificate issuance, allow-lists, and "
                        "OAuth redirect matching all follow the name, not the "
                        "infrastructure behind it."
                    ),
                    remediation=(
                        "Remove the DNS record, or restore the resource it points "
                        "at. Audit the domain for other names with the same "
                        "pattern, and monitor the provider for a resource "
                        "appearing under a name you no longer control."
                    ),
                    cwe="CWE-404",
                    owasp="A05:2021 - Security Misconfiguration",
                    cvss=7.4,
                    references=["https://cwe.mitre.org/data/definitions/404.html"],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.MATCH,
                            summary=f"CNAME points at {canonical} under {suffix}",
                            snippet=f"{host} → {canonical}",
                        ),
                        Evidence(
                            kind=EvidenceKind.RESPONSE,
                            summary="Provider reports the target no longer exists",
                            request=response.request_line(),
                            response_status=response.status,
                            snippet=response.body[:300],
                        ),
                    ],
                    source=self.name,
                    tags=["takeover", "dns", suffix],
                )
            )
        return found

    @staticmethod
    def _provider_for(canonical: str) -> tuple[str, str] | None:
        """The provider entry a canonical name falls under."""
        for suffix, gone_message in TAKEOVER_PROVIDERS:
            if canonical == suffix or canonical.endswith(f".{suffix}"):
                return (suffix, gone_message)
        return None


@dataclass
class CloudStorageCheck:
    """Detect a cloud container that lists itself to anyone.

    This only runs when the authorised target *is* the storage host.
    Buckets belonging to third parties are out of scope by definition,
    and this engine never widens its own scope to find one.
    """

    name: str = "cloud-storage"
    category: Category = Category.EXPOSURE
    cost: str = "deep"

    async def run(self, context: CheckContext) -> list[Finding]:
        host = urlparse(context.base_url).hostname or context.host
        kind = _storage_kind(host)
        if kind is None:
            return []

        query = _LISTING_QUERY.get(kind)
        if not query:
            return []

        url = f"{context.base_url.rstrip('/')}/?{query}"
        response = await context.fetch(url)
        if response.error or response.status != 200:
            return []
        if not self._is_listing(response.body):
            return []

        provider = next(name for suffix, name in STORAGE_HOST_SUFFIXES if host.endswith(suffix))
        return [
            finding(
                title=f"Public listing enabled on the {provider} container",
                severity=Severity.HIGH,
                category=self.category,
                confidence=Confidence.CONFIRMED,
                context=context,
                endpoint=url,
                description=(
                    f"The target is a {provider} container and its listing API "
                    "answered without credentials, returning the objects it holds."
                ),
                impact=(
                    "Every object in the container is enumerable, and depending on "
                    "the container's contents that may include backups, "
                    "credentials, or customer data. A container that lists but "
                    "refuses to read is still a directory of what exists."
                ),
                remediation=(
                    "Disable public listing on the container, and grant access "
                    "through signed URLs or an identity role instead of public "
                    "read. Review what the container already holds — a listing "
                    "that has been open is not closed by turning it off."
                ),
                cwe="CWE-732",
                owasp="A01:2021 - Broken Access Control",
                cvss=7.5,
                references=["https://cwe.mitre.org/data/definitions/732.html"],
                evidence=[
                    Evidence(
                        kind=EvidenceKind.RESPONSE,
                        summary="Container listing returned without credentials",
                        request=response.request_line(),
                        response_status=response.status,
                        snippet=response.body[:400],
                    )
                ],
                source=self.name,
                tags=["cloud", "storage", "bucket", "exposure"],
            )
        ]

    @staticmethod
    def _is_listing(body: str) -> bool:
        """Whether the body is a container listing rather than an error page."""
        lowered = body.lower()
        markers = ("<key>", "<contents>", "<name>", "<items>", "listbucketresult")
        if any(marker in lowered for marker in markers):
            return True
        return "accessdenied" not in lowered and "no such bucket" not in lowered

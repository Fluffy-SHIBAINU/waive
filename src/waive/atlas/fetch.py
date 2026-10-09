"""Direct downloads of policy PDFs that Tavily Extract cannot fetch (task 2.8c).

Hospitals often keep their policies on asset hosts (Baystate: baystatehealth.canto.com) where
Tavily Extract answers "Failed to fetch url". Fetching the file ourselves costs no credits.

The fetcher works for one hospital at a time and only ever requests that hospital's registered
domain or a known asset host (spec §11): redirects are followed by hand, each hop re-checked, a
literal or non-public address is never contacted, and a slow server cannot hold the stream past
a total deadline.
"""

import io
import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from waive.atlas.discover import host_of

USER_AGENT = "Waive/0.1 (hospital financial assistance atlas)"
MIN_TEXT_CHARS = 200
PDF_URL_MARKERS = (".pdf", "application%2fpdf", "application/pdf")
# Hospitals often keep their policy PDFs on a document/asset host rather than their own domain
# (Baystate: baystatehealth.canto.com, no .pdf in the URL). Such links are followed only when the
# label itself names the policy, so another organisation's documents are never fetched.
DOCUMENT_HOSTS = (
    "canto.com",
    "widen.net",
    "cloudfront.net",
    "amazonaws.com",
    "box.com",
    "sharepoint.com",
    "blob.core.windows.net",
)
MAX_HOPS = 3

log = logging.getLogger(__name__)
# Name resolution, replaceable so unit tests never touch DNS (tests/conftest.py).
resolve = socket.getaddrinfo


def looks_like_pdf(url: str, content_type: str) -> bool:
    """A PDF by Content-Type, or by a URL path/query that names one (asset hosts put the MIME
    type in the query string and serve the file as application/octet-stream)."""
    if "pdf" in content_type.lower():
        return True
    parts = urlparse(url)
    haystack = f"{parts.path}?{parts.query}".lower()
    return any(marker in haystack for marker in PDF_URL_MARKERS)


def is_asset_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOCUMENT_HOSTS)


def host_allowed_for(domain: str | None) -> Callable[[str], bool]:
    """Where the fetcher may go for one hospital: its registered domain (subdomains included) and
    the known asset hosts. With no domain, the asset hosts only."""
    base = host_of(f"https://www.{domain}") if domain else None

    def allowed(url: str) -> bool:
        return is_asset_host(url) or (base is not None and host_of(url) == base)

    return allowed


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _hop_allowed(url: str, allowed: Callable[[str], bool], resolver) -> bool:
    """One URL the fetcher is about to request: http(s), a name (never a literal address) on an
    allowed host, resolving only to public addresses. The resolve-then-connect gap is accepted:
    the allowlist already keeps the fetcher on hospital and asset hosts."""
    parts = urlparse(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    if not allowed(url):
        return False
    try:
        addresses = {info[4][0] for info in resolver(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(_is_public(address) for address in addresses)


def download_text(
    url: str,
    http: httpx.Client,
    *,
    allowed: Callable[[str], bool] | None = None,
    max_bytes: int = 15_000_000,
    timeout: float = 10.0,
    total_timeout: float = 60.0,
    max_hops: int = MAX_HOPS,
    resolver=None,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The text of the PDF at `url`, or None.

    None when any hop leaves the allowed hosts, when the body is HTML (Tavily handles pages), is
    not a PDF (judged on the final URL), exceeds `max_bytes`, outlasts `total_timeout`, yields
    fewer than MIN_TEXT_CHARS characters, cannot be parsed, or when the request fails. Never raises.
    """
    allowed = allowed or host_allowed_for(None)
    resolver = resolver or resolve
    deadline = clock() + total_timeout
    current = url
    try:
        for _hop in range(max_hops + 1):
            if clock() > deadline or not _hop_allowed(current, allowed, resolver):
                return None
            with http.stream(
                "GET",
                current,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.next_request is not None:  # a redirect: re-check the next hop
                    current = str(response.next_request.url)
                    continue
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "")
                if "html" in content_type.lower() or not looks_like_pdf(
                    str(response.url), content_type
                ):
                    return None
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return None
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes or clock() > deadline:
                        return None
            return pdf_text(bytes(body))
    except Exception as error:  # network, URL and stream errors alike: the caller has no recourse
        log.debug("download of %s failed: %s", url, type(error).__name__)
        return None
    return None  # too many hops


def pdf_text(body: bytes) -> str | None:
    """Page texts joined by blank lines; None when pypdf fails or the text is too short to use."""
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as error:
        log.debug("pdf parse failed: %s", type(error).__name__)
        return None
    text = "\n\n".join(page for page in pages if page)
    return text if len(text) >= MIN_TEXT_CHARS else None

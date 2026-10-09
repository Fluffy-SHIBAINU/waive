"""Direct downloads of policy documents that Tavily Extract cannot fetch or render (tasks 2.8c, 2.8h).

Hospitals often keep their policies on asset hosts (Baystate: baystatehealth.canto.com) where
Tavily Extract answers "Failed to fetch url", and some sites render as navigation only (Cape Cod
Hospital: 643 characters of menu). Fetching the file ourselves costs no credits; a PDF is parsed,
an HTML page (when the caller asks for pages) is reduced to its visible text and links.

The fetcher works for one hospital at a time and only ever requests that hospital's registered
domain or a known asset host (spec §11): redirects are followed by hand, each hop re-checked, a
literal or non-public address is never contacted, and a slow server cannot hold the stream past
a total deadline.
"""

import ipaddress
import logging
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from waive.atlas.discover import host_of
from waive.atlas.pdf_worker import MAX_PAGES, MAX_TEXT_CHARS

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
# Wall-clock bound on parsing one PDF (the worker is killed past it).
PDF_TIMEOUT_SECONDS = 20.0

log = logging.getLogger(__name__)
__all__ = ["MAX_PAGES", "MAX_TEXT_CHARS", "download_text", "html_text", "pdf_text"]
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
    allow_html: bool = False,
    max_bytes: int = 15_000_000,
    timeout: float = 10.0,
    total_timeout: float = 60.0,
    max_hops: int = MAX_HOPS,
    resolver=None,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The text of the PDF at `url`, or with `allow_html` of the PDF or web page there, or None.

    None when any hop leaves the allowed hosts, when the body is HTML and the caller did not ask
    for pages (Tavily handles them) or the URL promised a PDF (an error page, whatever its status),
    when a non-HTML body is not a PDF (judged on the final URL), exceeds `max_bytes`, outlasts
    `total_timeout`, yields fewer than MIN_TEXT_CHARS characters, cannot be parsed, or when the
    request fails. Never raises.
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
                final_url = str(response.url)
                is_html = "html" in content_type.lower()
                if is_html:
                    if not allow_html or looks_like_pdf(final_url, ""):
                        return None
                elif not looks_like_pdf(final_url, content_type):
                    return None
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return None
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes or clock() > deadline:
                        return None
                charset = response.charset_encoding
            if is_html:
                text = html_text(_decode(bytes(body), charset), base_url=final_url)
                return text if len(text) >= MIN_TEXT_CHARS else None
            return pdf_text(bytes(body))
    except Exception as error:  # network, URL and stream errors alike: the caller has no recourse
        log.debug("download of %s failed: %s", url, type(error).__name__)
        return None
    return None  # too many hops


def _decode(body: bytes, charset: str | None) -> str:
    """The body as text: the declared charset when it is one Python knows, else UTF-8; a stray
    byte never fails the page."""
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _TextCollector(HTMLParser):
    """Collects a page's visible text: script, style and the like are dropped, block elements
    start a new line, and links keep their target as markdown (`[label](href)`), the shape Tavily
    Extract renders them in, so the scout's link following works on downloaded pages too. Given
    the page's URL, targets are made absolute the way a browser would (the first `<base href>`
    counts; in-page anchors stay as written), since Tavily renders them absolute and the scout
    resolves whatever is left against the site root, not the page."""

    SKIPPED = frozenset({"script", "style", "noscript", "template", "svg"})
    BLOCKS = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl",
            "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
            "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
            "summary", "table", "tbody", "td", "tfoot", "th", "thead", "title", "tr", "ul",
        }
    )  # fmt: skip

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self._links: list[str | None] = []
        self._base = base_url
        self._base_element_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIPPED:
            self._skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag == "base" and self._base is not None and not self._base_element_seen:
            href = _href(attrs)
            if href is not None:
                self._base = self._resolve(href)
                self._base_element_seen = True
        elif tag == "a" and not self._skipping:
            href = _href(attrs)
            self._links.append(None if href is None else self._resolve(href))
            if href is not None:
                self.parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag == "a" and self._links:
            href = self._links.pop()
            if href is not None:
                self.parts.append(f"]({href})")

    def _resolve(self, href: str) -> str:
        if self._base is None or href.startswith("#"):
            return href
        try:
            return urljoin(self._base, href)
        except ValueError:  # a malformed target (a bad IPv6 literal); kept as written
            return href

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        if self._links and self._links[-1] is not None:
            data = data.replace("\r", " ").replace("\n", " ")  # a label stays on its line
        self.parts.append(data)


def _href(attrs: list[tuple[str, str | None]]) -> str | None:
    """A tag's non-empty href with its whitespace collapsed, or None."""
    value = next((value for name, value in attrs if name == "href" and value), None)
    return " ".join(value.split()) if value else None


def html_text(markup: str, base_url: str | None = None) -> str:
    """The visible text of an HTML page, one line per block, whitespace collapsed, links as
    markdown (absolute when `base_url`, the page's own URL, is given), at most MAX_TEXT_CHARS
    characters (the PDF cap)."""
    collector = _TextCollector(base_url)
    collector.feed(markup)
    collector.close()
    lines = (" ".join(line.split()) for line in "".join(collector.parts).splitlines())
    return "\n".join(line for line in lines if line)[:MAX_TEXT_CHARS]


def pdf_text(body: bytes) -> str | None:
    """Page texts joined by blank lines (at most MAX_PAGES pages and MAX_TEXT_CHARS characters);
    None when the file fails to parse, takes longer than PDF_TIMEOUT_SECONDS, or yields too little
    text to use. Parsed in a child process (waive.atlas.pdf_worker): a crafted or merely huge PDF
    must not stall the scheduler thread or exhaust the web process's memory."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "waive.atlas.pdf_worker"],
            input=body,
            capture_output=True,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.debug("pdf parse gave up after %s seconds", PDF_TIMEOUT_SECONDS)
        return None
    if done.returncode != 0:
        log.debug("pdf parse failed in the worker")
        return None
    text = done.stdout.decode("utf-8", errors="replace")
    return text if len(text) >= MIN_TEXT_CHARS else None

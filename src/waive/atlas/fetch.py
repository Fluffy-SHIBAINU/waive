"""Direct downloads of policy PDFs that Tavily Extract cannot fetch (task 2.8c).

Hospitals often keep their policies on asset hosts (Baystate: baystatehealth.canto.com) where
Tavily Extract answers "Failed to fetch url". Fetching the file ourselves costs no credits.
"""

import io
import logging
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

USER_AGENT = "Waive/0.1 (hospital financial assistance atlas)"
MIN_TEXT_CHARS = 200
PDF_URL_MARKERS = (".pdf", "application%2fpdf", "application/pdf")

log = logging.getLogger(__name__)


def looks_like_pdf(url: str, content_type: str) -> bool:
    """A PDF by Content-Type, or by a URL path/query that names one (asset hosts put the MIME
    type in the query string and serve the file as application/octet-stream)."""
    if "pdf" in content_type.lower():
        return True
    parts = urlparse(url)
    haystack = f"{parts.path}?{parts.query}".lower()
    return any(marker in haystack for marker in PDF_URL_MARKERS)


def download_text(
    url: str, http: httpx.Client, *, max_bytes: int = 15_000_000, timeout: float = 30.0
) -> str | None:
    """The text of the PDF at `url`, or None.

    None when the body is HTML (Tavily handles pages), is not a PDF, exceeds `max_bytes`, yields
    fewer than MIN_TEXT_CHARS characters, cannot be parsed, or when the request fails. Never raises.
    """
    try:
        with http.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "")
            if "html" in content_type.lower() or not looks_like_pdf(url, content_type):
                return None
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return None
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
    except Exception as error:  # network, URL and stream errors alike: the caller has no recourse
        log.debug("download of %s failed: %s", url, type(error).__name__)
        return None
    return pdf_text(bytes(body))


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

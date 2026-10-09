import io
import time
import zlib

import httpx
import respx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from waive.atlas import fetch
from waive.atlas.fetch import (
    MAX_PAGES,
    MAX_TEXT_CHARS,
    USER_AGENT,
    download_text,
    host_allowed_for,
    html_text,
    is_asset_host,
    pdf_text,
)
from waive.atlas.samples import SAMPLE_POLICY_TEXT

CANTO_URL = (
    "https://h.canto.com/direct/document/abc/def/original"
    "?content-type=application%2Fpdf&name=FAP+Policy.pdf"
)
# The fetcher works for one hospital at a time: its registered domain plus the asset hosts.
EXAMPLE_ORG = host_allowed_for("example.org")


def sample_pdf(text: str = SAMPLE_POLICY_TEXT) -> bytes:
    """A real PDF with the sample policy, one line per drawString."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    y = 750
    for line in text.splitlines():
        pdf.drawString(36, y, line)
        y -= 16
    pdf.save()
    return buffer.getvalue()


def pdf_response(body: bytes, content_type: str = "application/pdf") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": content_type})


def pdf_with_shared_stream(pages: int, content: bytes) -> bytes:
    """`pages` pages that all draw the same FlateDecode stream: pypdf re-inflates it for every
    page, so a few kilobytes cost minutes and megabytes unless pages and text are capped."""
    stream = zlib.compress(content)
    kids = " ".join(f"{5 + i} 0 R" for i in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode()
        + stream
        + b"\nendstream",
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
    ] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def text_lines(count: int) -> bytes:
    return "".join(
        f"BT /F1 12 Tf 36 {700 - (i % 40) * 16} Td (financial assistance policy line {i}) Tj ET\n"
        for i in range(count)
    ).encode()


def test_pdf_text_caps_pages_and_characters_so_a_shared_stream_bomb_stays_cheap():
    bomb = pdf_with_shared_stream(400, text_lines(600))  # 400 pages x ~27k characters each
    assert len(bomb) < 100_000
    started = time.monotonic()
    text = pdf_text(bomb)
    assert time.monotonic() - started < 10
    assert text is not None and "policy line 0" in text
    assert len(text) <= MAX_TEXT_CHARS == 60_000
    blank = pdf_with_shared_stream(2000, b"")
    assert pdf_text(blank) is None
    assert MAX_PAGES == 60


def test_pdf_parsing_runs_in_a_child_process_with_a_deadline(monkeypatch):
    pdf = sample_pdf()
    assert pdf_text(pdf) is not None
    monkeypatch.setattr(fetch, "PDF_TIMEOUT_SECONDS", 0.001)
    assert pdf_text(pdf) is None


@respx.mock
def test_download_text_parses_a_pdf_served_by_an_asset_host():
    route = respx.get(CANTO_URL).mock(return_value=pdf_response(sample_pdf()))
    with httpx.Client() as http:
        text = download_text(CANTO_URL, http)
    assert text is not None
    assert "250% of the Federal Poverty" in text
    assert "extraordinary collection actions" in text
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("Waive/")


@respx.mock
def test_download_text_trusts_the_url_when_the_content_type_is_generic():
    respx.get(CANTO_URL).mock(
        return_value=pdf_response(sample_pdf(), content_type="application/octet-stream")
    )
    with httpx.Client() as http:
        text = download_text(CANTO_URL, http)
    assert text is not None and "250% of the Federal Poverty" in text


@respx.mock
def test_download_text_follows_redirects_to_allowed_hosts():
    respx.get("https://www.example.org/fap.pdf").mock(
        return_value=httpx.Response(302, headers={"location": CANTO_URL})
    )
    respx.get(CANTO_URL).mock(return_value=pdf_response(sample_pdf()))
    with httpx.Client(follow_redirects=True) as http:  # the client's default is overridden
        text = download_text("https://www.example.org/fap.pdf", http, allowed=EXAMPLE_ORG)
        assert text is not None and "250% of the Federal Poverty" in text
        # Without the hospital's domain the fetcher may only start on an asset host.
        assert download_text("https://www.example.org/fap.pdf", http) is None


def test_host_allowed_for_covers_the_registered_domain_and_the_asset_hosts():
    assert EXAMPLE_ORG("https://www.example.org/fap.pdf")
    assert EXAMPLE_ORG("https://billing.example.org/docs/fap.pdf")
    assert EXAMPLE_ORG(CANTO_URL) and is_asset_host(CANTO_URL)
    assert not EXAMPLE_ORG("https://example.org.evil.net/fap.pdf")
    assert not EXAMPLE_ORG("https://evil.net/example.org/fap.pdf")
    anywhere = host_allowed_for(None)
    assert anywhere(CANTO_URL) and not anywhere("https://www.example.org/fap.pdf")


@respx.mock
def test_redirects_off_the_allowed_hosts_are_never_followed():
    """A hospital page, or a claimable bucket on an asset host, may redirect anywhere. The
    fetcher stays on the allowed hosts and never requests a private or link-local address."""
    for target in (
        "http://169.254.169.254/latest/internal-report.pdf",
        "http://10.0.0.5/financial-assistance-policy.pdf",
        "http://localhost:8000/admin",
        "https://evil.example.net/financial-assistance-policy.pdf",
        "https://pay.example.org.evil.net/fap.pdf",
    ):
        respx.get("https://www.example.org/fap.pdf").mock(
            return_value=httpx.Response(302, headers={"location": target})
        )
        leaked = respx.get(target).mock(return_value=pdf_response(sample_pdf()))
        with httpx.Client() as http:
            assert download_text("https://www.example.org/fap.pdf", http, allowed=EXAMPLE_ORG) is (
                None
            ), target
        assert not leaked.called, target


@respx.mock
def test_redirect_chains_stop_after_three_hops():
    for n in range(5):
        respx.get(f"https://www.example.org/hop{n}.pdf").mock(
            return_value=httpx.Response(
                302, headers={"location": f"https://www.example.org/hop{n + 1}.pdf"}
            )
        )
    final = respx.get("https://www.example.org/hop5.pdf").mock(
        return_value=pdf_response(sample_pdf())
    )
    with httpx.Client() as http:
        assert download_text("https://www.example.org/hop0.pdf", http, allowed=EXAMPLE_ORG) is None
        assert not final.called
        assert (
            download_text("https://www.example.org/hop2.pdf", http, allowed=EXAMPLE_ORG) is not None
        )


@respx.mock
def test_the_pdf_check_applies_to_the_final_url_not_the_first():
    respx.get("https://www.example.org/fap.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.example.org/report"})
    )
    respx.get("https://www.example.org/report").mock(
        return_value=pdf_response(sample_pdf(), content_type="application/octet-stream")
    )
    with httpx.Client() as http:
        assert download_text("https://www.example.org/fap.pdf", http, allowed=EXAMPLE_ORG) is None


@respx.mock
def test_names_that_resolve_to_private_addresses_and_literal_addresses_are_refused():
    route = respx.get(CANTO_URL).mock(return_value=pdf_response(sample_pdf()))
    respx.get("https://169.254.169.254/fap.pdf").mock(return_value=pdf_response(sample_pdf()))

    def private(host, port=None):
        return [(None, None, None, "", ("10.1.2.3", 0))]

    def mapped(host, port=None):
        return [(None, None, None, "", ("::ffff:10.1.2.3", 0, 0, 0))]

    def failing(host, port=None):
        raise OSError("no such host")

    with httpx.Client() as http:
        for resolver in (private, mapped, failing):
            assert download_text(CANTO_URL, http, resolver=resolver) is None
        assert not route.called
        assert download_text("https://169.254.169.254/fap.pdf", http, allowed=lambda u: True) is (
            None
        )
        assert download_text(CANTO_URL, http) is not None  # the test stub resolves publicly


@respx.mock
def test_a_slow_server_cannot_hold_the_download_past_the_deadline():
    respx.get(CANTO_URL).mock(return_value=pdf_response(sample_pdf()))
    # The clock is read when the deadline is set, before the hop, then per body chunk: the jump
    # lands while the body streams.
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    with httpx.Client() as http:
        assert download_text(CANTO_URL, http, total_timeout=60.0, clock=lambda: next(ticks)) is None
        assert download_text(CANTO_URL, http) is not None


@respx.mock
def test_download_text_leaves_html_to_tavily():
    respx.get("https://www.example.org/fap.pdf").mock(
        return_value=httpx.Response(
            200,
            text="<html>" + SAMPLE_POLICY_TEXT + "</html>",
            headers={"content-type": "text/html"},
        )
    )
    with httpx.Client() as http:
        assert download_text("https://www.example.org/fap.pdf", http) is None


@respx.mock
def test_download_text_gives_up_on_oversized_bodies():
    body = sample_pdf()
    respx.get(CANTO_URL).mock(return_value=pdf_response(body))
    with httpx.Client() as http:
        assert download_text(CANTO_URL, http, max_bytes=len(body) - 1) is None
        assert download_text(CANTO_URL, http, max_bytes=len(body)) is not None


@respx.mock
def test_download_text_returns_none_for_errors_and_broken_pdfs():
    respx.get("https://www.example.org/down.pdf").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://www.example.org/missing.pdf").mock(return_value=httpx.Response(404))
    respx.get("https://www.example.org/broken.pdf").mock(
        return_value=pdf_response(b"%PDF-1.4 this is not really a pdf")
    )
    respx.get("https://www.example.org/stub.pdf").mock(
        return_value=pdf_response(sample_pdf("Financial Assistance Policy. See website."))
    )
    respx.get("https://www.example.org/page").mock(
        return_value=httpx.Response(200, content=b"plain", headers={"content-type": "text/plain"})
    )
    with httpx.Client() as http:
        assert download_text("https://www.example.org/down.pdf", http) is None
        assert download_text("https://www.example.org/missing.pdf", http) is None
        assert download_text("https://www.example.org/broken.pdf", http) is None
        assert download_text("https://www.example.org/stub.pdf", http) is None
        assert download_text("https://www.example.org/page", http) is None
        assert download_text("not a url", http) is None


# A server-rendered policy page as a browser would receive it: navigation, inline script and
# style (dropped), the policy prose and a link to the PDF (kept as a markdown link).
HTML_PAGE = (
    "<!doctype html><html><head><title>Financial Assistance</title>"
    "<style>.nav{display:none}</style>"
    '<script type="application/ld+json">{"@type": "Hospital", "name": "ignored"}</script></head>'
    "<body><nav><ul><li><a href='/our-providers/'>Find a doctor</a></li>"
    '<li><a href="#SetLocation">Set   Your\n Location</a></li></ul></nav>'
    "<main><h1>Financial   Assistance</h1>"
    f"<p>{SAMPLE_POLICY_TEXT}</p>"
    '<p>Read the <a href="/docs/fap.pdf">Financial Assistance Policy (PDF)</a> or '
    "<a href='tel:6175550100'>call us</a>.<br/>Se habla espa&ntilde;ol.</p>"
    "<svg><title>decorative</title></svg><noscript>Enable JavaScript</noscript>"
    "<script>window.dataLayer = [];</script></main></body></html>"
)


def test_html_text_keeps_visible_text_and_links_and_drops_scripts_and_styles():
    text = html_text(HTML_PAGE)
    assert text.startswith("Financial Assistance\n")
    assert "[Find a doctor](/our-providers/)" in text
    assert "[Set Your Location](#SetLocation)" in text
    assert "[Financial Assistance Policy (PDF)](/docs/fap.pdf)" in text
    assert "[call us](tel:6175550100)" in text
    assert "250% of the Federal Poverty" in text
    assert "Se habla español." in text
    for dropped in ("dataLayer", "display:none", "ignored", "decorative", "Enable JavaScript"):
        assert dropped not in text
    assert "  " not in text and "\n\n" not in text
    assert len(html_text("<p>" + "x" * 70_000 + "</p>")) == MAX_TEXT_CHARS
    assert html_text("<p>unclosed <a href='/x'>anchor") == "unclosed [anchor"


def test_html_text_resolves_links_against_the_page_and_its_base_element():
    # Tavily renders links absolute; a downloaded page has them as written, and the scout resolves
    # links against the site root, so page-relative targets must be made absolute here (2.8h review).
    page = "https://patients.example.org/paying-for-care/financial-assistance/"
    markup = (
        '<p><a href="docs/fap.pdf">Policy</a> <a href="../apply.pdf">Apply</a> '
        '<a href="/summary.pdf">Summary</a> <a href="#top">Top</a> <a href="tel:1">Call</a></p>'
    )
    assert html_text(markup, base_url=page) == (
        "[Policy](https://patients.example.org/paying-for-care/financial-assistance/docs/fap.pdf) "
        "[Apply](https://patients.example.org/paying-for-care/apply.pdf) "
        "[Summary](https://patients.example.org/summary.pdf) [Top](#top) [Call](tel:1)"
    )
    based = '<head><base href="https://cdn.example.org/files/"></head><body>' + markup
    text = html_text(based, base_url=page)
    assert "[Policy](https://cdn.example.org/files/docs/fap.pdf)" in text
    assert "[Summary](https://cdn.example.org/summary.pdf)" in text
    relative_base = '<base href="/files/">' + markup
    assert "[Policy](https://patients.example.org/files/docs/fap.pdf)" in html_text(
        relative_base, base_url=page
    )
    # Without a page URL the targets stay as written.
    assert html_text(markup) == (
        "[Policy](docs/fap.pdf) [Apply](../apply.pdf) [Summary](/summary.pdf) [Top](#top) [Call](tel:1)"
    )


@respx.mock
def test_download_text_strips_html_only_when_asked():
    url = "https://www.example.org/patients/financial-assistance"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=HTML_PAGE.replace("espa&ntilde;ol", "español").encode("latin-1"),
            headers={"content-type": "text/html; charset=iso-8859-1"},
        )
    )
    with httpx.Client() as http:
        assert download_text(url, http, allowed=EXAMPLE_ORG) is None  # pages are Tavily's job
        text = download_text(url, http, allowed=EXAMPLE_ORG, allow_html=True)
    assert text is not None
    assert "250% of the Federal Poverty" in text and "Se habla español." in text
    assert "dataLayer" not in text
    # Link targets are resolved against the page's URL, as Tavily renders them.
    assert "[Financial Assistance Policy (PDF)](https://www.example.org/docs/fap.pdf)" in text


@respx.mock
def test_html_is_refused_where_a_pdf_was_promised_when_thin_or_off_the_allowed_hosts():
    page = httpx.Response(200, text=HTML_PAGE, headers={"content-type": "text/html"})
    respx.get("https://www.example.org/fap.pdf").mock(return_value=page)
    respx.get(CANTO_URL).mock(return_value=page)
    respx.get("https://www.example.org/thin").mock(
        return_value=httpx.Response(
            200, text="<html><body><p>Page not found</p></body></html>", headers=page.headers
        )
    )
    leaked = respx.get("https://www.otherhospital.org/financial-assistance").mock(return_value=page)
    with httpx.Client() as http:
        for url in ("https://www.example.org/fap.pdf", CANTO_URL, "https://www.example.org/thin"):
            assert download_text(url, http, allowed=EXAMPLE_ORG, allow_html=True) is None, url
        assert (
            download_text(
                "https://www.otherhospital.org/financial-assistance",
                http,
                allowed=EXAMPLE_ORG,
                allow_html=True,
            )
            is None
        )
    assert not leaked.called


def test_sitecore_and_webflow_asset_hosts_may_serve_policy_pdfs():
    # Alta Bates Summit (050043) and Adirondack (330079), 7.9: the policy PDFs sit on
    # edge.sitecorecloud.io and cdn.prod.website-files.com, so policy_links dropped them.
    for url in (
        "https://edge.sitecorecloud.io/sutterhealth/media/fap-application.pdf",
        "https://cdn.prod.website-files.com/5f3/Financial%20Assistance%20Policy%20Summary.pdf",
    ):
        assert is_asset_host(url) and EXAMPLE_ORG(url), url
    assert not is_asset_host("https://sitecorecloud.io.evil.net/x.pdf")

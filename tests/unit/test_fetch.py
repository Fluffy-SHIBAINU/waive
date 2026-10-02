import io

import httpx
import respx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from waive.atlas.fetch import USER_AGENT, download_text
from waive.atlas.samples import SAMPLE_POLICY_TEXT

CANTO_URL = (
    "https://h.canto.com/direct/document/abc/def/original"
    "?content-type=application%2Fpdf&name=FAP+Policy.pdf"
)


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
def test_download_text_follows_redirects():
    respx.get("https://www.example.org/fap.pdf").mock(
        return_value=httpx.Response(302, headers={"location": CANTO_URL})
    )
    respx.get(CANTO_URL).mock(return_value=pdf_response(sample_pdf()))
    with httpx.Client() as http:
        text = download_text("https://www.example.org/fap.pdf", http)
    assert text is not None and "250% of the Federal Poverty" in text


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

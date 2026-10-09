import io

import httpx
import respx
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from waive.atlas.fetch import USER_AGENT, download_text, host_allowed_for, is_asset_host
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

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
import respx

from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import session_scope
from waive.demo import seed_demo, write_demo_images
from waive.gallery import (
    CHROME_CANDIDATES,
    KROKI_URL,
    LAPTOP,
    PHONE,
    ScriptedAI,
    Shot,
    ShotPlan,
    capture,
    chrome_command,
    export_mermaid,
    find_chrome,
    frame_target,
    html_target,
    mermaid_blocks,
    plan_shots,
)

from tests.unit.test_web_app import make_client

BASE = "http://testserver"
EXPECTED_SHOTS = [
    "01-home-phone",
    "02-two-links-laptop",
    "03-senior-start-phone",
    "04-readback-phone",
    "05-household-phone",
    "06-income-phone",
    "07-result-phone",
    "08-caregiver-review-laptop",
    "09-caregiver-approved-laptop",
    "10-atlas-list-laptop",
    "11-atlas-sheet-laptop",
    "12-metrics-laptop",
]


def demo_client(tmp_path):
    client, engine = make_client(ai=ScriptedAI())
    with session_scope(engine) as session:
        seed_demo(session)
        publish_sheet(session, st_example_sheet())
    write_demo_images(tmp_path / "demo")
    bill = (tmp_path / "demo" / "bill.jpg").read_bytes()
    letter = (tmp_path / "demo" / "letter.jpg").read_bytes()
    return client, bill, letter


def test_plan_shots_walks_both_flows_and_every_get_page_renders(tmp_path):
    client, bill, letter = demo_client(tmp_path)
    plan = plan_shots(client, BASE, bill=bill, letter=letter, atlas_ccn="229999")
    assert [shot.name for shot in plan.shots] == EXPECTED_SHOTS
    assert plan.skipped == {}
    for shot in plan.shots:
        assert (shot.url is None) != (shot.html is None), shot.name
        if shot.url:
            assert shot.url.startswith(BASE + "/")
            assert client.get(shot.url.removeprefix(BASE)).status_code == 200, shot.name
    by_name = {shot.name: shot for shot in plan.shots}
    assert "Two links" in by_name["02-two-links-laptop"].html
    assert "Take a photo of the bill" in by_name["03-senior-start-phone"].html
    assert "$1,850.00" in by_name["04-readback-phone"].html
    assert "ST. EXAMPLE MEDICAL CENTER" in by_name["04-readback-phone"].html
    result = client.get(by_name["07-result-phone"].url.removeprefix(BASE))
    assert "likely do not have to pay" in result.text
    review = by_name["08-caregiver-review-laptop"].html
    # The pre-approval page already contains the word "Approved" (the by-hand outcome form's
    # "Approved: free care" option), so the assertion is on the approval banner.
    assert "Likely free care" in review and "250%" in review
    assert "Approved. Print the packet" not in review
    approved = client.get(by_name["09-caregiver-approved-laptop"].url.removeprefix(BASE))
    assert "Approved. Print the packet" in approved.text
    assert by_name["01-home-phone"].size == PHONE and by_name["01-home-phone"].scale == 2
    assert by_name["10-atlas-list-laptop"].size == LAPTOP
    assert by_name["10-atlas-list-laptop"].scale == 1
    assert plan.files["packet-sample.pdf"][:5] == b"%PDF-"


def test_plan_shots_skips_the_sheet_shot_when_the_hospital_has_no_sheet(tmp_path):
    client, bill, letter = demo_client(tmp_path)
    plan = plan_shots(client, BASE, bill=bill, letter=letter, atlas_ccn="220031")
    assert "11-atlas-sheet-laptop" in plan.skipped
    assert len(plan.shots) == len(EXPECTED_SHOTS) - 1


def test_find_chrome_prefers_the_explicit_path_and_tolerates_none(tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("")
    assert find_chrome(str(fake)) == str(fake)
    found = find_chrome(str(tmp_path / "missing"))
    assert found is None or Path(found).exists() or found in CHROME_CANDIDATES


def test_chrome_command_sets_size_scale_and_output(tmp_path):
    out = tmp_path / "x.png"
    cmd = chrome_command("/bin/chrome", "http://127.0.0.1:8765/", out, PHONE, 2)
    assert cmd[0] == "/bin/chrome" and cmd[-1] == "http://127.0.0.1:8765/"
    assert "--headless=new" in cmd and "--window-size=390,844" in cmd
    assert "--force-device-scale-factor=2" in cmd
    assert f"--screenshot={out.resolve()}" in cmd


def test_html_target_writes_the_page_with_a_base_href(tmp_path):
    target = html_target(
        "<html><head><title>x</title></head><body></body></html>",
        "http://127.0.0.1:8765",
        tmp_path / "p.html",
    )
    assert target.startswith("file://") and target.endswith("p.html")
    assert '<head><base href="http://127.0.0.1:8765/">' in (tmp_path / "p.html").read_text()


def test_frame_target_wraps_the_page_in_an_iframe_of_the_phone_size(tmp_path):
    target = frame_target("http://127.0.0.1:8765/s/abc", PHONE, tmp_path / "f.html")
    assert target.startswith("file://") and target.endswith("f.html")
    page = (tmp_path / "f.html").read_text()
    assert '<iframe src="http://127.0.0.1:8765/s/abc" width="390" height="844"' in page


def test_capture_runs_chrome_per_shot_writes_files_and_removes_token_bearing_html(tmp_path):
    calls = []
    opened = []  # what Chrome would have read: the URL, or the content of a file:// target

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        target = cmd[-1]
        if target.startswith("file://"):
            opened.append(Path(url2pathname(urlparse(target).path)).read_text())
        else:
            opened.append(target)
        out = next(arg for arg in cmd if arg.startswith("--screenshot="))
        Path(out.removeprefix("--screenshot=")).write_bytes(b"\x89PNG\r\n\x1a\n")

    plan = ShotPlan(
        shots=[
            Shot("a", PHONE, url="http://x/"),
            Shot("b", LAPTOP, html="<html><head></head><body>hi</body></html>", scale=1),
        ],
        skipped={},
        files={"packet-sample.pdf": b"%PDF-1.4"},
    )
    written = capture(plan, "/bin/chrome", tmp_path / "out", "http://x", runner=fake_run)
    assert [path.name for path in written] == ["a.png", "b.png", "packet-sample.pdf"]
    assert len(calls) == 2
    # Chrome refuses a window narrower than 500 CSS px, so a phone shot is framed in an iframe
    # of the phone's size; a laptop-sized page opens directly.
    assert calls[0][-1].startswith("file://") and calls[0][-1].endswith("a-frame.html")
    assert 'src="http://x/"' in opened[0] and 'width="390" height="844"' in opened[0]
    assert calls[1][-1].startswith("file://") and calls[1][-1].endswith("b.html")
    assert "hi" in opened[1] and "<iframe" not in opened[1]
    assert "--force-device-scale-factor=1" in calls[1]
    assert not (tmp_path / "out" / "_html").exists()
    assert (tmp_path / "out" / "packet-sample.pdf").read_bytes() == b"%PDF-1.4"


@respx.mock
def test_export_mermaid_posts_each_readme_diagram_to_kroki(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "# x\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\ntext\n\n"
        "```mermaid\nflowchart TD\n  C --> D\n```\n"
    )
    assert mermaid_blocks(readme.read_text()) == [
        "flowchart LR\n  A --> B\n",
        "flowchart TD\n  C --> D\n",
    ]
    route = respx.post(KROKI_URL).mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")
    )
    with httpx.Client() as http:
        written = export_mermaid(readme, tmp_path / "out", http)
    assert [path.name for path in written] == ["diagram-01.png", "diagram-02.png"]
    assert route.call_count == 2
    assert route.calls[0].request.content == b"flowchart LR\n  A --> B\n"
    assert route.calls[0].request.headers["content-type"] == "text/plain"
    assert (tmp_path / "out" / "diagram-02.png").read_bytes().startswith(b"\x89PNG")

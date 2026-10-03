"""Repeatable pictures for the Devpost gallery (Phase 8.5).

One demo case is driven through both flows in-process with scripted extraction output (the demo
bill's ground truth), the app is served on a local port, and headless Chrome — already on the
machine, no new Python dependency — photographs each page at phone or laptop size. Pages that
exist only as POST responses (the "Two links" page, the read-back) or that change as the case
advances (the senior start page, the review page before approval) are captured from their HTML
with a <base href> so the stylesheet still loads. Chrome will not open a window narrower than
500 CSS px, so phone-sized shots are framed in an <iframe> of the phone's exact size. README
diagrams are exported through kroki.io. Spends nothing unless `live=True` (then the real vision
model reads the two demo images).
"""

import html
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from waive.ai.client import AIClient
from waive.cases.extract import BillExtract, IncomeExtract
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.demo import DEMO_BILL, DEMO_DIR, DEMO_MONTHLY_BENEFIT, seed_demo, write_demo_images
from waive.governor import make_governor
from waive.web.app import create_app

PHONE = (390, 844)  # iPhone 14 CSS pixels; captured at 2x
LAPTOP = (1280, 800)
TALL = (1280, 1800)  # a whole procedure sheet
# Headless Chrome (new mode, 154 checked) clamps --window-size to its minimum window width and
# then crops the screenshot, so anything narrower is laid out inside an iframe of the right size.
CHROME_MIN_WIDTH = 500
DEFAULT_PORT = 8765
KROKI_URL = "https://kroki.io/mermaid/png"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)
_MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


class GalleryError(RuntimeError):
    pass


class ScriptedAI:
    """Answers the two vision calls of the demo flow with the demo bill's ground truth, so a
    gallery run spends nothing and always shows the same screens. Any other schema is an error:
    the flow changed and this script must be updated."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        if schema is BillExtract:
            return BillExtract(
                hospital_name=DEMO_BILL.hospital_name,
                hospital_phone=DEMO_BILL.hospital_phone,
                fap_phone=DEMO_BILL.fap_phone,
                fap_url=DEMO_BILL.fap_url,
                statement_date=DEMO_BILL.statement_date,
                amount_due=DEMO_BILL.amount_due,
                confidence=0.95,
            )
        if schema is IncomeExtract:
            return IncomeExtract(monthly_benefit=DEMO_MONTHLY_BENEFIT)
        raise GalleryError(f"the gallery script does not know how to answer {schema.__name__}")


@dataclass(frozen=True)
class Shot:
    name: str
    size: tuple[int, int]
    url: str | None = None  # a page Chrome can GET
    html: str | None = None  # a page that exists only as a POST response
    scale: int = 2


@dataclass
class ShotPlan:
    shots: list[Shot]
    skipped: dict[str, str] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)


def _link(text: str, prefix: str) -> str:
    match = re.search(rf'href="({re.escape(prefix)}[^"]+)"', text)
    if match is None:
        raise GalleryError(f"no {prefix} link on the page")
    return match.group(1)


def plan_shots(
    client, base_url: str, *, bill: bytes, letter: bytes, atlas_ccn: str = "220031"
) -> ShotPlan:
    """Drive one case through the senior and caregiver flows with `client` (FastAPI's TestClient
    or an httpx.Client with base_url — same methods) and return the pages to photograph."""
    plan = ShotPlan(shots=[])
    plan.shots.append(Shot("01-home-phone", PHONE, url=f"{base_url}/"))
    created = client.post("/cases", data={"state": "MA"})
    senior = _link(created.text, "/s/")
    caregiver = _link(created.text, "/c/")
    plan.shots.append(Shot("02-two-links-laptop", LAPTOP, html=created.text, scale=1))
    # GET /s/{token} shows the result once the case is evaluated, so the start page is kept as
    # it looks now; Chrome only runs after the whole flow has been driven.
    plan.shots.append(Shot("03-senior-start-phone", PHONE, html=client.get(senior).text))
    readback = client.post(f"{senior}/bill", files={"photo": ("bill.jpg", bill, "image/jpeg")})
    if readback.status_code != 200 or "Here is what we read" not in readback.text:
        raise GalleryError(
            "the bill was not read; with --live this usually means the zero-data-retention "
            "check refused the photo (see the demo script's note on WAIVE_REQUIRE_ZDR)"
        )
    plan.shots.append(Shot("04-readback-phone", PHONE, html=readback.text))
    client.post(f"{senior}/confirm", data={"answer": "yes"})
    plan.shots.append(Shot("05-household-phone", PHONE, url=f"{base_url}{senior}/household"))
    client.post(f"{senior}/household", data={"size": "1", "programs": "none"})
    plan.shots.append(Shot("06-income-phone", PHONE, url=f"{base_url}{senior}/income"))
    client.post(f"{senior}/income", files={"photo": ("letter.jpg", letter, "image/jpeg")})
    plan.shots.append(Shot("07-result-phone", PHONE, url=f"{base_url}{senior}/result"))
    # The review page changes once approved (shot 09), so the pre-approval view is kept as HTML.
    plan.shots.append(
        Shot("08-caregiver-review-laptop", LAPTOP, html=client.get(caregiver).text, scale=1)
    )
    client.post(f"{caregiver}/approve")
    plan.shots.append(
        Shot("09-caregiver-approved-laptop", LAPTOP, url=f"{base_url}{caregiver}", scale=1)
    )
    plan.files["packet-sample.pdf"] = client.get(f"{caregiver}/packet.pdf").content
    plan.shots.append(Shot("10-atlas-list-laptop", LAPTOP, url=f"{base_url}/atlas", scale=1))
    if client.get(f"/atlas/{atlas_ccn}").status_code == 200:
        plan.shots.append(
            Shot("11-atlas-sheet-laptop", TALL, url=f"{base_url}/atlas/{atlas_ccn}", scale=1)
        )
    else:
        plan.skipped["11-atlas-sheet-laptop"] = (
            f"no published sheet for CCN {atlas_ccn} in this database (pass --atlas-ccn)"
        )
    plan.shots.append(Shot("12-metrics-laptop", LAPTOP, url=f"{base_url}/metrics", scale=1))
    return plan


def find_chrome(explicit: str | None = None) -> str | None:
    for candidate in ([explicit] if explicit else []) + list(CHROME_CANDIDATES):
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def chrome_command(
    chrome: str, target: str, out: Path, size: tuple[int, int], scale: int
) -> list[str]:
    width, height = size
    return [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=2000",
        f"--screenshot={out.resolve()}",
        target,
    ]


def html_target(html: str, base_url: str, path: Path) -> str:
    """Write a POST response to disk so Chrome can open it; <base href> keeps /static/ working."""
    path.parent.mkdir(parents=True, exist_ok=True)
    page = html.replace("<head>", f'<head><base href="{base_url.rstrip("/")}/">', 1)
    path.write_text(page, encoding="utf-8")
    return path.resolve().as_uri()


def frame_target(target: str, size: tuple[int, int], path: Path) -> str:
    """A wrapper page whose only content is an <iframe> of exactly `size` showing `target`, so a
    phone-sized page is laid out at phone width even though Chrome's window cannot be that
    narrow. The screenshot is cropped to `size`, so only the frame is in the picture."""
    width, height = size
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head><style>html,body{margin:0}</style></head><body>"
        f'<iframe src="{html.escape(target, quote=True)}" width="{width}" height="{height}" '
        'style="border:0;display:block"></iframe></body></html>',
        encoding="utf-8",
    )
    return path.resolve().as_uri()


def capture(
    plan: ShotPlan,
    chrome: str,
    out_dir: Path,
    base_url: str,
    runner: Callable[..., object] = subprocess.run,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_dir = out_dir / "_html"
    written: list[Path] = []
    try:
        for shot in plan.shots:
            target = shot.url or html_target(
                shot.html or "", base_url, html_dir / f"{shot.name}.html"
            )
            if shot.size[0] < CHROME_MIN_WIDTH:
                target = frame_target(target, shot.size, html_dir / f"{shot.name}-frame.html")
            out = out_dir / f"{shot.name}.png"
            runner(
                chrome_command(chrome, target, out, shot.size, shot.scale),
                check=True,
                capture_output=True,
                timeout=90,
            )
            written.append(out)
    finally:
        # The HTML copies carry the demo case's capability tokens; they never stay on disk.
        shutil.rmtree(html_dir, ignore_errors=True)
    for name, content in plan.files.items():
        path = out_dir / name
        path.write_bytes(content)
        written.append(path)
    return written


def mermaid_blocks(markdown: str) -> list[str]:
    return _MERMAID.findall(markdown)


def export_mermaid(readme: Path, out_dir: Path, http: httpx.Client) -> list[Path]:
    """PNG for every ```mermaid block in the README, in order, via kroki.io (public diagram text
    is all that leaves the machine). Fallback by hand: paste the block into https://mermaid.live."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, block in enumerate(mermaid_blocks(readme.read_text(encoding="utf-8")), start=1):
        response = http.post(
            KROKI_URL, content=block.encode("utf-8"), headers={"Content-Type": "text/plain"}
        )
        if response.status_code != 200:
            raise GalleryError(f"kroki.io answered {response.status_code} for diagram {index}")
        path = out_dir / f"diagram-{index:02d}.png"
        path.write_bytes(response.content)
        written.append(path)
    return written


@contextmanager
def serving(app: FastAPI, port: int) -> Iterator[str]:
    """Serve `app` on 127.0.0.1 in a daemon thread until the block ends. uvicorn (0.54 here)
    installs its signal handlers only on the main thread, so running `Server.run` in a thread
    needs no override."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise GalleryError(f"the app did not start on port {port}")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run_gallery(
    settings: Settings,
    out_dir: Path,
    *,
    chrome: str | None = None,
    live: bool = False,
    port: int = DEFAULT_PORT,
    atlas_ccn: str = "220031",
    diagrams: bool = True,
) -> tuple[list[Path], dict[str, str]]:
    browser = find_chrome(chrome)
    if browser is None:
        raise GalleryError(
            "no Chrome or Chromium found; pass --chrome /path/to/binary, or photograph the "
            "pages by hand with the orchestrator's browser (see docs/devpost/gallery/README.md)"
        )
    engine = make_engine(settings.database_url)
    init_db(engine)
    with session_scope(engine) as session:
        seed_demo(session)
    write_demo_images(DEMO_DIR)
    bill = (DEMO_DIR / "bill.jpg").read_bytes()
    letter = (DEMO_DIR / "letter.jpg").read_bytes()
    ai = AIClient(settings, make_governor(settings, engine)) if live else ScriptedAI()
    app = create_app(settings, engine=engine, ai=ai)
    with (
        serving(app, port) as base_url,
        httpx.Client(base_url=base_url, timeout=120.0) as client,
    ):
        plan = plan_shots(client, base_url, bill=bill, letter=letter, atlas_ccn=atlas_ccn)
        written = capture(plan, browser, out_dir, base_url)
    if diagrams:
        with httpx.Client(timeout=60.0) as http:
            written += export_mermaid(Path("README.md"), out_dir, http)
    return written, plan.skipped

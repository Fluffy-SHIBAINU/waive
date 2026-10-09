"""Text of a PDF, bounded. Run as a child process by `waive.atlas.fetch.pdf_text`
(`python -m waive.atlas.pdf_worker`, PDF bytes on stdin, UTF-8 text on stdout) so a pathological
file costs at most a deadline: pure-Python tokenising cannot be interrupted from a thread, and
pypdf re-inflates a shared content stream for every page that uses it."""

import io
import sys

import pypdf
from pypdf import PdfReader

MAX_PAGES = 60
# What the scout keeps of a document (scout.MAX_CHARS); reading further is wasted work.
MAX_TEXT_CHARS = 60_000
# Per-stream inflation limit, down from pypdf's 75 MB: a policy page is a few hundred kilobytes.
MAX_STREAM_BYTES = 10_000_000


def extract_text(
    body: bytes, *, max_pages: int = MAX_PAGES, max_chars: int = MAX_TEXT_CHARS
) -> str:
    """Page texts joined by blank lines: at most `max_pages` pages, stopping once `max_chars`
    characters are in hand, so page count and text volume are both bounded."""
    reader = PdfReader(io.BytesIO(body))
    pages: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        if index >= max_pages or total >= max_chars:
            break
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
            total += len(text)
    return "\n\n".join(pages)[:max_chars]


def main() -> int:
    body = sys.stdin.buffer.read()
    try:
        with pypdf.apply_configuration(zlib_maximum_output_length=MAX_STREAM_BYTES):
            text = extract_text(body)
    except Exception:  # a broken or hostile file: the parent treats a failure as "no text"
        return 1
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

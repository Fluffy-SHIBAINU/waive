# Devpost gallery

Captured with `uv run waive demo gallery` from the repository root (headless Chrome, scripted
extraction output equal to the demo bill's ground truth; nothing paid). Re-run after any UI
change. The colour scheme is pinned to dark by `chrome_command` in `src/waive/gallery.py`
(`--blink-settings=preferredColorScheme=0`; `=1` is light), so a re-run on any machine takes the
same pictures whatever its appearance setting. Pages the senior cannot reach once the caregiver
has approved (household, income) are kept as HTML at the moment the senior sees them, like the
start page and the read-back. Upload the PNGs to Devpost in this order with these captions.
`packet-sample.pdf` is the caregiver's packet for the demo case, kept as a sample (it carries the
same first-bill note as the review page until the date is confirmed); the repository README links
it under "Screenshots", and it is not uploaded to the gallery.

| File | Caption |
|---|---|
| `diagram-01.png` | One Python service (deployment target: a Nebius AI Cloud CPU Serverless AI endpoint with Managed PostgreSQL; SQLite locally); NVIDIA Nemotron 3 Super, Nano and 3.5 Lightning plus MiniCPM-V 4.5 on Nebius Token Factory; Tavily Search, Extract and Map for the web |
| `07-result-phone.png` | The senior's answer in one sentence, large print, with a Read-this-to-me button |
| `04-readback-phone.png` | The bill read back for a Yes / Something-is-wrong confirmation |
| `03-senior-start-phone.png` | One button per screen: take a photo of the bill (camera only) |
| `05-household-phone.png` | Big-button household question |
| `06-income-phone.png` | Income from a photo of the Social Security letter, or skip |
| `08-caregiver-review-laptop.png` | The caregiver's review: what was read, the result with the exact policy quote, the dates that matter — with the note that they count from the photographed statement until the caregiver enters the first bill's date (below the frame: that date field, "Make new links", which retires both links, and delete) |
| `09-caregiver-approved-laptop.png` | Approved: the packet is ready to print, have signed and send |
| `02-two-links-laptop.png` | Two links: one for the person with the bill, one for the helper |
| `11-atlas-sheet-laptop.png` | A published procedure sheet: every field with its quote, source and date; versioned (the version number is on the page, the diffs in the admin console) |
| `10-atlas-list-laptop.png` | The public atlas |
| `12-metrics-laptop.png` | Atlas metrics: registry size, published and held sheets, core fields documented, coverage by state |
| `diagram-02.png` | How a sheet is built: Tavily → Nemotron 3 Super draft → Nemotron 3 Nano cross-check → quote verification → publish → refresh by content hash |
| `01-home-phone.png` | The home page: Waive never asks for money |

*(Replacement caption for `diagram-01.png` once Phase 6 task 6.8 closes and the README's
deployment status line says live: "One Python service on Nebius AI Cloud (CPU Serverless AI
endpoint + Managed PostgreSQL); NVIDIA Nemotron 3 Super, Nano and 3.5 Lightning plus MiniCPM-V
4.5 on Nebius Token Factory; Tavily Search, Extract and Map for the web".)*

If Chrome is not available, the same pages can be photographed with the orchestrator's browser
pane: run `uv run waive serve`, create a case at http://localhost:8000, follow the senior and
caregiver links in a 390×844 viewport, and save each screenshot under the names above.

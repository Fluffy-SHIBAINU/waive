# Devpost gallery

Captured with `uv run waive demo gallery` (headless Chrome, scripted extraction output equal to
the demo bill's ground truth; nothing paid). Re-run after any UI change. Headless Chrome follows
the Mac's light or dark appearance (these were taken in dark mode); change System Settings >
Appearance before re-running for the other look. Upload the PNGs to Devpost in this order with
these captions; `packet-sample.pdf` is for the README, not the gallery.

| File | Caption |
|---|---|
| `diagram-01.png` | One Python service on Nebius AI Cloud; NVIDIA Nemotron 3 Super, Nano and 3.5 Lightning plus MiniCPM-V 4.5 on Nebius Token Factory; Tavily Search, Extract and Map for the web |
| `07-result-phone.png` | The senior's answer in one sentence, large print, with a Read-this-to-me button |
| `04-readback-phone.png` | The bill read back for a Yes / Something-is-wrong confirmation |
| `03-senior-start-phone.png` | One button per screen: take a photo of the bill (camera only) |
| `05-household-phone.png` | Big-button household question |
| `06-income-phone.png` | Income from a photo of the Social Security letter, or skip |
| `08-caregiver-review-laptop.png` | The caregiver's review: what was read, the result with the exact policy quote, the dates that matter |
| `09-caregiver-approved-laptop.png` | Approved: download the packet and the calendar reminders |
| `02-two-links-laptop.png` | Two links: one for the person with the bill, one for the helper |
| `11-atlas-sheet-laptop.png` | A published procedure sheet: every field with its quote, source and date; versioned (the version number is on the page, the diffs in the admin console) |
| `10-atlas-list-laptop.png` | The public atlas |
| `12-metrics-laptop.png` | Coverage by state and Tavily credits per day |
| `diagram-02.png` | How a sheet is built: Tavily → Nemotron 3 Super draft → Nemotron 3 Nano cross-check → quote verification → publish → refresh by content hash |
| `01-home-phone.png` | The home page: Waive never asks for money |

If Chrome is not available, the same pages can be photographed with the orchestrator's browser
pane: run `uv run waive serve`, create a case at http://localhost:8000, follow the senior and
caregiver links in a 390×844 viewport, and save each screenshot under the names above.

# Waive — demo video script and shot list

Target length **2:50** (hard limit 3:00; Devpost rejects longer videos). Nine shots, narration
about 450 words at a calm 2.6 words per second. Record the phone shots first, the laptop shots
second, the two title cards last; cut in iMovie; export 1080p; upload to YouTube as **Public**
(gate U8.2).

**Deployment status (update this line when Phase 6 task 6.8 closes):** the recording runs
against `uv run waive serve` on the laptop, with the phone on the same Wi-Fi. *(Once the Nebius
endpoint is live, record the phone shots against the public URL instead and say variant A in shot 8.)*

## Before recording (10 minutes)

1. `uv run waive demo reset` — deletes every case, resets St. Example, writes `var/demo/bill.jpg`,
   `var/demo/bill.json`, `var/demo/letter.jpg`.
2. Zero data retention: the web flow sends photos with `phi=True`. If gate U0.4 is still open, set
   `WAIVE_REQUIRE_ZDR=false` in `.env` **for this session only** (synthetic images only, as in the
   U4.1 phone test) and set it back to `true` right after recording. If U0.4 is closed
   (`WAIVE_ZDR_CONFIRMED=true`), change nothing.
3. `uv run waive serve`. On the laptop open http://localhost:8000 and http://localhost:8000/atlas
   in two browser tabs; find the laptop's IP with `ipconfig getifaddr en0`.
4. Phone: open `http://<ip>:8000` once to warm the connection; Settings → Display → text size at
   default; Do Not Disturb on; battery above 50 %.
5. Laptop screen: `open var/demo/bill.jpg` and `open var/demo/letter.jpg` in Preview, full
   screen, so the phone can photograph them in shot 3 and shot 4. Good light, no glare.
6. QuickTime Player → File → New Movie Recording → camera source: the iPhone (USB) → record the
   phone screen. Separately, QuickTime → New Screen Recording for the laptop shots.
7. Terminal for shot 7: a clean window, font size 18, `cd` into the repo. Shot 7 runs one real
   Nemotron build (`--reuse-sources`: stored documents only, no Tavily, about one cent of Token
   Factory); it takes 30–90 seconds, so record the whole run and cut the wait in iMovie, keeping
   the command line and the result table on screen. Record shot 7 after shot 6.

## Shot list

| # | Time | Seconds | On screen | Action | Narration (read at a calm pace) |
|---|---|---|---|---|---|
| 1 | 0:00–0:17 | 17 | Title card: "Waive — free or discounted hospital care you're owed, from a photo of the bill." Below it, small: "Nebius x NVIDIA Global AI Hackathon · Personal AI" | Hold the card; fade to shot 2 | "Rosa is seventy-four. She lives on nineteen hundred dollars a month from Social Security. An ER visit leaves her a bill for eighteen hundred and fifty dollars. By law her nonprofit hospital must forgive bills like hers — if she applies. Most people never do." |
| 2 | 0:17–0:32 | 15 | Laptop: home page "Hospital bills you may not have to pay" → "Two links" page | Click *Start a case*; on "Two links" click *Share this link* (or hover the senior link) | "Her daughter Ana lives in another state. Ana opens Waive, starts a case, and gets two links: one for Rosa, one for herself. She shares Rosa's link by text. No app to install, no account, no typing for Rosa." |
| 3 | 0:32–0:49 | 17 | Phone: "Let's look at your hospital bill" → camera → "Here is what we read" | Tap *Take a photo of the bill*; photograph `bill.jpg` on the laptop screen; the read-back shows St. Example Medical Center, $1,850.00, September 3, 2026; tap *Yes, that's right* | "Rosa taps the link on her phone. One button: take a photo of the bill. Waive reads it with a vision model on Nebius Token Factory and reads it back in large print: the hospital, eighteen fifty, September third. Rosa taps 'Yes, that's right'." |
| 4 | 0:49–1:09 | 20 | Phone: "How many people live in your home, counting you?" → "Do you have your Social Security letter?" → "What we found" | Tap *1*, *No, or not sure*, *Next*; tap *Take a photo of the letter*; photograph `letter.jpg`; the result reads "Good news. You likely do not have to pay this bill."; tap *Read this to me* and let it speak for two seconds | "Two big-button questions: how many people at home, any MassHealth or SNAP. Then a photo of her Social Security letter instead of typing her income. Waive checks the hospital's own policy: at one hundred forty-three percent of the poverty line, Rosa likely owes nothing. She can have it read aloud." |
| 5 | 1:09–1:32 | 23 | Laptop: caregiver "Review" page → approved → packet PDF | Open the caregiver link; scroll "What we read from the bill", "Result" ("Likely free care", "Policy says … 250%"), "Dates that matter"; click *Approve: prepare the packet*; click *Download the packet (PDF)*; show page 1 (cover letter) and the checklist page | "Ana's review page shows the same result with the exact quote from the policy — free care up to two hundred fifty percent — the day one-twenty collections protection and the day two-forty application deadline. She approves, and Waive prints the packet: a cover letter citing the policy, the data sheet, the document checklist, and where to mail it." |
| 6 | 1:32–1:59 | 27 | Laptop: `/atlas` list → `/atlas/220031` (Boston Medical Center) → `/metrics` | Scroll the list; open Boston Medical Center; scroll a field: the quote sits under each value with its source link and date; scroll to "Sources"; open `/metrics` and show the MA row and the national total | "Behind the app is an open atlas. For each hospital, Tavily finds the official site and scouts the financial assistance policy, the application and the collections policy. Nemotron 3 Super turns them into a procedure sheet; Nemotron 3 Nano cross-checks it; every field keeps an exact quote that must verify against the source, or it is dropped. Twenty-seven of forty-six Massachusetts hospitals are published, out of twenty-seven hundred nonprofit hospitals seeded nationally." |
| 7 | 1:59–2:21 | 22 | Terminal: `uv run waive atlas build --state MA --ccn 220031 --reuse-sources` (Nemotron Super + Nano on stored documents, about $0.01, no Tavily) then `uv run waive atlas schedule --dry-run` (free) | Run the build; cut the wait so the result table shows `220031 · BOSTON MEDICAL CENTER · published · <version>` and the "Spend so far" line; run the dry run and let the queue table and the "Daily budget" line show | "Here Nemotron 3 Super and Nemotron 3 Nano rebuild Boston Medical Center's sheet live from stored documents — a new version only if a value changed. Scouting runs on a priority queue inside a daily credit budget. And when a hospital's real decision contradicts a sheet, Waive re-scouts, versions it, and after enough cases flags the hospital." |
| 8 | 2:21–2:40 | 19 | Architecture card: `docs/devpost/gallery/diagram-01.png` | Hold the diagram; a highlight moves from the phone to Token Factory to Tavily | Variant A (deployed): "One Python service: FastAPI on Nebius AI Cloud, NVIDIA Nemotron models and MiniCPM-V on Nebius Token Factory with zero data retention, Tavily for the web. Photos never touch disk, personal fields are encrypted, links are scoped and revocable, and one tap deletes everything. Every budget has a hard cap." — Variant B (not deployed): replace the first clause with "One Python service: FastAPI, built for Nebius AI Cloud, with NVIDIA Nemotron models and MiniCPM-V on Nebius Token Factory …" |
| 9 | 2:40–2:50 | 10 | Closing card: repository URL, "Code Apache-2.0 · Atlas data CC BY 4.0 · Waive never asks for money" | Hold; end | "Waive never asks for money. The code is Apache-2.0, the atlas is CC BY 4.0. Rosa keeps her eighteen hundred fifty dollars." |

Total: **2:50**.

## Honesty notes for the narration

- Shot 3 says "a vision model", not "an NVIDIA model": the vision model is `openbmb/MiniCPM-V-4_5`
  because Token Factory offers no NVIDIA vision model. The NVIDIA models are the three Nemotron
  models in shot 6.
- Shot 6's numbers (27 of 46; 2,703 seeded) are from `docs/reports/atlas-ma.md` and
  `docs/reports/atlas-national.md`; the 2,703 includes the 46 Massachusetts hospitals, so the
  narration says "out of", not "more". If more hospitals publish before recording, update the
  narration and this file in the same commit.
- Shot 7 is the one live NVIDIA model call in the video: `--reuse-sources` re-structures Boston
  Medical Center from stored documents with Nemotron 3 Super and Nemotron 3 Nano (no Tavily, about
  one cent). The Outcome column normally reads `published` with the same or a new version; if it
  reads `held` (the two models disagreed on a critical field this run), run the command once more
  — every run is kept in the sheet's version history either way.
- Shot 8: use variant A only when the public URL exists and the phone shots were recorded against
  it; otherwise variant B.
- Everything on screen is synthetic: St. Example is fictional, Rosa is fictional, the bill and the
  letter are generated images. Real hospitals appear only through their published policies.

## Title cards (make them in Keynote, 1920×1080, export PNG)

- Card 1: "Waive" (large) — "Free or discounted hospital care you're owed, from a photo of the
  bill." — "Nebius x NVIDIA Global AI Hackathon · Personal AI track".
- Card 9: repository URL `[USER FILLS: repository URL]` — "Code: Apache-2.0 · Atlas data: CC BY 4.0"
  — "Waive never asks for money, card numbers or bank logins."

## Recording, cutting and uploading

1. Record each shot as its own clip; re-take rather than trim awkward pauses.
2. iMovie → new project → drop the clips in shot order → record the narration with
   *Voiceover* per clip, or read it live while recording (quieter rooms give better audio).
3. Trim to the times above. Check the total: File → Share → File (1080p, Better quality). Then
   `mdls -name kMDItemDurationSeconds ~/Movies/waive-demo.mp4` — must print less than 180.
4. Watch it once with the sound off (does every screen read?) and once with the sound on.
5. Upload to YouTube: title "Waive — free or discounted hospital care from a photo of the bill
   (Nebius x NVIDIA hackathon)", visibility **Public**, description = the first paragraph of
   README.md plus the repository URL. Copy the URL into `README.md` and
   `docs/devpost/submission.md` (the `[USER FILLS: YouTube URL …]` fields).
6. Set `WAIVE_REQUIRE_ZDR=true` back in `.env` if it was changed, and run
   `uv run waive demo reset` so the recording's case is gone.

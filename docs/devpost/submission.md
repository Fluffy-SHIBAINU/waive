# Devpost submission — Waive

Copy each section into the matching Devpost field. Fields marked `[USER FILLS: …]` are the
user's; everything else is final text. Numbers are as of 2026-10-09 (see README "Status,
honestly"); refresh them on submission day from `docs/reports/` and the usage ledger.

- **Project name:** Waive
- **Tagline (60 characters max):** Hospital bills forgiven, from a photo — built for seniors
- **Longer tagline, if the field allows 80+:** Free or discounted hospital care you're owed, from a photo of the bill.
- **Track:** Personal AI
- **Side prize opt-in:** Best Use of Tavily
- **Team:** solo
- **Repository (public):** https://github.com/Fluffy-SHIBAINU/waive
- **Demo video (YouTube, public, under 3 minutes):** [USER FILLS: YouTube URL after gate U8.2]
- **Devpost project (fill in after submitting):** [USER FILLS: Devpost URL after gate U8.3]
- **Try it out:** **Deployment status (update when Phase 6 task 6.8 closes):** not deployed yet — the repository runs locally in eight commands (README → Setup). *(Once live: the public URL on Nebius AI Cloud, running during judging windows.)*
- **Gallery:** the PNGs in `docs/devpost/gallery/`, in file order; captions in `docs/devpost/gallery/README.md`

## Inspiration

Nonprofit hospitals are 58 % of US community hospitals, and the law (IRS §501(r)) makes them publish a financial assistance policy, cap charges for people who qualify, and print the policy's phone number on every bill. Eligible people get billed anyway — KFF Health News found 45 % of nonprofit hospitals doing it, and Dollar For estimates at least $14 billion a year in charity care goes unclaimed. The people most likely to qualify are the least likely to apply: seniors on fixed incomes who cannot find the PDF, read the income table, fill the form or keep the 240-day deadline.

Rosa is 74 and lives on $1,900 a month from Social Security. After an ER visit she gets a bill for $1,850. Her daughter Ana lives in another state. Waive is what we wished Ana could text her: one link, one photo, a plain answer, and a packet Ana can approve from her own phone.

## What it does

**A senior's flow, on the phone's browser, with no account and no typing.** Tap *Take a photo of the bill*. Waive reads the hospital, the amount and the statement date and reads them back in large print (*Yes, that's right* / *Something is wrong*). Two big-button questions (household size; MassHealth or SNAP), then a photo of the Social Security benefit letter instead of typing an income. The result is one sentence — "Good news. You likely do not have to pay this bill." — with a *Read this to me* button.

**A caregiver's flow.** A review page with what was read, the result and the exact quote from the hospital's policy that it rests on, the day-120 (collections) and day-240 (application window) dates, corrections for anything misread, *Approve*, and the packet: a cover letter citing the policy section, the data sheet, a document checklist, mailing or fax instructions, and `.ics` reminders. One tap deletes the case and every personal field.

**An open, cited atlas.** One versioned *procedure sheet* per nonprofit hospital: who qualifies (free care up to X % of the poverty line, discount tiers, presumptive programs) and exactly how to apply (documents, where to send them, the window, how long a decision takes). Every documented field carries an exact quote from a dated source document; a field without a verifiable quote is not published. Published as CC BY 4.0 JSON and as web pages with quotes, sources and a version number (the diff between versions is kept and shown in the admin console).

**A learning loop.** Photos of the hospital's decision letters become outcomes. An outcome that contradicts the sheet triggers a re-scout, a new version after review, and, after enough distinct cases, an accountability flag for a hospital that denies people its own policy says qualify. Patients' photos of public documents fill gaps after an automated personal-information check and admin review. Only enums, income bands and one-way hashes are stored.

**Where it stands (2026-10-09, honestly):** 46 Massachusetts nonprofit acute-care and critical-access hospitals in the registry, 28 published sheets (61 %), 16 held, 2 without documents; 2,703 hospitals seeded nationally from CMS, 65 published so far after the first national batch across CA, NY, TX, FL, PA, IL, OH and NJ (300 Tavily credits a day); bill reading on 30 synthetic bills: hospital name 100 %, statement date 97 %, amount due 100 %; no real bills processed yet — the Token Factory project runs with zero data retention, confirmed 2026-10-09 by the project owner, so real bills are now accepted; until that day photos with personal data were refused by design. Spend so far: 699 Tavily credits, $2.33 of Token Factory.

## How we built it

One Python service (FastAPI, server-rendered pages, plain CSS sized for older eyes and thumbs: 20 px base text, 56 px buttons, one action per screen) with an in-process scheduler, a Typer CLI for the same pipelines, SQLite locally and PostgreSQL in production, packaged as a two-stage non-root Docker image for a Nebius AI Cloud CPU Serverless AI endpoint.

**Nebius Token Factory is the only model API we call**, through the OpenAI SDK pointed at `api.tokenfactory.nebius.com`. Four models, four roles:

- `nvidia/nemotron-3-super-120b-a12b` (NVIDIA Nemotron 3 Super) turns a hospital's policy documents into a procedure-sheet draft with one exact quote per field.
- `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` (NVIDIA Nemotron 3 Nano) extracts the critical fields a second time; a disagreement counts only when the cross-check's own quote verifies against the source.
- `nvidia/Nemotron-3_5-Lightning` (NVIDIA Nemotron 3.5 Lightning) is the tie-break when Super and Nano disagree.
- `openbmb/MiniCPM-V-4_5` reads the photos (bill, benefit letter, hospital letters). It is not an NVIDIA model: Token Factory offers no NVIDIA vision model today, and we say so rather than imply otherwise. The role is one setting.

**Tavily does all of the web work**: Search to find each hospital's official website (directory sites rejected; the page title naming the hospital or the CMS phone number counts as evidence, anything weaker is flagged for review), Search with `include_domains` plus Map as a fallback to find the policy, application, summary and collections pages, Extract for HTML and PDF text (with a direct download fallback when Extract returns only navigation), and Extract again on a schedule to re-hash documents and re-structure only what changed — on a priority queue (staleness × case demand × (1 − accuracy)) inside a daily credit budget.

**Everything that must be right is deterministic**: quote verification (normalised substring check, value must appear in the quote), the 2026 poverty guidelines, eligibility tiers, the 120/240-day deadlines, hospital matching (rapidfuzz on name, plus phone, the policy web address printed on the bill and the city in the bill's address), the packet. The models only propose; the verifier decides.

**Privacy by construction**: every model call flagged as personal data raises whenever `WAIVE_ZDR_CONFIRMED` is false (it is true: the Token Factory project runs with zero data retention, confirmed 2026-10-09 by the project owner); photos live in memory only; personal fields are AES-GCM encrypted; access is by signed, scoped, revocable links; logs are filtered; the learning tables hold only enums, bands and hashes and an audit command proves it.

**Spend by construction**: every Tavily and Token Factory call passes a governor with hard caps and writes a ledger line; the scheduler adds a daily budget. From the ledger, as of 2026-10-09: the whole Massachusetts atlas, including every debugging re-run and re-scout, cost 438 credits and $1.52 of Token Factory; the first national batch of about 50 hospitals cost 261 credits and $0.64; the 30-bill vision evaluation $0.08; connectivity checks and model probes about $0.09 — $2.33 and 699 credits in all.

Built in a single day plus a self-paced build loop with Claude Code: a design spec, a master plan, and one detailed plan per phase, executed one task per iteration with tests first (280+ tests, none opens a network socket).

## Challenges we ran into

- **Reasoning models and JSON.** Nemotron 3 Super's thinking mode consumed the output budget before any JSON appeared; `enable_thinking: false` plus `response_format: json_object` and the JSON schema written into the prompt fixed it. On very long prompts Super sometimes answered a bare `{}` in JSON mode; one retry without JSON mode fixed that.
- **Cross-checks that hurt.** Nemotron 3 Nano is fast and cheap but sometimes "disagreed" with a three-character quote that could never verify, holding good sheets. The rule that saved the atlas: a cross-check disagreement counts only when the cross-check's own quote verifies. Four hospitals published the day we added it (Massachusetts General, Brigham and Women's Faulkner, Newton-Wellesley, MetroWest).
- **Policies that hide.** Tavily's search index does not always hold a hospital's financial-assistance PDF; Map found pages the index missed, Extract's Markdown links let us follow "Financial Assistance Policy (PDF)" links from entry pages, and a plain download with `pypdf` handled PDFs on asset hosts. Hospital systems (Baystate, Mass General Brigham, Beth Israel Lahey) share documents across facilities, so we store each document once by SHA-256 and link it to every hospital.
- **Policies the schema cannot hold, and pages that were not the policy.** Berkshire Medical Center's sliding scale was held until the tier parser learned percentage ranges and missing bounds; Beth Israel Deaconess Needham's discount tiers still do not parse; Milford Regional's policy URLs now answer with the site's navigation menu (its site moved into UMass Memorial Health), so no policy text is stored and the sheet stays held until the system policy is scouted; Harrington Hospital's discovered "site" was a billing directory whose pages describe hospitals in Georgia and Texas, so its sheet was withdrawn to held and the domain cleared; Cape Cod and Falmouth define eligibility through the state's Health Safety Net (0–300 % of the poverty line for HSN-eligible services) rather than an income limit of their own; and for the other eleven held hospitals (Holyoke, Lahey Burlington, Marlborough, MelroseWakefield, Mercy, Nantucket, Northeast/Beverly, South Shore, Sturdy, UMass Memorial HealthAlliance and UMass Memorial University Campus) the structurer found no income rule it could hold in the reachable text. Those 16 sheets are held, visibly, rather than guessed.
- **Zero data retention.** The Token Factory docs do not describe the switch; Nebius's HIPAA page says it must be enabled for the Token Factory scope. We built the app to refuse personal photos while `WAIVE_ZDR_CONFIRMED` is false, and built and evaluated everything on synthetic bills and letters. The project owner confirmed zero data retention for the project on 2026-10-09 (gate U0.4 closed), so real bills are now accepted; the first one is still ahead of us.

## Accomplishments that we're proud of

- 28 Massachusetts hospitals with published, cited, versioned procedure sheets, every documented field backed by a quote that verifies against the source — for $1.52 and 438 credits; the first national batch added 37 more hospitals in eight states for 261 credits and $0.64.
- A phone flow a 74-year-old can finish without typing: photo, read-back, two questions, photo, answer read aloud.
- A learning loop that cannot be poisoned: enums only, five distinct cases before anything patient-reported is published, admin review before a document changes a rule, accountability flags after three and five cases.
- Honest numbers everywhere, including the ones that are not flattering.

## What we learned

- Make the model propose and the verifier decide. Exact-quote verification turned "the model said 300 %" into "the document says 250 %, here is the sentence".
- Small models need grounding too: a cheap cross-check is only useful when it must prove its answer the same way the primary does.
- Budgets are a feature. A governor with hard caps and a daily budget let an unattended scheduler exist at all.
- Seniors do not need a smaller app; they need fewer decisions per screen and words like "likely".

## What's next for Waive

- Process the first real bills with a partner organisation (zero data retention is confirmed, so the app accepts them).
- National scouting state by state inside a credit budget (about 5 credits per hospital), and state repositories (California HCAI, Washington DOH) as document sources.
- A `charge_discount_percent` field for policies written as percentage of charges.
- Hand complex cases to Dollar For's advocates; an appeal letter from the hospital-slip flag.
- A home mode: the same app on a home computer with local NVIDIA models.

## Built with

Python 3.12 · uv · FastAPI · Jinja2 · SQLAlchemy 2 · SQLite / PostgreSQL · pydantic v2 · OpenAI SDK against Nebius Token Factory · NVIDIA Nemotron 3 Super, Nemotron 3 Nano, Nemotron 3.5 Lightning · MiniCPM-V 4.5 · Tavily (Search, Extract, Map) · tavily-python · rapidfuzz · Pillow · reportlab · cryptography (AES-GCM) · APScheduler · Typer · pytest, respx, Hypothesis, pytest-socket · Docker · Nebius AI Cloud (Container Registry, Managed PostgreSQL, SecretStash, Serverless AI endpoints — the deployment target; see the "Try it out" status line) · Claude Code

## Feedback on Nebius and NVIDIA tools

**Nebius Token Factory — what worked.** The OpenAI-compatible API worked on the first try with the stock `openai` SDK: `/models` for the catalog, `response_format: {"type": "json_object"}`, `image_url` data URLs for vision, usage counts in every response. Pricing is clear and low ($0.30 / $0.90 per million tokens for Nemotron 3 Super, $0.06 / $0.24 for Nano and Lightning): all of our Token Factory use — structuring the 46 Massachusetts hospitals many times over with cross-checks and tie-breaks, the first national batch of about 50 hospitals, the 30-bill vision evaluation and every connectivity check — cost $2.33 as of 2026-10-09. Super's 262K context would hold whole policy PDFs; we cap each document at 40,000 characters with keyword passage selection to keep a structuring pass at about a cent per hospital. Lightning's 1M context is tempting for system-wide documents.

**Nebius Token Factory — what we would change.**
1. **Zero data retention needs a visible, documented switch.** The HIPAA page says ZDR "must be enabled for the Token Factory scope" and the terms say users can opt out of storage, but we found no docs page describing it; our project runs with zero data retention (confirmed 2026-10-09 by the project owner), yet the app has no way to check that for itself. A per-project toggle plus a response header (or a `/models`-style endpoint) that confirms the current retention mode would let an app like ours verify it at startup instead of asking an operator to set `WAIVE_ZDR_CONFIRMED=true` by hand.
2. **An NVIDIA vision model.** There is no NVIDIA vision model in the catalog, so photos go to MiniCPM-V 4.5 (which read every field of our 30 synthetic bills except one statement date: 97 % on that field, 100 % on the rest). A Nemotron VL model on Token Factory would let a project meet the "NVIDIA model at runtime" requirement end to end.
3. **Schema-constrained output.** `json_object` mode gives valid JSON but not our schema; we put the JSON Schema in the prompt and repair once. A `json_schema` response format on Nemotron (and a documented `enable_thinking` flag in the request schema — we found it by trial) would remove our repair step.
4. **Catalog metadata.** `/models` is enough to verify IDs, but modality, context length and price had to be read from the web console; exposing them in the API would let `waive doctor` verify the whole table.

**NVIDIA Nemotron models — notes from live use.** Super (120B, A12B) is a strong structurer: with thinking off and the schema in the prompt it produced correct, quotable fields from long, messy policy PDFs, and it read the Mass General Brigham income table correctly where Nano did not. Nano (30B, A3B) is a good, cheap second opinion but tends to answer with very short quotes (three characters in one case) that cannot be verified; forcing it to prove its answer the same way as the primary made it useful. Lightning answers fast but was unreliable for structured output on long prompts; it serves as a tie-break only. Thinking mode on by default surprised us for a JSON task; a per-model default note in the docs would save others the first hour.

**Nebius AI Cloud.** The CLI docs are good and the pricing pages are precise, which made a cost table possible before creating anything (CPU endpoint `2vcpu-8gb` ≈ $0.066/h; Managed PostgreSQL `2vcpu-8gb` ≈ $0.143/h). Two things would help small, mostly idle apps: a Serverless AI endpoint tier that scales to zero (today an endpoint bills while it is running, so the deploy plan wraps `waive cloud start|stop` around demo windows — Phase 6 task 6.7, not written yet), and a pausable Managed PostgreSQL (today the database is the cost driver, and the docs describe no way to stop a cluster short of deleting it). At the time of writing the deployment itself is pending behind a cost-approval gate (see the "Try it out" status line above) and the Nebius CLI is not installed yet, so this feedback is from the documentation only.

## Best Use of Tavily

Tavily is the atlas's eyes, and the atlas is the product. For each hospital it scouts (about 100 so far, of 2,703 nonprofit hospitals in the registry) the pipeline makes about five Tavily calls: a **Search** to find the official website (directory sites such as Healthgrades and US News are rejected; a result counts as evidence only when its page title names the hospital or shows the phone number from the CMS record, and a domain found with weaker evidence is flagged for review), a **Search with `include_domains`** for the financial assistance policy, the application, the plain-language summary and the billing/collections policy, a **Map** of the domain when the index misses them (it did for several hospital systems), and **Extract** for the text of HTML pages and PDFs. Extract's Markdown output turned out to be a map of its own: we parse its links to follow "Financial Assistance Policy (PDF)" from entry pages, which published two more hospitals (BID Plymouth, Brigham and Women's), and four more (the Baystate system) once a direct download with `pypdf` covered the PDFs on asset hosts that Extract could not fetch. One hospital system's pages (Cape Cod Healthcare) come back as 643 characters of navigation at the basic extraction depth; such pages are now re-extracted once at the advanced depth (2 credits per 5 pages), which read the full page and reached the policy PDF, with a direct HTML download as a last resort. Cape Cod and Falmouth stay held for a different reason: their policy frames eligibility through the state's Health Safety Net rather than an income limit of its own. Documents are stored once by SHA-256 and linked to every hospital whose scout fetches the same file, so a system-wide policy fetched for Baystate Medical Center appears once in the database and on all four Baystate sheets. The scheduler re-hashes stored documents with Extract on a priority queue — stale sheets, hospitals that patients are asking about, and sheets whose predictions are failing go first — inside a daily credit budget, so the atlas stays current unattended. 438 credits built the Massachusetts atlas including every debugging re-run and re-scout; the next states are budgeted at five to seven credits per hospital (seven when a page needs the advanced depth). What we would ask Tavily for: an `extract_depth` that reliably reaches PDF text behind asset hosts (canto.com, widen.net), and the credit cost of each call in its response, so our governor could record what Tavily charged instead of estimating it from the published price rules.

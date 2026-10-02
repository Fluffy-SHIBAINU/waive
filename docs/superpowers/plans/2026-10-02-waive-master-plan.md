# Waive Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The build runs as a loop: see `docs/LOOP.md`; state lives in `docs/PROGRESS.md`.

**Goal:** Ship Waive — a phone-browser app that turns a photo of a hospital bill into a cited financial-assistance application, backed by a self-correcting Massachusetts atlas of hospital procedure sheets — running on Nebius, in phases a loop can execute one task at a time.

**Architecture:** One Python service (FastAPI + server-rendered HTMX pages) on Nebius AI Cloud, calling NVIDIA Nemotron models on Nebius Token Factory (zero data retention) and Tavily. Pure, unit-tested domain logic (eligibility, deadlines, quote verification) sits under thin adapters for external services. PostgreSQL stores the atlas, cases and learning data.

**Tech Stack:** Python 3.12 (uv), FastAPI, Jinja2 + HTMX, Tailwind (standalone CLI), SQLAlchemy 2 (SQLite locally, PostgreSQL 16 in production), pydantic v2, OpenAI SDK (Token Factory), tavily-python, rapidfuzz, Pillow, WeasyPrint, cryptography, APScheduler, Typer, pytest, respx, hypothesis, Playwright, ruff, Docker, Nebius CLI.

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md`

## Global Constraints

- Python `>=3.12,<3.13`; managed by `uv`; package `waive` in `src/waive/`.
- Token Factory base URL `https://api.tokenfactory.nebius.com/v1/`; key in `NEBIUS_API_KEY`; Tavily key in `TAVILY_API_KEY`; AI Cloud project in `NEBIUS_PROJECT_ID`. All in `.env` only (gitignored).
- Models (verified live 2026-10-02): reason `nvidia/nemotron-3-super-120b-a12b`, fast `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, vision `openbmb/MiniCPM-V-4_5` (Token Factory offers no NVIDIA vision model; the NVIDIA requirement is met by Nemotron doing structuring, cross-checks and reasoning). Fallback vision: `google/gemma-3-27b-it`.
- Personal data is processed only after `WAIVE_ZDR_CONFIRMED=true`; before that, synthetic fixtures only.
- Development caps (spec §15): Tavily 1,000 credits, Token Factory $15, AI Cloud $30 until Phase 7. Raising a cap needs the user's approval.
- No cloud resource creation, change or deletion, no push to a remote, and nothing made public without the user's explicit approval in chat.
- Senior screens: base text ≥ 20px, tap targets ≥ 48px, one action per screen, plain language, "likely" wording; the app never asks for money, card numbers or bank logins.
- Every documented atlas field carries an exact quote and a source; quote verification must pass before a sheet is published.
- Licenses: Apache-2.0 for code; CC BY 4.0 for atlas data.

## How this plan is executed

- One task per loop iteration (`docs/LOOP.md`). Progress, user gates and spend are tracked in `docs/PROGRESS.md`.
- Phases 0 and 1 have detailed plans now. Every later phase begins with task N.0, "Write the detailed Phase N plan", using superpowers:writing-plans with the spec sections named below. The detailed plan must keep the task boundaries listed here unless a task turns out to need splitting.
- A phase is done when its exit checks pass and its user gate, if any, is closed.

## Timeline

| When | Target |
|---|---|
| 2026-10-02 (build day, until 21:00 ET) | Phases 0–1 complete; Phase 2 first batch of 10–15 Massachusetts hospitals; Phase 3–4 core path (photo → result) if time allows |
| Week of 2026-10-05 | Finish Phases 2–4; Phase 5 |
| Week of 2026-10-12 | Phase 6 deploy; Phase 7 scheduler and expansion |
| Week of 2026-10-19 | Phase 8 packaging, video, buffer |
| 2026-10-28 | Submit (two days before the 2026-10-30 10:00 PT deadline) |

---

## Phase 0 — Foundations and connectivity

**Detailed plan:** `docs/superpowers/plans/2026-10-02-waive-phase-0-foundations.md`

**Goal:** A runnable project with typed settings, a spend governor, Token Factory and Tavily adapters, and `waive doctor` proving both APIs work without leaking secrets.

**Tasks:** 0.1 Project scaffold · 0.2 Settings · 0.3 Usage ledger and governor · 0.4 Token Factory client · 0.5 Tavily gateway · 0.6 `waive doctor` · 0.7 Live connectivity check.

**Exit checks:**
- `uv run ruff check .` and `uv run pytest` pass.
- `uv run waive doctor --live` shows Token Factory OK with all three model IDs found, and Tavily OK.
- Verified model IDs are recorded in `docs/PROGRESS.md`.

**User gates:** U0.1 Token Factory key, U0.2 Tavily key, U0.3 project ID, U0.4 zero data retention (not blocking until real data), U0.5 Nebius CLI login (blocking only for Phase 6), U0.6 optional GateGuard exemption.

**Budget:** ≤ 1 Tavily credit; ≤ $0.05 Token Factory.

## Phase 1 — Domain core

**Detailed plan:** `docs/superpowers/plans/2026-10-02-waive-phase-1-domain-core.md`

**Goal:** Pure, fully tested domain logic: poverty guidelines, the procedure-sheet schema, eligibility, deadlines, plain-language messages, and quote verification.

**Tasks:** 1.1 Poverty guidelines · 1.2 Procedure sheet schema and sample hospital · 1.3 Eligibility engine · 1.4 Deadlines · 1.5 Plain-language messages · 1.6 Quote verification · 1.7 Phase 1 exit check.

**Exit checks:** all tests pass; coverage of `waive.rules`, `waive.atlas.schema` and `waive.atlas.verify` ≥ 90%; no network calls in unit tests.

**Budget:** none (no paid calls).

## Phase 2 — Massachusetts atlas

**Spec sections:** §7, §8, §12, §15.

**Goal:** Published, verified procedure sheets for Massachusetts nonprofit acute-care and critical-access hospitals, stored with versions and exported as open data.

**Detailed plan:** `docs/superpowers/plans/2026-10-02-waive-phase-2-ma-atlas.md` (written 2026-10-02)

**Tasks:**
- 2.1 Database foundation: SQLAlchemy models (`hospitals`, `source_docs`, `sheets` with version + JSON body, `review_items`), `create_all` on startup, repository functions; SQLite in tests and local development, PostgreSQL in production via `WAIVE_DATABASE_URL`.
- 2.2 Registry seed: `waive atlas seed --state MA` reads the CMS Hospital General Information datastore API, keeps nonprofit acute-care and critical-access hospitals, upserts `hospitals`, and saves a dated snapshot under `data/seed/`. No key needed.
- 2.3 Domain discovery: find each hospital's official website with Tavily Search, reject directory sites, confirm by name tokens or phone in the result; uncertain matches become review items.
- 2.4 Document scouting: on the official domain, find FAP, application, plain-language summary and billing/collections URLs with Tavily Search (`include_domains`), extract text, store documents once by SHA-256 and link them to every hospital that shares them.
- 2.5 Structurer: prompt + JSON schema producing a procedure sheet draft from documents with Nemotron 3 Super; every documented field carries an exact quote; drafts are cast and invalid fields skipped.
- 2.6 Verification, cross-check and publishing: run `verify_sheet` and drop rejected fields; cross-check critical fields with Nemotron 3 Nano; conflicts hold the sheet; version bump on change with stored diff; open-data export (CC BY 4.0).
- 2.7 Pipeline and CLI: `waive atlas build / export / report`, coverage report `docs/reports/atlas-ma.md`.
- 2.8 First live batch of 10–15 MA hospitals (needs keys).
- 2.9 Massachusetts overlay: cited Health Safety Net entry from mass.gov on every MA sheet.
- 2.10 Full MA run and Phase 2 exit.

**Exit checks:** ≥ 80% of MA nonprofit acute-care and critical-access hospitals have published sheets; 100% of published documented fields pass quote verification; spend within budget; coverage report committed.

**User gate:** U2.1 spot-check 10 sheets field by field; record precision in PROGRESS.

**Budget:** Tavily ≤ 400 credits; Token Factory ≤ $3.

## Phase 3 — Bill reading and cases

**Spec sections:** §9, §11, §13.

**Detailed plan:** `docs/superpowers/plans/2026-10-02-waive-phase-3-bill-reading.md` (written 2026-10-02; its task list supersedes the sketch below: 3.1 image intake, 3.2 synthetic corpus, 3.3 vision extraction, 3.4 matching, 3.5 vault, 3.6 case service, 3.7 log hygiene, 3.8 accuracy report).

**Goal:** From a photo to an `EligibilityResult` with citations and deadlines, with personal data encrypted and nothing personal in logs.

**Tasks (sketch):**
- 3.1 Image intake: orientation fix, resize ≤ 2000px, metadata strip, JPEG encode, quality check (minimum size, blur score).
- 3.2 Synthetic corpus: generator for fictional bills (several layouts, fonts, rotations, blur), Social Security letters and decision letters, each with ground-truth JSON under `tests/fixtures/corpus/`.
- 3.3 Vision extraction: `BillExtract` schema and prompt; Token Factory vision call (`phi=True`); confirm the image input format live with one synthetic image; contract tests on recorded responses.
- 3.4 Hospital matching: rapidfuzz on name, city/ZIP, phone and the FAP web address printed on the bill; top-3 candidates when unsure.
- 3.5 Case vault: models for cases, bills, households and predictions; AES-GCM field encryption; signed capability tokens with senior and caregiver scopes; one-call delete of a case.
- 3.6 Case service: upload → extract → read-back data → match → evaluate → deadlines → prediction record; unknown hospital queues on-demand scouting.
- 3.7 Income from a Social Security benefit letter photo.
- 3.8 `waive eval bills`: accuracy report per field on the corpus.

**Exit checks:** corpus accuracy ≥ 95% on hospital name, statement date and amount due; matching ≥ 95% on the corpus; end-to-end service test passes; log scanner test finds no personal data in logs.

**User gate:** U0.4 must be closed before any real (non-synthetic) bill is processed.

**Budget:** Token Factory ≤ $3.

## Phase 4 — Phone web app and packet

**Spec sections:** §9, §11, §13.

**Detailed plan:** `docs/superpowers/plans/2026-10-02-waive-phase-4-phone-app.md` (written 2026-10-02; plain CSS instead of Tailwind, no HTMX, reportlab instead of WeasyPrint; its task list supersedes the sketch below).

**Goal:** The senior and caregiver flows working in a phone browser, plus a printable application packet and reminders.

**Tasks:**
- 4.0 Write the detailed Phase 4 plan.
- 4.1 App skeleton: FastAPI app factory, Jinja2 layout, HTMX, Tailwind build, senior design tokens (20px base, 48px targets, high contrast), error pages.
- 4.2 Senior flow: link landing (scoped token) → "Take a photo of your bill" (camera only, `capture="environment"`) → processing → read-back (Yes / Fix) → household quick questions (large buttons) → result in large text with a read-aloud button (`speechSynthesis`) → next step.
- 4.3 Caregiver flow: create case → share link (Web Share API with copy fallback) → review card (extracted fields, eligibility with citations, deadlines) → approve → packet.
- 4.4 Packet PDF (WeasyPrint): cover letter citing the policy section, application data sheet, document checklist, mailing/fax instructions, link to the hospital's own form.
- 4.5 Reminders: `.ics` files for check-ins (days 14, 30, 45) and deadlines (days 120, 240).
- 4.6 Public atlas pages: hospital search, sheet view with quotes, sources, versions and dates, JSON download.
- 4.7 E2E and accessibility: Playwright on iPhone 13 and Pixel 7 emulation for both flows; axe-core with no serious or critical violations.
- 4.8 Phone test path: run locally and expose through a temporary HTTPS tunnel (requires user approval of the tunnel tool) for U4.1.

**Exit checks:** E2E tests pass on both devices; axe-core clean; packet PDF renders with citations.

**User gate:** U4.1 the user completes the senior flow on their own phone with a synthetic bill.

**Budget:** Token Factory ≤ $2 (E2E uses recorded responses; live only for the phone test).

## Phase 5 — Learning loop

**Spec sections:** §10, §11, §12.

**Goal:** Photos and outcomes improve the atlas, with thresholds and review so bad data cannot rewrite rules.

**Tasks:**
- 5.0 Write the detailed Phase 5 plan.
- 5.1 Photo classifier (bill, explanation of benefits, decision letter, information request, plain-language summary, FAP, application form, Social Security letter, other) and routing.
- 5.2 Public document contributions: personal-information check (vision + patterns), admin review queue, approved items become `patient_photo` sources.
- 5.3 Gap check and targeted ask: completeness and confidence rules; one skippable question per case.
- 5.4 Outcome capture: `OutcomeExtract` from decision and request letters; check-in prompts.
- 5.5 Compare and triage: `case_issue`, `sheet_missing`, `sheet_wrong`, `hospital_slip`; actions (help resubmit, add reported evidence, re-scout, appeal draft, accountability flag).
- 5.6 Aggregation thresholds: reported fields publish after 5 distinct cases; accountability flags internal after 3, public after 5; fixed enums and income bands only.
- 5.7 Scoreboard: rolling per-hospital accuracy; sheets with ≥ 3 outcomes and accuracy < 0.8 get priority re-checks.
- 5.8 Admin console: review queue, sheet diffs, scoreboard, budget view.
- 5.9 Simulation suite: scripted outcomes drive the full loop end to end.

**Exit checks:** the simulation scenario passes (3 denials → re-scout → new version after review → open cases re-evaluated; a missing document appears as "reported by patients" after 5 cases; a hospital slip is flagged after 3); an automated check finds no personal data in evidence tables.

**Budget:** Token Factory ≤ $2; Tavily ≤ 50 credits.

## Phase 6 — Deploy on Nebius AI Cloud

**Spec sections:** §14, §15.

**Goal:** The app on a public HTTPS URL on Nebius, with PostgreSQL, Object Storage and secrets, and commands to stop and start it.

**Tasks:**
- 6.0 Write the detailed Phase 6 plan.
- 6.1 Dockerfile (multi-stage, WeasyPrint system libraries), `/healthz`, production settings.
- 6.2 Read-only discovery with the Nebius CLI: project region, CPU platforms and presets, PostgreSQL presets; write a cost table to `docs/reports/cloud-costs.md`. Then open gate U6.1.
- 6.3 Container Registry and image push (after U6.1).
- 6.4 Managed PostgreSQL and migrations (after U6.1).
- 6.5 Object Storage bucket; switch document storage to S3-compatible storage with a local fallback.
- 6.6 MysteryBox secrets and the Serverless AI endpoint (CPU, smallest preset, port 8000, no endpoint auth); fallback to one small Compute VM with Docker and Caddy if the endpoint does not fit.
- 6.7 `waive cloud start|stop|status|cleanup` (cleanup only touches `waive-` resources and asks first); in-app scheduler for scouting.
- 6.8 Phone smoke test on the public URL.

**Exit checks:** the public URL works on a phone; stop and start verified; cost table committed; ZDR confirmation recorded.

**User gates:** U0.5 Nebius CLI login (before 6.2); U6.1 approve resources and costs (before 6.3).

**Budget:** AI Cloud ≤ $30 through 2026-10-30.

## Phase 7 — Always-on scouting and national scale

**Spec sections:** §8, §15, §16.

**Goal:** Scouting that runs unattended within a credit budget, and coverage beyond Massachusetts.

**Tasks:**
- 7.0 Write the detailed Phase 7 plan.
- 7.1 Scheduler and priority queue: priority = staleness × case demand × (1 − accuracy); daily credit budget.
- 7.2 Content-hash refresh jobs.
- 7.3 National registry seed (all states).
- 7.4 Batch national scouting in budgeted batches (after U7.1).
- 7.5 State repositories as sources: California HCAI lookup, Washington DOH policies.
- 7.6 IRS Schedule H cross-check (optional).
- 7.7 Coverage map and metrics page.

**Exit checks:** the scheduler runs 48 hours unattended within budget; national coverage report committed.

**User gate:** U7.1 approve the national credit budget (estimate 4,800–6,600 credits).

**Budget:** as approved in U7.1.

## Phase 8 — Submission

**Goal:** Everything Devpost needs, ready two days before the deadline.

**Tasks:**
- 8.0 Write the detailed Phase 8 plan.
- 8.1 README: setup, architecture diagram, how Nebius, NVIDIA and Tavily are used, privacy, costs; LICENSE check; atlas data license note.
- 8.2 Demo data and a reset command.
- 8.3 Demo script and shot list for a video under 3 minutes.
- 8.4 Devpost draft in `docs/devpost/submission.md`, including feedback on Nebius and NVIDIA tools.
- 8.5 Screenshots and diagrams exported for the gallery.

**User gates:** U8.1 approve making the repo public; U8.2 record and upload the video (public on YouTube); U8.3 submit on Devpost; U8.4 stop cloud resources after judging (2026-12-15).

## Phase H — Home mode (optional)

**Goal:** The same app running on a home computer with local NVIDIA models.

**Tasks:** H.0 write plan · H.1 OpenAI-compatible local runtime adapter (Ollama or vLLM base URL) · H.2 local vision model support · H.3 `WAIVE_MODE=home` settings and docs.

---

## Self-review

- **Spec coverage:** §3 goals → G1 Phases 3–4, G2 Phase 2, G3 Phase 5, G4 tasks 0.4, 3.5, 4.x, G5 Phases 0, 6, 8, G6 Phase 7. §7 → 1.2, 2.5–2.7. §8 → 1.6, 2.x, 7.x. §9 → 1.3–1.5, 3.x, 4.x. §10 → 5.x. §11 → 0.2, 0.4, 3.5, 4.2–4.3. §12–13 → tests in every phase, 3.2, 3.8, 4.7, 5.9. §14–15 → 6.x. §16 → 3.8, 5.7, 7.7. §18 → 0.7, 2.8, 6.2.
- **Gates:** every paid or public action sits behind a user gate or a budget cap.

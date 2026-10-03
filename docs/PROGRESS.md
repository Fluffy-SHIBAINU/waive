# Waive build progress

Last updated: 2026-10-03 00:45 ET (loop iteration 10 done)
Current phase: 2 nearly done (Massachusetts atlas: 46 hospitals, 27 published (59%), 17 held, 2 without documents; Tavily cap for Phase 2 reached; exit criterion of 80% needs gate U2.2 for the 11 hospitals whose stored text has no income rules) — Phases 0, 1, 3 and 4 are complete
Current plan: `docs/superpowers/plans/2026-10-02-waive-phase-7-scale.md`
Next task: 7.1 Scheduler and priority queue; then 7.2, 7.3 (no spend), 7.7 (metrics), then 8.0 (Phase 8 plan). Blocked until gates close: 6.1b (U6.0), 6.3 (U0.5), 6.4–6.8 (U6.1), 7.4 (U7.1), Phase 2 finish (U2.2).

Master plan: `docs/superpowers/plans/2026-10-02-waive-master-plan.md` · Spec: `docs/superpowers/specs/2026-10-02-waive-design.md` · Loop rules: `docs/LOOP.md`

## Handoff (read this first if you are new to the build)

- The user approved: build the whole project phase by phase, with user gates for keys, cloud resources, publishing and spending. Build day 2026-10-02 runs until 21:00 ET; afterwards the user runs `/loop` (see `docs/LOOP.md`), possibly on a different Claude model.
- Working method: one task per iteration, tests first, commit per task, PROGRESS.md updated every time. Implementation subagents must not edit PROGRESS.md; the orchestrator (or the loop) does.
- Nothing is pushed to a remote yet. There is no remote. Ask the user before adding one.
- Spend so far is in the table below; the ledger file `var/usage.jsonl` is the source of truth once code exists.

## User gates (only the user closes these)

- [x] **U0.1** Token Factory key is in `.env` (closed 2026-10-02 17:05; the user had put it in `.env.example`, which git tracks — the orchestrator moved all three values to `.env` and blanked the template before anything was committed; git history never contained a key).
- [x] **U0.2** Tavily key is in `.env` (closed 2026-10-02 17:05).
- [x] **U0.3** AI Cloud project ID is in `.env` (closed 2026-10-02 17:05).
- [ ] **U0.4** Turn on zero data retention for Token Factory, then set `WAIVE_ZDR_CONFIRMED=true` in `.env`. Checked 2026-10-02: the Token Factory docs index has no page describing the switch; Nebius's HIPAA page says ZDR "must be enabled for the Token Factory scope" and its terms say users can opt out of input/output storage at any time. Look for a data-retention or "store requests" setting in the Token Factory console (project settings); if there is none, ask Nebius support to enable zero retention for the project. Until then the build uses synthetic data only. Blocks real bills.
- [ ] **U0.5** Install the Nebius CLI (`curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash`) and run `nebius profile create` (browser sign-in). Needed in Phase 6.
- [ ] **U0.6** Optional: exempt this repo from GateGuard first-touch prompts (`GATEGUARD_EXEMPT_GLOBS`) so loop iterations don't stall on every new file.
- [ ] **U2.1** Spot-check 10 Massachusetts procedure sheets field by field (Phase 2 exit). Open since 2026-10-02 22:30: start with `uv run waive serve` → http://localhost:8000/atlas (published sheets show every quote and source link).
- [ ] **U2.2** Phase 2 Tavily cap (400 credits) is reached at 389. Approve extra credits (suggested 100) before the loop spends more on Massachusetts scouting; free re-structuring continues meanwhile. To approve, raise `WAIVE_TAVILY_CREDIT_CAP` in `.env` (it is the hard governor cap; currently 1000 overall) and write the new Phase 2 allowance here.
- [ ] **U4.1** Complete the senior flow on your own phone with a synthetic bill (Phase 4 exit). Steps: `uv run waive corpus generate --count 3` (bills in `var/corpus/`), `uv run waive serve`, find the Mac's IP with `ipconfig getifaddr en0`, open `http://<ip>:8000` on a phone on the same Wi-Fi, start a case, open the senior link, photograph `var/corpus/bill-000.jpg` shown on the laptop screen. The web flow sends photos with `phi=True`, so for this synthetic-only test set `WAIVE_REQUIRE_ZDR=false` in `.env` temporarily (or close U0.4 first). The demo hospital is St. Example (`uv run waive demo seed` already ran); real MA sheets exist for Boston Medical Center, BIDMC and Anna Jaques.
- [ ] **U6.0** Run `docker logout ghcr.io` once (a stale ghcr.io login in your keychain makes every ghcr pull fail), then tell the loop; it will build and smoke-test the image (task 6.1b).
- [ ] **U6.1** Approve the Nebius resources and their costs before anything is created (Phase 6). The loop writes `docs/reports/cloud-costs.md` in task 6.3 first (needs U0.5).
- [ ] **U7.1** Approve the national Tavily credit budget (Phase 7). Realistic cost is ≈5 credits per hospital; with ≈8,000 credits total and 389 used, "all states" is out of reach — pick states (suggested: CA, NY, TX, FL, PA, IL, OH ≈ 1,000 hospitals ≈ 5,000 credits) or a daily cap, and set `WAIVE_NATIONAL_SCOUTING=on` plus the state list when approving.
- [ ] **U8.1** Approve making the GitHub repo public.
- [ ] **U8.2** Record and upload the demo video (under 3 minutes, public on YouTube).
- [ ] **U8.3** Submit on Devpost (target 2026-10-28; deadline 2026-10-30 10:00 PT).
- [ ] **U8.4** Stop cloud resources after judging ends (2026-12-15).

## Phase checklist

### Phase 0 — Foundations and connectivity
- [x] 0.1 Project scaffold (1318d56)
- [x] 0.2 Settings (df5dad3)
- [x] 0.3 Usage ledger and governor (265052c)
- [x] 0.4 Token Factory client (d10a4fa)
- [x] 0.5 Tavily gateway (e23cf58)
- [x] 0.6 `waive doctor` (d47a952)
- [x] 0.7 Live connectivity check (2026-10-02 17:15): Tavily OK; Token Factory OK after fixing model IDs. Verified catalog: reason `nvidia/nemotron-3-super-120b-a12b` ($0.30/$0.90 per M tokens, 262K ctx), fast `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` ($0.06/$0.24), alt fast `nvidia/Nemotron-3_5-Lightning` ($0.06/$0.24, 1M ctx). No NVIDIA vision model is offered; vision = `openbmb/MiniCPM-V-4_5` ($0.658/$1.11, 32K ctx, modality text+image), fallback `google/gemma-3-27b-it` ($0.10/$0.30). Both read a synthetic statement correctly via the standard OpenAI `image_url` data-URL format. `.env` updated. Code defaults (`config.py`, `PRICES_PER_MILLION`, `test_doctor.MODELS`, `.env.example`) still carry the old IDs → task 0.9.
- [ ] 0.9 Update model defaults in code to the verified IDs above (config.py, ai/client.py prices incl. MiniCPM-V and gemma-3, tests/unit/test_doctor.py MODELS, tests/unit/test_ai_client.py price test, .env.example). Do this when no other agent is editing those files.
- [x] 0.8 Test hardening with `pytest-socket` (b2e4fc5): unit tests cannot open sockets; live tests use `@pytest.mark.enable_socket`.

### Phase 1 — Domain core (done 2026-10-02 16:55; coverage of waive.rules + waive.atlas 96.56%)
- [x] 1.1 Poverty guidelines (20791f4)
- [x] 1.2 Procedure sheet schema and sample hospital (ce2329f)
- [x] 1.3 Eligibility engine (6e5718a)
- [x] 1.4 Deadlines (e3fd79f)
- [x] 1.5 Plain-language messages (5a0504e)
- [x] 1.6 Quote verification (1833714)
- [x] 1.7 Phase 1 exit check: 72 tests pass, ruff clean, coverage 96.56% ≥ 90%

### Phase 2 — Massachusetts atlas
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-2-ma-atlas.md`
- [x] 2.0 Write detailed Phase 2 plan
- [ ] 2.1 Database foundation
- [ ] 2.2 Registry seed from CMS (no key needed)
- [ ] 2.3 Official domain discovery
- [ ] 2.4 Document scouting
- [ ] 2.5 Structurer
- [ ] 2.6 Verification, cross-check and publishing
- [ ] 2.7 Pipeline and CLI
- [x] 2.1–2.7 done by a subagent (a538636…fef7b0f), 46 MA hospitals seeded from CMS
- [x] 2.8 First live batches (2026-10-02 17:30–19:30). Live runs exposed and fixed: Nemotron thinking starving JSON (`enable_thinking: false`), schema not shown to the model (schema hint + `json_object` mode), null-wrapped draft fields, bare-list fields from Nano, quote variance (quotes trimmed to verified spans), bogus residency/window values (semantic guards), wrong domains (registered-domain + title evidence), search index missing policy pages (Tavily Map fallback), presumptive-only conflicts (publish without that field), 100% "discount" tiers. Published so far: Boston Medical Center (11 fields), BIDMC; held: Baystate ×4, Athol, BID Plymouth, Berkshire (reachable pages state no income limits — needs PDF-link following, see Phase 7 ideas). `--reuse-sources` re-structures without Tavily spend.
- [x] 2.8b Done 2026-10-02 (5088ade, 9f3a482, 531906e, b8e9c81; 161 tests): the scout now follows labelled policy links from entry pages (same domain or known asset hosts such as canto.com, widen.net, cloudfront), `classify_doc` ignores `application/pdf` query strings, `_tiers` skips unparseable items. Re-ran 16 hospitals: BID Plymouth and Brigham and Women's now published (real published = 5: BMC, BIDMC, Anna Jaques, BID Plymouth, BWH); report line "Published sheets: 6 (13%)" includes the demo hospital. Spend after: 204 Tavily credits, $0.51 Token Factory. Follow-ups:
  - [x] 2.8c Done 2026-10-02 (34e4a98, ccac292; 170 tests): `atlas/fetch.py` downloads PDFs Tavily cannot fetch (streamed GET, 15 MB cap, pypdf); the scout falls back to it for any selected or linked URL with no usable text. Baystate ×4 published (real published = 10). Spend after: 241 credits, $0.61. New follow-ups:
  - [x] 2.8i/2.8g/2.8f done 2026-10-02 (aca0dab, bea5678, 97352b9, 7a6b552; 182 tests): tier ranges and missing bounds parse, free-care limit derived from a 100% band, long documents get keyword passage selection (head 6k + ±1.5k windows), state-overlay docs are kept out of the structurer, `complete_json` retries a bare `{}` once without `json_object` mode (Nemotron Super sometimes answers `{}` in JSON mode on long prompts), the conflict note is correct. Free re-run of all 29 held hospitals (Token Factory ≈$0.25, no Tavily): Athol, Berkshire, Emerson, Heywood flipped to published (+ others pending in the final tally below).
  - [x] 2.9c Done 2026-10-02 (47c3c00; 183 tests): `build_hospital` carries the previous version's `state_programs` and its source over; the 8 rebuilt sheets were repaired from their history and the 26 later-built sheets received the overlay from the stored mass.gov source (no Tavily spend). All 45 MA sheets carry the Health Safety Net entry.
  - [x] 2.8j Done 2026-10-03 (82d6839, c09db7c, a002c7d, 79f0e3f; 198 tests). Diagnosis: Nemotron Super read the MGB table correctly (free care ≤150% FPL, tiers 85%/70%); Nemotron Nano answered "300" with a 3-character quote that can never verify. Two changes: (1) a third-model tie-break (`model_tiebreak` = Nemotron 3.5 Lightning, role `tiebreak`, `publish.resolve_conflicts`) — useful but Lightning is unreliable on long prompts; (2) the decisive rule: a cross-check disagreement counts only when the cross-check's own quote verifies (trim + verify the secondary like the primary). Faulkner, MGH, MetroWest and Newton-Wellesley are now published; BID Needham stays held (tiers). Report: 46 hospitals, 27 published (59%). Token Factory spend $1.52.
  - [ ] 2.8k Hospitals whose stored text has no income rules (Cape Cod, Falmouth, South Shore, Lowell General, Milford, Mount Auburn, Nantucket, North Shore, Martha's Vineyard) need new scouting — blocked on gate U2.2.
  - [x] 2.8g Heywood (220095) — fixed by passage selection above. Original note: the stored 2016 Credit and Collection Policy contains an FPL table ("0%-200% 100% … 201%-400%") at offset ~49.5k but the structurer returned no fields. Inspect the draft with `--reuse-sources`; likely the table is beyond `MAX_DOC_CHARS` (40,000) — raise it or select the passages around "Federal Poverty" before structuring. Free to iterate (no Tavily).
  - [ ] 2.8h Cape Cod (220012): Tavily Extract returns only 643 chars of navigation for the financial-assistance page. Try `extract_depth="advanced"` for pages under 1,000 chars, or the direct HTML download with a simple tag strip.
  - [ ] 2.8i Baystate ×4 and Emerson: "no usable tier (1 of 1 items failed to parse)" — capture the raw tier text the model produced and extend `_tiers`/the prompt (sliding scales written as ranges with percentages).
  - [x] 2.8d Done 2026-10-02 (c3638f7): directory hosts blocklisted; domains set by hand (Faulkner → massgeneralbrigham.org, Fairview → berkshirehealthsystems.org, Falmouth → capecodhealth.org) and rebuilt. Fairview published (real published = 6); Faulkner and Falmouth held (no income limits in reachable text). Spend after: 215 credits, $0.55.
  - [ ] 2.8e Berkshire's policy is a percentage-of-charges table by facility, not FPL bands; the schema cannot represent it. Leave held; revisit with a `charge_discount_percent` field if time allows.
  - [ ] 2.8f Cosmetic: the pipeline note "critical fields disagree" is appended even when the only conflict is `programs.presumptive` (which it then tolerates).
- Original 2.8b notes (kept for context): for 14 held hospitals the fetched pages contain no poverty-level rules at all. Ideas, in order: (1) after extracting an entry page (class `billing`/`fap` HTML), collect links whose text or URL mentions financial assistance / policy / application / PDF and extract those too (Tavily Extract returns markdown with `[text](url)` links — parse them); (2) `search_depth="advanced"` for the two scouting queries; (3) Tavily Map with `max_depth=3`; (4) for hospital systems (Baystate, Mass General Brigham, Beth Israel Lahey, UMass Memorial, Tufts Medicine, Berkshire Health Systems) scout once per system domain and share the documents. Also: Berkshire's sliding-scale tiers do not parse (`discount_tiers: no number`) — inspect the draft and extend `_tiers`. Re-run held hospitals with `--rebuild --reuse-sources` after prompt/parser changes (no Tavily spend) and `--rebuild` after scouting changes (≈4 credits each).
- [x] 2.9 Massachusetts overlay done 2026-10-02 (1d8bbd7, a43d890; 173 tests): `waive atlas overlay --state MA` added a cited Health Safety Net entry to all 19 MA sheets (2 credits; source: mass.gov Senior Guide to Health Care Coverage PDF).
  - [ ] 2.9b The chosen quote is navigational ("…can be found on page 3"). Prefer non-`/doc/…/download` mass.gov hits or a query like "Health Safety Net eligibility income Massachusetts residents" so the quote states eligibility; then rerun the overlay (≈2 credits).
- [ ] 2.10 All 46 hospitals attempted by 2026-10-02 22:30 (loop iteration 6): 15 real hospitals published (BMC, BIDMC, Anna Jaques, BID Plymouth, BWH, Fairview, Baystate ×4, Mercy, Merrimack, New England Baptist, Southcoast, Winchester), the rest held (no income limits found, unparseable discount tiers, or cross-model conflicts) or skipped (Cooley Dickinson, Mass Eye and Ear: no documents). **Phase 2 Tavily cap reached: 389 of 400 credits.** Exit criterion (≥ 80% published) not met at 34%. Next, all free (no Tavily): 2.8i tier parsing, 2.8g long-document passage selection, 2.8f cosmetic note, then re-run every held hospital with `--rebuild --reuse-sources`. New gate **U2.2**: ask the user before any further Tavily spend on Phase 2 (suggested extra budget: 100 credits to retry MGH/Newton-Wellesley/MetroWest/UMass with hand-set system domains and the two skipped hospitals). Batch 1 notes (2026-10-02 22:05): Mercy Medical Center published; Holyoke, Lahey (critical-field conflict), Lowell General, Marlborough, Martha's Vineyard, MGH (low-confidence domain — set `massgeneralbrigham.org` by hand like Faulkner), MelroseWakefield held; Cooley Dickinson and Mass Eye and Ear skipped (no documents). Spend after: 289 credits, $0.69. Remaining to attempt: 11 hospitals (≈50 credits). Earlier status 2026-10-02 19:50: 21 of 46 hospitals attempted; 3 published (Boston Medical Center, BIDMC, Anna Jaques), 14 held, 4 skipped/failed. Spend: 123 Tavily credits, $0.35 Token Factory. Remaining 25 hospitals ≈ 100 credits. Budget left for Phase 2: 277 credits.

### Phase 3 — Bill reading and cases (done 2026-10-02 19:10 by a subagent; 136 tests pass)
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-3-bill-reading.md`
- [x] 3.0 Write detailed Phase 3 plan
- [x] 3.1 Image intake (e34073d; MIN_SHARPNESS 600)
- [x] 3.2 Synthetic bill corpus + `waive corpus generate` (e2be369)
- [x] 3.3 Vision extraction (b86049e); live smoke on MiniCPM-V 4.5 read every field correctly
- [x] 3.4 Hospital matching (fa1131d)
- [x] 3.5 Case vault: AES-GCM, signed capability tokens, `waive keygen` (2c157f6)
- [x] 3.6 Case service (ac58852)
- [x] 3.7 Log hygiene filter (f58dea8)
- [x] 3.8 `waive eval bills` (403a7e4): 30 synthetic bills — hospital_name 100%, statement_date 97%, amount_due 100%, fap_phone 100%, fap_url 100%, collection_notice 100%; report in `docs/reports/bill-eval.md`; $0.08 of vision calls

### Phase 4 — Phone web app and packet (done 2026-10-02 20:00 by a subagent; 153 tests pass)
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-4-phone-app.md`
- [x] 4.0 Write detailed Phase 4 plan
- [x] 4.1 App skeleton, layout and styles (0cfa370)
- [x] 4.2 Senior flow (dab9a6b)
- [x] 4.3 Caregiver flow (3fd8797)
- [x] 4.4 Application packet PDF, reportlab (67ccbba)
- [x] 4.5 Calendar reminders .ics (a5924f0)
- [x] 4.6 Public atlas pages (bd7d3cc)
- [x] 4.7 Demo seed + `waive demo seed|forget-cases` (80ffcac). `uv run waive serve` verified locally: `/healthz` and `/` return 200. Vault secrets were generated into `.env` by the orchestrator (`waive keygen`). Gate U4.1 (phone test) is open.
- [ ] 4.8 Accessibility and E2E checks (optional)
- [x] 4.9 Done 2026-10-03 (5a141e8, a7d6f23; 187 tests): `repo.DEMO_CCNS`/`is_demo`; exports, the coverage report and the `/atlas` list skip the demo hospital (`/atlas?demo=1` shows it; `/atlas/229999` stays reachable for the phone demo; case matching unchanged). Report now: 46 hospitals, 23 published (50%).

### Phase 5 — Learning loop
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-5-learning-loop.md` (781c6a3; 9 tasks; also adds `service.get_row/load_sealed/save_sealed`, `repo.set_review_status/sheet_versions`, `publish.carry_over_reported`, `Apply.documents_reported`)
- [x] 5.0 Write detailed Phase 5 plan
- [x] 5.1 Photo classifier and paper intake routes (29ac7d1; `/s|/c/{token}/paper`)
- [x] 5.2 Public document contributions (bbed61a; `contributions` table, `waive learn rebuild --ccn`)
- [x] 5.3 Gap check and one skippable ask (477f39a; 214 tests at this point)
- [x] 5.4 Outcome capture (f370d89; `cases.outcome` column, check-ins at 14/30/45 days)
- [x] 5.5 Compare and triage (070d70a; `reported_evidence` table, `rescout_request` review items, appeal draft)
- [x] 5.6 Aggregation thresholds (bb692ce; `Apply.documents_reported`, `publish.carry_over_reported`, flags internal at 3 / public at 5, `waive learn publish-reported|audit`; 233 tests)
- (5.10 moved above — done.)
- [x] 5.7 Scoreboard and priority re-checks (954e55e; `waive learn scoreboard --state MA [--queue]`)
- [x] 5.8 Admin console behind `WAIVE_ADMIN_TOKEN` (57d06d4; `/admin/login`, review queue, contributions, sheet diffs, scoreboard, budget). A token was generated into `.env` by the orchestrator on 2026-10-03.
- [x] 5.9 Simulation test of the whole loop (e4c3293; all four exit checks pass)
- [x] 5.10 `waive db upgrade` / `upgrade_schema` adds missing nullable columns (41c0122). Phase 5 exit: 248 tests pass; `waive learn audit` → "Evidence tables are clean."

### Phase 6 — Deploy on Nebius AI Cloud
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-6-deploy.md` (a0a989a; 8 tasks)
- [x] 6.0 Write detailed Phase 6 plan
- [x] 6.1 Dockerfile, `.dockerignore`, `WAIVE_ENV=production` (2797bed; 253 tests). Image build NOT yet verified: `docker build` fails pulling `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` with "failed to fetch oauth token: denied" because a stale ghcr.io credential sits in the macOS keychain → gate U6.0.
- [x] 6.2 DB-backed usage ledger `WAIVE_LEDGER_BACKEND=db` (b44cb7d; 257 tests)
- [ ] 6.1b After U6.0: `docker build -t waive:dev .` and the plan's step-7 smoke test (`/healthz` from the container with SQLite); record the image size.
- [ ] 6.3 `waive cloud discover` → `docs/reports/cloud-costs.md`; then STOP for gate U6.1 (needs U0.5: Nebius CLI + `nebius profile create`)
- [ ] 6.4 Container Registry + image push (needs U6.1)
- [ ] 6.5 Managed PostgreSQL + `waive db copy` (cases excluded) + `waive db upgrade` (needs U6.1)
- [ ] 6.6 MysteryBox secrets + Serverless AI endpoint (fallback: small VM + Compose + Caddy) (needs U6.1)
- [ ] 6.7 `waive cloud start|stop|status|cleanup` (waive-* only)
- [ ] 6.8 Public URL smoke test and phone test

### Phase 7 — Always-on scouting and national scale
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-7-scale.md` (601f318; 7 tasks). Corrected estimate: national scouting ≈ 5 credits × 2,500–3,000 nonprofit hospitals ≈ 12,000–15,000 Tavily credits — more than the ≈8,000 credits the user has; gate U7.1 must choose states/priorities rather than "all".
- [x] 7.0 Write detailed Phase 7 plan
- [ ] 7.1 Scheduler and priority queue (`WAIVE_SCOUT_DAILY_CREDITS`, `WAIVE_SCHEDULER=on`, `waive atlas schedule`) — no spend
- [ ] 7.2 Content-hash refresh (`atlas/refresh.py`) — no spend in tests
- [ ] 7.3 National registry seed `waive atlas seed --all-states` — free (CMS API)
- [ ] 7.4 Budgeted national scouting (gate U7.1; `WAIVE_NATIONAL_SCOUTING`)
- [ ] 7.5 State repositories as sources (CA HCAI, WA DOH)
- [ ] 7.6 IRS Form 990 / ProPublica cross-check (optional)
- [ ] 7.7 `/metrics` page and `waive atlas report --national`

### Phase 8 — Submission
- [ ] 8.0 Write detailed Phase 8 plan

## Open items to verify (spec §18)

- [ ] How zero data retention is switched on for Token Factory
- [x] Exact Token Factory model IDs, vision image format and prices — verified 2026-10-02 (see task 0.7)
- [ ] AI Cloud project region; smallest CPU endpoint preset; smallest PostgreSQL preset and price
- [x] 2026 poverty guidelines — verified 2026-10-02 (ASPE): 48 states and DC $15,960 + $5,680; AK $19,950 + $7,100; HI $18,360 + $6,530
- [ ] Massachusetts Health Safety Net rules and how MA hospital FAPs route applications
- [ ] 501(r) notice rules beyond days 120 and 240
- [ ] Licenses (defaults: Apache-2.0 code, CC BY 4.0 atlas data)

## Spend

| Date | Tavily credits | Token Factory $ | AI Cloud $ | Note |
|---|---|---|---|---|
| 2026-10-02 | 0 | 0 | 0 | Planning only |
| 2026-10-02 | 2 | ~0.001 | 0 | Two live doctor runs (1 credit each) + two vision smoke calls |
| 2026-10-02 | 123 (total) | 0.36 (total) | 0 | Atlas batches for 21 MA hospitals incl. debugging reruns; bill-eval 72 vision calls ($0.08). Ledger: `var/usage.jsonl` |
| 2026-10-02 | 204 (total) | 0.51 (total) | 0 | Loop iteration 1: link-following scout re-run on 16 hospitals |
| 2026-10-02 | 215 (total) | 0.55 (total) | 0 | Loop iteration 2: three domain fixes rebuilt |
| 2026-10-02 | 241 (total) | 0.61 (total) | 0 | Loop iteration 3: PDF download fallback; Baystate ×4, Cape Cod, Heywood, Athol rebuilt |
| 2026-10-02 | 243 (total) | 0.61 (total) | 0 | Loop iteration 4: Health Safety Net overlay on 19 sheets |
| 2026-10-02 | 289 (total) | 0.69 (total) | 0 | Loop iteration 5: batch 1 (10 hospitals) of the remaining MA hospitals |
| 2026-10-02 | 389 (total) | ~0.95 (total) | 0 | Loop iteration 6: final 20 hospitals; Phase 2 Tavily cap reached |

## Log

- 2026-10-02 15:40 — Spec, master plan, Phase 0 and Phase 1 plans written. Ready for task 0.1.
- 2026-10-02 16:20 — Phase 0 tasks 0.1–0.6 done by an implementation subagent: 28 tests pass, ruff clean. Deviations: `extend-exclude = ["docs"]` for ruff (it was reformatting code blocks in plan files); `AIClient` now creates an explicit `httpx.Client` when none is given (openai 3.x's default transport bypasses respx). Incident: one early test run reached the real Token Factory endpoint with a fake key (401, no secret leaked, no spend) → task 0.8 added. Spend: 0.
- 2026-10-03 04:55 — Loop iteration 17: task 7.0 done by a subagent (Phase 7 plan, 3,732 lines); national cost estimate corrected upward.
- 2026-10-03 04:15 — Loop iteration 16: Phase 6 tasks 6.1–6.2 done by a subagent (257 tests); Docker build blocked by a stale ghcr.io keychain credential → gate U6.0.
- 2026-10-03 03:35 — Loop iteration 15: task 6.0 done by a subagent (Phase 6 plan, 2,634 lines, Nebius docs verified 2026-10-02).
- 2026-10-03 03:00 — Loop iteration 14: Phase 5 tasks 5.7–5.10 done by a subagent; Phase 5 complete (248 tests). Admin token generated into `.env`.
- 2026-10-03 02:25 — Loop iteration 13: Phase 5 tasks 5.4–5.6 done by a subagent (233 tests); dev DB column added by hand; follow-up 5.10.
- 2026-10-03 01:50 — Loop iteration 12: Phase 5 tasks 5.1–5.3 done by a subagent (214 tests, no paid calls).
- 2026-10-03 01:15 — Loop iteration 11: task 5.0 done by a subagent (Phase 5 plan, 3,856 lines).
- 2026-10-03 00:45 — Loop iteration 10: task 2.8j (tie-break by a subagent, grounded-disagreement rule inline; 198 tests); four MGB hospitals published → 27 of 46. Spend 389 credits / $1.52.
- 2026-10-03 00:05 — Loop iteration 9: task 4.9 done by a subagent (187 tests); demo hospital hidden from public outputs.
- 2026-10-02 23:40 — Loop iteration 8: task 2.9c done inline (183 tests); overlay restored/applied on all 45 MA sheets for free. Report: 24 published (23 real + demo), 21 held, 2 none.
- 2026-10-02 23:15 — Loop iteration 7: tasks 2.8i/2.8g/2.8f done by a subagent (182 tests); free re-run of 29 held hospitals; follow-ups 2.9c, 2.8j, 2.8k added. Spend 389 credits / ≈$1.25.
- 2026-10-02 22:30 — Loop iteration 6: task 2.10 live runs finished (all 46 attempted, 15 published); Phase 2 Tavily cap reached (389); gates U2.1 and U2.2 opened; next work is free re-structuring.
- 2026-10-02 22:05 — Loop iteration 5: task 2.10 batch 1 (10 hospitals) run inline; Mercy published, 7 held, 2 skipped. Spend 289 credits / $0.69.
- 2026-10-02 21:55 — Loop iteration 4: task 2.9 implemented and run by a subagent (173 tests); all MA sheets carry the Health Safety Net entry. Spend 243 credits / $0.61.
- 2026-10-02 21:30 — Loop iteration 3: task 2.8c done by a subagent (170 tests); Baystate ×4 published; follow-ups 2.8g–2.8i added. Spend 241 credits / $0.61.
- 2026-10-02 21:00 — Loop iteration 2: task 2.8d done inline (161 tests); Fairview published. Spend 215 credits / $0.55.
- 2026-10-02 20:45 — Loop iteration 1: task 2.8b done by an implementer subagent (161 tests, ruff clean); 2 more hospitals published; follow-ups 2.8c–2.8f added. Spend 204 credits / $0.51.
- 2026-10-02 20:05 — Build day wrap-up. Phases 0, 1, 3, 4 complete (153 tests, ruff clean). Phase 2 code complete; live atlas runs for 21/46 MA hospitals produced 3 published + 14 held sheets and a long list of robustness fixes (see 2.8). Vision model = MiniCPM-V 4.5 (no NVIDIA vision model on Token Factory); Nemotron Super/Nano do structuring and cross-checks. Keys and vault secrets are in `.env`. Nothing pushed; no remote. Next for the loop: 2.8b, 2.9, 2.10, 4.9, then Phase 5 plan.
- 2026-10-02 19:10 — Phase 3 done by a subagent (136 tests); 2026-10-02 20:00 — Phase 4 done by a subagent (153 tests).
- 2026-10-02 16:55 — Phase 1 (1.1–1.7) and 0.8 done by a subagent: 72 tests pass, coverage 96.56%, ruff clean. Extra commit 67bd317 ignores `.hypothesis/`. Spend: 0.
- 2026-10-02 16:25 — Detailed plans for Phase 2 (MA atlas, 10 tasks) and Phase 3 (bill reading, 8 tasks) written. Only tasks 0.7, 2.8, 2.10, the live steps of 3.3/3.8 and the overlay live run need API keys; everything else runs on fakes.

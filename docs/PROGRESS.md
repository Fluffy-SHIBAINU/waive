# Waive build progress

Last updated: 2026-10-02 20:05 ET
Current phase: 2 (finishing the Massachusetts atlas) — Phases 0, 1, 3 and 4 are complete; Phase 2 has its code done and 21 of 46 hospitals attempted
Current plan: `docs/superpowers/plans/2026-10-02-waive-phase-2-ma-atlas.md`
Next task: 2.8b Improve document acquisition (see Phase 2 below), then 2.9, 2.10, then 5.0 (write the Phase 5 plan). Task 4.9 (demo hospital in real DB) is small and can be slotted in any time.

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
- [ ] **U2.1** Spot-check 10 Massachusetts procedure sheets field by field (Phase 2 exit).
- [ ] **U4.1** Complete the senior flow on your own phone with a synthetic bill (Phase 4 exit). Steps: `uv run waive corpus generate --count 3` (bills in `var/corpus/`), `uv run waive serve`, find the Mac's IP with `ipconfig getifaddr en0`, open `http://<ip>:8000` on a phone on the same Wi-Fi, start a case, open the senior link, photograph `var/corpus/bill-000.jpg` shown on the laptop screen. The web flow sends photos with `phi=True`, so for this synthetic-only test set `WAIVE_REQUIRE_ZDR=false` in `.env` temporarily (or close U0.4 first). The demo hospital is St. Example (`uv run waive demo seed` already ran); real MA sheets exist for Boston Medical Center, BIDMC and Anna Jaques.
- [ ] **U6.1** Approve the Nebius resources and their costs before anything is created (Phase 6).
- [ ] **U7.1** Approve the national Tavily credit budget (Phase 7).
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
- [ ] 2.8b Improve document acquisition (the real bottleneck, 2026-10-02 evening): for 14 held hospitals the fetched pages contain no poverty-level rules at all. Ideas, in order: (1) after extracting an entry page (class `billing`/`fap` HTML), collect links whose text or URL mentions financial assistance / policy / application / PDF and extract those too (Tavily Extract returns markdown with `[text](url)` links — parse them); (2) `search_depth="advanced"` for the two scouting queries; (3) Tavily Map with `max_depth=3`; (4) for hospital systems (Baystate, Mass General Brigham, Beth Israel Lahey, UMass Memorial, Tufts Medicine, Berkshire Health Systems) scout once per system domain and share the documents. Also: Berkshire's sliding-scale tiers do not parse (`discount_tiers: no number`) — inspect the draft and extend `_tiers`. Re-run held hospitals with `--rebuild --reuse-sources` after prompt/parser changes (no Tavily spend) and `--rebuild` after scouting changes (≈4 credits each).
- [ ] 2.9 Massachusetts overlay (`uv run waive atlas overlay --state MA`, ≈3 credits) — not yet run
- [ ] 2.10 Full MA run and Phase 2 exit (opens U2.1). Status 2026-10-02 19:50: 21 of 46 hospitals attempted; 3 published (Boston Medical Center, BIDMC, Anna Jaques), 14 held, 4 skipped/failed. Spend: 123 Tavily credits, $0.35 Token Factory. Remaining 25 hospitals ≈ 100 credits. Budget left for Phase 2: 277 credits.

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
- [ ] 4.9 The fictional demo hospital (CCN 229999, St. Example) now sits in the real `var/waive.db` and appears in `atlas export/report`. Either exclude it from exports and the public atlas list (flag demo rows) or run demos against a separate `WAIVE_DATABASE_URL`.

### Phase 5 — Learning loop
- [ ] 5.0 Write detailed Phase 5 plan

### Phase 6 — Deploy on Nebius AI Cloud
- [ ] 6.0 Write detailed Phase 6 plan

### Phase 7 — Always-on scouting and national scale
- [ ] 7.0 Write detailed Phase 7 plan

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

## Log

- 2026-10-02 15:40 — Spec, master plan, Phase 0 and Phase 1 plans written. Ready for task 0.1.
- 2026-10-02 16:20 — Phase 0 tasks 0.1–0.6 done by an implementation subagent: 28 tests pass, ruff clean. Deviations: `extend-exclude = ["docs"]` for ruff (it was reformatting code blocks in plan files); `AIClient` now creates an explicit `httpx.Client` when none is given (openai 3.x's default transport bypasses respx). Incident: one early test run reached the real Token Factory endpoint with a fake key (401, no secret leaked, no spend) → task 0.8 added. Spend: 0.
- 2026-10-02 20:05 — Build day wrap-up. Phases 0, 1, 3, 4 complete (153 tests, ruff clean). Phase 2 code complete; live atlas runs for 21/46 MA hospitals produced 3 published + 14 held sheets and a long list of robustness fixes (see 2.8). Vision model = MiniCPM-V 4.5 (no NVIDIA vision model on Token Factory); Nemotron Super/Nano do structuring and cross-checks. Keys and vault secrets are in `.env`. Nothing pushed; no remote. Next for the loop: 2.8b, 2.9, 2.10, 4.9, then Phase 5 plan.
- 2026-10-02 19:10 — Phase 3 done by a subagent (136 tests); 2026-10-02 20:00 — Phase 4 done by a subagent (153 tests).
- 2026-10-02 16:55 — Phase 1 (1.1–1.7) and 0.8 done by a subagent: 72 tests pass, coverage 96.56%, ruff clean. Extra commit 67bd317 ignores `.hypothesis/`. Spend: 0.
- 2026-10-02 16:25 — Detailed plans for Phase 2 (MA atlas, 10 tasks) and Phase 3 (bill reading, 8 tasks) written. Only tasks 0.7, 2.8, 2.10, the live steps of 3.3/3.8 and the overlay live run need API keys; everything else runs on fakes.

# Waive build progress

Last updated: 2026-10-02 16:58 ET
Current phase: 2 — Massachusetts atlas (Phases 0 and 1 done; 0.7 live check waits for keys)
Current plan: `docs/superpowers/plans/2026-10-02-waive-phase-2-ma-atlas.md`
Next task: 2.1 Database foundation (2.1–2.7 need no keys; 2.8+ need U0.1 and U0.2)

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
- [ ] **U0.4** Turn on zero data retention for Token Factory (console setting if available, otherwise ask Nebius support), then set `WAIVE_ZDR_CONFIRMED=true` in `.env`. Until then the build uses synthetic data only. Blocks real bills.
- [ ] **U0.5** Install the Nebius CLI (`curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash`) and run `nebius profile create` (browser sign-in). Needed in Phase 6.
- [ ] **U0.6** Optional: exempt this repo from GateGuard first-touch prompts (`GATEGUARD_EXEMPT_GLOBS`) so loop iterations don't stall on every new file.
- [ ] **U2.1** Spot-check 10 Massachusetts procedure sheets field by field (Phase 2 exit).
- [ ] **U4.1** Complete the senior flow on your own phone with a synthetic bill (Phase 4 exit).
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
- [ ] 2.8 First live batch (needs U0.1, U0.2, task 0.7)
- [ ] 2.9 Massachusetts overlay
- [ ] 2.10 Full MA run and Phase 2 exit (opens U2.1)

### Phase 3 — Bill reading and cases
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-3-bill-reading.md`
- [x] 3.0 Write detailed Phase 3 plan
- [ ] 3.1 Image intake
- [ ] 3.2 Synthetic bill corpus
- [ ] 3.3 Vision extraction (live smoke needs U0.1)
- [ ] 3.4 Hospital matching
- [ ] 3.5 Case vault: encryption, capability tokens, case rows
- [ ] 3.6 Case service
- [ ] 3.7 Log hygiene check
- [ ] 3.8 Accuracy report `waive eval bills` (live run needs U0.1)

### Phase 4 — Phone web app and packet
Plan: `docs/superpowers/plans/2026-10-02-waive-phase-4-phone-app.md`
- [x] 4.0 Write detailed Phase 4 plan
- [ ] 4.1 App skeleton, layout and styles
- [ ] 4.2 Senior flow
- [ ] 4.3 Caregiver flow
- [ ] 4.4 Application packet (PDF, reportlab)
- [ ] 4.5 Calendar reminders (.ics)
- [ ] 4.6 Public atlas pages
- [ ] 4.7 Demo seed and phone test (opens U4.1)
- [ ] 4.8 Accessibility and E2E checks (optional)

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

## Log

- 2026-10-02 15:40 — Spec, master plan, Phase 0 and Phase 1 plans written. Ready for task 0.1.
- 2026-10-02 16:20 — Phase 0 tasks 0.1–0.6 done by an implementation subagent: 28 tests pass, ruff clean. Deviations: `extend-exclude = ["docs"]` for ruff (it was reformatting code blocks in plan files); `AIClient` now creates an explicit `httpx.Client` when none is given (openai 3.x's default transport bypasses respx). Incident: one early test run reached the real Token Factory endpoint with a fake key (401, no secret leaked, no spend) → task 0.8 added. Spend: 0.
- 2026-10-02 16:55 — Phase 1 (1.1–1.7) and 0.8 done by a subagent: 72 tests pass, coverage 96.56%, ruff clean. Extra commit 67bd317 ignores `.hypothesis/`. Spend: 0.
- 2026-10-02 16:25 — Detailed plans for Phase 2 (MA atlas, 10 tasks) and Phase 3 (bill reading, 8 tasks) written. Only tasks 0.7, 2.8, 2.10, the live steps of 3.3/3.8 and the overlay live run need API keys; everything else runs on fakes.

# Waive build progress

Last updated: 2026-10-02 15:40 ET
Current phase: 0 — Foundations and connectivity
Current plan: `docs/superpowers/plans/2026-10-02-waive-phase-0-foundations.md`
Next task: 0.1 Project scaffold

Master plan: `docs/superpowers/plans/2026-10-02-waive-master-plan.md` · Spec: `docs/superpowers/specs/2026-10-02-waive-design.md` · Loop rules: `docs/LOOP.md`

## User gates (only the user closes these)

- [ ] **U0.1** Create a Token Factory API key at https://tokenfactory.nebius.com/project/api-keys and put it in `.env` as `NEBIUS_API_KEY=...` (copy `.env.example` to `.env` first). Never paste it in chat. Blocks task 0.7.
- [ ] **U0.2** Put your Tavily key in `.env` as `TAVILY_API_KEY=...`. Blocks task 0.7.
- [ ] **U0.3** Put your AI Cloud project ID in `.env` as `NEBIUS_PROJECT_ID=...` (the `project-…` part of your console URL). Needed in Phase 6.
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
- [ ] 0.1 Project scaffold
- [ ] 0.2 Settings
- [ ] 0.3 Usage ledger and governor
- [ ] 0.4 Token Factory client
- [ ] 0.5 Tavily gateway
- [ ] 0.6 `waive doctor`
- [ ] 0.7 Live connectivity check (needs U0.1, U0.2)

### Phase 1 — Domain core
- [ ] 1.1 Poverty guidelines
- [ ] 1.2 Procedure sheet schema and sample hospital
- [ ] 1.3 Eligibility engine
- [ ] 1.4 Deadlines
- [ ] 1.5 Plain-language messages
- [ ] 1.6 Quote verification
- [ ] 1.7 Phase 1 exit check

### Phase 2 — Massachusetts atlas
- [ ] 2.0 Write detailed Phase 2 plan (tasks are listed here once written)

### Phase 3 — Bill reading and cases
- [ ] 3.0 Write detailed Phase 3 plan

### Phase 4 — Phone web app and packet
- [ ] 4.0 Write detailed Phase 4 plan

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
- [ ] Exact Token Factory model IDs (reason, fast, vision); vision image input format; vision price
- [ ] AI Cloud project region; smallest CPU endpoint preset; smallest PostgreSQL preset and price
- [x] 2026 poverty guidelines — verified 2026-10-02 (ASPE): 48 states and DC $15,960 + $5,680; AK $19,950 + $7,100; HI $18,360 + $6,530
- [ ] Massachusetts Health Safety Net rules and how MA hospital FAPs route applications
- [ ] 501(r) notice rules beyond days 120 and 240
- [ ] Licenses (defaults: Apache-2.0 code, CC BY 4.0 atlas data)

## Spend

| Date | Tavily credits | Token Factory $ | AI Cloud $ | Note |
|---|---|---|---|---|
| 2026-10-02 | 0 | 0 | 0 | Planning only |

## Log

- 2026-10-02 15:40 — Spec, master plan, Phase 0 and Phase 1 plans written. Ready for task 0.1.

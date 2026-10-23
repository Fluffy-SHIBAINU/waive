# Waive atlas expansion: run report and quality findings

Run window: 2026-10-22 23:45 UTC to 2026-10-23 04:30 UTC. Agent: `waive-agent` on Vercel (Eve 0.82), team `maic-vercel-jfish`.

## Final tally, run complete (2026-10-23 04:30 UTC)

The national pass finished: every queued hospital was attempted. No gateway errors occurred; the run ended at a balance of -$57.82 on the user's instruction to finish on overdraft.

| Metric | Value |
|---|---|
| Draft sheets on disk | 1,348 |
| States with drafts | 42 (every state outside the 9 already published, plus DC) |
| Unique hospitals attempted | 1,701 |
| Built under Opus 5.5 structuring (first generation) | 645 |
| Built under Sonnet 5 structuring (tagged) | 703 |
| No official policy found by the scout | 196 |
| Site blocked the fetcher (403) | 201 |
| Drafts with a free-care line | 1,037 (77%) |
| Drafts with discount tiers | 749 (56%); 649 survive Waive's tier-order schema |
| Drafts with no fields | 20 |
| Structural validator | 705 clean, 162 errors, 846 warnings |
| Converted into Waive draft procedure sheets | 1,348 of 1,348, 42 state files in `data/atlas-eve/` |

Largest: Wisconsin 105, Michigan 88, Indiana 70, Minnesota 62, Kentucky 58, Missouri 57, North Carolina 53, Virginia 53, Georgia 49.

Snapshot: `~/waive-atlas-backup/complete-run-20261022-2332` with a SHA-256 manifest.

## What is required before any of this reaches patients

Measured with Waive's own `verify_sheet` and `sheet_inconsistencies` over all 1,348 converted sheets, each source stood in by its quotes (every rule runs except substring-in-document, which the agent already enforced against the fetched text). Results are in `data/atlas-eve/_waive-verify-review.md`.

| Metric | Count |
|---|---|
| Sheets whose income rule (free-care line or tiers) passes Waive's verifier | 972 of 1,348 (72%) |
| Review-ready: income rule verified, no inconsistency, core completeness at least 50% | 926 (68%) |
| No verified income rule | 376 |
| Publish-time inconsistency (free line above first band, discounts rising with income) | 23 |
| Core completeness under 50% | 30 |

### 1. Refetch and verify (no model cost, network only)

The import stores no source text. Run the normal path on the stored URLs so `quote_found` runs against real documents: `waive atlas build --state XX --ccn NNNNNN --reuse-sources` after a fetch, or a small `refetch` step over `sources[].url`. Expect a few percent of quotes to fail on PDFs whose text layer differs between `unpdf` and `pdf_worker.py`.

### 2. Two verifier changes in this PR, to review

- `quote_found` accepts a quote made of up to three verbatim spans joined with `" || "`, each checked on its own. Tier tables and document lists span rows and sentences.
- `value_in_quote` accepts a tier's exclusive lower bound when the quote states the written inclusive start: "201% to 400%" supports `min_fpl_exclusive = 200`, as do "200.1%" and "200.01%". This moved verified tier tables from 114 to 279. Previously only a quote that also contained the previous band's upper bound passed.

### 3. Rebuild the 376 sheets with no verified income rule, and the 370 rejected tier tables

Most are Opus-era drafts whose tier quote holds bounds without percentages, or percentages without bounds. The current prompt (v4, Sonnet 5 structuring) quotes whole table rows. Cost about $0.12 per hospital, so about $90 for the income-rule set; the tier-only rejections largely overlap with it.

### 4. Align three list fields with Waive's vocabulary, then prune

`apply.documents_required` (820 rejected), `programs.presumptive` (746) and `eligibility.asset_test` (607) fail Waive's marker rules: the quote must name each document type, describe automatic eligibility, or contain an asset word plus a tested or negated verb. These are not blocking; `prune_lists` keeps the supported subset at publish. Adding Waive's marker vocabulary to the extraction prompt fixes the next run. Asset-test quotes like "Assets will also be used when:" need the verb Waive expects (considered, reviewed, counted, included).

### 5. Recover 180 blocked hospitals with a browser fetch

Sites behind a WAF return 403 to server-side fetches: Intermountain (13), Sanford (11), Avera (10), CHI Health (10), PeaceHealth, MultiCare, VMFH, MercyOne, CHRISTUS. Concentrated in SD, UT, NE, WA, MT, ND, CO. A headless browser or the Vercel Sandbox with a real browser recovers most. No model cost beyond the normal build.

### 6. Scout again for 177 hospitals with no official policy found

Puerto Rico 24, Minnesota 20, Iowa 15, Oklahoma 12, Michigan 11. Some are system hospitals whose policy sits on the parent domain; a second scout with the system name usually finds it. Others may have no web policy, which is itself a finding.

### 7. Human review queue

853 review-ready sheets carry judge notes; 23 carry a publish-time inconsistency and 30 are under 50% core completeness. The notes sit in each sheet's `eve_review` list and in the `eve-import` review item when imported with `--db`. A reviewer reads the income rule, the quote, and the source URL; everything else can be pruned by rule.

### 8. Waive-side fixes found along the way

Asset tests and application windows stated in the same documents are missing from several published MA, NY and IL sheets. (An earlier draft of this report called four published phone fields concatenated; that was the evaluation stripping non-digits from "A or B" values Waive stores on purpose. Withdrawn.)

## What was produced

| Metric | Value |
|---|---|
| Draft sheets on disk | 1,039 |
| States covered (previously 9 published states) | 27 new states |
| Unique hospitals attempted | 1,200 |
| Drafts built under Opus 5.5 structuring (untagged, first generation) | 645 |
| Drafts built under Sonnet 5 structuring (tagged `models.structuring`) | 394 |
| No official policy found by the scout | 153 |
| Site blocked the fetcher (HTTP 403) | 52 |
| Drafts with a free-care line | 821 (79%) |
| Drafts with discount tiers | 555 (53%) |
| Drafts with no fields at all | 14 |

Largest new states: Wisconsin 105, Michigan 88, Indiana 70, Minnesota 62, Kentucky 58, Missouri 57, North Carolina 53, Virginia 53.

Locations: live set at `~/waive-atlas/atlas-drafts/<STATE>/<CCN>.json` (symlinked from `data/atlas-drafts`), outcomes in `_log.jsonl`, structural validation in `_validation.jsonl` and `_validation-report.md`. Immutable snapshots with SHA-256 manifests: `~/waive-atlas-backup/opus-run-20261022-2227` (645 drafts) and `~/waive-atlas-backup/final-run-20261022-2255` (1,039 drafts plus all eval sets).

Every draft has `status: "draft"`. Every non-null field carries a verbatim quote that was verified as an exact substring of its fetched source (or a typography-normalized match, flagged), the source id, the source SHA-256, and the check date.

## Accuracy against published Waive sheets

Method: rebuild hospitals Waive has already published, compare field by field. `scripts/eval-vs-atlas.ts`.

### 22 hospitals (MA, NY, IL), prompt v3, Sonnet 5 structuring

| Field | Agree | Disagree | Eve only | Waive only |
|---|---|---|---|---|
| free_care_max_fpl | 15 | 1 | 1 | 3 |
| discount_tiers | 9 | 0 | 3 | 2 |
| asset_test | 4 | 0 | 10 | 0 |
| window_days_from_first_bill | 5 | 0 | 5 | 3 |
| decision_days | 2 | 0 | 3 | 1 |
| eca_wait_days | 1 | 0 | 3 | 0 |
| phone | 10 | 6 | 6 | 0 |

Before the prompt fixes (baseline, same hospitals): tiers 5 agree and 5 disagree, 14 ceilings over-read. After: tiers 9 agree and 0 disagree, 1 ceiling over-read.

### Model comparison, same 8 hospitals

| Configuration | free_care agree | tiers agree | tiers wrong |
|---|---|---|---|
| Sonnet 5 structuring | 6 | 4 | 0 |
| Haiku 5.5 structuring, strict Haiku judge | 3 | 0 | 1 |
| Haiku 5.5 structuring, annotate-only judge | 5 | 0 | 2 |

Haiku folds the free-care row into the tier table. Sonnet 5 is the floor for structuring. The Haiku judge was too strict to be allowed to drop fields; it now writes `review_notes` only.

## Findings for Waive's own pipeline

1. Phone disagreements are a comparison artifact, not a Waive bug. Waive keeps a hospital's readable text such as "518-262-1981 or 1-866-262-7476" on purpose (its own test pins this); the evaluation stripped non-digits before comparing, which glued the two numbers together. Eve returns one number. Both are defensible; nothing to fix in Waive.
2. Asset tests missed. Ten of 22 published sheets have `asset_test` empty where the same policy states one.
3. Application windows missed. Five published sheets lack `window_days_from_first_bill` where the policy states it.
4. OSF Holy Family free-care line: published 350, Eve 250. Needs a human read of the PDF.
5. Faulkner Hospital phone: published 617-983-7218, Eve 617-726-3884 (the system-wide line). Both are real; the sheet should say which.

## Findings for the Eve pipeline (open issues in the drafts)

Structural validator over 1,039 drafts: 601 clean, 99 errors, 578 warnings.

| Check | Drafts | Meaning |
|---|---|---|
| no_income_rule | 200 | neither free-care line nor tiers captured; sheet cannot drive eligibility |
| normalized_match | 145 | quote matched only after typography normalization; usually fine |
| tier_values_in_quote | 98 | a tier bound or percent is not in the tiers quote; mostly Opus-era drafts |
| value_in_quote | 62 | number not present as digits in quote; often words such as "forty-eight hours" |
| form_url_absolute | 50 | link text instead of URL; HTML href extraction was added mid-run |
| tier_discount_monotonic | 11 | discounts not decreasing with income; check manually |
| ceiling_equals_free_no_tiers | 6 | residual ceiling over-reads from the Opus era |

Judge review notes (annotate-only, Sonnet-era drafts): 361 drafts, 1,064 notes. Most are list fields where a quote covers only part of a list.

Recommended before publishing any draft: (1) rebuild the 645 Opus-era drafts with the final prompt and Sonnet structuring, since they predate the tier, ceiling, URL, and insured-patient rules; (2) human review of every draft with a validator error or a review note on `eligibility.*`; (3) treat `no_income_rule` drafts as contact-only records.

## Cost and incidents

| Item | Value |
|---|---|
| Credit at start | $494.78 |
| Credit at end | -$6.58 |
| waive-agent share | about $460 |
| Cost per hospital, Opus era | about $0.45 |
| Cost per hospital, Sonnet structuring with Haiku root, scout, judge | $0.10 to $0.14 |

What went wrong:

1. The local `eve dev` REPL ships Eve's self-modification agent. At 01:51 UTC it rewrote `agent/agent.ts`, the scout, and `structure.ts` to `anthropic/claude-opus-5.5` and added a second Opus pass (`judge.ts`). Every deploy after that shipped Opus. That is where most of the credit went.
2. Two batch runners ran at once near the end (one at concurrency 32 that this session did not start, one at 16). Together they spent $47 in ten minutes and overdrew the pool before the balance guard, which checks once a minute, could stop the 16-runner. The 32-runner predates the guard.
3. Hospital sites behind WAFs return 403 to server-side fetches: Concord NH, FirstHealth NC, MyMichigan, Bronson, Johns Hopkins Howard County, and others. A headless-browser fetch would recover these.
4. Killing processes by script name also killed the monitors, whose command text contained the name. Monitors now watch a pid file.

## Next steps

1. Rebuild the 645 Opus-era drafts with the final prompt (Sonnet 5): about $90.
2. Finish the remaining uncovered hospitals, mostly critical-access: about 400 at $0.12 each.
3. Recover the 52 blocked sites with a browser fetch.
4. Import drafts into Waive: `data/atlas/<STATE>.json` is the repo's export format; a `waive atlas import` for draft sheets is needed, with `status: draft` preserved until a reviewer publishes.
5. Fix the Waive phone-concatenation bug and backfill asset tests and application windows from the same sources.

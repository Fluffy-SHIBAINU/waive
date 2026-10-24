# Confirm held sheets' websites with Waive's own discovery rule (speed-up step 2)

Spec: the user, 2026-10-24: "Feel free to speed up the process by using more tavily credit and nebius credit as long nebius credit doesn't go below $20", with the plan recorded in `docs/PROGRESS.md` (Log, 2026-10-24 00:20Z): "confirm the held sheets' websites with Waive's own discovery rule over a Tavily search". Hard rules: every paid call through the governor; only Tavily and Nebius-served models; never retry a CCN on the skip list; published sheets only through the normal verify and publish path.

Why: 75 Eve-state sheets (and more after the running rebuild) carry an income rule Waive's verifier accepted but are held only because their website was matched with low confidence (0.6, `domain_unconfirmed`). Waive normally learns a website through `discover.discover_domain` (one Tavily search, `pick_domain`: 0.9 when sure, else 0.6). Running that same search for these hospitals and accepting its sure answer only when it names the website already stored is the normal discovery path, not an admin confirmation (which is 1.0 and stays the admin's).

## Global constraints

- Python 3.12; tests first; `uv run ruff format . && uv run ruff check . && uv run pytest` must pass (1,172 tests pass before this task). Unit tests cannot open sockets; fake gateways only.
- Never set confidence 1.0 (`CONFIRMED_CONFIDENCE`); only an admin confirms. Use the discovery result's own confidence.
- A search whose sure answer names a different site changes nothing on the hospital: it is reported (and filed as a review item) for a person.
- Skip-listed CCNs (`waive.atlas.skiplist.load_skip_list`) are never searched.
- Do not touch `agent/`, `data/`, `docs/PROGRESS.md`, `docs/LOOP.md` or the plan file.

## Task 1: `waive atlas confirm-websites`

Files: create `src/waive/atlas/website_confirm.py`; modify `src/waive/cli.py`; tests `tests/unit/test_website_confirm.py` (module and a hermetic CLI test in the style of `tests/unit/test_cli_skiplist.py`).

Behaviour:
1. `candidates(session, *, states=None, skip=None) -> list[HospitalRow]`: hospitals whose `website_domain` is set with `domain_confidence` below `MIN_CONFIDENCE` (not None, not 1.0), that have an open `domain_unconfirmed` review item, whose latest sheet is held and carries at least one of `free_care_max_fpl`, `discount_tiers`, `assistance_ceiling_fpl`; skip-listed CCNs and other states left out; ordered by state then CCN.
2. `confirm_by_search(session, gateway, row) -> SearchOutcome(ccn, state, outcome, found_domain, confidence, evidence_url)`: one `discover.discover_domain(gateway, repo.hospital_ref(row), discover.rejected_domains(session, row.ccn))` call.
   - `confirmed` when the result's confidence is at least `MIN_CONFIDENCE` and `host_of` of its domain equals `host_of` of the stored website: set `row.domain_confidence` to the result's confidence and file a review item of kind `website_searched` with `{"domain", "confidence", "evidence_url", "outcome": "confirmed"}`.
   - `other_site` when the result is sure but names another domain: no change to the hospital; file `website_searched` with the outcome and the found domain.
   - `unconfirmed` otherwise (no result, or 0.6): no change; file `website_searched` with the outcome.
   - `BudgetExceeded` propagates to the caller.
3. CLI `waive atlas confirm-websites [--state XX] [--limit N] [--dry-run/--run] [--out PATH]` (default dry run, which spends nothing and prints `website search plan: <n> held sheets on unconfirmed websites; about <credits> Tavily credits (one search each)`, credits from `tavily_gateway.search_cost` at the depth `discover_domain` uses). With `--run`: one search per candidate in order, commit after each, stop cleanly on `BudgetExceeded` (say so), print counts per outcome, and merge the `ST CCN` lines of the confirmed hospitals into `--out` (default `var/websites-confirmed.txt`, union and sorted, as `waive atlas eve-refetch` does with its list) for the rebuilds. A non-budget error stops the run after writing what was confirmed so far, then exits 1.

Tests to write first (each must fail before the change):
- `candidates`: a held sheet with an income rule on a 0.6 website and an open `domain_unconfirmed` item is listed; one without an income rule, one at 0.9, one at 1.0, one with no open item, one published, and one on the skip list are not.
- `confirm_by_search` with a fake gateway whose search hits make `pick_domain` sure for the stored site → `confirmed`, confidence 0.9 stored, a `website_searched` item; sure for another site → `other_site`, hospital unchanged; a hit that is only a mention → `unconfirmed`, unchanged.
- CLI: dry run spends nothing (the fake is never called) and prints the plan line; `--run` writes and merges the out file; a budget stop keeps what was confirmed.

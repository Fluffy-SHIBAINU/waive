# Task 7.12d — refetch the Eve sheets' missing sources through Tavily Extract

Spec: the user's 7.12d instruction (`docs/PROGRESS.md`, Phase 7, block 7.12): "data/atlas-eve/_missing-sources.json lists 134 sheets with 175 source URLs our downloader could not fetch. Refetch them through the scout's Tavily Extract path (advanced depth), then rebuild those CCNs with --reuse-sources. Budget for this block: 200 Tavily credits; stop and ask if it needs more." Block rules: "Never retry a CCN in _skip.json."

Measured 2026-10-23 on this Mac's atlas: the 175 listed URLs are 100 unique URLs (one Novant Health document is listed by 32 hospitals); for 32 of the 175 (hospital, URL) pairs the stored source already has text, so they need nothing; 131 pairs point at a stored source with empty text and 12 at a URL the hospital has no stored source for. No listed CCN is on the skip list. Tavily Extract at advanced depth costs 2 credits per 5 URLs (`tavily_gateway.extract_cost`), so 5 URLs per call keeps the whole refetch near 40 credits.

## Global constraints

- Python 3.12; tests first; `uv run ruff format . && uv run ruff check . && uv run pytest` must pass (1,133 tests pass before this task). Unit tests cannot open sockets; tests use fake gateways only, never the real Tavily client.
- Every paid call goes through the governor (`TavilyGateway.extract` already does: `ensure_tavily` before, `record_tavily` after). Nothing here may bypass it.
- The skip list (`waive.atlas.skiplist.load_skip_list`) wins: a listed CCN is never refetched.
- `repo.save_source` never overwrites an existing row's text, so a refetched document is stored as a new source the way the scout stores what it scouts: `scout.store_scouted(session, ccn, [ScoutedDoc], today)` with id `scout.source_id_for(doc_class, sha256)`, kind `hospital_web`, today's date.
- Do not touch `agent/`, the Eve runner, anything under `data/`, `docs/PROGRESS.md` or the plan file.
- Comments and docstrings in the repo's style: plain sentences saying why.

## Task 1: `waive atlas eve-refetch`

Files:
- Create `src/waive/atlas/eve_refetch.py`.
- Modify `src/waive/cli.py` (new command).
- Tests: create `tests/unit/test_eve_refetch.py` (module and CLI; the CLI test in the hermetic CliRunner style of `tests/unit/test_cli_skiplist.py`).

Behaviour:

1. `load_missing(path: Path) -> list[MissingEntry]` reads `data/atlas-eve/_missing-sources.json` (a list of objects with `ccn`, `state`, `name`, `missing_urls` (list of strings), `fetched`, `total`); a missing file is an empty list.
2. `plan_refetch(session, entries, skip) -> RefetchPlan`: drops skip-listed CCNs; for each remaining (CCN, URL) pair, drops it when a source linked to that CCN with that exact URL already has non-empty text; the plan holds the unique URLs to fetch (first-seen order) and, for each URL, the CCNs that want it and the title to store it under (the title of a stored source with that URL, else ""). The plan reports `pairs`, `already_have_text`, `skipped_ccns`, `urls`, and `estimated_credits` = `extract_cost(len(urls), "advanced")` computed per batch of 5.
3. `run_refetch(session, gateway, plan, today, *, batch_size=5) -> RefetchReport`: calls `gateway.extract(batch, purpose="atlas.eve_refetch", depth="advanced")` once per batch of up to 5 unique URLs. A page is kept only when its text, cut to `scout.MAX_CHARS`, has at least `scout.MIN_CHARS` characters, it is not a navigation shell (`scout.navigation_shells` over the batch's pages), and it is a PDF, has `scout.THIN_CHARS` characters or more, or its prose names financial assistance (the same test as the scout's `_usable_download`; reuse it, do not copy it). A kept page is stored once per CCN that wants it with `store_scouted` (doc class `scout.classify_doc(url, title)`, else `"fap"`). `BudgetExceeded` stops the run with what was stored so far (the report says so). The report gives URLs requested, pages returned, pages kept, sources stored, credits spent (governor before and after), the reasons pages were not kept, and the sorted list of `(state, ccn)` that gained at least one source.
4. CLI `waive atlas eve-refetch [--file PATH] [--dry-run/--run] [--out PATH]`: default dry run (nothing spent) prints the plan line `refetch plan: <urls> unique URLs for <ccns> hospitals (<pairs> listed pairs, <have> already have text, <skipped> on the skip list); about <credits> Tavily credits at advanced depth`. With `--run` it runs, commits, prints `refetched <kept> of <urls> URLs; stored <n> sources for <m> hospitals; <credits> Tavily credits spent`, and writes the `(state, ccn)` pairs that gained a source to `--out` (default `var/eve-refetched.txt`, one `ST CCN` per line) for the rebuilds.

Tests to write first (each must fail before the change):
- `load_missing` reads entries; a missing file is `[]`.
- `plan_refetch` on an in-memory database: a URL listed by two hospitals is fetched once and wanted by both; a pair whose stored source already has text is dropped and counted; a skip-listed CCN is dropped and counted; the credit estimate for 7 URLs is 4 (two batches at advanced depth).
- `run_refetch` with a fake gateway that records its calls: batches of at most 5 at `depth="advanced"` with the purpose above; a kept page is stored as a new `hospital_web` source linked to every CCN that wants it (and the old empty-text row is left alone); a too-short page, a navigation shell (two URLs, same menu text) and a URL the gateway did not return are not stored and are counted with their reasons; `BudgetExceeded` from the gateway stops the run and keeps what was stored before.
- CLI: the dry run prints the plan line and spends nothing (the fake gateway is never called); `--run` writes the `ST CCN` file.

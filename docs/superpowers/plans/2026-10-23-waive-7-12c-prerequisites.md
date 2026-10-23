# Task 7.12c prerequisites — Eve websites without search, and the owner's decisions kept on rebuild

Spec: the user's 7.12c instruction and block rules, recorded in `docs/PROGRESS.md` (Phase 7, block 7.12): "7.12c Rebuild from stored documents, no Tavily. data/atlas-eve/_rebuild.json lists 384 hospitals … For each, `uv run waive atlas build --state XX --ccn NNNNNN --reuse-sources`, about 50 per iteration, respecting the Token Factory cap" and "Keep every decision in _decisions.json intact; if a rebuild would overwrite one of those 30 sheets, keep the owner's decision and note it." Also: "Published sheets must come only through the normal verify and publish path."

Why these two pieces come first:
- The 1,340 Eve hospitals were stored by `waive atlas import-eve --db` with registry rows from the CMS seed, which has no website. The Eve draft's website lives only in the stored sheet body (`sheet.hospital.website_domain`). `build_hospital` runs `discover_domain` (a Tavily search) whenever the registry row has no `website_domain`, so a `--reuse-sources` rebuild would spend Tavily credits on every one of them.
- Ten hospitals the owner ruled on are on the rebuild or missing-sources lists (decisions `discount`: 040076, 281339, 150044, 180103, 180104, 180130, 241372; `unsure`: 060107, 110036; `accept`: 351323). A rebuild asks the model again and may read the same number as free care that the owner ruled is not.

## Global constraints

- Python 3.12; tests first; `uv run ruff format . && uv run ruff check . && uv run pytest` must pass (1,061 tests pass before this task). Unit tests cannot open sockets; nothing in this task may make a paid call.
- Waive's discovery rule is `waive.atlas.discover.pick_domain(hospital, hits, rejected)`; it returns a `DomainResult(domain, confidence, evidence_url)` with confidence 0.9 ("sure": the title names the hospital and the hit names its place, or the phone matches, and the hit does not name only other states) or 0.6. `host_of(url)` gives the registered domain. `is_directory(host)` and `is_not_a_website(url)` refuse directories and file hosts. Reuse these; do not write a second rule.
- `MIN_CONFIDENCE` is 0.8 and `CONFIRMED_CONFIDENCE` 1.0 (`waive.atlas.discover`). Never set 1.0 here: only an admin confirms a website.
- Owner decisions are read with `waive.atlas.eve_import.load_decisions(path)` from `data/atlas-eve/_decisions.json`: a list of objects with `ccn`, `decision` (`accept` | `discount` | `unsure` | `hold`), `value` (the recorded percentage as a string, e.g. "300", "40.9", "0"), `note`, `at` (ISO time), `hospital`, `state`, `bucket`. Do not edit that file or anything else under `data/`.
- The guard changes what the model proposed before Waive verifies it; it never adds a value the documents do not quote and never skips verification.
- Do not touch anything under `agent/`, the Eve runner, `data/`, or `docs/PROGRESS.md`. Comments and docstrings in the repo's style: plain sentences saying why.

## Task 1: Eve websites from the stored documents, and the owner's decisions kept on rebuild

Files:
- Modify `src/waive/atlas/eve_import.py` (new functions below), `src/waive/atlas/pipeline.py` (`build_hospital`, `_structure_and_verify`, `_tiebreak`, `build_state`), `src/waive/cli.py` (`atlas build`, a new `atlas eve-websites`), `src/waive/config.py`.
- Tests: create `tests/unit/test_eve_import.py`; add to `tests/unit/test_pipeline.py`; a CLI test file `tests/unit/test_cli_eve.py` in the style of `tests/unit/test_cli_skiplist.py`.

Behaviour:

1. `eve_import.eve_website(hospital: HospitalRef, domain: str, sources: list[tuple[SourceDoc, str]]) -> DomainResult | None`:
   - Normalise `domain` with `host_of("https://" + domain)`. Return None when it is empty, a directory (`is_directory`) or a file host (`is_not_a_website`).
   - Build a `SearchHit(url=source.url, title=source.title, content=text, score=0.0)` for every stored source whose URL's `host_of` equals the normalised domain, and run `pick_domain(hospital, hits)`.
   - Return `DomainResult(normalised_domain, 0.9, evidence_url)` when that result is sure (0.9) for the same domain, otherwise `DomainResult(normalised_domain, 0.6, evidence_url or "")`. No network, no gateway.
2. `eve_import.set_eve_websites(session, state: str | None = None) -> EveWebsites` (a small dataclass of counts: `sure`, `unsure`, `already_set`, `no_domain`, `refused`):
   - The hospitals: every CCN with a review item of kind `eve-import` (any status), optionally one state.
   - A registry row that already has a `website_domain` is left alone (`already_set`).
   - Otherwise take `website_domain` from the latest sheet's `hospital.website_domain`; none → `no_domain`; `eve_website` None → `refused`; else set `row.website_domain` and `row.domain_confidence` from the result (`sure` for 0.9, `unsure` for 0.6).
   - Rerunning changes nothing (everything is then `already_set`).
3. CLI `waive atlas eve-websites [--state XX]`: runs `set_eve_websites`, commits, prints `websites set for <n> Eve hospitals: <sure> sure (0.9), <unsure> unconfirmed (0.6); <already_set> already had one, <no_domain> without one in the draft, <refused> refused (directory or file host)`. No paid calls.
4. `eve_import.keep_owner_decision(sheet: ProcedureSheet, decision: Mapping[str, Any] | None) -> tuple[ProcedureSheet, list[str]]`:
   - Returns the sheet unchanged and `[]` when `decision` is None, its verdict is `accept` or unknown, its `value` does not parse as a number, the sheet has no `free_care_max_fpl`, or that field's value differs from the decided value (a different reading is a different claim, left to the verifier).
   - `discount` with equal values: drop `free_care_max_fpl`; when the sheet has no `assistance_ceiling_fpl`, record the same cited field (value, quote, source, layer, checked date) as `assistance_ceiling_fpl`. Note: `eligibility.free_care_max_fpl: owner review says <v>% is a discount or assistance limit, not free care; recorded as assistance_ceiling_fpl` (or `; dropped, the sheet already records a ceiling`).
   - `unsure` or `hold` with equal values: drop `free_care_max_fpl`. Note: `eligibility.free_care_max_fpl: owner review could not tell whether <v>% is free care; not recorded` (for `hold`: `owner review holds <v>%; not recorded`).
5. Pipeline: `build_hospital(..., decision: Mapping[str, Any] | None = None)`. Each model read of the build — the primary in `_structure_and_verify`, the fast cross-check, and the third read in `_tiebreak` — passes through `keep_owner_decision` right after `structure_sheet` returns and before grounding and `verify_sheet`, so the verifier judges what is left (a moved ceiling must pass the ceiling rules like any other). When the guard changed the primary read, its notes go into `result.notes` and one review item of kind `owner_decision` is filed with `{"decision", "value", "kept_as": "assistance_ceiling_fpl" | "dropped", "decided_at"}`. `build_state(..., decisions: Mapping[str, Mapping[str, Any]] | None = None)` passes `decision=decisions.get(ccn)`.
6. `Settings.eve_decisions: Path = Path("data/atlas-eve/_decisions.json")` (env `WAIVE_EVE_DECISIONS`). `waive atlas build` loads it with `load_decisions` (a missing file is no decisions) and passes the CCN's decision with `--ccn`, and the mapping to `build_state` without it.

Tests to write first (each must fail before the change):
- `eve_website`: a stored document on the site whose title names the hospital and whose text names its town and state gives 0.9 for that domain; one that only mentions the hospital in its body gives 0.6; documents only on another host give 0.6 with an empty evidence URL; "www." and a subdomain in the draft's domain normalise to the registered domain; a directory domain gives None.
- `set_eve_websites` on an in-memory database: an Eve-imported hospital without a website gets the draft's domain and the computed confidence; one that already has a website is untouched; one whose sheet names no domain is counted `no_domain`; a second run reports everything `already_set`.
- `keep_owner_decision`: discount without a ceiling moves the cited field to the ceiling with the same quote and source; discount with a ceiling drops it; unsure drops it; accept, None, a different value, and an unparseable value change nothing.
- `build_hospital` with a `discount` decision whose value equals what FakeAI reads as free care: the stored version has no `free_care_max_fpl`, an `owner_decision` review item exists, and the result notes say so; with no decision the same build keeps the free-care field (existing behaviour).
- CLI: `waive atlas eve-websites` prints the counts line on a temporary database; `waive atlas build --state MA --ccn <ccn>` hands `build_hospital` the decision for that CCN read from a temporary `WAIVE_EVE_DECISIONS` file (capture the kwargs with a monkeypatched `build_hospital`).

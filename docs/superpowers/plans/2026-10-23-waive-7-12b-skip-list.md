# Task 7.12b — the scout's skip list (Eve atlas sync)

Spec: the user's 7.12b instruction, recorded in `docs/PROGRESS.md` (Phase 7, block 7.12): "data/atlas-eve/_skip.json holds 356 hospitals: 173 whose sites refuse server fetches and 183 where no official policy was found. Do not scout them, do not spend Tavily on them, do not retry. Make the scheduler and `waive atlas build --state` honor this list (a small, tested change: read the file, exclude those CCNs from the unscouted queue, log how many were skipped). They get a review item of kind "eve-skip" with the reason from the file so they show up in the atlas as unscouted with a cause." Block rule: "Never retry a CCN in _skip.json."

## Global constraints

- Python 3.12, `uv run ruff format . && uv run ruff check . && uv run pytest` must pass (1,038 tests pass before this task). Tests first.
- Unit tests cannot open sockets (`pytest-socket`); no paid calls anywhere in this task.
- The skip list file is `data/atlas-eve/_skip.json`: a JSON list of objects with keys `ccn` (string, 6 characters, e.g. "020012"), `state` ("AK"), `name`, `reason` (today one of "site blocks server fetches (403)" and "scout found no official policy"), `urls_seen` (list of strings, may be empty), `last_tried` ("2026-10-23"). Do not edit that file.
- The review item kind is exactly `eve-skip`.
- The container image copies only `src/` (no `data/`), so a missing skip-list file must mean an empty list, never an error.
- Comments and docstrings in the repo's existing style: plain sentences saying why.
- Do not touch anything under `agent/`, the Eve runner, `data/`, or `docs/PROGRESS.md`.

## Task 1: skip list honored by the scheduler, `waive atlas build` and the atlas list

Files:
- Create `src/waive/atlas/skiplist.py`.
- Modify `src/waive/config.py`, `src/waive/atlas/schedule.py`, `src/waive/atlas/pipeline.py`, `src/waive/cli.py`, `src/waive/web/routes_atlas.py`, `src/waive/web/templates/atlas_list.html` (and `src/waive/atlas/repo.py` only if a `kind` filter on `open_review_items` is the cleanest way to read the items).
- Tests: create `tests/unit/test_skiplist.py`; add tests to `tests/unit/test_schedule.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_web_atlas.py`, and a CLI test file (new `tests/unit/test_cli_skiplist.py`, following `tests/unit/test_cli_recheck.py`).

Behaviour:

1. `waive.atlas.skiplist`:
   - `EVE_SKIP = "eve-skip"`; `SKIP_LIST = Path("data/atlas-eve/_skip.json")`.
   - A frozen dataclass `SkipEntry(ccn, state, name, reason, urls_seen: tuple[str, ...] = (), last_tried: str | None = None)`.
   - `load_skip_list(path: Path = SKIP_LIST) -> dict[str, SkipEntry]` keyed by CCN. Missing file → `{}`. Rows that are not objects or lack a `ccn` or a `reason` are ignored.
   - `file_skip_items(session, skip: Mapping[str, SkipEntry]) -> int`: one open review item of kind `eve-skip` per listed hospital, `detail = {"reason", "name", "state", "urls_seen" (list), "last_tried"}`; a hospital that already has an open `eve-skip` item gets none, so a rerun files nothing new. Returns how many it filed. The CCN need not be in the registry (24 Puerto Rico hospitals on the list are not).
   - `plain_reason(reason: str) -> str` for the public atlas: "site blocks server fetches (403)" → "the hospital's website blocks automated reading"; "scout found no official policy" → "no official financial assistance policy found online"; anything else is returned unchanged.
2. `Settings.scout_skip_list: Path = Path("data/atlas-eve/_skip.json")` (env `WAIVE_SCOUT_SKIP_LIST`), with a comment.
3. Scheduler (`waive.atlas.schedule`):
   - `build_queue(..., skip: Mapping[str, SkipEntry] | None = None)`: a listed hospital never enters `entries`; it is recorded in `QueueReport.skipped` as `"eve-skip: <reason>"`, is not counted in `without_sheet` (it will never be scouted, so it must not inflate the credit estimate), and is counted in a new field `QueueReport.skip_listed: int = 0`. When that count is positive, `log.info("skip list: %d hospitals left out of the scouting queue", n)`.
   - `run_once(..., skip=None)` passes it to `build_queue`.
   - `scout_tick` passes `load_skip_list(settings.scout_skip_list)`.
   - `waive atlas schedule` (dry run and `--run`) passes the loaded list, and its "Queue:" line also says how many hospitals the skip list left out.
4. `waive.atlas.pipeline.build_state(..., skip: Mapping[str, SkipEntry] | None = None)`: a listed hospital is not built and adds no result; when any were left out, `log.info("skip list: %d hospitals in %s left out", n, STATE)`.
5. `waive atlas build` (CLI): loads `settings.scout_skip_list`. With `--ccn` on the list it builds nothing, prints `<ccn> is on the skip list (eve-skip: <reason>); not built` and exits 0 (so 7.12c's `--state XX --ccn NNNNNN --reuse-sources` batches pass over the 22 CCNs that are on both lists). Without `--ccn` it passes the list to `build_state` and prints `Skip list: <n> hospitals in <STATE> left out` when n > 0.
6. `waive atlas import-eve --db` also files the `eve-skip` items through `file_skip_items` from a new option `--skip PATH` (default `data/atlas-eve/_skip.json`) and echoes `filed <n> eve-skip review items for the <m> hospitals on the skip list`. Without `--db` it files nothing. A rerun files 0.
7. Public atlas list (`GET /atlas`): a hospital with no sheet and an open `eve-skip` item shows status `not scouted` and its cause, `plain_reason(detail["reason"])`, e.g. "Fairbanks — Fairbanks, AK — not scouted (the hospital's website blocks automated reading)". Read the items with one query, not one per hospital. Hospitals with a sheet are unchanged (published / under review), and a hospital with no sheet and no such item keeps today's `none`.

Tests to write first (each must fail before the change):
- `load_skip_list` reads a temporary file (two good rows, one without a CCN, one without a reason) into two entries with their reasons; a missing path gives `{}`.
- `file_skip_items` on an in-memory database files two open `eve-skip` items carrying the reason, name, state, urls and date; the second call files 0 and leaves two items.
- `plain_reason` maps both known reasons and returns an unknown one unchanged.
- `build_queue` with a skip list leaves the listed hospital out of `entries`, puts `"eve-skip: <reason>"` in `skipped`, sets `skip_listed == 1` and does not count it in `without_sheet`; without a list the same hospital is queued.
- `scout_tick` hands `run_once` the list loaded from `settings.scout_skip_list` (a temporary file), in the style of `test_scout_tick_passes_the_configured_depth_to_the_run`.
- `build_state` with a skip list builds every other hospital of the state and not the listed one (FakeGateway/FakeAI from `tests/unit/test_pipeline.py`).
- CLI: `waive atlas build --state MA --ccn <listed>` prints the refusal, exits 0 and never calls `build_hospital` (monkeypatched to fail if called); `waive atlas import-eve --dir <empty tmp dir> --db --skip <tmp file>` prints `filed 2 eve-skip review items…` and a second run prints `filed 0`.
- `GET /atlas` shows "not scouted" and the plain cause for an unsheeted hospital with an open `eve-skip` item, and still shows "none" for an unsheeted hospital without one.

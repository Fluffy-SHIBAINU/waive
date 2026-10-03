# Pre-submission checklist

Target: submit on **2026-10-28**. Deadline: **2026-10-30 10:00 PT** (13:00 ET). Everything below
is verifiable from the repository; the four gates are the only steps that need the user.

## Devpost requirements → evidence in the repo

| Requirement | Evidence | Verify with |
|---|---|---|
| Runtime use of Nebius Token Factory and/or Nebius AI Cloud | Token Factory is the only model API: `token_factory_base_url` in `src/waive/config.py`, client in `src/waive/ai/client.py`; AI Cloud: `Dockerfile`, the `WAIVE_ENV=production` mode in `src/waive/config.py` (refuses the SQLite default; the endpoint sets it), the Phase 6 plan `docs/superpowers/plans/2026-10-02-waive-phase-6-deploy.md`; README "How Nebius, NVIDIA and Tavily are used at runtime" | `grep -n tokenfactory src/waive/config.py` → one line; `uv run waive doctor --live` (1 credit) → Token Factory OK with all four model IDs found |
| At least one NVIDIA open model used at runtime | `model_reason`, `model_fast`, `model_tiebreak` in `src/waive/config.py` are `nvidia/…`; called from `src/waive/atlas/pipeline.py` (`build_hospital`, `_tiebreak`) through `src/waive/atlas/structure.py` on every build and rebuild, and on refresh whenever a stored document's hash changed; shot 7 of the video shows a live `waive atlas build --reuse-sources` run (Nemotron Super + Nano) ending in the result table | `grep -n "nvidia/" src/waive/config.py` → three lines; `uv run pytest tests/unit/test_readme.py`; the video at 1:59–2:21 |
| Public demo video under 3 minutes on YouTube | Gate U8.2; the URL in `README.md` and `docs/devpost/submission.md`; script `docs/devpost/demo-script.md` (2:50) | `mdls -name kMDItemDurationSeconds <file>.mp4` → under 180; the YouTube page shows "Public" |
| Public repository with LICENSE and README | Gate U8.1; `LICENSE` (canonical Apache-2.0, 11,358 bytes); `README.md` with setup, architecture, privacy, costs, licenses | `git ls-files LICENSE README.md` → both; `uv run pytest tests/unit/test_readme.py` |
| Project text, gallery, track | `docs/devpost/submission.md`; `docs/devpost/gallery/*.png` with captions in `gallery/README.md`; Personal AI track | `uv run pytest tests/unit/test_devpost_docs.py`; `ls docs/devpost/gallery/*.png | wc -l` → 14 |
| Feedback on Nebius and NVIDIA tools | `docs/devpost/submission.md` → "Feedback on Nebius and NVIDIA tools" | same test |
| "Best Use of Tavily" side prize | `docs/devpost/submission.md` → "Best Use of Tavily"; the gateway `src/waive/atlas/tavily_gateway.py` and its callers `discover.py`, `scout.py`, `overlays.py`, `refresh.py` (the scheduler `schedule.py` reaches Tavily through `build_hospital`) | `grep -ln "gateway\." src/waive/atlas/*.py` → exactly `discover.py`, `overlays.py`, `refresh.py`, `scout.py`; `/metrics` shows credits per day |
| Honest about what is not done | README "Status, honestly"; the deployment status lines; "not an NVIDIA model" | `grep -n "Deployment status" README.md docs/devpost/submission.md docs/devpost/demo-script.md` → one line each, updated or not |
| No secrets, ever | `.env` gitignored; keys never committed (see PROGRESS U0.1 note); the six runtime secrets are `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `WAIVE_VAULT_KEY`, `WAIVE_TOKEN_SECRET`, `WAIVE_ADMIN_TOKEN`, `WAIVE_CLOUD_DATABASE_URL` | `git ls-files | grep -E "^\.env$"` → nothing; `git log --all --name-only --pretty=format: | sort -u | grep -Ex '\.env(\..*)?|var/.*'` → only `.env.example`; `git grep -lE "^(NEBIUS_API_KEY|TAVILY_API_KEY|WAIVE_VAULT_KEY|WAIVE_TOKEN_SECRET|WAIVE_ADMIN_TOKEN|WAIVE_CLOUD_DATABASE_URL)=.+" $(git rev-list --all) -- . ':!.env.example'` → nothing; `git grep -lE "tvly-[A-Za-z0-9_-]{20,}" $(git rev-list --all)` → nothing (catches plain and prefixed Tavily keys such as `tvly-dev-…`; `-l` prints file names only, never the matching line). **Any hit stops gate U8.1**: no push; the user rotates the key and the history is rewritten before the repository is created |
| Licenses | `LICENSE` (Apache-2.0); `data/atlas/LICENSE.md` (CC BY 4.0); `pyproject.toml` `license = { text = "Apache-2.0" }`; exports carry `"license": "CC BY 4.0"` | `uv run pytest tests/unit/test_readme.py` |

## Final dry run (the loop runs this on 2026-10-27 or the day before submission)

```bash
uv run ruff format . && uv run ruff check . && uv run pytest     # all green
uv run waive doctor                                              # offline checks OK; spend line
uv run waive demo reset && uv run waive demo gallery && uv run waive demo reset   # fresh gallery
uv run waive atlas report --state MA && uv run waive atlas report --national      # fresh numbers
git status --short && git diff --stat                            # the gallery PNGs and the national report always change
                                                                 # (headless Chrome is not byte-stable; the report header carries
                                                                 # the generation date and the median sheet age) — look at the
                                                                 # changed PNGs; only visual changes and changed numbers matter
git add docs/reports docs/devpost/gallery && git commit -m "docs: refresh reports and gallery before submission"
grep -rn "USER FILLS" README.md data/atlas/LICENSE.md docs/devpost --exclude=checklist.md
                                                                 # after U8.1–U8.3 prints exactly two lines, neither of them a field:
                                                                 # submission.md's opening note ("Fields marked `[USER FILLS: …]` are the
                                                                 # user's") and demo-script.md "Recording, cutting and uploading" step 5
                                                                 # ("the `[USER FILLS: YouTube URL …]` fields"). Any other line is an
                                                                 # unfilled field. This file names the fields in the gate steps below,
                                                                 # hence the exclusion
grep -rnE "T[B]D" README.md docs/devpost                          # placeholder scan; bracketed so this file passes its own test
git status --short                                               # must print nothing
```

Then compare the numbers in `README.md` "Status, honestly" and `docs/devpost/submission.md` with
the two reports and the `Spend so far` line of `uv run waive doctor` (the ledger is the source of
truth for spend; the PROGRESS Spend table is a copy of it); fix any drift in one commit.

## User gates (exact wording; only the user closes these)

**U8.1 — Approve making the GitHub repository public.** What the user does:
1. On GitHub, create an empty repository (suggested name `waive`; no README, no license — the repo has both). Keep it **private** for now.
2. In chat, write: "U8.1 approved. Remote: `git@github.com:<account>/waive.git`" (or the HTTPS URL). Only then does the loop run `git remote add origin <url>` and `git push -u origin main`; the loop never adds a remote or pushes without this sentence.
3. Before the push the loop runs all four secret scans from the "No secrets, ever" row above (`git ls-files` for `.env`; `git log --all --name-only` for any `.env*` or `var/` path ever committed → only `.env.example`; `git grep -l` over every revision for `NAME=value` lines of the six secrets outside `.env.example` → nothing; `git grep -l` for `tvly-[A-Za-z0-9_-]{20,}`, plain and prefixed Tavily keys → nothing) and reports "no `.env`, no `var/`, no keys in history". A hit on any scan stops the gate: no remote, no push; the loop tells the user which file and revision (never the value), the user rotates that key, and the history is rewritten before step 2 is retried. The user checks the repository page: `README.md` renders both Mermaid diagrams, `LICENSE` shows "Apache License 2.0", `.env` is absent, `docs/devpost/gallery/` has the images.
4. The user fills `[USER FILLS: repository URL]` and `[USER FILLS: copyright holder]` in `README.md`, `data/atlas/LICENSE.md`, `docs/devpost/submission.md` and `docs/devpost/demo-script.md` (title card 9) (or tells the loop the values and lets it edit), the loop commits and pushes.
5. The user switches the repository to **Public** (Settings → General → Danger Zone → Change visibility) and tells the loop "U8.1 closed".

**U8.2 — Record and upload the demo video (under 3 minutes, public on YouTube).** What the user does:
1. Follow `docs/devpost/demo-script.md` "Before recording" (reset, ZDR setting for a synthetic-only session, serve, phone on the same Wi-Fi).
2. Record the nine shots (the only paid steps: the two vision calls in shots 3–4 and the one `--reuse-sources` Nemotron build in shot 7, about two cents in all, no Tavily credits), cut in iMovie, export 1080p, check `mdls -name kMDItemDurationSeconds` < 180.
3. Upload to YouTube with visibility **Public** (Devpost requires public, not unlisted), title "Waive — free or discounted hospital care from a photo of the bill (Nebius x NVIDIA hackathon)".
4. Paste the URL into the two `[USER FILLS: YouTube URL …]` fields (`README.md`, `docs/devpost/submission.md`), or tell the loop the URL; set `WAIVE_REQUIRE_ZDR=true` back if it was changed; run `uv run waive demo reset`; commit and push; write "U8.2 closed" in chat.

**U8.3 — Submit on Devpost (target 2026-10-28; deadline 2026-10-30 10:00 PT).** What the user does:
1. On the hackathon's Devpost page, "Join hackathon" if not yet joined, then "Submit a project" → "Create a project".
2. Fill the form from `docs/devpost/submission.md`: name, tagline, the "About the project" sections in order, "Built with" tags, the repository link, the video link, the "Try it out" link (repository URL, or the live URL if Phase 6 task 6.8 is done), track **Personal AI**, opt in to **Best Use of Tavily**, and paste the "Feedback on Nebius and NVIDIA tools" section wherever the form asks for tool feedback (if there is no dedicated field, it stays inside the project description).
3. Upload the gallery PNGs from `docs/devpost/gallery/` in the order of `gallery/README.md` with its captions; the first image is the project thumbnail.
4. Preview, submit, and keep the confirmation e-mail. Paste the project URL into `[USER FILLS: Devpost URL …]` in `README.md` and `docs/devpost/submission.md` (or tell the loop), commit, push, and write "U8.3 closed" in chat.

**U8.4 — Stop cloud resources after judging ends (2026-12-15).** What the user does:
- If Phase 6 deployed the endpoint: run `uv run waive cloud stop` right after the demo window and `uv run waive cloud status` to confirm; after 2026-12-15 run `uv run waive cloud cleanup` (it lists only `waive-*` resources and asks before deleting each) and check Console → Billing → Usage shows no running Serverless AI endpoint or PostgreSQL cluster. The `waive cloud` command group is written by Phase 6 task 6.7, which runs before any deployment; if the endpoint was created another way, stop and delete the same `waive-*` resources from the Console. Write "U8.4 closed" in chat with the final AI Cloud total for the PROGRESS spend table.
- If Phase 6 never deployed: nothing bills; write "U8.4 closed (nothing deployed)" in chat.

## After submission

- Keep the repository public and the video public through judging.
- Do not merge changes that alter the demo flow before judging ends; bug fixes with tests are fine.
- Spend caps stay as they are; the scheduler stays off unless U2.2/U7.1 are closed.

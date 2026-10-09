# Security policy

Waive handles hospital bills and the personal details of seniors who need financial assistance.
If you find a weakness, please tell us privately first so it can be fixed before it is public.

## Reporting a vulnerability

Please do not open a public issue or pull request for a security problem.

- Preferred: report it privately through GitHub for this repository:
  https://github.com/Fluffy-SHIBAINU/waive/security/advisories/new
  (GitHub's private vulnerability reporting). The report is visible only to you and the maintainer.
- If that page is not available, contact the repository owner,
  [@Fluffy-SHIBAINU](https://github.com/Fluffy-SHIBAINU), through GitHub and ask for a private
  channel before sharing details.

Include what is needed to reproduce the problem: the route, command or file involved, the steps,
and what an attacker gains. Please do not send real personal data or real hospital bills; the
synthetic fixtures under `tests/` and the demo case from `uv run waive demo reset` are enough.

This is a volunteer project without a security team. You should hear back within seven days, and
you will be kept informed until the fix lands. Reporters are credited in the fixing commit if they
wish.

## Supported versions

Only `main` on GitHub is supported. There are no releases or maintenance branches: a fix lands on
`main`, is pushed, and deployments are rebuilt from it (`Dockerfile`).

## Scope

In scope:

- The web application (`src/waive/web`): capability links and their scopes, the admin console,
  photo uploads, the public atlas and metrics pages, templates and response headers.
- The `waive` command-line tool and the atlas pipeline (`src/waive/atlas`): fetching hospital
  pages and PDFs, the private-address guards, parsing untrusted documents.
- Case storage and encryption (`src/waive/cases/vault.py`), link signing, data retention,
  deletion and what is written to logs.
- The rules that decide what a senior is told (`src/waive/rules`): wrong eligibility or deadline
  advice is treated as a security problem here.
- The container image (`Dockerfile`), the dependency lock (`uv.lock`) and the CI configuration.

Out of scope:

- The hospitals' own websites and the services Waive calls (Nebius Token Factory, Tavily, CMS,
  HHS). Please report those to the respective operator.
- Denial of service by sheer request volume against a self-hosted instance.
- Issues that require control of the host, the database, or the `.env` file.
- Items already listed as open in the pre-publication audit, `docs/reports/security-audit.md`,
  which also records what was checked and what was fixed. Please read it before reporting so a
  known item is not reported twice.

## No bounty

This is an open-source hackathon project with no funding. There is no bug bounty programme and no
payment for reports. Thank you for reporting anyway: the people this project serves are the ones
you protect.

"""A thin, testable wrapper around the `nebius` CLI (gate U0.5).

Every call is `subprocess.run` with an argument list — no shell, so nothing is expanded, globbed
or written to a shell history. `--format json` is added to machine-readable calls, the project id
becomes `--parent-id`, and every non-help call carries `--no-browser` plus short CLI timeouts:
without them an expired session makes the CLI open a browser sign-in and wait for it. A wrapper
is read-only by default and refuses mutating verbs before the CLI runs; tasks 6.4+ opt out with
`read_only=False` once gate U6.1 is closed. Commands that carry secret values run with
`redact=True`, which keeps the CLI's stderr out of error messages.

CLI reference: https://docs.nebius.com/cli/configure.md
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import takewhile
from typing import Any

from waive.config import Settings

INSTALL_HINT = (
    "install it with `curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash`, then run "
    "`nebius profile create` (browser sign-in) — gate U0.5"
)
# Appended to every call that is not `--help`. `--no-browser` stops the CLI from opening a
# sign-in page on an expired session; the timeouts stop it from waiting for one.
SAFE_FLAGS = ("--no-browser", "--auth-timeout", "30s", "--timeout", "90s")
# Verbs that create, change or delete something, as the CLI names them (nebius <service> --help).
MUTATING_VERBS = frozenset(
    {
        "activate",
        "apply",
        "attach",
        "configure-helper",
        "create",
        "create-default",
        "create-helper",
        "delete",
        "edit",
        "edit-by-name",
        "issue",
        "restart",
        "restore",
        "set",
        "start",
        "stop",
        "undelete",
        "unset",
        "update",
    }
)
SUBPROCESS_TIMEOUT_SECONDS = 120
PROJECT_PREFIX = "project-"
# The CLI colours `--help` output even when stdout is a pipe (bold command names).
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class NebiusError(RuntimeError):
    """A `nebius` command failed, timed out or was refused."""


class NebiusMissing(NebiusError):
    """The CLI or its configuration is absent (gates U0.3 and U0.5)."""


def run_subprocess(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def mask_id(resource_id: str) -> str:
    """`project-e00abcd1234` -> `project-…1234`: enough to recognise, never the whole id."""
    prefix, dash, rest = resource_id.partition("-")
    return f"{prefix}-…{rest[-4:]}" if dash else resource_id


def _shown(args: Sequence[str]) -> str:
    """The subcommand words before the first flag, with ids masked — never a flag value."""
    words = list(takewhile(lambda word: not word.startswith("-"), args))[:5]
    return " ".join(mask_id(word) if word.startswith(PROJECT_PREFIX) else word for word in words)


@dataclass
class Nebius:
    project_id: str
    runner: Runner = run_subprocess
    binary: str = "nebius"
    read_only: bool = True
    project_id_source: str = ".env (NEBIUS_PROJECT_ID)"
    warning: str = ""

    @classmethod
    def from_settings(
        cls, settings: Settings, runner: Runner | None = None, *, read_only: bool = True
    ) -> "Nebius":
        if shutil.which(cls.binary) is None:
            raise NebiusMissing(f"the nebius CLI is not installed: {INSTALL_HINT}")
        runner = runner or run_subprocess
        configured = settings.nebius_project_id or ""
        if configured.startswith(PROJECT_PREFIX):
            return cls(configured, runner, read_only=read_only)
        if not configured:
            raise NebiusMissing("NEBIUS_PROJECT_ID is not set in .env (gate U0.3)")
        # Seen 2026-10-09: .env held a `tenantuseraccount-…` id, which every parented call
        # rejects ("expected id types: project"). The profile from `nebius profile create`
        # carries the real project id, so read it from there and say so.
        kind = configured.partition("-")[0] or "unknown"
        try:
            parent = cls("", runner).text("config", "get", "parent-id").strip()
        except NebiusError:
            parent = ""
        if not parent.startswith(PROJECT_PREFIX):
            raise NebiusMissing(
                f"NEBIUS_PROJECT_ID in .env is a `{kind}-…` id, not a `project-…` id, and the "
                "nebius profile has no project parent either (gate U0.3): after `nebius profile "
                "create`, put the output of `nebius config get parent-id` into .env"
            )
        warning = (
            f"NEBIUS_PROJECT_ID in .env is a `{kind}-…` id, not a `project-…` id; every parented "
            f"call rejects it. Discovery used {mask_id(parent)} from the nebius CLI profile "
            "instead. Before task 6.4, replace the .env value with the output of "
            "`nebius config get parent-id`."
        )
        return cls(
            parent,
            runner,
            read_only=read_only,
            project_id_source="the nebius CLI profile (`nebius config get parent-id`)",
            warning=warning,
        )

    def run(
        self,
        *args: str,
        parent: bool = True,
        json_output: bool = True,
        redact: bool = False,
    ) -> Any:
        is_help = "--help" in args
        shown = _shown(args)
        if self.read_only and not is_help and set(args) & MUTATING_VERBS:
            raise NebiusError(
                f"refusing `nebius {shown}`: this wrapper is read-only (gate U6.1 approves "
                "resources before anything is created)"
            )
        argv = [self.binary, *args]
        if parent:
            argv += ["--parent-id", self.project_id]
        if json_output:
            argv += ["--format", "json"]
        if not is_help:
            argv += list(SAFE_FLAGS)
        try:
            result = self.runner(argv)
        except subprocess.TimeoutExpired as error:
            raise NebiusError(f"`nebius {shown}` timed out after {error.timeout} s") from None
        except OSError as error:
            raise NebiusError(f"`nebius {shown}` could not run: {type(error).__name__}") from None
        if result.returncode != 0:
            detail = (
                "stderr withheld because the command carried secret values"
                if redact
                else result.stderr.strip()[:500]
            )
            raise NebiusError(
                f"`nebius {shown}` failed with exit code {result.returncode}: {detail}"
            )
        if not json_output:
            return _ANSI.sub("", result.stdout)
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise NebiusError(f"`nebius {shown}` printed something other than JSON") from error

    def text(self, *args: str) -> str:
        return self.run(*args, parent=False, json_output=False)


def items(payload: Any) -> list[dict[str, Any]]:
    """List responses arrive as `{"items": [...]}` (`{}` when empty); a bare list is tolerated."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [item for item in payload.get("items", []) if isinstance(item, dict)]
    return []


def name_of(resource: dict[str, Any]) -> str:
    return str(resource.get("metadata", {}).get("name", ""))


def id_of(resource: dict[str, Any]) -> str:
    return str(resource.get("metadata", {}).get("id", ""))

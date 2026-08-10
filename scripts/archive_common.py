"""Shared helpers for discovery and reconciliation.

Deliberately dependency-free: stdlib only, so the workflows need no pip install
step. TOML is read with `tomllib` (stdlib) and written by a small emitter here
rather than pulling in `tomli-w` — the lockfile schema is fixed and narrow, and
a purpose-built writer keeps the output byte-stable so review diffs stay honest.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

API_ROOT = "https://api.github.com"

# Language key -> (GitHub search qualifier, GitHub linguist name, display name).
#
# The search qualifier and the linguist name differ in case/encoding handling,
# and the linguist name is what keys the /languages byte map, so both are needed.
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "c": ("C", "C", "C"),
    "cpp": ("C++", "C++", "C++"),
    "csharp": ("C#", "C#", "C#"),
    "java": ("Java", "Java", "Java"),
    "python": ("Python", "Python", "Python"),
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_policy(path: Path | None = None) -> dict:
    path = path or CONFIG_DIR / "policy.toml"
    with path.open("rb") as fh:
        policy = tomllib.load(fh)

    for lang in policy["targets"]["languages"]:
        if lang not in LANGUAGES:
            raise SystemExit(
                f"policy.toml: unknown language {lang!r}; "
                f"known: {', '.join(sorted(LANGUAGES))}"
            )

    cap = policy["build"]["max_jobs_per_run"]
    if not 1 <= cap <= 256:
        raise SystemExit(
            f"policy.toml: build.max_jobs_per_run must be 1..256 "
            f"(GitHub caps a matrix at 256 jobs per run), got {cap}"
        )

    strikes = policy["build"]["max_failures"]
    if strikes < 1:
        raise SystemExit(
            f"policy.toml: build.max_failures must be >= 1 "
            f"(quarantine is `count >= max_failures`, so 0 would quarantine "
            f"everything; set it high to effectively disable), got {strikes}"
        )
    return policy


def load_blocklist(path: Path | None = None) -> set[str]:
    path = path or CONFIG_DIR / "blocklist.txt"
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.add(line.lower())
    return entries


# --------------------------------------------------------------------------
# failure ledger
#
# The automatic half of "stop building this"; blocklist.txt above is the manual
# half. The blocklist is permanent and repo-scoped and drops a system at
# discovery; quarantine is automatic, reversible, and scoped to one
# (system, version, srcml version) triple, and only stops it being *planned* —
# the system stays in the lockfiles and stays visible in the index as failed.
#
# Written by scripts/render_index.py, read by scripts/plan_work.py.
# --------------------------------------------------------------------------

FAILURES_SCHEMA = 1

# Kept ASCII-only: json.dumps escapes non-ASCII by default, so an em dash here
# would be written to the committed file as an escape sequence instead.
_FAILURES_BANNER = (
    "scripts/render_index.py (do not hand-edit). To retry a quarantined system, "
    "run the Reconcile archives workflow with retry_failed: true."
)


def failures_path() -> Path:
    """Resolved lazily, like lockfile_path: tests repoint CONFIG_DIR."""
    return CONFIG_DIR / "failures.json"


def load_failures(path: Path | None = None) -> dict[str, dict]:
    """Map release tag -> failure record. A missing file is an empty ledger.

    Never raises on a malformed or future-schema file. This is read by the
    nightly reconciler, and refusing to plan any work at all is a far worse
    outcome than planning a build that has been failing.
    """
    path = path or failures_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  warning: unreadable {path} ({exc}); treating as empty", file=sys.stderr)
        return {}
    schema = data.get("schema")
    if schema != FAILURES_SCHEMA:
        print(
            f"  warning: {path} has schema {schema!r}, expected {FAILURES_SCHEMA}; "
            f"treating as empty",
            file=sys.stderr,
        )
        return {}
    failures = data.get("failures")
    return failures if isinstance(failures, dict) else {}


def write_failures(failures: dict[str, dict], path: Path | None = None) -> str:
    """Emit the ledger deterministically and return the text written.

    Sorted keys and a fixed indent for the same reason write_lockfile is
    byte-stable: this file is committed by the index workflow on every render,
    so an unchanged ledger must produce an unchanged file or the git log fills
    with key-reordering noise.
    """
    path = path or failures_path()
    text = json.dumps(
        {
            "schema": FAILURES_SCHEMA,
            "generated_by": _FAILURES_BANNER,
            "failures": failures,
        },
        indent=1,
        sort_keys=True,
    ) + "\n"
    path.write_text(text)
    return text


def quarantined_tags(failures: dict[str, dict], max_failures: int) -> set[str]:
    """Tags that have hit the strike limit.

    Derived rather than stored, so changing build.max_failures takes effect on
    the next reconcile without having to rewrite the ledger.
    """
    return {
        tag
        for tag, rec in failures.items()
        if rec.get("count", 0) >= max_failures
    }


# --------------------------------------------------------------------------
# release tag naming
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")

# Keep well under the ~255-byte filesystem component limit: asset names append
# ".manifest.json" to the tag, and refs become paths under .git/refs.
MAX_TAG_LEN = 200


def sanitize_ref_component(raw: str) -> str:
    """Reduce an arbitrary string to something legal inside a git ref.

    Upstream version strings are not constrained: `release/1.2`, `v1.0 (final)`,
    and `@scope/pkg@1.0` all occur in the wild.
    """
    out = _UNSAFE.sub("-", raw)
    out = _DASHES.sub("-", out)
    out = out.replace("..", ".")
    out = out.strip("-.")
    # git rejects refs ending in ".lock"
    while out.lower().endswith(".lock"):
        out = out[: -len(".lock")].strip("-.")
    return out or "unknown"


def release_tag(language: str, name: str, version: str, srcml_version: str) -> str:
    """Build the release tag for one (system, version, srcml version) triple.

    The owner is part of the tag because repo names collide across owners
    (there are many `*/cpython`).
    """
    owner, _, repo = name.partition("/")
    parts = [
        "archive",
        sanitize_ref_component(language),
        sanitize_ref_component(owner),
        sanitize_ref_component(repo),
        sanitize_ref_component(version),
        "srcml",
        sanitize_ref_component(srcml_version),
    ]
    tag = "-".join(parts)
    if len(tag) > MAX_TAG_LEN:
        # Truncating alone could collide, so fold the full tag into a short
        # digest and keep it. Deterministic, so re-runs stay idempotent.
        import hashlib

        digest = hashlib.sha256(tag.encode()).hexdigest()[:12]
        tag = tag[: MAX_TAG_LEN - len(digest) - 1].rstrip("-.") + "-" + digest
    return tag


def release_title(language: str, name: str, version: str, srcml_version: str) -> str:
    """`c | torvalds/linux/v6.6 | srcml/v1.1.0`"""
    display = LANGUAGES.get(language, (language, language, language))[2]
    return f"{display} | {name}/{version} | srcml/v{srcml_version}"


# --------------------------------------------------------------------------
# lockfile
# --------------------------------------------------------------------------


@dataclass
class System:
    name: str  # owner/repo
    repo: str  # clone URL
    version: str  # human-facing version label
    version_kind: str  # release | tag | head
    commit: str  # resolved SHA — what actually gets checked out
    license: str = ""
    # Discovery-time metrics. Deliberately NOT written to the lockfile: stars
    # move constantly and would make every discovery run produce a diff even
    # when the selected set is unchanged. They go in the PR body instead.
    stars: int = 0
    language_bytes: int = 0
    language_share: float = 0.0


@dataclass
class Lockfile:
    language: str
    srcml_flags: list[str] = field(default_factory=list)
    systems: list[System] = field(default_factory=list)


DEFAULT_SRCML_FLAGS = ["-r", "-j", "4", "--src-encoding=UTF-8"]


def lockfile_path(language: str) -> Path:
    return CONFIG_DIR / f"{language}.lock.toml"


def _toml_str(value: str) -> str:
    return json.dumps(value)  # JSON string escaping is valid TOML basic-string escaping


def write_lockfile(lock: Lockfile, path: Path | None = None) -> str:
    """Emit a deterministic lockfile.

    No timestamp is written. The file is a pure function of (policy, blocklist,
    upstream GitHub state), so an unchanged selection produces a byte-identical
    file and the discovery workflow opens no PR. Systems are ordered by star
    rank, so position carries the ranking and no volatile `rank` field is needed.
    """
    path = path or lockfile_path(lock.language)
    lines = [
        "# GENERATED by scripts/discover.py — do not hand-edit.",
        "# To drop a system, add it to config/blocklist.txt and re-run discovery.",
        "# Ordered by star rank at discovery time.",
        "",
        "schema = 1",
        f"language = {_toml_str(lock.language)}",
        "srcml_flags = ["
        + ", ".join(_toml_str(f) for f in lock.srcml_flags)
        + "]",
        "",
    ]
    for sysrec in lock.systems:
        lines += [
            "[[systems]]",
            f"name = {_toml_str(sysrec.name)}",
            f"repo = {_toml_str(sysrec.repo)}",
            f"version = {_toml_str(sysrec.version)}",
            f"version_kind = {_toml_str(sysrec.version_kind)}",
            f"commit = {_toml_str(sysrec.commit)}",
            f"license = {_toml_str(sysrec.license)}",
            "",
        ]
    text = "\n".join(lines).rstrip("\n") + "\n"
    path.write_text(text)
    return text


def read_lockfile(language: str, path: Path | None = None) -> Lockfile:
    path = path or lockfile_path(language)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Lockfile(
        language=data["language"],
        srcml_flags=data.get("srcml_flags", list(DEFAULT_SRCML_FLAGS)),
        systems=[
            System(
                name=s["name"],
                repo=s["repo"],
                version=s["version"],
                version_kind=s["version_kind"],
                commit=s["commit"],
                license=s.get("license", ""),
            )
            for s in data.get("systems", [])
        ],
    )


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------


class RateLimited(RuntimeError):
    pass


class GitHub:
    """Minimal GitHub REST client.

    Handles the two limits that actually bite this project: the primary
    per-hour quota (1,000/hr for GITHUB_TOKEN, 5,000/hr for a PAT) and the
    secondary abuse limit that fires on bursty search traffic.
    """

    def __init__(self, token: str | None = None, verbose: bool = True):
        self.token = token or os.environ.get("GITHUB_TOKEN") or ""
        self.verbose = verbose
        self.calls = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def request(self, path: str, params: dict | None = None, retries: int = 5):
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "srcMLSystemArchive",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        delay = 2.0
        for attempt in range(retries):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.calls += 1
                    remaining = resp.headers.get("x-ratelimit-remaining")
                    # Slow down before hitting zero rather than after.
                    if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                        reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                        wait = max(0, reset - int(time.time())) + 2
                        self._log(f"  rate limit nearly exhausted; sleeping {wait}s")
                        time.sleep(wait)
                    return json.loads(resp.read().decode()), resp.headers
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None, exc.headers
                if exc.code in (403, 429):
                    retry_after = exc.headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        wait = int(retry_after) + 1
                    else:
                        reset = exc.headers.get("x-ratelimit-reset")
                        wait = (
                            max(0, int(reset) - int(time.time())) + 2
                            if reset and reset.isdigit()
                            else delay
                        )
                    wait = min(wait, 900)
                    self._log(f"  {exc.code} rate limited on {url}; sleeping {wait}s")
                    time.sleep(wait)
                    delay *= 2
                    continue
                if 500 <= exc.code < 600:
                    self._log(f"  {exc.code} from {url}; retry in {delay}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                self._log(f"  network error {exc}; retry in {delay}s")
                time.sleep(delay)
                delay *= 2
        raise RateLimited(f"giving up on {url} after {retries} attempts")

    def get(self, path: str, params: dict | None = None):
        body, _ = self.request(path, params)
        return body

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        max_pages: int = 10,
        key: str | None = None,
    ):
        """Yield items across pages.

        `key` names the field holding the list for endpoints that wrap it under
        something other than "items" — /actions/runs/{id}/jobs returns
        {"total_count": N, "jobs": [...]}, and without `key` the fallback below
        would yield the *dict keys* rather than the jobs.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while page <= max_pages:
            params["page"] = page
            body, headers = self.request(path, params)
            if not body:
                return
            if key:
                items = body.get(key, [])
            else:
                items = body["items"] if isinstance(body, dict) and "items" in body else body
            if not items:
                return
            yield from items
            if len(items) < params["per_page"]:
                return
            page += 1

#!/usr/bin/env python3
"""Render INDEX.md and index.json from the lockfiles and the published releases.

The lockfiles say what should exist; the releases say what does. The index shows
both, so a system that is selected but not yet built is visible as pending rather
than silently absent.

Usage:
    render_index.py --repo owner/name [--out INDEX.md] [--json index.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from archive_common import (
    LANGUAGES,
    REPO_ROOT,
    GitHub,
    failures_path,
    load_failures,
    load_policy,
    lockfile_path,
    read_lockfile,
    release_tag,
    write_failures,
)

MAX_RELEASE_PAGES = 100

# The reconcile workflow names each build job
# "<language> · <system> · <version> · srcml <srcml version>", which is exactly
# the identity of a row here. That makes the Actions history a usable status
# source with no extra bookkeeping: a system whose last build attempt failed is
# reported as failed rather than being indistinguishable from one that has
# simply not been built yet. It is also what the strike counter keys on, so the
# name has to carry the full triple — see legacy_job_name below.
RECONCILE_WORKFLOW_FILE = "reconcile.yml"
JOB_SEP = " · "
MAX_RUNS_SCANNED = 20

# Pages of jobs to walk per run. build.max_jobs_per_run is clamped to 256 by
# plan_work, plus the plan and report jobs, so three pages always covers a run.
MAX_JOB_PAGES = 3

# What counts as a strike, and what gets reported as "failed". A timeout is
# not a capacity problem: srcml does not legitimately need two hours on a
# source tree, so a job that hits build.timeout_minutes has almost always hung
# during processing. That is a failure, and it is reported as one rather than
# as its own status. A cancelled job, by contrast, says nothing about the
# system: it says a human or a newer run intervened.
STRIKE_CONCLUSIONS = frozenset({"failure", "timed_out"})

# ...except that GitHub does not distinguish those two cases in `conclusion`.
# A job killed by `timeout-minutes` is reported as "cancelled", exactly like one
# a human stopped, so a hung srcml would silently never strike and would burn
# the full timeout every night forever. The one signal that separates them is
# how long the job ran: a job cancelled at (essentially) the timeout was killed
# by it, and nothing else plausibly stops within a minute of that boundary.
# Such attempts are normalized to "timed_out" at collection time, so everything
# downstream sees an ordinary failure.
TIMEOUT_TOLERANCE_SECONDS = 60

# Only the nightly schedule is the retry loop that quarantine governs.
# workflow_dispatch inputs are not readable back from the REST API, so a
# dry run cannot be told apart from a real one at this layer. Counting manual
# runs would make a dry run able to *add* strikes while never being able to
# clear one (it publishes no release), so someone testing a fix could
# quarantine the very system they were fixing. Excluding them can only
# under-count, which is the safe direction, and it is what makes the
# retry_failed escape hatch free.
STRIKE_EVENTS = frozenset({"schedule"})

# Statuses the index reports as failing. Every STRIKE_CONCLUSIONS outcome maps
# into one of these, so a system that is accruing strikes is always visible as
# failing on the way to being quarantined rather than dropping out of the build
# with no warning ever shown.
FAILING_STATUSES = ("failed", "quarantined")


def job_name(language: str, system: str, version: str, srcml_version: str) -> str:
    return f"{language}{JOB_SEP}{system}{JOB_SEP}{version}{JOB_SEP}srcml {srcml_version}"


def legacy_job_name(language: str, system: str, version: str) -> str:
    """The pre-quarantine three-part job name.

    Used only to keep INDEX.md's build-log links working for runs that predate
    the rename. Deliberately NOT used for strike counting: under more than one
    srcml version this name is ambiguous between two tags, and awarding both of
    them the same strike is exactly the bug the rename fixes.

    Remove once every run older than the rename has aged out of the scan window.
    """
    return f"{language}{JOB_SEP}{system}{JOB_SEP}{version}"


def job_seconds(job: dict) -> float | None:
    """How long a job ran, or None if the API did not report both ends."""
    started, completed = job.get("started_at"), job.get("completed_at")
    if not started or not completed:
        return None
    try:
        return (
            datetime.fromisoformat(completed.replace("Z", "+00:00"))
            - datetime.fromisoformat(started.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None


def classify(job: dict, timeout_minutes: int) -> str | None:
    """The job's conclusion, with a timeout-kill recovered from "cancelled".

    See TIMEOUT_TOLERANCE_SECONDS: GitHub reports a job killed by
    `timeout-minutes` as "cancelled", indistinguishable by conclusion from one
    a human stopped. Duration is what separates them.
    """
    conclusion = job.get("conclusion")
    if conclusion != "cancelled":
        return conclusion
    ran = job_seconds(job)
    if ran is None:
        return conclusion
    if ran >= timeout_minutes * 60 - TIMEOUT_TOLERANCE_SECONDS:
        return "timed_out"
    return conclusion


def collect_job_attempts(
    gh: GitHub, repo: str, timeout_minutes: int
) -> tuple[dict[str, list[dict]], int]:
    """Map build-job name -> every attempt in the scan window, newest first.

    Also returns the id of the oldest run scanned, which is what lets
    apply_strikes prove a zeroed record is safe to prune.

    Keeping the full list rather than only the newest sighting is what makes
    strike counting idempotent: each ledger record remembers the newest run it
    has already counted, so re-scanning the same window — which happens on
    every single render — adds nothing.

    Scanned via the workflow-scoped endpoint rather than /actions/runs: the
    latter returns runs from *all* workflows, so slicing the first
    MAX_RUNS_SCANNED reconciles out of one page of 100 silently yields a much
    shorter window on a busy week, exactly when builds are being pushed.
    """
    attempts: dict[str, list[dict]] = {}
    runs = (
        gh.get(
            f"/repos/{repo}/actions/workflows/{RECONCILE_WORKFLOW_FILE}/runs",
            {"per_page": MAX_RUNS_SCANNED, "exclude_pull_requests": "true"},
        )
        or {}
    )
    reconciles = runs.get("workflow_runs", [])
    if not reconciles:
        print(
            f"  warning: no runs found for {RECONCILE_WORKFLOW_FILE}; "
            f"statuses will show as pending and no strikes will be counted",
            file=sys.stderr,
        )
        return {}, 0

    oldest_run_id = min(int(r["id"]) for r in reconciles)
    for run in reconciles:
        jobs = gh.paginate(
            f"/repos/{repo}/actions/runs/{run['id']}/jobs",
            max_pages=MAX_JOB_PAGES,
            key="jobs",
        )
        for job in jobs:
            name = job.get("name", "")
            if JOB_SEP not in name:
                continue
            attempts.setdefault(name, []).append(
                {
                    "conclusion": classify(job, timeout_minutes),
                    "event": run.get("event", ""),
                    "run_id": int(run["id"]),
                    "run_url": run.get("html_url", ""),
                    "attempted_at": run.get("created_at", ""),
                }
            )
    return attempts, oldest_run_id


def latest_outcomes(attempts: dict[str, list[dict]]) -> dict[str, dict]:
    """Newest attempt per job name — what the index displays."""
    return {name: rows[0] for name, rows in attempts.items() if rows}


def human_bytes(n: int) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def collect_releases(gh: GitHub, repo: str) -> dict[str, dict]:
    """Map release tag -> {url, asset bytes} for releases carrying an archive."""
    found: dict[str, dict] = {}
    for rel in gh.paginate(f"/repos/{repo}/releases", max_pages=MAX_RELEASE_PAGES):
        tag = rel.get("tag_name")
        if not tag or rel.get("draft"):
            continue
        for asset in rel.get("assets", []):
            if asset["name"] == f"{tag}.xml.zst":
                found[tag] = {
                    "url": rel.get("html_url", ""),
                    "download_url": asset.get("browser_download_url", ""),
                    "bytes_compressed": asset.get("size", 0),
                    "published_at": rel.get("published_at", ""),
                }
                break
    return found


def iter_targets(policy: dict) -> list[dict]:
    """Every (system, srcml version) the lockfiles call for, with its identity.

    Lifted out of build_rows so that strike accounting and rendering agree on
    what a row *is* by construction rather than by two parallel loops that have
    to be kept in step.
    """
    targets: list[dict] = []
    for language in policy["targets"]["languages"]:
        path = lockfile_path(language)
        if not path.exists():
            continue
        lock = read_lockfile(language, path)
        for rank, sysrec in enumerate(lock.systems, start=1):
            for srcml_version in policy["srcml"]["versions"]:
                targets.append(
                    {
                        "language": language,
                        "rank": rank,
                        "sysrec": sysrec,
                        "system": sysrec.name,
                        "version": sysrec.version,
                        "srcml_version": srcml_version,
                        "tag": release_tag(
                            language, sysrec.name, sysrec.version, srcml_version
                        ),
                        "job": job_name(
                            language, sysrec.name, sysrec.version, srcml_version
                        ),
                        "legacy_job": legacy_job_name(
                            language, sysrec.name, sysrec.version
                        ),
                    }
                )
    return targets


def stale_languages(policy: dict) -> set[str]:
    """Languages whose lockfile is missing, so iter_targets skipped them."""
    return {
        language
        for language in policy["targets"]["languages"]
        if not lockfile_path(language).exists()
    }


def apply_strikes(
    failures: dict[str, dict],
    attempts: dict[str, list[dict]],
    targets: list[dict],
    published: set[str],
    oldest_run_id: int,
    stale: set[str],
) -> dict[str, dict]:
    """Fold this scan's attempts into the ledger. Pure — no I/O.

    Each record carries `last_run_id`, the newest run already counted, so
    re-running this over the same window is a no-op. That is what makes the
    index workflow's `cancel-in-progress: true` safe: a cancelled render is
    only ever superseded by a newer one scanning a superset of its window, so
    cancellation defers a strike rather than dropping it.
    """
    updated = {tag: dict(rec) for tag, rec in failures.items()}
    live: set[str] = set()

    for target in targets:
        tag = target["tag"]
        live.add(tag)
        rec = updated.get(tag)

        if tag in published:
            # Success zeroes the count but KEEPS the watermark. Deleting the
            # record would drop the watermark, and the next scan would re-count
            # the old failures that are still inside the window — one failure
            # after a success would come back as three and quarantine
            # immediately.
            if rec:
                rec["count"] = 0
            continue

        watermark = rec.get("last_run_id", 0) if rec else 0
        # Oldest first, so first_failed_at and last_failed_at land in order.
        new = [
            a
            for a in reversed(attempts.get(target["job"], []))
            if a["run_id"] > watermark
            and a["conclusion"] in STRIKE_CONCLUSIONS
            and a["event"] in STRIKE_EVENTS
        ]
        if not new:
            # Nothing was counted, so the watermark must NOT move. A job that
            # is still running has conclusion None and lands here; advancing
            # past its run would lose that strike forever once it concluded.
            continue

        newest = new[-1]
        if rec is None:
            rec = updated[tag] = {
                "language": target["language"],
                "system": target["system"],
                "version": target["version"],
                "srcml_version": target["srcml_version"],
                "count": 0,
                "first_failed_at": new[0]["attempted_at"],
            }
        elif rec.get("count", 0) == 0:
            # Was passing (or is brand new); this is the start of a fresh streak.
            rec["first_failed_at"] = new[0]["attempted_at"]
        rec["count"] = rec.get("count", 0) + len(new)
        rec["last_failed_at"] = newest["attempted_at"]
        rec["last_run_id"] = newest["run_id"]
        rec["last_run_url"] = newest["run_url"]

    for tag in list(updated):
        rec = updated[tag]
        if tag not in live:
            # The lockfiles no longer call for this tag — the system was
            # dropped, or its version was re-pinned so the tag changed and
            # strikes correctly start over. Languages whose lockfile is missing
            # are exempt: iter_targets skipped them, so pruning here would wipe
            # a whole language's ledger on a partial checkout.
            if rec.get("language") not in stale:
                del updated[tag]
            continue
        if rec.get("count", 0) == 0 and rec.get("last_run_id", 0) < oldest_run_id:
            # A zeroed record whose watermark predates everything still visible
            # is behaviourally identical to no record at all: every attempt the
            # scan can observe is newer, so it would be counted either way.
            del updated[tag]

    return updated


def build_rows(
    targets: list[dict],
    releases: dict[str, dict],
    outcomes: dict[str, dict],
    failures: dict[str, dict],
    max_failures: int,
) -> list[dict]:
    rows = []
    for target in targets:
        tag = target["tag"]
        sysrec = target["sysrec"]
        rel = releases.get(tag)
        # Fall back to the pre-rename job name so build-log links survive the
        # transition; see legacy_job_name.
        outcome = outcomes.get(target["job"]) or outcomes.get(target["legacy_job"]) or {}
        rec = failures.get(tag, {})
        strikes = rec.get("count", 0)

        # A published release is the ground truth; everything below it only
        # explains what happened to the ones that are not there.
        if rel:
            status = "published"
        elif strikes >= max_failures:
            status = "quarantined"
        elif outcome.get("conclusion") in STRIKE_CONCLUSIONS:
            status = "failed"
        elif outcome.get("conclusion") == "cancelled":
            status = "cancelled"
        elif strikes:
            # The failing run has scrolled out of the scan window, but the
            # ledger remembers it. This is what keeps a quarantined system's
            # build-log link alive indefinitely.
            status = "failed"
        else:
            status = "pending"

        # Publishing zeroes the count, so a published row still reports its
        # successful run rather than the last failure before it.
        if strikes:
            run_url = rec.get("last_run_url", "") or outcome.get("run_url", "")
            attempted_at = rec.get("last_failed_at", "") or outcome.get("attempted_at", "")
        else:
            run_url = outcome.get("run_url", "")
            attempted_at = outcome.get("attempted_at", "")

        rows.append(
            {
                "language": target["language"],
                "language_display": LANGUAGES[target["language"]][2],
                "rank": target["rank"],
                "system": sysrec.name,
                "version": sysrec.version,
                "version_kind": sysrec.version_kind,
                "commit": sysrec.commit,
                "license": sysrec.license,
                "srcml_version": target["srcml_version"],
                "tag": tag,
                "status": status,
                "published": bool(rel),
                "strikes": strikes,
                "last_run_url": run_url,
                "last_attempt_at": attempted_at,
                **(rel or {}),
            }
        )
    return rows


STATUS_LABEL = {
    "published": "",
    "failed": "❌ failed",
    "quarantined": "⛔ failed (not retried)",
    "cancelled": "⏹ cancelled",
    "pending": "_pending_",
}


def render_markdown(rows: list[dict], repo: str, max_failures: int) -> str:
    published = [r for r in rows if r["published"]]
    failed = [r for r in rows if r["status"] in FAILING_STATUSES]
    total_bytes = sum(r.get("bytes_compressed", 0) for r in published)

    out = [
        "# Archive index",
        "",
        "<!-- GENERATED by scripts/render_index.py — do not hand-edit. -->",
        "",
        f"**{len(published)}** of **{len(rows)}** archives published, "
        f"{human_bytes(total_bytes) if total_bytes else '0 B'} total.",
        "",
    ]

    if failed:
        out += [
            f"⚠️ **{len(failed)} system(s) currently failing to build.** Archives are "
            "published only when they are well-formed XML, so a failure here means "
            "`srcml` either could not produce a valid archive for that source or hung "
            f"trying. After {max_failures} consecutive failures a system stops being "
            "retried; run the **Reconcile archives** workflow with `retry_failed: true` "
            "to attempt one again.",
            "",
        ]
        for r in failed:
            link = f" — [build log]({r['last_run_url']})" if r["last_run_url"] else ""
            note = (
                f" — {r['strikes']} failed attempts, no longer retried"
                if r["status"] == "quarantined"
                else ""
            )
            out.append(
                f"- `{r['language']}` **{r['system']}** `{r['version']}` "
                f"(srcml {r['srcml_version']}){note}{link}"
            )
        out.append("")

    by_language: dict[str, list[dict]] = {}
    for row in rows:
        by_language.setdefault(row["language"], []).append(row)

    for language, group in by_language.items():
        done = sum(1 for r in group if r["published"])
        bad = sum(1 for r in group if r["status"] in FAILING_STATUSES)
        heading = f"{done}/{len(group)} published."
        if bad:
            heading += f" **{bad} failing.**"
        out += [
            f"## {group[0]['language_display']}",
            "",
            heading,
            "",
            "| language | system | version | srcml version | size | archive |",
            "|---|---|---|---|---|---|",
        ]
        for r in group:
            system_link = f"[`{r['system']}`](https://github.com/{r['system']})"
            version = f"`{r['version']}`"
            if r["version_kind"] != "release":
                version += f" <sup>{r['version_kind']}</sup>"
            if r["published"]:
                size = human_bytes(r.get("bytes_compressed", 0))
                link = f"[download]({r.get('download_url','')})"
            else:
                size = "—"
                label = STATUS_LABEL.get(r["status"], r["status"])
                link = (
                    f"[{label}]({r['last_run_url']})"
                    if r["last_run_url"] and r["status"] != "pending"
                    else label
                )
            out.append(
                f"| {r['language_display']} | {system_link} | {version} "
                f"| `{r['srcml_version']}` | {size} | {link} |"
            )
        out.append("")

    out += [
        "---",
        "",
        "Each archive is a zstd-compressed srcML XML archive. To use one:",
        "",
        "```sh",
        "gh release download <tag> --repo " + repo + " --pattern '*.xml.zst'",
        "zstd -d <tag>.xml.zst",
        "```",
        "",
        "Every release also carries a `.manifest.json` recording the exact commit, "
        "srcml flags, sha256, and per-language unit counts.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "INDEX.md")
    ap.add_argument("--json", type=Path, default=REPO_ROOT / "index.json")
    ap.add_argument(
        "--failures",
        type=Path,
        help="strike ledger to read and update (default: config/failures.json)",
    )
    ap.add_argument("--token")
    args = ap.parse_args()

    if not args.repo:
        ap.error("--repo is required outside Actions (no $GITHUB_REPOSITORY)")

    ledger = args.failures or failures_path()
    policy = load_policy()
    max_failures = policy["build"]["max_failures"]

    gh = GitHub(token=args.token)
    releases = collect_releases(gh, args.repo)
    attempts, oldest_run_id = collect_job_attempts(
        gh, args.repo, policy["build"]["timeout_minutes"]
    )
    targets = iter_targets(policy)

    failures = apply_strikes(
        load_failures(ledger),
        attempts,
        targets,
        published=set(releases),
        oldest_run_id=oldest_run_id,
        stale=stale_languages(policy),
    )
    rows = build_rows(targets, releases, latest_outcomes(attempts), failures, max_failures)

    args.out.write_text(render_markdown(rows, args.repo, max_failures))
    args.json.write_text(json.dumps({"repo": args.repo, "archives": rows}, indent=1) + "\n")
    write_failures(failures, ledger)

    done = sum(1 for r in rows if r["published"])
    bad = sum(1 for r in rows if r["status"] in FAILING_STATUSES)
    walled = sum(1 for r in rows if r["status"] == "quarantined")
    print(
        f"wrote {args.out}, {args.json} and {ledger}: "
        f"{done}/{len(rows)} published, {bad} failing ({walled} quarantined)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

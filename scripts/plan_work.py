#!/usr/bin/env python3
"""Diff the lockfiles against published releases and emit the work still to do.

This is the reconciler. Git records what *should* exist (config/<lang>.lock.toml);
GitHub Releases record what *does* exist. The difference, capped, becomes an
Actions matrix. Nothing is committed per build, which is what lets the corpus
grow to 500 systems without generating 500 pointer commits.

A release only counts as done if it actually carries its `.xml.zst` asset. A
release whose asset upload failed halfway is worse than no release at all,
because it would otherwise mask the missing archive forever.

Usage:
    plan_work.py --repo owner/name [--limit N] [--language python] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from archive_common import (
    LANGUAGES,
    GitHub,
    load_policy,
    lockfile_path,
    read_lockfile,
    release_tag,
    release_title,
)

# Releases to page through when building the "already done" set.
# 100 per page, so this covers 10,000 releases.
MAX_RELEASE_PAGES = 100


def existing_archives(gh: GitHub, repo: str) -> tuple[set[str], set[str]]:
    """Return (complete_tags, incomplete_tags).

    Complete = a published (non-draft) release carrying `<tag>.xml.zst`.
    """
    complete: set[str] = set()
    incomplete: set[str] = set()
    for rel in gh.paginate(f"/repos/{repo}/releases", max_pages=MAX_RELEASE_PAGES):
        tag = rel.get("tag_name")
        if not tag:
            continue
        if rel.get("draft"):
            incomplete.add(tag)
            continue
        assets = {a["name"] for a in rel.get("assets", [])}
        if f"{tag}.xml.zst" in assets:
            complete.add(tag)
        else:
            incomplete.add(tag)
    return complete, incomplete


def desired_work(policy: dict, languages: list[str]) -> list[dict]:
    """Every (system, srcml version) pair the lockfiles call for.

    Emitted round-robin by rank across languages rather than language-by-language:
    with a 50-job cap and 100 systems per language, a language-major ordering
    would spend every run on C and never touch Python. Round-robin keeps all five
    languages advancing together.
    """
    per_language: dict[str, list[dict]] = {}

    for language in languages:
        path = lockfile_path(language)
        if not path.exists():
            print(f"  no lockfile for {language}; run discover.py", file=sys.stderr)
            continue
        lock = read_lockfile(language, path)
        rows: list[dict] = []
        for rank, sysrec in enumerate(lock.systems, start=1):
            for srcml_version in policy["srcml"]["versions"]:
                tag = release_tag(language, sysrec.name, sysrec.version, srcml_version)
                rows.append(
                    {
                        "tag": tag,
                        "title": release_title(
                            language, sysrec.name, sysrec.version, srcml_version
                        ),
                        "language": language,
                        "name": sysrec.name,
                        "repo": sysrec.repo,
                        "version": sysrec.version,
                        "version_kind": sysrec.version_kind,
                        "commit": sysrec.commit,
                        "license": sysrec.license,
                        "srcml_version": srcml_version,
                        "srcml_flags": " ".join(lock.srcml_flags),
                        "rank": rank,
                    }
                )
        per_language[language] = rows

    ordered: list[dict] = []
    for i in range(max((len(v) for v in per_language.values()), default=0)):
        for language in languages:
            rows = per_language.get(language, [])
            if i < len(rows):
                ordered.append(rows[i])
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name to check for existing releases (default: $GITHUB_REPOSITORY)",
    )
    ap.add_argument("--limit", type=int, help="override build.max_jobs_per_run")
    ap.add_argument("--language", help="restrict to one language")
    ap.add_argument("--json", action="store_true", help="print the matrix to stdout")
    ap.add_argument("--token", help="GitHub token (default: $GITHUB_TOKEN)")
    args = ap.parse_args()

    if not args.repo:
        ap.error("--repo is required outside Actions (no $GITHUB_REPOSITORY)")

    policy = load_policy()
    languages = policy["targets"]["languages"]
    if args.language:
        if args.language not in LANGUAGES:
            ap.error(f"unknown language {args.language!r}")
        languages = [args.language]

    limit = args.limit if args.limit is not None else policy["build"]["max_jobs_per_run"]
    limit = max(0, min(limit, 256))  # Actions caps a matrix at 256 jobs per run

    gh = GitHub(token=args.token)
    complete, incomplete = existing_archives(gh, args.repo)
    desired = desired_work(policy, languages)

    todo = [row for row in desired if row["tag"] not in complete]
    rebuilds = [row for row in todo if row["tag"] in incomplete]
    batch = todo[:limit]

    print(
        f"desired={len(desired)} complete={len(complete)} "
        f"todo={len(todo)} rebuild={len(rebuilds)} batch={len(batch)}",
        file=sys.stderr,
    )
    for row in batch:
        marker = "rebuild" if row["tag"] in incomplete else "new"
        print(f"  {marker:8} {row['tag']}", file=sys.stderr)

    matrix = {"include": batch}

    if out := os.environ.get("GITHUB_OUTPUT"):
        # Build-job settings are surfaced here too, so policy.toml stays the one
        # place they are configured — the workflow reads them from this step
        # rather than duplicating them in YAML.
        with open(out, "a") as fh:
            fh.write(f"matrix={json.dumps(matrix)}\n")
            fh.write(f"count={len(batch)}\n")
            fh.write(f"todo={len(todo)}\n")
            fh.write(f"has_work={'true' if batch else 'false'}\n")
            fh.write(f"runner={policy['srcml']['runner']}\n")
            fh.write(f"ubuntu={policy['srcml']['ubuntu']}\n")
            fh.write(f"timeout={policy['build']['timeout_minutes']}\n")
            fh.write(f"max_asset_bytes={policy['build']['max_asset_bytes']}\n")

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write("## Reconcile plan\n\n")
            fh.write(f"- desired archives: **{len(desired)}**\n")
            fh.write(f"- already published: **{len(complete)}**\n")
            fh.write(f"- outstanding: **{len(todo)}**\n")
            fh.write(f"- building this run: **{len(batch)}** (cap {limit})\n\n")
            if batch:
                fh.write("| | language | system | version | srcml |\n")
                fh.write("|---|---|---|---|---|\n")
                for row in batch:
                    kind = "rebuild" if row["tag"] in incomplete else "new"
                    fh.write(
                        f"| {kind} | {row['language']} | `{row['name']}` "
                        f"| {row['version']} | {row['srcml_version']} |\n"
                    )
            if len(todo) > len(batch):
                fh.write(
                    f"\n_{len(todo) - len(batch)} archive(s) deferred to the next run._\n"
                )

    if args.json:
        print(json.dumps(matrix, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

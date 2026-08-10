#!/usr/bin/env python3
"""Discover the top-N real systems per language and write config/<lang>.lock.toml.

Ranking is by stars, but stars alone select the wrong things: the highest-starred
"Python" repos on GitHub are markdown collections (public-apis, awesome-python)
and the highest-starred "Java" repos are tutorials. Candidates are therefore
walked in star order and admitted only if they carry real code mass in the
target language, until N have been admitted.

Version resolution uses a cascade — latest GitHub Release, else newest tag, else
default-branch HEAD — because major projects do not agree on how they publish.
Django, for instance, has zero GitHub Releases but does tag. Everything resolves
to a commit SHA so an archive stays reproducible even if a tag is moved.

Usage:
    discover.py --language python [--count 10] [--out PATH] [--report PATH]
    discover.py --all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from archive_common import (
    DEFAULT_SRCML_FLAGS,
    LANGUAGES,
    GitHub,
    Lockfile,
    System,
    load_blocklist,
    load_policy,
    lockfile_path,
    write_lockfile,
)

# Candidates examined per language before giving up on filling the quota.
# The search API itself caps out at 1000 results per query.
MAX_CANDIDATES = 1000

# Search returns 100 per page; walking deeper costs one request per page plus
# ~2 per surviving candidate, which is the dominant cost of a discovery run.
SEARCH_PAGE = 100


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_metadata(item: dict, filters: dict, blocklist: set[str]) -> None:
    """Cheap rejections using only fields the search response already returned."""
    name = item["full_name"]
    if name.lower() in blocklist:
        raise Rejected("blocklisted")
    if not filters.get("allow_archived", False) and item.get("archived"):
        raise Rejected("archived")
    if not filters.get("allow_forks", False) and item.get("fork"):
        raise Rejected("fork")
    if not filters.get("allow_templates", False) and item.get("is_template"):
        raise Rejected("template repo")
    if filters.get("require_license", True):
        # Reject only repos with no detected license file at all — those
        # default to all-rights-reserved, and srcML XML embeds full source.
        #
        # `NOASSERTION` must NOT be treated as unlicensed. It means licensee
        # found a license it could not map to an SPDX id, which is the norm for
        # major projects: torvalds/linux reports NOASSERTION because its COPYING
        # file is GPL-2.0 plus per-file SPDX annotations. Conflating the two
        # rejects the single most important C system in existence.
        lic = item.get("license")
        spdx = (lic or {}).get("spdx_id") or ""
        if lic is None or spdx == "NONE":
            raise Rejected("no license file detected")


def check_code_mass(gh: GitHub, name: str, linguist: str, filters: dict) -> tuple[int, float]:
    """Reject curated lists and polyglot tutorials.

    Two distinct failure modes need two distinct tests:

      bytes  — `awesome-python` is 119 KB of Python; `django` is 19 MB. An
               absolute floor separates a list from a codebase.
      share  — `hello-algo` has 1.0 MB of Java, clearing any sane floor, but it
               reimplements the same tutorial in 12 languages so Java is only
               ~11% of it. A dominance test separates a system from a polyglot
               teaching repo.
    """
    langs = gh.get(f"/repos/{name}/languages") or {}
    total = sum(langs.values())
    lang_bytes = langs.get(linguist, 0)
    share = (lang_bytes / total) if total else 0.0

    if lang_bytes < filters["min_language_bytes"]:
        raise Rejected(
            f"only {lang_bytes:,} bytes of {linguist} "
            f"(need {filters['min_language_bytes']:,})"
        )
    if share < filters["min_language_share"]:
        raise Rejected(
            f"{linguist} is {share:.0%} of the repo "
            f"(need {filters['min_language_share']:.0%})"
        )
    return lang_bytes, share


# A version-like tag: optional `v`, then dot-separated integers, then an
# optional suffix. Requiring all-numeric core components rejects branch-shaped
# tags such as Django's `stable/5.1.x` and prose tags such as
# `open-source-java-design-patterns-2nd-edition`.
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)([-._+].*)?$", re.IGNORECASE)

# Prerelease markers in the suffix. Without this, torvalds/linux resolves to
# `v7.2-rc7` — the newest tag chronologically, but a release candidate.
PRERELEASE_RE = re.compile(
    r"(?:^|[-._])(?:rc|alpha|beta|pre|preview|dev|nightly|snapshot|canary|"
    r"unstable|test|m|a|b)\d*(?:[-._]|$)",
    re.IGNORECASE,
)

# Tag pages to scan when falling back from Releases. The API returns newest
# first, so 300 tags is ample for finding the highest stable version.
TAG_PAGES = 3


def parse_version(tag: str) -> tuple[tuple[int, ...], bool] | None:
    """Return ((numeric components), is_stable) or None if not version-like."""
    m = VERSION_RE.match(tag.strip())
    if not m:
        return None
    core = tuple(int(p) for p in m.group(1).split("."))
    suffix = m.group(2) or ""
    return core, not PRERELEASE_RE.search(suffix)


def pick_best_tag(tags: list[dict], allow_prerelease: bool) -> dict | None:
    """Choose the highest stable version-like tag, or None if there is none.

    Returning None rather than falling back to the API's first tag is
    deliberate. That ordering is neither documented nor version-aware, and the
    first tag is frequently not a release at all: Django's tag list begins with
    branch-shaped `stable/5.1.x` entries, and java-design-patterns' begins with
    `open-source-java-design-patterns-2nd-edition`. Archiving a branch tip while
    labelling it a tag is worse than admitting the version is unknown and
    falling through to the default branch, which at least says so.
    """
    scored = []
    for t in tags:
        parsed = parse_version(t["name"])
        if parsed is None:
            continue
        core, stable = parsed
        if not stable and not allow_prerelease:
            continue
        scored.append((core, stable, t))
    if not scored:
        return None
    # Highest version wins; a stable tag beats a prerelease at equal version.
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return scored[0][2]


def resolve_version(
    gh: GitHub, name: str, default_branch: str, allow_prerelease: bool = False
) -> tuple[str, str, str]:
    """Return (version_label, version_kind, commit_sha).

    Cascade: latest GitHub Release -> highest stable tag -> default-branch HEAD.
    """
    # 1. Latest published release. GitHub already excludes drafts and anything
    #    flagged prerelease, but projects sometimes ship `v1.0-rc1` as a full
    #    release, so the name is checked too.
    latest = gh.get(f"/repos/{name}/releases/latest")
    if latest and latest.get("tag_name"):
        tag = latest["tag_name"]
        parsed = parse_version(tag)
        if allow_prerelease or parsed is None or parsed[1]:
            sha = _sha_for_ref(gh, name, f"tags/{tag}")
            if sha:
                return tag, "release", sha

    # 2. Highest stable tag. Django and the kernel tag without cutting Releases.
    tags = list(
        gh.paginate(f"/repos/{name}/tags", {"per_page": 100}, max_pages=TAG_PAGES)
    )
    best = pick_best_tag(tags, allow_prerelease)
    if best:
        return best["name"], "tag", best["commit"]["sha"]

    # 3. Default branch HEAD.
    branch = gh.get(f"/repos/{name}/branches/{default_branch}")
    if branch:
        sha = branch["commit"]["sha"]
        return f"{default_branch}-{sha[:7]}", "head", sha

    raise Rejected("could not resolve any version")


def _sha_for_ref(gh: GitHub, name: str, ref: str) -> str:
    """Resolve a ref to a commit SHA, dereferencing annotated tags."""
    obj = gh.get(f"/repos/{name}/git/ref/{ref}")
    if not obj:
        return ""
    target = obj.get("object") or {}
    sha, kind = target.get("sha", ""), target.get("type")
    if kind == "tag":
        # Annotated tag: the ref points at a tag object, not the commit.
        tag_obj = gh.get(f"/repos/{name}/git/tags/{sha}")
        if tag_obj:
            return (tag_obj.get("object") or {}).get("sha", "")
    return sha


def discover_language(
    gh: GitHub, language: str, count: int, policy: dict, blocklist: set[str]
) -> tuple[Lockfile, list[dict]]:
    qualifier, linguist, _display = LANGUAGES[language]
    filters = policy["filters"]

    lock = Lockfile(language=language, srcml_flags=list(DEFAULT_SRCML_FLAGS))
    report: list[dict] = []
    examined = 0

    print(f"[{language}] searching for top {count} systems", file=sys.stderr)

    for item in gh.paginate(
        "/search/repositories",
        {
            "q": f"language:{qualifier}",
            "sort": "stars",
            "order": "desc",
            "per_page": SEARCH_PAGE,
        },
        max_pages=MAX_CANDIDATES // SEARCH_PAGE,
    ):
        if len(lock.systems) >= count:
            break
        if examined >= MAX_CANDIDATES:
            break
        examined += 1
        name = item["full_name"]

        try:
            check_metadata(item, filters, blocklist)
            lang_bytes, share = check_code_mass(gh, name, linguist, filters)
            version, kind, sha = resolve_version(
                gh,
                name,
                item["default_branch"],
                allow_prerelease=filters.get("allow_prerelease", False),
            )
        except Rejected as exc:
            print(f"  reject {name}: {exc.reason}", file=sys.stderr)
            report.append({"name": name, "accepted": False, "reason": exc.reason})
            continue

        lic = (item.get("license") or {}).get("spdx_id", "") or ""
        lock.systems.append(
            System(
                name=name,
                repo=item["clone_url"],
                version=version,
                version_kind=kind,
                commit=sha,
                license=lic,
                stars=item.get("stargazers_count", 0),
                language_bytes=lang_bytes,
                language_share=share,
            )
        )
        report.append(
            {
                "name": name,
                "accepted": True,
                "stars": item.get("stargazers_count", 0),
                "language_bytes": lang_bytes,
                "language_share": round(share, 4),
                "version": version,
                "version_kind": kind,
                "commit": sha,
                "license": lic,
            }
        )
        print(
            f"  accept {name} @ {version} ({kind}) "
            f"[{lang_bytes:,} B {linguist}, {share:.0%}]",
            file=sys.stderr,
        )

    if len(lock.systems) < count:
        print(
            f"[{language}] WARNING: wanted {count}, found {len(lock.systems)} "
            f"after examining {examined} candidates",
            file=sys.stderr,
        )
    return lock, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--language", help="single language key (c, cpp, csharp, java, python)")
    ap.add_argument("--all", action="store_true", help="every language in policy.toml")
    ap.add_argument("--count", type=int, help="override targets.count")
    ap.add_argument("--out", type=Path, help="lockfile path (single-language mode only)")
    ap.add_argument("--report", type=Path, help="write a JSON accept/reject report here")
    ap.add_argument("--token", help="GitHub token (default: $GITHUB_TOKEN)")
    args = ap.parse_args()

    policy = load_policy()
    blocklist = load_blocklist()

    if args.all:
        languages = policy["targets"]["languages"]
    elif args.language:
        if args.language not in LANGUAGES:
            ap.error(f"unknown language {args.language!r}")
        languages = [args.language]
    else:
        ap.error("pass --language or --all")

    if args.out and len(languages) > 1:
        ap.error("--out only makes sense with a single --language")

    count = args.count if args.count is not None else policy["targets"]["count"]
    gh = GitHub(token=args.token)

    reports: dict[str, list[dict]] = {}
    for language in languages:
        lock, report = discover_language(gh, language, count, policy, blocklist)
        reports[language] = report
        path = args.out or lockfile_path(language)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_lockfile(lock, path)
        print(f"[{language}] wrote {path} ({len(lock.systems)} systems)", file=sys.stderr)

    if args.report:
        args.report.write_text(json.dumps(reports, indent=2) + "\n")

    print(f"API calls: {gh.calls}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# srcMLSystemArchive

A continuously-maintained corpus of [srcML](https://www.srcml.org/) archives for the
most-starred real systems on GitHub, in C, C++, C#, Java, and Python.

For each `(system, version, srcml version)` triple, `srcml` is run over the pinned source
tree and the compressed result is published as its own GitHub Release. Everything is
discovered, built, and refreshed by GitHub Actions.

**[Browse the archive index →](INDEX.md)**

## Relationship to srcMLLargeSystems

[srcML/srcMLLargeSystems](https://github.com/srcML/srcMLLargeSystems) hand-curates a handful
of large systems as regression *baselines*, and drives `srcml --parser-test` against them.
This repo is its sibling, not its replacement: it auto-discovers hundreds of systems and
publishes them as an *archive*. The build machinery is adapted from that repo, and release
titles use the same `language | system | srcml version` convention.

## How it works

```
config/policy.toml          what to look for
config/blocklist.txt        what to never look at
        |
        v
  discover.yml  (monthly)   search GitHub -> filter -> resolve version
        |                   -> PR updating config/<lang>.lock.toml
        v                      ^ you review and merge here
config/<lang>.lock.toml     the systems, pinned to commit SHAs
        |
        v
  reconcile.yml (nightly)   desired = lockfiles x srcml versions
        |                   have    = published releases
        |                   build   = the difference, capped per run
        v
  GitHub Releases           one release per archive
        |
        v
  index.yml                 INDEX.md + index.json
```

Git records **what should exist**; Releases record **what does exist**. Nothing is committed
per build, which is what lets the corpus grow to hundreds of systems without generating a
commit per archive.

## Selecting "systems"

Ranking by stars alone selects the wrong things. The highest-starred *Python* repositories on
GitHub are `public-apis`, `free-programming-books`, and `awesome-python` — markdown
collections with almost no Python in them. The highest-starred *Java* repositories are
tutorials.

Candidates are therefore walked in star order and admitted only if they clear two thresholds:

| filter | why |
| --- | --- |
| ≥ 1 MB of the target language | `awesome-python` has 119 KB of Python; `django` has 19 MB |
| ≥ 40% of the repo is that language | `hello-algo` has 1 MB of Java but reimplements one tutorial in a dozen languages, so Java is only 16% |

plus a license check, repo-state checks, and a manual [blocklist](config/blocklist.txt).
See [docs/selection-policy.md](docs/selection-policy.md).

## Versions

Each system is pinned by a cascade — **latest GitHub Release → highest stable tag →
default-branch HEAD** — and always resolved to a commit SHA, so an archive stays reproducible
even if a tag moves.

The cascade exists because projects do not agree on how they publish. Django cuts no GitHub
Releases at all and would be lost to a release-only rule. The kernel publishes no Releases
either, and its newest tag is nearly always a release candidate, so tags are ranked by parsed
version with prereleases excluded rather than taken in API order.

## Using an archive

```sh
gh release download <tag> --repo <owner>/srcMLSystemArchive --pattern '*.xml.zst'
zstd -d <tag>.xml.zst
```

Every release carries three assets:

| asset | contents |
| --- | --- |
| `<tag>.xml.zst` | the srcML archive, zstd-compressed |
| `<tag>.manifest.json` | commit SHA, srcml version and flags, sha256, sizes, per-language unit counts |
| `<tag>.generate.log` | the full build log |

See [docs/archive-format.md](docs/archive-format.md).

## Operating it

| task | how |
| --- | --- |
| Change how many systems per language | `targets.count` in [`config/policy.toml`](config/policy.toml) |
| Add a srcML version to archive against | `srcml.versions` in `config/policy.toml` |
| Exclude a system | add it to [`config/blocklist.txt`](config/blocklist.txt), re-run discovery |
| Refresh the system list now | run the **Discover systems** workflow |
| Build outstanding archives now | run the **Reconcile archives** workflow |
| Try a build without publishing | run either workflow with `dry_run: true` |
| Run the tests | `python3 -m unittest discover -s tests -t .` |

Everything is stdlib-only Python plus `bash`, `git`, `zstd`, `jq`, and `xmllint`. There is
nothing to `pip install`.

### Rate limits

`GITHUB_TOKEN` is capped at 1,000 REST requests/hour per repository, and discovery at
`targets.count = 100` needs a few thousand. Set a `DISCOVERY_TOKEN` secret (a PAT with
`public_repo`) to get 5,000/hour. Without one, discovery still finishes — it just sleeps
until the limit resets.

## Licensing

srcML XML embeds the full source text, so each release redistributes the upstream project's
code. Every archive records the upstream SPDX identifier in its manifest and release notes,
and is redistributed under that project's own license. Repositories with no detected license
are excluded from discovery. If you maintain a project archived here and want it removed, open
an issue — it will be added to the blocklist.

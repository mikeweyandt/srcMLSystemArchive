# Selection policy

How `scripts/discover.py` decides which repositories become part of the corpus, and why the
rules are shaped the way they are. Every threshold here was set against live API data, not
guessed; the numbers quoted are real measurements.

## The problem

The obvious implementation — "take the top 100 repos for `language:Python` sorted by stars" —
produces a corpus of markdown files. As measured in August 2026, the five highest-starred
Python repositories on GitHub were:

| repo | stars | actual Python |
| --- | --- | --- |
| `public-apis/public-apis` | 455,230 | 40,479 B |
| `EbookFoundation/free-programming-books` | 394,042 | 27,037 B |
| `donnemartin/system-design-primer` | 362,722 | 57,260 B |
| `vinta/awesome-python` | 313,103 | 119,285 B |
| `practical-tutorials/project-based-learning` | 277,618 | 44,939 B |

None is a system. GitHub classifies a repository by its *dominant* language, which for a
curated list of Python links is Python. Java is no better: its top four are `hello-algo`,
`java-design-patterns`, `advanced-java`, and `LeetCodeAnimation`.

## The filters

Candidates are walked in descending star order and admitted only if all of the following hold.
The walk continues until `targets.count` systems are admitted.

### 1. Code mass — `min_language_bytes` (default 1 MB)

From `GET /repos/{owner}/{repo}/languages`, the byte count attributed to the target language
must clear an absolute floor. This is the single most effective filter:

```
awesome-python          119,285 B Python     rejected
public-apis              40,479 B Python     rejected
yt-dlp               10,142,414 B Python     accepted
django               19,027,736 B Python     accepted
elasticsearch       272,648,699 B Java       accepted
linux             1,430,202,756 B C          accepted
```

Two orders of magnitude separate the two groups, so the exact threshold is not delicate.

### 2. Language dominance — `min_language_share` (default 40%)

Byte mass alone is not sufficient. `krahets/hello-algo` carries 1,094,232 bytes of Java, which
clears any sane floor — but it reimplements the same tutorial in a dozen languages, so Java is
only 16% of it. Requiring the target language to dominate rejects polyglot teaching repos
while leaving genuinely polyglot systems alone (TensorFlow is 55% C++, PowerToys 54% C).

### 3. License present

Repositories where GitHub detects no license file at all are rejected: they are
all-rights-reserved by default, and srcML XML embeds the complete source text.

**`NOASSERTION` is not treated as unlicensed.** It means GitHub's licensee found a license it
could not map to a single SPDX identifier, which is the norm for large projects —
`torvalds/linux` reports `NOASSERTION` because its `COPYING` is GPL-2.0 plus per-file SPDX
annotations. Conflating the two rejects the most important C system there is. This is covered
by a regression test.

### 4. Repository state

Archived, forked, and template repositories are rejected. Archived projects are frozen, forks
duplicate an upstream that is likely already in the corpus, and templates contain no real code.

### 5. Blocklist

`config/blocklist.txt` excludes specific `owner/repo` entries regardless of rank. This is the
escape hatch for anything the automated filters cannot judge: mirrors, generated-source repos,
datasets committed as code, systems too large for the runner disk budget, or a maintainer
asking to be removed.

## Version resolution

Once admitted, a system is pinned by this cascade:

1. **Latest GitHub Release.** GitHub already excludes drafts and prereleases here. The tag name
   is checked too, since projects sometimes publish `v1.0-rc1` as a full release.
2. **Highest stable tag.** Tags are fetched (up to 300), parsed as versions, prereleases
   discarded, and the numerically highest chosen.
3. **Default-branch HEAD**, labelled `<branch>-<short sha>`.

Everything resolves to a **commit SHA**, which is what the build actually checks out. Tags are
mutable; an archive that cannot be tied to an immutable commit is not reproducible.

### Why tags are parsed rather than taken in order

The `/tags` endpoint's ordering is neither documented nor version-aware, and its first entry is
routinely not a release:

| repo | first tag returned | what it actually is |
| --- | --- | --- |
| `torvalds/linux` | `v7.2-rc7` | a release candidate |
| `django/django` | `stable/5.1.x` | a maintenance branch |
| `iluwatar/java-design-patterns` | `open-source-java-design-patterns-2nd-edition` | prose |

Parsing gives `v7.1`, `6.1`, and `1.25.0` instead. Ordering is numeric, not lexical, so
`1.10.0` correctly outranks `1.9.0`.

If **no** tag looks like a version, resolution falls through to HEAD rather than picking an
arbitrary tag. Labelling `stable/5.1.x` as a pinned tag would misrepresent a moving branch tip;
falling to HEAD at least says what it is.

## Tuning

All thresholds live in `config/policy.toml`. Raising `min_language_share` tightens toward
monolingual systems; lowering `min_language_bytes` admits smaller projects along with more
noise. After changing either, run the **Discover systems** workflow with `dry_run: true` and
read the accept/reject table in the run summary before merging anything.

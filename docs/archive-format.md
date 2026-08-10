# Archive format

## Release naming

```
tag:   archive-<language>-<owner>-<repo>-<version>-srcml-<srcml version>
title: C | torvalds/linux/v7.1 | srcml/v1.1.0
```

The owner is part of the tag because repository names collide across owners. Every component is
sanitized to `[A-Za-z0-9._-]`, because real version labels contain slashes and spaces
(`release/1.2`, `v1.0 (final)`), and git rejects refs containing `..`, ending in `.lock`, or
leading with `-`. Tags longer than 200 characters are truncated with a deterministic 12-character
digest appended, so re-runs stay idempotent and distinct versions cannot collide.

## Assets

| asset | contents |
| --- | --- |
| `<tag>.xml.zst` | the srcML archive, `zstd -19 --long=27` |
| `<tag>.manifest.json` | full provenance record |
| `<tag>.generate.log` | build log including srcml's own stderr |

A release is only considered complete if `<tag>.xml.zst` is present. The reconciler treats a
release missing it — from a failed or partial upload — as outstanding work and rebuilds it,
rather than letting the bare tag mask a missing archive forever.

## Manifest

```json
{
  "language": "c",
  "system": "torvalds/linux",
  "repo_url": "https://github.com/torvalds/linux.git",
  "version": "v7.1",
  "version_kind": "tag",
  "commit": "8cd9520d35a6c38db6567e97dd93b1f11f185dc6",
  "license": "NOASSERTION",
  "srcml_version": "1.1.0",
  "srcml_flags": "-r -j 4 --src-encoding=UTF-8",
  "srcml_exit": 0,
  "asset_name": "archive-c-torvalds-linux-v7.1-srcml-1.1.0.xml.zst",
  "bytes_raw": 8123456789,
  "bytes_compressed": 164235776,
  "units": 61234,
  "units_by_language": { "C": 58021, "C++": 2110, "Java": 1103 },
  "sha256": "…",
  "generated_at": "2026-08-09T04:12:33+00:00",
  "runner_os": "Ubuntu 24.04.1 LTS"
}
```

`version_kind` records how the version was determined — `release`, `tag`, or `head` — so a
consumer can tell an official release from a branch snapshot. `commit` is authoritative: it is
what was checked out.

`srcml_exit` is retained even when non-zero. Across hundreds of repositories some inputs make
srcml complain, and an archive that still validates is worth keeping; the field lets consumers
filter if they would rather not.

## What is and is not in an archive

**Filenames are repository-relative.** `srcml` records each path exactly as it was given and
offers no option to rewrite them, so the invocation is the only lever. It is run from *inside*
the checkout against explicitly named top-level entries:

| invocation | recorded filename |
| --- | --- |
| `srcml -r project-src` | `project-src/v2rayN/Program.cs` — scratch directory leaks in |
| `srcml -r .` | `/home/runner/work/…/project-src/v2rayN/Program.cs` — canonicalized to absolute |
| `cd project-src && srcml -r v2rayN …` | `v2rayN/Program.cs` |

A *named* relative path is preserved verbatim, but `.` is canonicalized to an absolute path,
which would bake the runner's working directory into every path in the corpus. Hence the
explicit entry list.

**Files under dot-directories are absent.** `srcml`'s recursive traversal skips hidden
directories, so `.github/`, `.config/`, and similar are not parsed. This is verified rather
than assumed: `2dust/v2rayN` at `7.24.4` has 274 `.cs` files, of which exactly one lives under
`.github/`, and the archive contains the other 273 — nothing missing, nothing extra.

For a corpus this is usually the behaviour you want, since CI helper scripts are not really
part of the system. But it does mean a unit count will not match a naive
`find . -name '*.cs' | wc -l`. Compare against non-hidden paths only.

**Only languages srcml recognizes are parsed.** Files it has no parser for are skipped
silently, so an archive's unit count reflects parseable source, not repository file count.

`units_by_language` matters because archives are **not** restricted to the classifying language.
`srcml` runs over the whole tree with extension-based detection, so the C archive of the kernel
also contains its C++ and assembly-adjacent units. The language in the release title is a
*classification*, not a content filter. To work with one language only, filter on the `language`
attribute of each `<unit>`.

## Reading an archive

```sh
gh release download <tag> --repo <owner>/srcMLSystemArchive --pattern '*.xml.zst'
zstd -d <tag>.xml.zst -o archive.xml
srcml archive.xml --list          # files in the archive
```

Archives reach several GB uncompressed, so prefer streaming over loading into memory. See
[Validating an archive yourself](#validating-an-archive-yourself) below for the parser caveat.

Verify integrity against the manifest:

```sh
sha256sum -c <<<"$(jq -r '.sha256 + "  " + .asset_name' <tag>.manifest.json)"
```

## How archives are built

`scripts/generate_archive.sh` streams `srcml` straight into `zstd`. The uncompressed XML is
never written to disk — the kernel alone is ~1.4 GB of C and its srcML output runs to many GB,
against roughly 20 GB free on a stock runner. The composite action also reclaims ~30 GB of
preinstalled toolchains before starting, and deletes the `.git` directory after checkout.

One streaming pass (`scripts/archive_stats.py`) then validates the archive and collects its
statistics together, with a bounded buffer so memory stays flat regardless of archive size.

A build fails if the archive is not well-formed XML, contains zero units, or exceeds GitHub's
2 GiB per-asset limit — checked before upload rather than discovered by a failed upload an hour
later. **Only valid XML is published.** A system that srcml cannot render as well-formed XML is
left unpublished and reported as failing in [INDEX.md](../INDEX.md), rather than being quietly
dropped or published broken.

## Validating an archive yourself

Use a parser with **namespace processing off**. Do not use `xmllint`:

```sh
zstd -dc <tag>.xml.zst | python3 -c '
import sys
from xml.parsers import expat
p = expat.ParserCreate()            # no namespace_separator
while (c := sys.stdin.buffer.read(1 << 20)):
    p.Parse(c, False)
p.Parse(b"", True)
print("ok")'
```

srcML declares `xmlns:cpp` on individual units that need it rather than on the archive root,
and misses cases — so a namespace-aware parser reports `Namespace prefix cpp on if is not
defined` against otherwise fine archives. This is not specific to this pipeline:
srcMLLargeSystems' published `baseline-c-linux-v6.6-srcml-1.1.0` fails `xmllint --stream` at
line 32,599,292.

### Known srcml defect

srcml 1.1.0 emits structurally malformed XML when a GNU `__attribute__` spans a preprocessor
conditional:

```c
#ifdef __GNUC__
__attribute__((format(printf, 1, 2)))
#endif
void sigsafe_printf(const char *format, ...);
```

It opens `<attribute>` and never closes it, so the enclosing `<unit>` never balances. srcml
still exits 0. This is why `torvalds/linux` currently fails to archive — the defect occurs in
`tools/testing/selftests/mm/pkey-helpers.h`. The idiom is common in C, so other systems may hit
it too; affected systems stay in the lockfiles and show as failing until srcml fixes it.

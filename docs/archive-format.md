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

**Filenames are repository-relative.** `srcml` records each path exactly as it was given, so it
is invoked from *inside* the checkout (`cd project-src && srcml -r .`) rather than from the
parent. Parsing `project-src/…` from outside would stamp the scratch checkout directory into
every filename in the corpus.

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

Archives reach several GB uncompressed, so prefer streaming over loading into memory:

```sh
zstd -dc <tag>.xml.zst | xmllint --stream --noout -
```

Verify integrity against the manifest:

```sh
sha256sum -c <<<"$(jq -r '.sha256 + "  " + .asset_name' <tag>.manifest.json)"
```

## How archives are built

`scripts/generate_archive.sh` streams `srcml` straight into `zstd`. The uncompressed XML is
never written to disk — the kernel alone is ~1.4 GB of C and its srcML output runs to many GB,
against roughly 20 GB free on a stock runner. The composite action also reclaims ~30 GB of
preinstalled toolchains before starting, and deletes the `.git` directory after checkout.

Two streaming passes then run over the compressed archive:

1. **`xmllint --stream --noout`** gates well-formedness. This is what catches a srcml crash
   mid-stream, which leaves a truncated archive that still opens with an XML declaration and
   still contains units, but whose root element never closes. Only a real parse detects it.
2. **`scripts/archive_stats.py`** collects uncompressed size and unit counts in a single pass
   with a bounded buffer, so memory stays flat regardless of archive size.

A build fails if the archive is not well-formed, contains zero units, or exceeds GitHub's
2 GiB per-asset limit — checked before upload rather than discovered by a failed upload an
hour later.

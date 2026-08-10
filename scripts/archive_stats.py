#!/usr/bin/env python3
"""Single-pass validation and statistics over a srcML archive streamed on stdin.

Reads the decompressed XML once and reports uncompressed size, unit count, and a
per-language unit breakdown, while checking well-formedness as it goes. One pass
matters: these archives reach several GB uncompressed for systems like the
kernel, so re-reading the stream per check is the difference between seconds and
minutes.

Well-formedness uses expat with **namespace processing off**, which is a
deliberate choice rather than an oversight. srcML declares `xmlns:cpp` on
individual units that need it rather than on the archive root, and misses cases:
srcML's own published baselines contain `<cpp:ifdef>` under a prefix that was
never declared. A namespace-aware parser therefore rejects known-good srcML
output — verified against srcMLLargeSystems' Linux baseline, which fails
`xmllint --stream` at line 32,599,292. Structural well-formedness is what
actually matters here: it catches the failure that occurs in practice, a srcml
crash leaving the archive truncated mid-stream.

Usage:
    zstd -dc archive.xml.zst | archive_stats.py --out stats.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from xml.parsers import expat

CHUNK = 8 << 20  # 8 MiB

# srcML emits one <unit> per source file, each carrying a language attribute.
# Matched on bytes to avoid decoding cost and any encoding surprises in the
# source text embedded in the archive.
UNIT_RE = re.compile(rb'<unit\b[^>]*?\blanguage="([^"]*)"')

# Longest plausible `<unit ...>` tag. Chunk boundaries retain this many bytes so
# a tag straddling two reads still matches on the following pass.
OVERLAP = 16384


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument(
        "--skip-validation",
        action="store_true",
        help="collect stats without checking well-formedness",
    )
    args = ap.parse_args()

    # No namespace_separator: element names stay opaque strings, so srcML's
    # undeclared `cpp:` prefix is not an error. See the module docstring.
    parser = None if args.skip_validation else expat.ParserCreate()

    total_bytes = 0
    languages: Counter[str] = Counter()
    head = b""

    # Absolute stream offsets keep the seam handling honest: `base` is where the
    # retained buffer starts, `counted_upto` is the offset past the last counted
    # match. A tag truncated at a chunk edge simply fails to match this pass and
    # is picked up next pass from the retained tail; `counted_upto` stops the
    # overlapping region from counting anything twice.
    buf = b""
    base = 0
    counted_upto = 0

    stream = sys.stdin.buffer
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        total_bytes += len(chunk)
        if not head:
            head = chunk[:256]

        if parser is not None:
            try:
                parser.Parse(chunk, False)
            except expat.ExpatError as exc:
                print(f"archive_stats: malformed XML: {exc}", file=sys.stderr)
                return 1

        buf += chunk
        for m in UNIT_RE.finditer(buf):
            abs_start = base + m.start()
            if abs_start < counted_upto:
                continue
            languages[m.group(1).decode("utf-8", "replace")] += 1
            counted_upto = abs_start + 1

        keep = min(len(buf), OVERLAP)
        base += len(buf) - keep
        buf = buf[len(buf) - keep :]

    if total_bytes == 0:
        print("archive_stats: empty stream", file=sys.stderr)
        return 1
    if not head.lstrip().startswith(b"<?xml"):
        print(
            f"archive_stats: stream does not start with an XML declaration: "
            f"{head[:64]!r}",
            file=sys.stderr,
        )
        return 1

    # Finalizing is what catches truncation: a srcml crash mid-stream leaves the
    # root element unclosed, which is only detectable at end of input.
    if parser is not None:
        try:
            parser.Parse(b"", True)
        except expat.ExpatError as exc:
            print(
                f"archive_stats: archive is truncated or unclosed: {exc}",
                file=sys.stderr,
            )
            return 1

    stats = {
        "bytes_raw": total_bytes,
        "units": sum(languages.values()),
        "units_by_language": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
    }
    text = json.dumps(stats, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    print(
        f"archive_stats: {total_bytes:,} bytes, {stats['units']:,} units, "
        f"{len(languages)} languages",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

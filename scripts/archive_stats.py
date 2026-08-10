#!/usr/bin/env python3
"""Single-pass statistics over a srcML archive streamed on stdin.

Reads the decompressed XML once and reports uncompressed size, unit count, and
a per-language unit breakdown. One pass matters: these archives reach several
GB uncompressed for systems like the kernel, so re-reading the stream per
statistic is the difference between seconds and minutes.

Because it consumes the decompressed stream anyway, this doubles as an
integrity check — `zstd -dc archive.xml.zst | archive_stats.py` fails loudly if
the archive does not decompress or does not look like a srcML archive.

Usage:
    zstd -dc archive.xml.zst | archive_stats.py --out stats.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

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
    args = ap.parse_args()

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

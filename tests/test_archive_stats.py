#!/usr/bin/env python3
"""Tests for the streaming archive statistics pass."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS = ROOT / "scripts" / "archive_stats.py"


def run(payload: bytes):
    return subprocess.run(
        [sys.executable, str(STATS)], input=payload, capture_output=True
    )


def archive(units: list[str], filler=(200, 400), seed=1) -> bytes:
    rng = random.Random(seed)
    out = [
        b'<?xml version="1.0" encoding="UTF-8"?>\n',
        b'<unit xmlns="http://www.srcML.org/srcML/src" revision="1.1.0">\n',
    ]
    for i, lang in enumerate(units):
        out.append(
            b'<unit revision="1.1.0" language="'
            + lang.encode()
            + b'" filename="src/dir/file%d.ext">' % i
        )
        out.append(b"x" * rng.randint(*filler))
        out.append(b"</unit>\n")
    out.append(b"</unit>\n")
    return b"".join(out)


class TestArchiveStats(unittest.TestCase):
    def test_counts_units_by_language(self):
        payload = archive(["C"] * 7 + ["C++"] * 3 + ["Java"] * 2)
        proc = run(payload)
        self.assertEqual(0, proc.returncode, proc.stderr.decode())
        stats = json.loads(proc.stdout)
        self.assertEqual({"C": 7, "C++": 3, "Java": 2}, stats["units_by_language"])
        self.assertEqual(12, stats["units"])
        self.assertEqual(len(payload), stats["bytes_raw"])

    def test_counts_exactly_once_across_chunk_boundaries(self):
        # The read buffer is 8 MiB with a retained overlap window; a unit tag
        # straddling that seam must be counted once, not zero or twice.
        langs = ["C"] * 600 + ["C++"] * 350 + ["Java"] * 161
        random.Random(9).shuffle(langs)
        payload = archive(langs, filler=(2000, 45000), seed=3)
        self.assertGreater(len(payload), 16 << 20, "must span multiple 8 MiB reads")
        stats = json.loads(run(payload).stdout)
        self.assertEqual(dict(Counter(langs)), stats["units_by_language"])
        self.assertEqual(len(payload), stats["bytes_raw"])

    def test_rejects_empty_stream(self):
        self.assertEqual(1, run(b"").returncode)

    def test_rejects_non_xml(self):
        proc = run(b"Segmentation fault\n")
        self.assertEqual(1, proc.returncode)
        self.assertIn(b"XML declaration", proc.stderr)

    def test_archive_with_no_units_reports_zero(self):
        # Not an error here — generate_archive.sh is what treats zero units as
        # a failed parse, so this pass only has to report it accurately.
        payload = b'<?xml version="1.0"?>\n<unit xmlns="x" revision="1.1.0"/>\n'
        stats = json.loads(run(payload).stdout)
        self.assertEqual(0, stats["units"])

    def test_language_values_with_entities_and_symbols(self):
        payload = archive(["C++", "C#", "Objective-C"])
        stats = json.loads(run(payload).stdout)
        self.assertEqual({"C++": 1, "C#": 1, "Objective-C": 1}, stats["units_by_language"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the reconciler: what is desired, what exists, what still needs building."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from archive_common import (  # noqa: E402
    Lockfile,
    System,
    quarantined_tags,
    write_lockfile,
)
import archive_common  # noqa: E402
import plan_work  # noqa: E402


def make_policy(languages, versions=("1.1.0",), count=2):
    return {
        "targets": {"count": count, "languages": list(languages)},
        "srcml": {"versions": list(versions), "ubuntu": "24.04", "runner": "ubuntu-24.04"},
        "build": {
            "max_jobs_per_run": 50,
            "timeout_minutes": 120,
            "max_asset_bytes": 2147483648,
            "max_failures": 3,
        },
        "filters": {},
    }


def system(n):
    return System(f"own/repo{n}", f"https://github.com/own/repo{n}.git", f"v{n}.0", "tag", str(n) * 40, "MIT")


class FakeGitHub:
    """Serves a canned /releases listing."""

    def __init__(self, releases):
        self.releases = releases

    def paginate(self, path, params=None, max_pages=10):
        return iter(self.releases)


class TestExistingArchives(unittest.TestCase):
    def test_release_without_its_asset_counts_as_incomplete(self):
        # The failure this guards against: `gh release create` succeeds, the
        # asset upload dies, and the tag now exists forever with no archive
        # behind it. Keying "done" on the tag alone would mask that permanently.
        gh = FakeGitHub([
            {"tag_name": "good", "assets": [{"name": "good.xml.zst"}, {"name": "good.manifest.json"}]},
            {"tag_name": "assetless", "assets": []},
            {"tag_name": "wrong-asset", "assets": [{"name": "wrong-asset.manifest.json"}]},
            {"tag_name": "drafted", "draft": True, "assets": [{"name": "drafted.xml.zst"}]},
        ])
        complete, incomplete = plan_work.existing_archives(gh, "o/r")
        self.assertEqual({"good"}, complete)
        self.assertEqual({"assetless", "wrong-asset", "drafted"}, incomplete)


class TestDesiredWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = archive_common.CONFIG_DIR
        archive_common.CONFIG_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(archive_common, "CONFIG_DIR", self._orig))

    def write(self, language, n):
        lock = Lockfile(language=language, srcml_flags=["-r"], systems=[system(i) for i in range(1, n + 1)])
        write_lockfile(lock, Path(self.tmp.name) / f"{language}.lock.toml")

    def test_one_row_per_system_per_srcml_version(self):
        self.write("c", 3)
        rows = plan_work.desired_work(make_policy(["c"], versions=("1.0.0", "1.1.0")), ["c"])
        self.assertEqual(6, len(rows))
        self.assertEqual({"1.0.0", "1.1.0"}, {r["srcml_version"] for r in rows})
        self.assertEqual(6, len({r["tag"] for r in rows}), "tags must be unique")

    def test_languages_are_interleaved_by_rank(self):
        # With a 50-job cap and 100 systems per language, language-major ordering
        # would spend every run on C and never reach Python. Round-robin keeps
        # all languages advancing together.
        self.write("c", 3)
        self.write("python", 3)
        rows = plan_work.desired_work(make_policy(["c", "python"]), ["c", "python"])
        self.assertEqual(
            ["c", "python", "c", "python", "c", "python"], [r["language"] for r in rows]
        )
        self.assertEqual([1, 1, 2, 2, 3, 3], [r["rank"] for r in rows])

    def test_uneven_lockfiles_do_not_drop_rows(self):
        self.write("c", 4)
        self.write("python", 1)
        rows = plan_work.desired_work(make_policy(["c", "python"]), ["c", "python"])
        self.assertEqual(5, len(rows))
        self.assertEqual(1, sum(1 for r in rows if r["language"] == "python"))

    def test_missing_lockfile_is_skipped_not_fatal(self):
        self.write("c", 2)
        rows = plan_work.desired_work(make_policy(["c", "java"]), ["c", "java"])
        self.assertEqual(2, len(rows))

    def test_row_carries_everything_the_build_needs(self):
        self.write("c", 1)
        row = plan_work.desired_work(make_policy(["c"]), ["c"])[0]
        for key in ("tag", "title", "language", "name", "repo", "version",
                    "version_kind", "commit", "license", "srcml_version", "srcml_flags"):
            self.assertIn(key, row)
        self.assertTrue(row["commit"])
        self.assertEqual("-r", row["srcml_flags"])
        # Matrix rows are serialized into the workflow; anything unserializable
        # would fail only at run time.
        json.dumps(row)


class TestReconcileDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = archive_common.CONFIG_DIR
        archive_common.CONFIG_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(archive_common, "CONFIG_DIR", self._orig))
        lock = Lockfile(language="c", srcml_flags=["-r"], systems=[system(i) for i in range(1, 4)])
        write_lockfile(lock, Path(self.tmp.name) / "c.lock.toml")
        self.rows = plan_work.desired_work(make_policy(["c"]), ["c"])

    def test_idempotency(self):
        # Re-running with everything published must plan zero work, or the
        # nightly schedule would rebuild the whole corpus every night.
        gh = FakeGitHub([{"tag_name": r["tag"], "assets": [{"name": f"{r['tag']}.xml.zst"}]} for r in self.rows])
        complete, _ = plan_work.existing_archives(gh, "o/r")
        self.assertEqual([], [r for r in self.rows if r["tag"] not in complete])

    def test_only_missing_are_planned(self):
        published = self.rows[0]
        gh = FakeGitHub([{"tag_name": published["tag"], "assets": [{"name": f"{published['tag']}.xml.zst"}]}])
        complete, _ = plan_work.existing_archives(gh, "o/r")
        todo = [r for r in self.rows if r["tag"] not in complete]
        self.assertEqual(2, len(todo))
        self.assertNotIn(published["tag"], {r["tag"] for r in todo})

    def test_partial_release_is_replanned(self):
        broken = self.rows[1]
        gh = FakeGitHub([{"tag_name": broken["tag"], "assets": [{"name": f"{broken['tag']}.generate.log"}]}])
        complete, incomplete = plan_work.existing_archives(gh, "o/r")
        todo = [r for r in self.rows if r["tag"] not in complete]
        self.assertIn(broken["tag"], {r["tag"] for r in todo})
        self.assertIn(broken["tag"], incomplete)


class TestQuarantine(unittest.TestCase):
    """The filter that stops a hopeless system consuming a slot every night."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = archive_common.CONFIG_DIR
        archive_common.CONFIG_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(archive_common, "CONFIG_DIR", self._orig))
        lock = Lockfile(language="c", srcml_flags=["-r"], systems=[system(i) for i in range(1, 4)])
        write_lockfile(lock, Path(self.tmp.name) / "c.lock.toml")
        self.rows = plan_work.desired_work(make_policy(["c"]), ["c"])
        self.walled = self.rows[1]["tag"]
        self.failures = {
            self.rows[0]["tag"]: {"count": 2},          # below the limit
            self.walled: {"count": 3},                  # at the limit
        }

    def test_only_tags_at_the_limit_are_quarantined(self):
        self.assertEqual({self.walled}, quarantined_tags(self.failures, 3))

    def test_quarantined_tag_is_dropped_from_todo(self):
        walled = quarantined_tags(self.failures, 3)
        todo = [r for r in self.rows if r["tag"] not in walled]
        self.assertEqual(2, len(todo))
        self.assertNotIn(self.walled, {r["tag"] for r in todo})

    def test_a_tag_below_the_threshold_is_still_built(self):
        walled = quarantined_tags(self.failures, 3)
        self.assertIn(self.rows[0]["tag"], {r["tag"] for r in self.rows if r["tag"] not in walled})

    def test_include_quarantined_puts_them_at_the_front(self):
        # Retrying is useless if the backlog pushes the retry past the cap.
        walled = quarantined_tags(self.failures, 3)
        skipped = [r for r in self.rows if r["tag"] in walled]
        remaining = [r for r in self.rows if r["tag"] not in walled]
        todo = skipped + remaining
        self.assertEqual(self.walled, todo[0]["tag"])
        self.assertEqual(len(self.rows), len(todo))

    def test_quarantined_rows_stay_in_desired_work(self):
        # They must keep appearing in the index as failed; only the matrix
        # drops them.
        self.assertIn(self.walled, {r["tag"] for r in self.rows})

    def test_empty_ledger_quarantines_nothing(self):
        self.assertEqual(set(), quarantined_tags({}, 3))


if __name__ == "__main__":
    unittest.main()

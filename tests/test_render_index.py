#!/usr/bin/env python3
"""Tests for the index renderer and the build-failure strike counter.

The strike counter is the subtlest logic in the repo, because it is a state
machine driven by a re-scanned API window. `apply_strikes` is pure, so most of
what matters here needs no fake API at all.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from archive_common import (  # noqa: E402
    Lockfile,
    System,
    load_failures,
    write_failures,
    write_lockfile,
)
import archive_common  # noqa: E402
import render_index  # noqa: E402


def make_policy(languages=("c",), versions=("1.1.0",)):
    return {
        "targets": {"count": 2, "languages": list(languages)},
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
    return System(
        f"own/repo{n}",
        f"https://github.com/own/repo{n}.git",
        f"v{n}.0",
        "tag",
        str(n) * 40,
        "MIT",
    )


def attempt(run_id, conclusion="failure", event="schedule"):
    return {
        "conclusion": conclusion,
        "event": event,
        "run_id": run_id,
        "run_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "attempted_at": f"2026-08-{run_id:02d}T04:00:00Z",
    }


class FakeGitHub:
    """Serves canned workflow runs, jobs, and releases.

    `runs` are given newest-first, as the real API returns them, each carrying
    its own job list.
    """

    def __init__(self, runs=(), releases=()):
        self.runs = list(runs)
        self.releases = list(releases)

    def get(self, path, params=None):
        if "/actions/workflows/" in path:
            return {"workflow_runs": [{k: v for k, v in r.items() if k != "jobs"} for r in self.runs]}
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path, params=None, max_pages=10, key=None):
        if path.endswith("/jobs"):
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            for run in self.runs:
                if run["id"] == run_id:
                    return iter(run.get("jobs", []))
            return iter([])
        if path.endswith("/releases"):
            return iter(self.releases)
        raise AssertionError(f"unexpected paginate {path}")


def run(run_id, jobs, event="schedule"):
    return {
        "id": run_id,
        "event": event,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "created_at": f"2026-08-{run_id:02d}T04:00:00Z",
        "jobs": jobs,
    }


class LockfileTestCase(unittest.TestCase):
    """Repoints CONFIG_DIR at a tempdir, like test_plan_work does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = archive_common.CONFIG_DIR
        archive_common.CONFIG_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(archive_common, "CONFIG_DIR", self._orig))

    def write(self, language, n):
        lock = Lockfile(
            language=language,
            srcml_flags=["-r"],
            systems=[system(i) for i in range(1, n + 1)],
        )
        write_lockfile(lock, Path(self.tmp.name) / f"{language}.lock.toml")


class TestJobNames(unittest.TestCase):
    def test_job_name_carries_the_full_triple(self):
        # Without the srcml version two tags share one job name, and a single
        # night's failure would strike both.
        a = render_index.job_name("c", "own/repo1", "v1.0", "1.0.0")
        b = render_index.job_name("c", "own/repo1", "v1.0", "1.1.0")
        self.assertNotEqual(a, b)
        self.assertIn(render_index.JOB_SEP, a)

    def test_legacy_name_is_the_three_part_form(self):
        self.assertEqual(
            "c · own/repo1 · v1.0", render_index.legacy_job_name("c", "own/repo1", "v1.0")
        )


class TestCollectJobAttempts(unittest.TestCase):
    def test_every_sighting_is_kept_newest_first(self):
        job = render_index.job_name("c", "own/repo1", "v1.0", "1.1.0")
        gh = FakeGitHub(runs=[
            run(9, [{"name": job, "conclusion": "failure"}]),
            run(7, [{"name": job, "conclusion": "failure"}]),
            run(5, [{"name": job, "conclusion": "success"}]),
        ])
        attempts, oldest = render_index.collect_job_attempts(gh, "o/r")
        self.assertEqual([9, 7, 5], [a["run_id"] for a in attempts[job]])
        self.assertEqual(5, oldest)

    def test_non_build_jobs_are_ignored(self):
        job = render_index.job_name("c", "own/repo1", "v1.0", "1.1.0")
        gh = FakeGitHub(runs=[run(9, [
            {"name": "plan", "conclusion": "success"},
            {"name": job, "conclusion": "failure"},
            {"name": "report", "conclusion": "success"},
        ])])
        attempts, _ = render_index.collect_job_attempts(gh, "o/r")
        self.assertEqual([job], list(attempts))

    def test_no_runs_is_not_fatal(self):
        attempts, oldest = render_index.collect_job_attempts(FakeGitHub(runs=[]), "o/r")
        self.assertEqual({}, attempts)
        self.assertEqual(0, oldest)

    def test_latest_outcomes_takes_the_newest_attempt(self):
        job = render_index.job_name("c", "own/repo1", "v1.0", "1.1.0")
        attempts = {job: [attempt(9), attempt(7, "success")]}
        self.assertEqual(9, render_index.latest_outcomes(attempts)[job]["run_id"])


class TestApplyStrikes(LockfileTestCase):
    def setUp(self):
        super().setUp()
        self.write("c", 1)
        self.targets = render_index.iter_targets(make_policy())
        self.target = self.targets[0]
        self.tag = self.target["tag"]
        self.job = self.target["job"]

    def apply(self, failures, attempts, published=(), oldest=1, stale=()):
        return render_index.apply_strikes(
            failures, attempts, self.targets, set(published), oldest, set(stale)
        )

    def test_a_scheduled_failure_opens_a_record(self):
        led = self.apply({}, {self.job: [attempt(9)]})
        self.assertEqual(1, led[self.tag]["count"])
        self.assertEqual(9, led[self.tag]["last_run_id"])
        self.assertEqual("own/repo1", led[self.tag]["system"])
        self.assertEqual("1.1.0", led[self.tag]["srcml_version"])

    def test_rescanning_the_same_window_adds_no_strike(self):
        # THE core idempotency claim. Every render re-scans the same runs, and
        # index.yml cancels in-progress renders, so this happens constantly.
        attempts = {self.job: [attempt(9), attempt(7)]}
        once = self.apply({}, attempts)
        twice = self.apply(once, attempts)
        self.assertEqual(2, once[self.tag]["count"])
        self.assertEqual(once, twice)

    def test_only_runs_newer_than_the_watermark_count(self):
        led = self.apply({}, {self.job: [attempt(7)]})
        led = self.apply(led, {self.job: [attempt(9), attempt(7)]})
        self.assertEqual(2, led[self.tag]["count"])
        self.assertEqual(9, led[self.tag]["last_run_id"])

    def test_cancelled_does_not_strike_but_failure_and_timeout_do(self):
        led = self.apply({}, {self.job: [attempt(9, "cancelled")]})
        self.assertEqual({}, led)
        led = self.apply({}, {self.job: [attempt(9, "timed_out"), attempt(7, "failure")]})
        self.assertEqual(2, led[self.tag]["count"])

    def test_manual_dispatch_does_not_strike(self):
        # Dry runs share the workflow and the job names, and publish nothing.
        # If they struck, they could add strikes but never clear them.
        led = self.apply({}, {self.job: [attempt(9, event="workflow_dispatch")]})
        self.assertEqual({}, led)

    def test_in_flight_run_does_not_advance_the_watermark(self):
        # The easiest thing to get wrong: skipping an unconcluded run while
        # moving the watermark past it would lose that strike forever.
        led = self.apply({}, {self.job: [attempt(9, None)]})
        self.assertEqual({}, led)
        led = self.apply(led, {self.job: [attempt(9, "failure")]})
        self.assertEqual(1, led[self.tag]["count"])

    def test_publish_zeroes_the_count_but_keeps_the_watermark(self):
        led = self.apply({}, {self.job: [attempt(7)]})
        led = self.apply(led, {self.job: [attempt(7)]}, published=[self.tag])
        self.assertEqual(0, led[self.tag]["count"])
        self.assertEqual(7, led[self.tag]["last_run_id"])

    def test_a_failure_after_a_publish_counts_one_not_three(self):
        # Deleting the record on success would drop the watermark, and this
        # rescan would re-count runs 5 and 7 alongside 9 — instant quarantine
        # after a single failure.
        attempts = {self.job: [attempt(7), attempt(5)]}
        led = self.apply({}, attempts)
        self.assertEqual(2, led[self.tag]["count"])
        led = self.apply(led, attempts, published=[self.tag])
        attempts = {self.job: [attempt(9), attempt(7), attempt(5)]}
        led = self.apply(led, attempts)
        self.assertEqual(1, led[self.tag]["count"])
        self.assertEqual("2026-08-09T04:00:00Z", led[self.tag]["first_failed_at"])

    def test_two_srcml_versions_strike_independently(self):
        self.targets = render_index.iter_targets(make_policy(versions=("1.0.0", "1.1.0")))
        old, new = self.targets
        led = self.apply({}, {new["job"]: [attempt(9)]})
        self.assertEqual([new["tag"]], list(led))
        self.assertNotIn(old["tag"], led)

    def test_legacy_job_name_earns_no_strike(self):
        # It is ambiguous between srcml versions; that ambiguity is the bug the
        # rename fixes, so it must not feed the counter.
        led = self.apply({}, {self.target["legacy_job"]: [attempt(9)]})
        self.assertEqual({}, led)

    def test_tag_dropped_from_the_lockfiles_is_pruned(self):
        led = self.apply({"archive-c-gone-v1-srcml-1.1.0": {"language": "c", "count": 2}}, {})
        self.assertEqual({}, led)

    def test_entries_survive_a_missing_lockfile(self):
        # iter_targets skips a language with no lockfile, so an unconditional
        # prune would wipe that language's ledger on a partial checkout.
        stale_rec = {"archive-java-x-v1-srcml-1.1.0": {"language": "java", "count": 2}}
        led = self.apply(stale_rec, {}, stale=["java"])
        self.assertEqual(stale_rec, led)

    def test_zeroed_record_older_than_the_window_is_pruned(self):
        rec = {self.tag: {"language": "c", "count": 0, "last_run_id": 3}}
        self.assertEqual({}, self.apply(rec, {}, oldest=5))
        # Still inside the window, so the watermark is still load-bearing.
        self.assertEqual(rec, self.apply(rec, {}, oldest=2))


class TestQuarantineIsDerived(LockfileTestCase):
    def test_threshold_changes_without_rewriting_the_ledger(self):
        self.write("c", 1)
        targets = render_index.iter_targets(make_policy())
        tag = targets[0]["tag"]
        failures = {tag: {"language": "c", "count": 3, "last_run_url": "u"}}

        rows = render_index.build_rows(targets, {}, {}, failures, max_failures=3)
        self.assertEqual("quarantined", rows[0]["status"])
        rows = render_index.build_rows(targets, {}, {}, failures, max_failures=4)
        self.assertEqual("failed", rows[0]["status"])
        self.assertEqual(3, failures[tag]["count"], "ledger must not be mutated")


class TestBuildRows(LockfileTestCase):
    def setUp(self):
        super().setUp()
        self.write("c", 1)
        self.targets = render_index.iter_targets(make_policy())
        self.target = self.targets[0]
        self.tag = self.target["tag"]

    def rows(self, releases=None, outcomes=None, failures=None):
        return render_index.build_rows(
            self.targets, releases or {}, outcomes or {}, failures or {}, 3
        )

    def test_published_beats_everything(self):
        rel = {self.tag: {"url": "r", "download_url": "d", "bytes_compressed": 10}}
        failures = {self.tag: {"count": 5, "last_run_url": "old"}}
        row = self.rows(releases=rel, failures=failures)[0]
        self.assertEqual("published", row["status"])
        self.assertTrue(row["published"])

    def test_ledger_url_is_used_once_the_run_ages_out(self):
        # The whole point: no live outcome at all, but the index still says
        # failed and still links to the last build log.
        failures = {self.tag: {
            "count": 3,
            "last_run_url": "https://example/runs/9",
            "last_failed_at": "2026-08-09T04:00:00Z",
        }}
        row = self.rows(failures=failures)[0]
        self.assertEqual("quarantined", row["status"])
        self.assertEqual("https://example/runs/9", row["last_run_url"])
        self.assertEqual("2026-08-09T04:00:00Z", row["last_attempt_at"])

    def test_published_row_keeps_its_successful_run_url(self):
        # Publishing zeroes the count, so the live outcome wins and the row
        # does not point at the last failure before the fix.
        rel = {self.tag: {"url": "r"}}
        outcomes = {self.target["job"]: {"conclusion": "success", "run_url": "new", "attempted_at": "t"}}
        failures = {self.tag: {"count": 0, "last_run_url": "old"}}
        row = self.rows(releases=rel, outcomes=outcomes, failures=failures)[0]
        self.assertEqual("new", row["last_run_url"])

    def test_legacy_job_name_still_renders_a_link(self):
        outcomes = {self.target["legacy_job"]: {
            "conclusion": "failure", "run_url": "legacy", "attempted_at": "t"
        }}
        row = self.rows(outcomes=outcomes)[0]
        self.assertEqual("failed", row["status"])
        self.assertEqual("legacy", row["last_run_url"])

    def test_untouched_system_is_pending(self):
        self.assertEqual("pending", self.rows()[0]["status"])

    def test_quarantined_renders_as_failed_and_says_not_retried(self):
        failures = {self.tag: {"count": 3, "last_run_url": "https://example/runs/9"}}
        md = render_index.render_markdown(self.rows(failures=failures), "o/r", 3)
        self.assertIn("1 system(s) currently failing", md)
        self.assertIn("3 failed attempts, no longer retried", md)
        self.assertIn("https://example/runs/9", md)
        self.assertIn("retry_failed", md)


class TestLedgerRoundTrip(LockfileTestCase):
    def test_write_is_byte_stable(self):
        # The index workflow commits this file on every render; unstable
        # ordering would fill the git log with noise.
        path = Path(self.tmp.name) / "failures.json"
        rec = {
            "b-tag": {"count": 2, "language": "c", "last_run_id": 9},
            "a-tag": {"language": "python", "count": 1, "last_run_id": 7},
        }
        first = write_failures(rec, path)
        self.assertEqual(first, write_failures(load_failures(path), path))

    def test_missing_file_is_an_empty_ledger(self):
        self.assertEqual({}, load_failures(Path(self.tmp.name) / "nope.json"))

    def test_malformed_file_does_not_crash_the_nightly(self):
        path = Path(self.tmp.name) / "failures.json"
        path.write_text("{ not json")
        self.assertEqual({}, load_failures(path))

    def test_future_schema_is_ignored_rather_than_misread(self):
        path = Path(self.tmp.name) / "failures.json"
        path.write_text('{"schema": 99, "failures": {"t": {"count": 3}}}')
        self.assertEqual({}, load_failures(path))


if __name__ == "__main__":
    unittest.main()

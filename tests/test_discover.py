#!/usr/bin/env python3
"""Tests for selection and version resolution.

Fixtures in tests/fixtures.json are real API responses, recorded rather than
invented — the whole point of these filters is that the live data is
counterintuitive (Python's top repos are markdown, Linux reports its license as
NOASSERTION), so hand-written fixtures would test a fantasy.

Run: python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from archive_common import (  # noqa: E402
    Lockfile,
    System,
    load_policy,
    read_lockfile,
    release_tag,
    release_title,
    sanitize_ref_component,
    write_lockfile,
)
from discover import (  # noqa: E402
    Rejected,
    check_code_mass,
    check_metadata,
    parse_version,
    pick_best_tag,
    resolve_version,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text())
FILTERS = load_policy()["filters"]


class FakeGitHub:
    """Serves the recorded fixtures; raises on any unrecorded path."""

    def __init__(self):
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        parts = path.strip("/").split("/")
        name = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        if rest == ["languages"]:
            return FIXTURES["languages"].get(name)
        if rest == ["releases", "latest"]:
            return FIXTURES["releases_latest"].get(name)
        if rest == ["tags"]:
            return FIXTURES["tags"].get(name, [])
        if rest[:2] == ["git", "ref"]:
            tag = "/".join(rest[3:])
            for t in FIXTURES["tags"].get(name, []):
                if t["name"] == tag:
                    return {"object": {"sha": t["commit"]["sha"], "type": "commit"}}
            return None
        if rest[0] == "branches":
            return {"commit": {"sha": "0" * 40}}
        raise AssertionError(f"unrecorded API path: {path}")

    def paginate(self, path, params=None, max_pages=10):
        result = self.get(path, params)
        return iter(result or [])


def search_item(name: str) -> dict:
    """A search-result-shaped dict for a recorded repo."""
    return dict(FIXTURES["repos"][name])


class TestCodeMass(unittest.TestCase):
    """Stars alone select markdown collections; code mass is what filters them."""

    def _reject_reason(self, name, linguist):
        with self.assertRaises(Rejected) as ctx:
            check_code_mass(FakeGitHub(), name, linguist, FILTERS)
        return ctx.exception.reason

    def test_curated_lists_rejected_on_byte_mass(self):
        # The two highest-starred "Python" repos on GitHub are not Python code.
        self.assertIn("40,479 bytes", self._reject_reason("public-apis/public-apis", "Python"))
        self.assertIn("119,285 bytes", self._reject_reason("vinta/awesome-python", "Python"))

    def test_polyglot_tutorial_rejected_on_share(self):
        # hello-algo clears the 1 MB floor with 1,094,232 bytes of Java, so the
        # byte test alone would admit it. It reimplements one tutorial in a dozen
        # languages, and the dominance test is what catches that.
        reason = self._reject_reason("krahets/hello-algo", "Java")
        self.assertIn("% of the repo", reason)

    def test_real_systems_accepted(self):
        for name, linguist, min_bytes in [
            ("django/django", "Python", 19_000_000),
            ("yt-dlp/yt-dlp", "Python", 10_000_000),
            ("torvalds/linux", "C", 1_400_000_000),
            ("elastic/elasticsearch", "Java", 272_000_000),
        ]:
            with self.subTest(name):
                lang_bytes, share = check_code_mass(FakeGitHub(), name, linguist, FILTERS)
                self.assertGreaterEqual(lang_bytes, min_bytes)
                self.assertGreaterEqual(share, FILTERS["min_language_share"])


class TestLicenseGate(unittest.TestCase):
    def test_noassertion_is_not_treated_as_unlicensed(self):
        # Regression guard. GitHub reports torvalds/linux as NOASSERTION because
        # licensee cannot map its COPYING file to a single SPDX id. Treating that
        # as "no license" rejects the single most important C system there is.
        check_metadata(search_item("torvalds/linux"), FILTERS, set())
        check_metadata(search_item("elastic/elasticsearch"), FILTERS, set())

    def test_missing_license_rejected(self):
        item = search_item("django/django")
        item["license"] = None
        with self.assertRaises(Rejected) as ctx:
            check_metadata(item, FILTERS, set())
        self.assertIn("no license", ctx.exception.reason)

    def test_blocklist_and_repo_state(self):
        with self.assertRaises(Rejected) as ctx:
            check_metadata(search_item("django/django"), FILTERS, {"django/django"})
        self.assertEqual("blocklisted", ctx.exception.reason)

        for flag, reason in [("archived", "archived"), ("fork", "fork"), ("is_template", "template repo")]:
            with self.subTest(flag):
                item = search_item("django/django")
                item[flag] = True
                with self.assertRaises(Rejected) as ctx:
                    check_metadata(item, FILTERS, set())
                self.assertEqual(reason, ctx.exception.reason)


class TestVersionParsing(unittest.TestCase):
    def test_stable_versus_prerelease(self):
        cases = {
            "v7.1": ((7, 1), True),
            "1.25.0": ((1, 25, 0), True),
            "v2026.8.3": ((2026, 8, 3), True),
            "7.24.4": ((7, 24, 4), True),
            "v7.2-rc7": ((7, 2), False),
            "2.0.0-beta3": ((2, 0, 0), False),
            "v1.0.0-alpha": ((1, 0, 0), False),
            "3.1.0.dev1": ((3, 1, 0), False),
        }
        for tag, expected in cases.items():
            with self.subTest(tag):
                self.assertEqual(expected, parse_version(tag))

    def test_non_version_tags_ignored(self):
        # Django publishes branch-shaped tags; java-design-patterns publishes prose.
        for tag in [
            "stable/5.1.x",
            "open-source-java-design-patterns-2nd-edition",
            "latest",
            "nightly",
        ]:
            with self.subTest(tag):
                self.assertIsNone(parse_version(tag))

    def test_picks_highest_stable_not_newest(self):
        tags = [
            {"name": "v7.2-rc7", "commit": {"sha": "a" * 40}},
            {"name": "v7.1", "commit": {"sha": "b" * 40}},
            {"name": "v7.0", "commit": {"sha": "c" * 40}},
        ]
        self.assertEqual("v7.1", pick_best_tag(tags, allow_prerelease=False)["name"])
        self.assertEqual("v7.2-rc7", pick_best_tag(tags, allow_prerelease=True)["name"])

    def test_ordering_is_numeric_not_lexical(self):
        tags = [{"name": n, "commit": {"sha": "0" * 40}} for n in ["1.9.0", "1.10.0", "1.2.0"]]
        self.assertEqual("1.10.0", pick_best_tag(tags, allow_prerelease=False)["name"])


class TestVersionResolution(unittest.TestCase):
    def test_kernel_falls_back_to_stable_tag(self):
        # The kernel publishes no GitHub Releases, and its newest tag is almost
        # always a release candidate.
        version, kind, sha = resolve_version(FakeGitHub(), "torvalds/linux", "master")
        self.assertEqual("tag", kind)
        self.assertTrue(parse_version(version)[1], f"{version} should be stable")
        self.assertNotIn("rc", version.lower())
        self.assertEqual(40, len(sha))

    def test_django_resolves_despite_zero_releases(self):
        # Django has no GitHub Releases at all. A release-count filter would drop
        # it entirely; the tag fallback keeps it.
        self.assertIsNone(FIXTURES["releases_latest"]["django/django"])
        version, kind, sha = resolve_version(FakeGitHub(), "django/django", "main")
        self.assertEqual("tag", kind)
        # Django tags `6.1` alongside `6.1rc1`, `6.1b1` and `6.1a1`; the stable
        # one must win, and branch-shaped `stable/*.x` tags must be ignored.
        self.assertTrue(parse_version(version)[1], f"{version} should be stable")
        self.assertFalse(version.startswith("stable/"))
        self.assertEqual(40, len(sha))

    def test_branch_shaped_tags_fall_through_to_head(self):
        # A repo whose only tags are branch names has no discoverable release.
        # Labelling `stable/5.1.x` as a tag would misrepresent a moving branch
        # tip as a pinned version, so resolution drops to HEAD instead.
        version, kind, sha = resolve_version(FakeGitHub(), "fake/branchtags", "main")
        self.assertEqual("head", kind)
        self.assertTrue(version.startswith("main-"))

    def test_release_preferred_when_present(self):
        version, kind, _ = resolve_version(FakeGitHub(), "yt-dlp/yt-dlp", "master")
        self.assertEqual("release", kind)
        self.assertEqual(FIXTURES["releases_latest"]["yt-dlp/yt-dlp"]["tag_name"], version)

    def test_head_fallback_when_no_tags(self):
        version, kind, sha = resolve_version(FakeGitHub(), "public-apis/public-apis", "master")
        self.assertEqual("head", kind)
        self.assertTrue(version.startswith("master-"))
        self.assertEqual("0" * 40, sha)


class TestReleaseNaming(unittest.TestCase):
    def test_tag_shape(self):
        self.assertEqual(
            "archive-c-torvalds-linux-v7.1-srcml-1.1.0",
            release_tag("c", "torvalds/linux", "v7.1", "1.1.0"),
        )

    def test_title_uses_display_language(self):
        self.assertEqual(
            "C++ | tensorflow/tensorflow/v2.21.0 | srcml/v1.1.0",
            release_title("cpp", "tensorflow/tensorflow", "v2.21.0", "1.1.0"),
        )
        self.assertTrue(release_title("csharp", "a/b", "1", "1.1.0").startswith("C# |"))

    def test_unsafe_version_strings_become_legal_refs(self):
        # Real version labels contain slashes, spaces, and git-illegal forms.
        for raw in ["release/1.2", "v1.0 (final)", "@scope/pkg@1.0", "..", "-lead", "x.lock"]:
            with self.subTest(raw):
                tag = release_tag("java", "own/repo", raw, "1.1.0")
                self.assertRegex(tag, r"^[A-Za-z0-9._-]+$")
                self.assertNotIn("..", tag)
                self.assertFalse(tag.endswith(".lock"))
                self.assertFalse(tag.startswith("-"))

    def test_owner_disambiguates_same_repo_name(self):
        self.assertNotEqual(
            release_tag("python", "alice/cpython", "3.13", "1.1.0"),
            release_tag("python", "bob/cpython", "3.13", "1.1.0"),
        )

    def test_long_names_truncate_deterministically(self):
        long_version = "v" + "9" * 400
        a = release_tag("c", "owner/repo", long_version, "1.1.0")
        b = release_tag("c", "owner/repo", long_version, "1.1.0")
        self.assertEqual(a, b)
        self.assertLessEqual(len(a), 200)
        self.assertNotEqual(a, release_tag("c", "owner/repo", long_version + "8", "1.1.0"))

    def test_sanitize_never_returns_empty(self):
        self.assertTrue(sanitize_ref_component("///"))
        self.assertTrue(sanitize_ref_component(""))


class TestLockfileRoundTrip(unittest.TestCase):
    def test_write_then_read(self):
        import tempfile

        lock = Lockfile(
            language="c",
            srcml_flags=["-r", "-j", "4"],
            systems=[
                System("torvalds/linux", "https://github.com/torvalds/linux.git",
                       "v7.1", "tag", "a" * 40, "NOASSERTION"),
                System("redis/redis", "https://github.com/redis/redis.git",
                       "8.10.0", "release", "b" * 40, "BSD-3-Clause"),
            ],
        )
        with tempfile.NamedTemporaryFile(suffix=".toml") as tmp:
            path = Path(tmp.name)
            text = write_lockfile(lock, path)
            back = read_lockfile("c", path)
            self.assertEqual([s.name for s in lock.systems], [s.name for s in back.systems])
            self.assertEqual(lock.srcml_flags, back.srcml_flags)
            self.assertEqual("tag", back.systems[0].version_kind)
            # Order carries the star ranking, so it must survive the round trip.
            self.assertEqual("torvalds/linux", back.systems[0].name)
            # No timestamp: an unchanged selection must produce a byte-identical
            # file, otherwise every monthly discovery run opens a noisy PR.
            self.assertEqual(text, write_lockfile(lock, path))


if __name__ == "__main__":
    unittest.main()

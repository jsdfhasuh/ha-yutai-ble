"""Offline release guards: real packages, fake GitHub responses, no remote writes."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
REPOSITORY = "example/ha-yutai-ble"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("release_test_builder", ROOT / "tools/build_release.py")
with patch.dict(sys.modules, {"build_release": builder}):
    publisher = load("release_test_publisher", ROOT / "tools/publish_release.py")


class FakeGitHub:
    def __init__(self, files, version):
        self.files = files
        self.tag = f"v{version}"
        self.release = None
        self.assets = []
        self.target = None
        self.calls = []
        self.corrupt_upload = False
        self.latest_id = 1

    def make_release(self, *, draft=True):
        self.release = {"id": 1, "tag_name": self.tag, "target_commitish": SHA,
                        "draft": draft, "prerelease": False,
                        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{self.tag}"}

    def add_asset(self, path):
        self.assets.append({"name": path.name, "size": path.stat().st_size,
                            "digest": f"sha256:{publisher.digest(path)}", "state": "uploaded"})

    def api(self, repository, path, *, method="GET", data=None, paginate=False):
        assert repository == REPOSITORY
        self.calls.append((method, path, copy.deepcopy(data)))
        if path == "releases?per_page=100":
            return [copy.deepcopy(self.release)] if self.release else []
        if path == "releases/latest":
            return {"id": self.latest_id}
        if path.startswith("git/matching-refs/"):
            # Prefix matches must not be mistaken for the exact release tag.
            refs = [{"ref": f"refs/tags/{self.tag}0", "object": {"type": "commit", "sha": "b" * 40}}]
            if self.target:
                refs.append({"ref": f"refs/tags/{self.tag}", "object": copy.deepcopy(self.target)})
            return refs
        if path.startswith("git/tags/"):
            return {"object": {"type": "commit", "sha": SHA}}
        if path == "git/refs" and method == "POST":
            assert self.target is None
            self.target = {"type": "commit", "sha": data["sha"]}
            return {"object": self.target}
        if path == "releases/1/assets?per_page=100":
            return copy.deepcopy(self.assets)
        if path == "releases/1":
            if method == "PATCH":
                assert self.release["draft"], "Must never mutate a published release"
                self.release.update(data)
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def gh(self, *arguments, input=None):
        self.calls.append(("gh", arguments, input))
        assert arguments[:2] in (("release", "create"), ("release", "upload"))
        assert "--clobber" not in arguments
        if arguments[1] == "create":
            assert self.release is None
            assert "--draft" in arguments
            assert arguments[arguments.index("--target") + 1] == SHA
            self.make_release()
        else:
            assert self.release["draft"]
        for path in self.files:
            if str(path) in arguments:
                assert path.name not in {asset["name"] for asset in self.assets}
                self.add_asset(path)
        if self.corrupt_upload:
            self.assets[-1]["digest"] = "sha256:" + "0" * 64
        return ""


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.files = list(builder.build(self.directory / "dist"))
        self.manifest = json.loads((ROOT / builder.COMPONENT / "manifest.json").read_text())
        self.version = self.manifest["version"]
        self.notes = self.directory / "notes.md"
        self.notes.write_text("Release notes from the tested commit.\n", encoding="utf-8")

    def publish(self, fake):
        with patch.object(publisher, "api", fake.api), patch.object(publisher, "gh", fake.gh):
            return publisher.publish_release(REPOSITORY, SHA, self.version, self.files, self.notes)

    def rewrite_zip(self, path, *, replace=None, remove=None):
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        if replace:
            entries.update(replace)
        if remove:
            entries.pop(remove)
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents in entries.items():
                archive.writestr(name, contents)
        self.files[2].write_text("".join(f"{publisher.digest(item)}  {item.name}\n" for item in self.files[:2]))

    def test_trigger_requires_exact_main_push_and_version_subject(self):
        environment = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SHA}
        event = {"after": SHA, "head_commit": {"id": SHA, "message": f"Release v{self.version}\n\nDetails"}}
        self.assertTrue(publisher.is_release_push(environment, event, self.version))
        for key, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "workflow_dispatch"),
                           ("GITHUB_REF", "refs/heads/other"), ("GITHUB_REF", f"refs/tags/v{self.version}"),
                           ("GITHUB_SHA", "b" * 40)):
            with self.subTest(key=key, value=value):
                self.assertFalse(publisher.is_release_push({**environment, key: value}, event, self.version))
        for subject in ("Ordinary commit", f"Release v{self.version} ", f"Release v{self.version} extra", "Release v999.0.0", ""):
            with self.subTest(subject=subject):
                changed = {**event, "head_commit": {"id": SHA, "message": subject}}
                self.assertFalse(publisher.is_release_push(environment, changed, self.version))
        self.assertFalse(publisher.is_release_push(environment, {}, self.version))

    def test_real_packages_pass_local_validation_and_verify_only(self):
        self.assertEqual(publisher.validate_artifacts(self.directory / "dist", self.manifest), self.files)
        environment = os.environ.copy()
        environment["PATH"] = ""  # A validation-only run cannot invoke gh or git.
        environment.pop("GITHUB_EVENT_PATH", None)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools/publish_release.py"), "--verify-only", "--artifacts", str(self.directory / "dist")],
            env=environment, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Verified v{self.version}", result.stdout)

    def test_wrong_filenames_and_checksum_fail(self):
        unexpected = self.directory / "dist" / "unexpected.zip"
        unexpected.write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "exactly"):
            publisher.validate_artifacts(self.directory / "dist", self.manifest)
        unexpected.unlink()
        self.files[2].write_text("0" * 64 + "  other.zip\n")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            publisher.validate_artifacts(self.directory / "dist", self.manifest)

    def test_matching_hashes_do_not_hide_wrong_manifest_or_layout(self):
        changed = {**self.manifest, "version": "999.0.0"}
        self.rewrite_zip(self.files[0], replace={f"{builder.COMPONENT}/manifest.json": json.dumps(changed)})
        with self.assertRaisesRegex(ValueError, "manifest"):
            publisher.validate_artifacts(self.directory / "dist", self.manifest)
        self.files = list(builder.build(self.directory / "dist"))
        self.rewrite_zip(self.files[1], remove="start_qr.cmd")
        with self.assertRaisesRegex(ValueError, "contents"):
            publisher.validate_artifacts(self.directory / "dist", self.manifest)

    def test_new_release_verifies_assets_and_tag_before_publishing(self):
        fake = FakeGitHub(self.files, self.version)
        self.assertTrue(self.publish(fake).endswith(f"/v{self.version}"))
        self.assertFalse(fake.release["draft"])
        self.assertEqual(fake.release["body"], self.notes.read_text())
        self.assertEqual(fake.release["make_latest"], "true")
        create = next(call for call in fake.calls if call[0] == "gh")
        self.assertIn("--notes-file", create[1])
        tag_index = next(i for i, call in enumerate(fake.calls) if call[:2] == ("POST", "git/refs"))
        publish_index = next(i for i, call in enumerate(fake.calls) if call[:2] == ("PATCH", "releases/1"))
        self.assertLess(tag_index, publish_index)
        self.assertTrue(any(call[:2] == ("GET", "releases/1/assets?per_page=100") for call in fake.calls[:tag_index]))
        self.assertEqual(fake.target, {"type": "commit", "sha": SHA})

    def test_matching_published_release_is_read_only_on_rerun(self):
        fake = FakeGitHub(self.files, self.version)
        fake.make_release(draft=False)
        fake.target = {"type": "commit", "sha": SHA}
        for path in self.files:
            fake.add_asset(path)
        self.publish(fake)
        self.assertTrue(all(call[0] == "GET" for call in fake.calls))
        fake.assets.pop()
        fake.calls.clear()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.publish(fake)
        self.assertTrue(all(call[0] == "GET" for call in fake.calls))

    def test_same_commit_draft_only_uploads_missing_assets(self):
        fake = FakeGitHub(self.files, self.version)
        fake.make_release()
        fake.add_asset(self.files[0])
        self.publish(fake)
        uploads = [call[1] for call in fake.calls if call[0] == "gh"]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][:2], ("release", "upload"))
        self.assertNotIn(str(self.files[0]), uploads[0])
        self.assertTrue(all(str(path) in uploads[0] for path in self.files[1:]))

    def test_conflicting_draft_or_tag_is_never_changed(self):
        for conflict in ("draft", "tag"):
            with self.subTest(conflict=conflict):
                fake = FakeGitHub(self.files, self.version)
                fake.make_release()
                if conflict == "draft":
                    fake.release["target_commitish"] = "b" * 40
                else:
                    fake.target = {"type": "commit", "sha": "b" * 40}
                with self.assertRaises(ValueError):
                    self.publish(fake)
                self.assertTrue(all(call[0] == "GET" for call in fake.calls))

    def test_bad_remote_size_digest_or_state_cannot_be_published(self):
        for key, value in (("size", 0), ("digest", None), ("digest", "sha256:" + "0" * 64), ("state", "starter")):
            with self.subTest(key=key, value=value):
                fake = FakeGitHub(self.files, self.version)
                fake.make_release()
                for path in self.files:
                    fake.add_asset(path)
                fake.assets[0][key] = value
                with self.assertRaisesRegex(ValueError, "Remote asset"):
                    self.publish(fake)
                self.assertTrue(all(call[0] == "GET" for call in fake.calls))

    def test_failed_post_upload_digest_check_leaves_release_draft(self):
        fake = FakeGitHub(self.files, self.version)
        fake.corrupt_upload = True
        with self.assertRaisesRegex(ValueError, "Remote asset"):
            self.publish(fake)
        self.assertTrue(fake.release["draft"])
        self.assertIsNone(fake.target)
        self.assertFalse(any(call[0] in ("POST", "PATCH") for call in fake.calls))

    def test_new_publication_must_be_confirmed_as_latest(self):
        fake = FakeGitHub(self.files, self.version)
        fake.latest_id = 2
        with self.assertRaisesRegex(ValueError, "Latest"):
            self.publish(fake)
        self.assertEqual(sum(call[0] == "PATCH" for call in fake.calls), 1)

    def test_annotated_tag_is_resolved_to_commit(self):
        fake = FakeGitHub(self.files, self.version)
        fake.make_release(draft=False)
        fake.target = {"type": "tag", "sha": "c" * 40}
        for path in self.files:
            fake.add_asset(path)
        self.publish(fake)
        self.assertIn(("GET", "git/tags/" + "c" * 40, None), fake.calls)
        self.assertTrue(all(call[0] == "GET" for call in fake.calls))

    def test_api_flattens_pages_and_sends_patch_json_on_stdin(self):
        with patch.object(publisher, "gh", return_value='[[{"id":1}],[{"id":2}]]') as gh:
            self.assertEqual(publisher.api(REPOSITORY, "releases?per_page=100", paginate=True), [{"id": 1}, {"id": 2}])
            self.assertIn("--slurp", gh.call_args.args)
        with patch.object(publisher, "gh", return_value='{"draft":false}') as gh:
            publisher.api(REPOSITORY, "releases/1", method="PATCH", data={"draft": False, "make_latest": "true"})
            self.assertEqual(json.loads(gh.call_args.kwargs["input"]), {"draft": False, "make_latest": "true"})
            self.assertIn("--input", gh.call_args.args)


if __name__ == "__main__":
    unittest.main()

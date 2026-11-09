"""Publish this repository's tested artifacts from an explicit main release commit."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from build_release import AUTH_FILES, COMPONENT, COMPONENT_FILES

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def is_release_push(environment, event, version):
    head = event.get("head_commit") or {}
    lines = head.get("message", "").splitlines()
    sha = environment.get("GITHUB_SHA", "")
    return (
        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is not None
        and environment.get("GITHUB_EVENT_NAME") == "push"
        and environment.get("GITHUB_REF") == "refs/heads/main"
        and re.fullmatch(r"[0-9a-f]{40}", sha) is not None
        and event.get("after") == head.get("id") == sha
        and bool(lines) and lines[0] == f"Release v{version}"
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_artifacts(directory, manifest):
    version = manifest["version"]
    files = [directory / f"yutai-ble-v{version}.zip",
             directory / f"yutai-ble-qr-windows-v{version}.zip",
             directory / "SHA256SUMS.txt"]
    require({path.name for path in directory.iterdir()} == {path.name for path in files},
            "Artifact directory must contain exactly the two versioned ZIPs and SHA256SUMS.txt")
    require(all(path.is_file() and not path.is_symlink() for path in files), "Invalid artifact file")
    expected_sums = "".join(f"{digest(path)}  {path.name}\n" for path in files[:2])
    require(files[2].read_text(encoding="ascii") == expected_sums, "Local SHA256 verification failed")
    component_files = {f"{COMPONENT}/{name}" for name in COMPONENT_FILES} | {
        "README.md", "docs/INSTALL.md", "docs/MODELS.md", "LICENSE", "THIRD_PARTY_NOTICES.md"}
    helper_files = set(AUTH_FILES) | {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    for path, expected in zip(files[:2], (component_files, helper_files)):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)) and set(names) == expected,
                    f"Unexpected ZIP contents: {path.name}")
            require(archive.testzip() is None, f"Corrupt ZIP: {path.name}")
            if path == files[0]:
                packaged = json.loads(archive.read(f"{COMPONENT}/manifest.json"))
                require(packaged == manifest, "Packaged manifest does not match this commit")
    return files


def gh(*arguments, input=None):
    return subprocess.run(["gh", *arguments], input=input, text=True,
                          capture_output=True, check=True).stdout


def api(repository, path, *, method="GET", data=None, paginate=False):
    arguments = ["api", "--method", method, f"repos/{repository}/{path}"]
    if paginate:
        arguments += ["--paginate", "--slurp"]
    if data is not None:
        arguments += ["--input", "-"]
    result = json.loads(gh(*arguments, input=None if data is None else json.dumps(data)))
    return [item for page in result for item in page] if paginate else result


def find_release(repository, tag):
    releases = api(repository, "releases?per_page=100", paginate=True)
    matches = [release for release in releases if release["tag_name"] == tag]
    require(len(matches) <= 1, "Multiple releases use this tag")
    return matches[0] if matches else None


def tag_target(repository, tag):
    refs = api(repository, f"git/matching-refs/tags/{tag}")
    refs = [ref for ref in refs if ref["ref"] == f"refs/tags/{tag}"]
    require(len(refs) <= 1, "Ambiguous release tag")
    if not refs:
        return None
    target = refs[0]["object"]
    while target["type"] == "tag":
        target = api(repository, f"git/tags/{target['sha']}")["object"]
    require(target["type"] == "commit", "Release tag does not point to a commit")
    return target["sha"]


def verify_target(release, tag, sha):
    require(release is not None and release["tag_name"] == tag, "Release tag mismatch")
    require(release["target_commitish"] == sha, "Release target is not this exact commit")
    require(not release["prerelease"], "An existing prerelease must not be overwritten")


def verify_assets(repository, release, files, *, complete):
    assets = api(repository, f"releases/{release['id']}/assets?per_page=100", paginate=True)
    expected = {path.name: path for path in files}
    seen = set()
    for asset in assets:
        name = asset["name"]
        require(name in expected and name not in seen, "Unexpected or duplicate remote asset")
        path = expected[name]
        require(asset["state"] == "uploaded" and asset["size"] == path.stat().st_size
                and asset.get("digest") == f"sha256:{digest(path)}",
                f"Remote asset does not match tested artifact: {name}")
        seen.add(name)
    missing = [path for path in files if path.name not in seen]
    require(not complete or not missing, "Release is missing tested artifacts")
    return missing


def publish_release(repository, sha, version, files, notes):
    tag = f"v{version}"
    target = tag_target(repository, tag)
    require(target in (None, sha), "Existing tag points to a different commit")
    release = find_release(repository, tag)
    if release is not None:
        verify_target(release, tag, sha)
        if not release["draft"]:
            require(target == sha, "Published release tag is missing or incorrect")
            verify_assets(repository, release, files, complete=True)
            return release["html_url"]  # Never edit an already published release.
    else:
        gh("release", "create", tag, "--repo", repository, "--draft", "--target", sha,
           "--title", tag, "--notes-file", str(notes), *(str(path) for path in files))
        release = find_release(repository, tag)
        verify_target(release, tag, sha)
        require(release["draft"], "Expected a draft release")

    missing = verify_assets(repository, release, files, complete=False)
    if missing:
        # No --clobber: only fill absent assets on a draft for this exact SHA.
        gh("release", "upload", tag, "--repo", repository, *(str(path) for path in missing))
    verify_assets(repository, release, files, complete=True)

    # GitHub may not create a new tag until a draft is published. Establish and
    # verify it now so a wrong/moving target can never become the public release.
    if tag_target(repository, tag) is None:
        api(repository, "git/refs", method="POST", data={"ref": f"refs/tags/{tag}", "sha": sha})
    require(tag_target(repository, tag) == sha, "Release tag target changed")
    release = api(repository, f"releases/{release['id']}")
    verify_target(release, tag, sha)
    published_here = release["draft"]
    if published_here:
        api(repository, f"releases/{release['id']}", method="PATCH", data={
            "draft": False, "prerelease": False, "make_latest": "true",
            "name": tag, "body": notes.read_text(encoding="utf-8"),
        })
    release = api(repository, f"releases/{release['id']}")
    verify_target(release, tag, sha)
    require(not release["draft"] and tag_target(repository, tag) == sha,
            "Published release verification failed")
    verify_assets(repository, release, files, complete=True)
    if published_here:
        require(api(repository, "releases/latest")["id"] == release["id"],
                "Published release was not marked Latest")
    return release["html_url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-trigger", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    manifest = json.loads((ROOT / COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    triggered = is_release_push(os.environ, event, version)
    if args.check_trigger:
        output = f"publish={'true' if triggered else 'false'}\n"
        if output_path := os.environ.get("GITHUB_OUTPUT"):
            with open(output_path, "a", encoding="utf-8") as stream:
                stream.write(output)
        print(output, end="")
        return
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "Expected a stable release version")
    notes = ROOT / "docs" / "releases" / f"v{version}.md"
    require(notes.is_file() and notes.read_text(encoding="utf-8").strip(), "Release notes are missing")
    files = validate_artifacts(args.artifacts, manifest)
    if args.verify_only:
        print(f"Verified v{version}: " + ", ".join(path.name for path in files))
        return
    require(triggered, f"Publishing requires an exact Release v{version} main push")
    sha = os.environ["GITHUB_SHA"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout.strip()
    require(head == sha, "Checkout is not the release event commit")
    print(publish_release(os.environ["GITHUB_REPOSITORY"], sha, version, files, notes))


if __name__ == "__main__":
    main()

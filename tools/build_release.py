"""Build two reproducible ZIPs from explicit source-file allowlists."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "custom_components/yutai_ble"
COMPONENT_FILES = (
    "__init__.py", "binary_sensor.py", "bridge.py", "config_flow.py", "const.py",
    "coordinator.py", "entity.py", "manifest.json", "models.py", "number.py",
    "select.py", "sensor.py", "strings.json", "switch.py", "time.py", "request_queue.py",
    "translations/en.json", "translations/zh-Hans.json",
    "protocol/bridge_worker.py", "protocol/device_identity.py", "protocol/fsov7_codec.py", "protocol/read_probe.py",
    "protocol/relay_control.py", "protocol/settings_codec.py", "protocol/settings_control.py",
    "protocol/settings_scan.py", "protocol/vendor/channel.py",
    "protocol/vendor/miot_ble_specv2.py", "protocol/vendor/LICENSE",
)
AUTH_FILES = (
    "README.md", "simple_qr.py", "cloud_auth.py", "copy_key.py", "requirements.txt",
    "setup.cmd", "run.cmd", "start_qr.cmd", "copy_key.cmd", "vendor/cloud.py",
    "vendor/LICENSE", "vendor/LICENSE.tokens-extractor",
)


def archive(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for source, destination in sorted(entries):
            source_path = ROOT / source
            if source_path.is_symlink() or not source_path.resolve().is_relative_to(ROOT):
                raise ValueError("Source escapes release tree")
            data = source_path.read_bytes()
            if source_path.suffix == ".cmd":
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            item = zipfile.ZipInfo(destination, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            output.writestr(item, data)


def build(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]
    integration = destination / f"yutai-ble-v{version}.zip"
    helper = destination / f"yutai-ble-qr-windows-v{version}.zip"
    docs = [(name, name) for name in ("README.md", "docs/INSTALL.md", "docs/MODELS.md", "LICENSE", "THIRD_PARTY_NOTICES.md")]
    archive(integration, [(f"{COMPONENT}/{name}", f"{COMPONENT}/{name}") for name in COMPONENT_FILES] + docs)
    archive(helper, [(f"tools/auth/{name}", name) for name in AUTH_FILES]
            + [("LICENSE", "LICENSE"), ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")])
    hashes = destination / "SHA256SUMS.txt"
    hashes.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                              for path in (integration, helper)), encoding="ascii")
    return integration, helper, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    for path in build(args.output):
        print(path.name)


if __name__ == "__main__":
    main()

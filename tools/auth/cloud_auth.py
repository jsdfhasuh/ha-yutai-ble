"""Loopback-only Xiaomi QR login and explicit single-device credential selection."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib.util
import json
import logging
import os
import re
import secrets
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web


DPAPI_ENTROPY = b"OpenWrt-MiHome-TargetBLE-v1"
MAX_QR_BYTES = 2 * 1024 * 1024
QR_WAIT_SECONDS = 300
SELECTION_WAIT_SECONDS = 300
WORKFLOW_TIMEOUT_SECONDS = 900
PRIVATE_ROOT = Path(__file__).resolve().parent / "private"
SOURCE_PATH = Path(__file__).resolve().parent / "vendor" / "cloud.py"
PHASES = frozenset({
    "starting", "requesting_qr", "waiting_for_scan", "authenticated",
    "listing_own_homes", "locating_target", "waiting_for_selection", "querying_target_key",
    "protecting_target_key", "complete", "failed", "timeout", "cancelled",
    "server_stopped",
})


class AuthDiagnosticError(RuntimeError):
    pass


class URLValidationError(AuthDiagnosticError):
    pass


class CloudResponseError(AuthDiagnosticError):
    pass


class TargetNotFoundError(AuthDiagnosticError):
    pass


class TargetAmbiguousError(AuthDiagnosticError):
    pass


class TargetMismatchError(AuthDiagnosticError):
    pass


class TargetKeyFormatError(AuthDiagnosticError):
    pass


class SelectionInputError(AuthDiagnosticError):
    pass


class SelectionStateError(AuthDiagnosticError):
    pass


def validated_https_host(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise URLValidationError()
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise URLValidationError()
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise URLValidationError() from None
    if (
        parsed.scheme != "https" or not host.isascii()
        or parsed.username is not None or parsed.password is not None
        or port not in (None, 443) or parsed.fragment
        or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
    ):
        raise URLValidationError()
    return host


def validate_qr_url(url: str) -> str:
    if not validated_https_host(url).endswith(".xiaomi.com"):
        raise URLValidationError()
    return url


def validate_cloud_url(url: str) -> None:
    host = validated_https_host(url)
    if not (host.endswith(".xiaomi.com") or host.endswith(".io.mi.com")):
        raise URLValidationError()


def local_request_allowed(host: str, origin: str | None, fetch_site: str | None, port: int) -> bool:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{value}" for value in hosts}
    return (
        port > 0 and host.lower() in hosts
        and (origin is None or origin in origins)
        and fetch_site not in {"cross-site", "same-site"}
    )


def normalize_address(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):
        raise TargetMismatchError()
    return value.upper()


def normalize_model(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value):
        raise TargetMismatchError()
    return value


def normalize_devices(devices: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_did = {}
    by_address = {}
    for device in devices:
        if not isinstance(device, dict):
            raise TargetMismatchError()
        identity = {
            "address": normalize_address(device.get("mac", device.get("address"))),
            "model": normalize_model(device.get("model")), "did": normalize_did(device.get("did")),
        }
        previous = by_did.get(identity["did"])
        if previous is not None:
            if any(previous[key] != value for key, value in identity.items()):
                raise TargetAmbiguousError()
            continue
        if identity["address"] in by_address:
            raise TargetAmbiguousError()
        name = device.get("name")
        identity["name"] = re.sub(r"[\x00-\x1f\x7f]", " ", name).strip()[:128] if isinstance(name, str) else identity["model"]
        identity["name"] = identity["name"] or identity["model"]
        by_did[identity["did"]] = identity
        by_address[identity["address"]] = identity["did"]
    return list(by_did.values())


def select_target(devices: list[dict[str, Any]], address: str, model: str) -> dict[str, str]:
    address, model = normalize_address(address), normalize_model(model)
    matches = [device for device in normalize_devices(devices) if device["address"] == address]
    if not matches:
        raise TargetNotFoundError()
    if len(matches) != 1:
        raise TargetAmbiguousError()
    device = matches[0]
    if device["model"] != model:
        raise TargetMismatchError()
    return device


def normalize_did(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TargetMismatchError()
    did = str(value)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", did):
        raise TargetMismatchError()
    return did


def require_result(response: Any) -> dict[str, Any]:
    if (
        not isinstance(response, dict) or type(response.get("code")) is not int
        or response["code"] != 0
    ):
        raise CloudResponseError()
    result = response.get("result")
    if not isinstance(result, dict):
        raise CloudResponseError()
    return result


def validate_target_key(result: dict[str, Any], target: dict[str, str]) -> str:
    if normalize_did(result.get("did")) != target["did"]:
        raise TargetMismatchError()
    for field_name in ("mac", "address"):
        if field_name in result and normalize_address(result[field_name]) != target["address"]:
            raise TargetMismatchError()
    if "model" in result and normalize_model(result["model"]) != target["model"]:
        raise TargetMismatchError()
    key = result.get("gatt_ltmk")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise TargetKeyFormatError()
    return key.lower()


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    if os.name != "nt" or not data:
        raise AuthDiagnosticError()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_wchar_p if protect else ctypes.c_void_p,
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(DataBlob),
    ]
    function.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = DataBlob(len(DPAPI_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    try:
        # UI_FORBIDDEN without LOCAL_MACHINE binds protection to this Windows user.
        description = "Single BLE device authentication" if protect else None
        if not function(
            ctypes.byref(source), description, ctypes.byref(entropy),
            None, None, 0x1, ctypes.byref(output),
        ):
            raise AuthDiagnosticError()
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        ctypes.memset(source_buffer, 0, len(data))
        ctypes.memset(entropy_buffer, 0, len(DPAPI_ENTROPY))


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi_transform(data, True)


def dpapi_unprotect(data: bytes) -> bytes:
    """Return decrypted bytes in memory; callers must never print or persist them."""
    return _dpapi_transform(data, False)


def save_target(target: dict[str, str], key: str) -> str:
    payload = {"address": normalize_address(target.get("address")),
        "model": normalize_model(target.get("model")), "did": normalize_did(target.get("did")),
        "gatt_ltmk": key}
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise TargetKeyFormatError()
    protected = dpapi_protect(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    private_root = PRIVATE_ROOT / payload["address"].replace(":", "").lower()
    private_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = private_root / f"{stamp}-{secrets.token_hex(8)}"
    run_directory.mkdir(exist_ok=False)
    saved_file = run_directory / "target.dpapi"
    with saved_file.open("xb") as handle:
        handle.write(protected)
        handle.flush()
        os.fsync(handle.fileno())
    return str(saved_file.resolve())


def load_cloud_class():
    name = "single_target_xiaomi_cloud"
    spec = importlib.util.spec_from_file_location(name, SOURCE_PATH)
    if spec is None or spec.loader is None:
        raise AuthDiagnosticError()
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.XiaomiCloud


@dataclass
class State:
    phase: str = "starting"
    stage: str = "starting"
    error_class: str | None = None
    qr_bytes: bytes = b""
    qr_mime: str = "image/png"
    key_present: bool = False
    key_length: int = 0
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    selection_ready: asyncio.Event = field(default_factory=asyncio.Event)
    devices: list[dict[str, str]] = field(default_factory=list)
    selected_device: dict[str, str] | None = None
    saved_file: str | None = None
    skipped_missing_address_count: int = 0
    targets_by_id: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    selected_target: dict[str, str] | None = field(default=None, repr=False)

    def public_status(self) -> dict[str, Any]:
        return {
            "phase": self.phase, "stage": self.stage, "error_class": self.error_class,
            "qr_ready": bool(self.qr_bytes),
            "key_present": self.key_present, "key_length": self.key_length,
            "devices": self.devices, "selected_device": self.selected_device,
            "saved_file": self.saved_file,
            "skipped_missing_address_count": self.skipped_missing_address_count,
        }

    def publish_devices(self, devices: list[dict[str, str]]) -> None:
        for target in devices:
            token = secrets.token_urlsafe(24)
            if token in self.targets_by_id:
                raise AuthDiagnosticError()
            self.targets_by_id[token] = dict(target)
            self.devices.append({"id": token, **{field: target[field] for field in ("name", "model", "address")}})

    def choose(self, data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"device_id"} or not isinstance(data["device_id"], str):
            raise SelectionInputError()
        if self.phase != "waiting_for_selection" or self.selected_target is not None or self.selection_ready.is_set():
            raise SelectionStateError()
        target = self.targets_by_id.get(data["device_id"])
        if target is None:
            raise SelectionInputError()
        self.selected_target = dict(target)
        self.selected_device = {field: target[field] for field in ("name", "model", "address")}
        self.selection_ready.set()

    def set_phase(self, phase: str, error: BaseException | None = None) -> None:
        if phase not in PHASES:
            raise AuthDiagnosticError()
        if phase not in {"failed", "timeout", "cancelled", "server_stopped"}:
            self.stage = phase
        self.phase = phase
        if error is not None:
            self.error_class = re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:80]
        diagnostic = self.public_status()
        diagnostic.pop("devices")
        print(json.dumps(diagnostic, separators=(",", ":")), flush=True)


async def fetch_qr_image(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
    validate_qr_url(url)
    async with session.get(url, allow_redirects=False) as response:
        if response.status != 200 or response.content_type not in {
            "image/png", "image/jpeg", "image/webp", "image/gif",
        }:
            raise CloudResponseError()
        content = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            content.extend(chunk)
            if len(content) > MAX_QR_BYTES:
                raise CloudResponseError()
        if not content:
            raise CloudResponseError()
        return bytes(content), response.content_type


async def lookup_devices(cloud, state: State) -> list[dict[str, str]]:
    state.set_phase("listing_own_homes")
    uid = str(cloud._user_id)
    if not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
        raise CloudResponseError()
    result = require_result(await cloud._call("cn", "/v2/homeroom/gethome", {
        "fg": True, "fetch_share": False, "fetch_share_dev": False,
        "limit": 300, "app_ver": 7,
    }))
    homes = result.get("homelist")
    if not isinstance(homes, list) or len(homes) >= 300 or result.get("has_more"):
        raise CloudResponseError()
    state.set_phase("locating_target")
    candidates = []
    seen_homes = set()
    for home in homes:
        if not isinstance(home, dict):
            raise CloudResponseError()
        if any(str(home[field]) != uid for field in ("uid", "home_owner", "owner") if field in home):
            continue
        home_id = str(home.get("id", ""))
        if not home_id.isascii() or not home_id.isdigit() or int(home_id) <= 0:
            raise CloudResponseError()
        if home_id in seen_homes:
            continue
        seen_homes.add(home_id)
        devices_result = require_result(await cloud._call("cn", "/v2/home/home_device_list", {
            "home_owner": int(uid), "home_id": int(home_id), "limit": 200,
            "get_split_device": True, "support_smart_home": True,
        }))
        devices = devices_result.get("device_info")
        if not isinstance(devices, list) or len(devices) >= 200 or devices_result.get("has_more"):
            raise CloudResponseError()
        for device in devices:
            if not isinstance(device, dict):
                raise CloudResponseError()
            if device.get("mac") is None or device.get("mac") == "":
                state.skipped_missing_address_count += 1
                continue
            candidates.append({field: device.get(field) for field in ("mac", "model", "did", "name")})
    devices = normalize_devices(candidates)
    if not devices:
        raise TargetNotFoundError()
    return devices


async def cloud_workflow(session: aiohttp.ClientSession, state: State,
                         target_filter: tuple[str, str] | None = None) -> None:
    cloud = None
    key = None
    try:
        cloud = load_cloud_class()(session)
        state.set_phase("requesting_qr")
        await cloud.start_qr_login()
        validate_cloud_url(cloud._lp_url)
        state.qr_bytes, state.qr_mime = await fetch_qr_image(session, cloud.qr_image_url)
        state.set_phase("waiting_for_scan")
        await asyncio.wait_for(cloud.wait_for_scan(), timeout=QR_WAIT_SECONDS)
        state.qr_bytes = b""
        state.set_phase("authenticated")
        devices = await lookup_devices(cloud, state)
        if target_filter is None:
            state.publish_devices(devices)
            state.set_phase("waiting_for_selection")
            await asyncio.wait_for(state.selection_ready.wait(), timeout=SELECTION_WAIT_SECONDS)
            target = state.selected_target
            if target is None:
                raise SelectionStateError()
        else:
            target = select_target(devices, *target_filter)
            state.selected_device = {field: target[field] for field in ("name", "model", "address")}
        state.set_phase("querying_target_key")
        result = require_result(await cloud._call(
            "cn", "/v2/blemesh/query_dev", {"did": target["did"]}
        ))
        key = validate_target_key(result, target)
        state.set_phase("protecting_target_key")
        state.saved_file = save_target(target, key)
        state.key_present = True
        state.key_length = 32
        state.set_phase("complete")
    finally:
        state.qr_bytes = b""
        state.targets_by_id.clear()
        state.selected_target = None
        key = None
        if cloud is not None:
            for field_name in ("qr_image_url", "login_url", "_lp_url", "_user_id", "_ssecurity", "_service_token"):
                setattr(cloud, field_name, None)
        session.cookie_jar.clear()


def request_trace() -> aiohttp.TraceConfig:
    trace = aiohttp.TraceConfig()

    async def on_start(_session, _context, params):
        validate_cloud_url(str(params.url))

    async def on_redirect(_session, _context, params):
        location = params.response.headers.get("Location")
        if location is not None:
            validate_cloud_url(urljoin(str(params.url), location))

    trace.on_request_start.append(on_start)
    trace.on_request_redirect.append(on_redirect)
    return trace


def selection_json(text: str) -> Any:
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SelectionInputError()
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=unique_fields)


def build_app(state: State, nonce: str, prefix: str, port_getter, html: str) -> web.Application:
    @web.middleware
    async def guard(request, handler):
        if request.query_string or not local_request_allowed(
            request.headers.get("Host", ""), request.headers.get("Origin"),
            request.headers.get("Sec-Fetch-Site"), port_getter(),
        ):
            return web.Response(status=403, text="Forbidden")
        response = await handler(request)
        response.headers.update({
            "Cache-Control": "no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self'; connect-src 'self'; "
                f"script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; frame-ancestors 'none'"
            ),
        })
        return response

    async def page(_request):
        return web.Response(text=html.replace("__NONCE__", nonce).replace("__PREFIX__", prefix), content_type="text/html")

    async def status(_request):
        return web.json_response(state.public_status())

    async def qr(_request):
        if not state.qr_bytes:
            return web.Response(status=404, text="Not ready")
        return web.Response(body=state.qr_bytes, content_type=state.qr_mime)

    async def cancel(_request):
        state.cancelled.set()
        return web.json_response({"phase": "cancelling"})

    async def select(request):
        if request.content_type != "application/json":
            return web.Response(status=415, text="JSON required")
        try:
            state.choose(await request.json(loads=selection_json))
        except SelectionStateError:
            return web.Response(status=409, text="Selection unavailable")
        except (SelectionInputError, ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="Invalid selection")
        return web.json_response({"accepted": True})

    app = web.Application(middlewares=[guard], client_max_size=1024)
    app.router.add_get(prefix, page)
    app.router.add_get(prefix + "status", status)
    app.router.add_get(prefix + "qr", qr)
    app.router.add_post(prefix + "cancel", cancel)
    app.router.add_post(prefix + "select", select)
    return app


async def run(*, open_browser: bool = False, target_filter: tuple[str, str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    state = State()
    nonce = secrets.token_urlsafe(24)
    prefix = f"/session/{nonce}/"
    port = 0
    loop = asyncio.get_running_loop()
    loop.set_debug(False)
    html = (Path(__file__).resolve().parent / "qr_page.html").read_text(encoding="utf-8")
    app = build_app(state, nonce, prefix, lambda: port, html)
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: state.set_phase("failed"))
    runner = web.AppRunner(app, access_log=None)
    workflow_task = None
    cancel_task = None
    session = None
    exit_code = 1
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = int(site._server.sockets[0].getsockname()[1])
        loopback_url = f"http://127.0.0.1:{port}{prefix}"
        print(json.dumps({"loopback_url": loopback_url}), flush=True)
        if open_browser:
            try:
                opened = await asyncio.to_thread(webbrowser.open, loopback_url, new=2)
            except Exception:
                opened = False
            print(json.dumps({"browser_opened": bool(opened)}), flush=True)
        timeout = aiohttp.ClientTimeout(total=45, connect=10)
        async with aiohttp.ClientSession(
            timeout=timeout, trace_configs=[request_trace()], trust_env=False,
        ) as session:
            workflow_task = asyncio.create_task(asyncio.wait_for(
                cloud_workflow(session, state, target_filter), timeout=WORKFLOW_TIMEOUT_SECONDS
            ))
            cancel_task = asyncio.create_task(state.cancelled.wait())
            done, _pending = await asyncio.wait(
                {workflow_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and not workflow_task.done():
                workflow_task.cancel()
                await asyncio.gather(workflow_task, return_exceptions=True)
                state.set_phase("cancelled")
            else:
                try:
                    await workflow_task
                except TimeoutError as error:
                    state.set_phase("timeout", error)
                except Exception as error:
                    state.set_phase("failed", error)
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            session.cookie_jar.clear()
            exit_code = 0 if state.phase == "complete" else 2 if state.phase == "cancelled" else 1
        await asyncio.sleep(5)
    finally:
        for task in (workflow_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (workflow_task, cancel_task) if task is not None),
            return_exceptions=True,
        )
        state.qr_bytes = b""
        if session is not None:
            session.cookie_jar.clear()
        await runner.cleanup()
        state.set_phase("server_stopped")
        loop.set_exception_handler(previous_exception_handler)
    return exit_code


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-browser", action="store_true", help="Open the local QR page in the default browser")
    parser.add_argument("--address", help="Select exactly one account device by MAC; requires --model")
    parser.add_argument("--model", help="Expected device model; requires --address")
    args = parser.parse_args(argv)
    if (args.address is None) != (args.model is None):
        parser.error("--address and --model must be supplied together")
    args.target_filter = None
    if args.address is not None:
        try:
            args.target_filter = (normalize_address(args.address), normalize_model(args.model))
        except TargetMismatchError:
            parser.error("Invalid device address or model")
    return args


def main(argv=None):
    args = parse_arguments(argv)
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(run(open_browser=args.open_browser, target_filter=args.target_filter))
    except KeyboardInterrupt:
        print(json.dumps({"phase": "cancelled"}), flush=True)
        return 130
    except Exception as error:
        print(json.dumps({"phase": "failed", "stage": "server", "error_class": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

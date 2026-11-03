"""Scan in Mi Home, choose one device number, and save its encrypted BLE key."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import webbrowser

import aiohttp
from aiohttp import web
from cloud_auth import CloudResponseError, dpapi_protect, load_cloud_class, require_result

ROOT = Path(__file__).resolve().parent


class KeyFetchError(ValueError):
    """Only fixed local text and numeric cloud codes are safe to display."""

    def __init__(self, endpoint, reason, code=None):
        detail = f"接口 {endpoint}：{reason}"
        if type(code) is int:
            detail += f"（云端错误码 {code}）"
        super().__init__(detail)


def key_field(device):
    return "bindkey" if str(device["did"]).startswith("blt.") else "gatt_ltmk"


async def list_devices(cloud):
    homes = require_result(await cloud._call("cn", "/v2/homeroom/gethome", {
        "fg": True, "fetch_share": False, "fetch_share_dev": False, "limit": 300, "app_ver": 7,
    }))
    devices = {}
    for home in homes.get("homelist", []):
        if any(str(home[field]) != str(cloud._user_id) for field in ("uid", "home_owner", "owner") if field in home):
            continue
        result = require_result(await cloud._call("cn", "/v2/home/home_device_list", {
            "home_owner": int(cloud._user_id), "home_id": int(home["id"]), "limit": 200,
            "get_split_device": True, "support_smart_home": True,
        }))
        for device in result.get("device_info", []):
            if device.get("mac") and device.get("model") and device.get("did"):
                devices[str(device["did"])] = {key: device.get(key) for key in ("name", "model", "mac", "did")}
    return list(devices.values())


async def fetch_key(cloud, device):
    did = str(device["did"])
    beacon = key_field(device) == "bindkey"
    if beacon:
        endpoint = "/v2/device/blt_get_beaconkey"
        params = {"did": did, "pdid": 1}
        response_field, key_length = "beaconkey", 32
    else:
        endpoint = "/v2/blemesh/query_dev"
        params = {"did": did}
        response_field, key_length = "gatt_ltmk", 64
    try:
        response = await cloud._call("cn", endpoint, params)
    except TimeoutError:
        raise KeyFetchError(endpoint, "密钥请求超时") from None
    except aiohttp.ClientResponseError as error:
        raise KeyFetchError(endpoint, f"HTTP 请求失败，状态码 {error.status}") from None
    except aiohttp.ClientError:
        raise KeyFetchError(endpoint, "网络请求失败") from None
    try:
        result = require_result(response)
    except CloudResponseError:
        code = response.get("code") if isinstance(response, dict) else None
        raise KeyFetchError(endpoint, "云端未返回有效结果", code) from None
    # Successful beacon responses may contain did=""; Mesh must echo the DID.
    returned_did = result.get("did")
    check_did = not beacon or result.get("did", "") != ""
    if check_did and str(returned_did) != did:
        raise KeyFetchError(endpoint, f"返回的设备编号不匹配（did_type={type(returned_did).__name__}，did_empty={returned_did == ''}）")
    for field in ("mac", "address"):
        if field in result and str(result[field]).upper() != str(device["mac"]).upper():
            raise KeyFetchError(endpoint, "返回的设备地址不匹配")
    if "model" in result and result["model"] != device["model"]:
        raise KeyFetchError(endpoint, "返回的设备型号不匹配")
    key = result.get(response_field, "")
    if not isinstance(key, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{key_length}}}", key):
        raise KeyFetchError(endpoint, f"未取得有效的 {response_field}，应为 {key_length // 2} 字节")
    return key


def save_key(device, key, root=ROOT):
    address = str(device["mac"]).upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        raise ValueError("Invalid device address")
    field = key_field(device)
    key_length = 32 if field == "bindkey" else 64
    if not isinstance(key, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{key_length}}}", key):
        raise ValueError("Invalid key for device type")
    payload = {"address": address, "model": device["model"], "did": str(device["did"]), field: key}
    encrypted = dpapi_protect(json.dumps(payload).encode("utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = root / "private" / address.replace(":", "").lower() / f"{stamp}-{secrets.token_hex(4)}"
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "target.dpapi"
    with path.open("xb") as output:
        output.write(encrypted)
    return path


async def run():
    async with aiohttp.ClientSession() as session:
        cloud = load_cloud_class()(session)
        await cloud.start_qr_login()
        async with session.get(cloud.qr_image_url) as response:
            response.raise_for_status()
            qr = await response.read()
            mime = response.content_type
        status = {"message": "等待米家扫码并确认登录", "show_qr": True, "done": False}
        prefix = "/" + secrets.token_urlsafe(16) + "/"
        html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>米家扫码登录</title>
<style>body{font:16px/1.6 system-ui;margin:32px auto;padding:0 20px;max-width:460px;color:#222}
h1{font-size:24px}img{width:320px;max-width:100%;aspect-ratio:1}</style>
<h1>米家扫码登录</h1><p id="status"></p><img id="qr" src="qr.png" alt="米家登录二维码">
<script>async function poll(){try{const s=await(await fetch('status')).json();
document.getElementById('status').textContent=s.message;document.getElementById('qr').hidden=!s.show_qr;
if(!s.done)setTimeout(poll,800);}catch(_){}}poll();</script></html>"""
        async def page(_request):
            return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})
        async def qr_image(_request):
            return web.Response(body=qr, content_type=mime, headers={"Cache-Control": "no-store"})
        async def current_status(_request):
            return web.json_response(status, headers={"Cache-Control": "no-store"})
        app = web.Application()
        app.router.add_get(prefix, page)
        app.router.add_get(prefix + "qr.png", qr_image)
        app.router.add_get(prefix + "status", current_status)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        try:
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}{prefix}"
            print(f"扫码页面：{url}", flush=True)
            await asyncio.to_thread(webbrowser.open, url, new=2)
            await asyncio.wait_for(cloud.wait_for_scan(), timeout=300)
            status.update(message="登录成功，等待命令窗口选择设备", show_qr=False)
            print("\n登录成功，正在读取设备列表……", flush=True)
            devices = await list_devices(cloud)
            if not devices:
                print("没有找到带地址的设备。", flush=True)
                return 1
            for number, device in enumerate(devices, 1):
                name = str(device.get("name") or device["model"]).replace("\n", " ").replace("\r", " ")
                print(f"{number}. {name} | {device['model']} | {device['mac']}", flush=True)
            while True:
                choice = (await asyncio.to_thread(input, "\n输入设备编号，直接回车退出：")).strip()
                if not choice:
                    return 0
                if choice.isdecimal() and 1 <= int(choice) <= len(devices):
                    break
                print("请输入列表中的编号。", flush=True)
            device = devices[int(choice) - 1]
            kind = "普通 BLE 广播密钥（bindkey）" if key_field(device) == "bindkey" else "Mesh/GATT 长期密钥（gatt_ltmk）"
            print(f"正在获取：{kind}", flush=True)
            key = await fetch_key(cloud, device)
            path = save_key(device, key)
            key = None
            status.update(message="认证完成，文件已加密保存", done=True)
            print(f"\n认证成功：{device['model']} | {device['mac']}\n密钥类型：{kind}\n保存位置：{path}", flush=True)
            await asyncio.sleep(1)
            return 0
        finally:
            await runner.cleanup()


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        return 130
    except TimeoutError:
        print("\n扫码超时，请重新运行脚本。", flush=True)
        return 1
    except KeyFetchError as error:
        print(f"\n未完成：{error}。", flush=True)
        return 1
    except Exception as error:
        print(f"\n未完成：{type(error).__name__}。", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

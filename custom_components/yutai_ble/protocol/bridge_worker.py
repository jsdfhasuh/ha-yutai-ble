"""One bounded BLE request; keys enter over stdin and never enter reports."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys

import read_probe as read
import settings_codec as codec
import settings_scan as scan
from device_identity import ORDINARY_MODEL, SUPPORTED_MODEL


def model_codec(model):
    if model == SUPPORTED_MODEL:
        return codec
    if model == ORDINARY_MODEL:
        import fsov7_codec
        return fsov7_codec
    read.fail("target_not_authorized")


def validated_input(data):
    required = {"address", "model", "gatt_ltmk", "operation"}
    if not isinstance(data, dict):
        read.fail("input_fields_invalid")
    operation = data.get("operation")
    if operation == "read":
        if set(data) != required:
            read.fail("input_fields_invalid")
        setting = None
    elif operation == "setting":
        if set(data) != required | {"field", "value"}:
            read.fail("input_fields_invalid")
        setting = (data["field"], data["value"])
    elif operation == "relay":
        if set(data) != required | {"value"} or type(data["value"]) is not bool:
            read.fail("relay_bool_required")
        setting = data["value"]
    else:
        read.fail("operation_not_supported")
    target = read.validated_input({name: data[name] for name in required - {"operation"}})
    if type(setting) is tuple:
        model_codec(target.model).validate_setting(*setting)
    return target, setting


async def run_request(target, setting):
    selected_codec = model_codec(target.model)
    relay = type(setting) is bool
    if relay:
        from relay_control import PROPERTY, make_relay_client, perform_relay, snapshot_values
        factory = lambda upstream: make_relay_client(upstream, setting, codec_module=selected_codec)
        operation = perform_relay
        good_status = "relay_verified"
    elif setting is None:
        factory = lambda upstream: scan.make_settings_client(upstream, selected_codec.READ_PROPERTIES)
        operation = scan.perform_scan
        good_status = "settings_scan_succeeded"
    else:
        from settings_control import make_control_client, perform_control
        factory = lambda upstream: make_control_client(upstream, *setting, codec_module=selected_codec)
        operation = perform_control
        good_status = "setting_verified"
    result = await read.run_probe(target, client_factory=factory, operation=operation,
                                  connect_retry=setting is not None)
    relay_writes = result.get("relay_writes")
    if relay:
        relay_ok = (
            type(relay_writes) is int and relay_writes in (0, 1)
            and type(result.get("relay_write_attempts")) is int
            and result["relay_write_attempts"] == relay_writes
            and result.get("requested_value") is setting
            and type(result.get("initial_relay_state")) is bool
            and result.get("observed_relay_state") is setting
            and relay_writes == int(result["initial_relay_state"] is not setting)
            and result.get("maybe_executed") is bool(relay_writes)
            and result.get("verified") is True
            and result.get("initial_snapshot_complete") is True
            and result.get("verification_snapshot_complete") is True
            and result.get("settings_unchanged") is True
        )
        if relay_ok:
            relay_ok = snapshot_values(result.get("properties"), selected_codec.READ_PROPERTIES)[PROPERTY] is setting
    else:
        relay_ok = type(relay_writes) is int and relay_writes == 0
    if (result.get("status") == good_status
            and result.get("address") == target.address
            and result.get("model") == target.model
            and result.get("authentication_verified") is True
            and result.get("f0_verified") is True
            and relay_ok
            and result.get("connected_after_cleanup") is False
            and not result.get("cleanup_error_code")):
        settings, capabilities = selected_codec.decode_settings(result.get("properties"))
        result.update(settings=settings, capabilities=capabilities)
        if setting is None:
            result["status"] = "read_verified"
        elif not relay and (result.get("verified") is not True
                or type(settings.get(setting[0])) is not type(setting[1])
                or settings.get(setting[0]) != setting[1]):
            read.fail("setting_decoded_readback_mismatch")
    else:
        result["status"] = "error"
    return result


async def cancellable_request(target, setting):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass
        return await run_request(target, setting)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main():
    logging.disable(logging.CRITICAL)
    key = b""
    try:
        if len(sys.argv) != 1:
            read.fail("command_line_arguments_not_allowed")
        raw_input = sys.stdin.buffer.read(4097)
        if len(raw_input) > 4096:
            read.fail("input_too_large")
        data = json.loads(raw_input, object_pairs_hook=read.no_duplicate_keys)
        target, setting = validated_input(data)
        key = target.key
        del raw_input, data
        result = asyncio.run(cancellable_request(target, setting))
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if key.hex() in output.lower():
            read.fail("credential_in_result")
    except (Exception, asyncio.CancelledError) as error:
        result = {"status": "error", "error_code": (
            error.args[0] if isinstance(error, read.ProbeError) else "worker_failed"),
            "error_class": type(error).__name__}
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    finally:
        key = b""
    print(output)
    return 0 if result.get("status") in {"read_verified", "setting_verified", "relay_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""NDJSON worker owning one bounded, target-bound authenticated BLE session."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import sys
import time

import read_probe as read
from device_identity import SUPPORTED_MODEL
from session_client import make_session_client

LINE_LIMIT = 4096
OPERATION_SECONDS = 25
RECONNECT_COOLDOWN = 3.0


def validated_init(data):
    fields = {"id", "operation", "address", "model", "gatt_ltmk", "idle_timeout"}
    if (type(data) is not dict or set(data) != fields or data.get("operation") != "init"
            or type(data.get("id")) is not int or data["id"] != 0):
        read.fail("session_init_invalid")
    idle = data["idle_timeout"]
    if type(idle) is not int or not 1 <= idle <= 120:
        read.fail("session_idle_timeout_invalid")
    target = read.validated_input({name: data[name] for name in ("address", "model", "gatt_ltmk")})
    if target.model != SUPPORTED_MODEL:
        read.fail("session_model_not_supported")
    return target, idle


def validated_request(data, previous_id):
    if (type(data) is not dict or type(data.get("id")) is not int
            or not previous_id < data["id"] <= 0x7FFFFFFF):
        read.fail("session_request_id_invalid")
    operation = data.get("operation")
    fields = {"id", "operation", "value"} if operation == "relay" else {"id", "operation"}
    if operation not in {"read", "relay", "close"} or set(data) != fields:
        read.fail("session_request_invalid")
    if operation == "relay" and type(data["value"]) is not bool:
        read.fail("relay_bool_required")
    return data["id"], operation, data.get("value")


def safe_error(error):
    code = "phase_timeout" if isinstance(error, TimeoutError) else "probe_failed"
    if isinstance(error, read.ProbeError) and error.args:
        candidate = error.args[0]
        if type(candidate) is str and re.fullmatch(r"[a-z_]{1,80}", candidate):
            code = candidate
    return {"error_code": code, "error_class": type(error).__name__}


class SessionManager:
    def __init__(self, target, idle_timeout):
        self.target, self.idle_timeout = target, idle_timeout
        self.client = self.bus = None
        self.result = {}
        self.next_connect_at = 0.0

    def identity(self):
        return {"address": self.target.address, "model": self.target.model,
                "transport": "retained_session"}

    def idle_remaining(self):
        return self.client.remaining_idle() if self.client is not None else None

    async def _open(self):
        def factory(upstream):
            cls = make_session_client(upstream)
            return lambda device, key, raw, result: cls(
                device, key, raw, result, idle_timeout=self.idle_timeout)
        await read.open_connection(self.target, self.result, self, client_factory=factory,
                                   connect_retry=self.result.get("connect_retry_managed", False))

    async def close(self):
        result = {**self.identity(), "status": "close_session_verified",
                  "operation": "close", "connected_after_cleanup": False}
        try:
            if self.client is not None:
                cleanup_ok = await read.close_client(self.client, self.result)
                for name in ("connected_after_cleanup", "cleanup_error_code", "cleanup_error_class"):
                    if name in self.result:
                        result[name] = self.result[name]
                if cleanup_ok:
                    self.client = None
                else:
                    result["status"] = "error"
                self.next_connect_at = time.monotonic() + RECONNECT_COOLDOWN
        finally:
            if self.bus is not None:
                try:
                    self.bus.disconnect()
                finally:
                    self.bus = None
        result.update(read.connection_telemetry(self.result))
        return result

    async def execute(self, operation, value=None):
        started = time.monotonic()
        read.reset_connection_telemetry(self.result, connect_retry=operation == "relay")
        self.result.pop("connect_auth_elapsed_seconds", None)
        self.result.update(relay_writes=0, relay_write_attempts=0, maybe_executed=False,
                           phase="session_preflight")
        reused = False
        connect_seconds = 0.0
        try:
            budget = 3 if operation == "relay" else 4
            if self.client is not None and not self.client.reusable(budget):
                cleanup = await self.close()
                if cleanup["status"] == "error":
                    read.fail("session_previous_cleanup_failed")
            if self.client is None:
                self.result.update(self.identity(), connection_attempted=False,
                                   disconnect_attempted=False, phase="dependencies")
                while self.next_connect_at > time.monotonic():
                    await asyncio.sleep(self.next_connect_at - time.monotonic())
                connect_started = time.monotonic()
                await asyncio.wait_for(self._open(), read.WORK_SECONDS)
                connect_seconds = self.result.get("connect_auth_elapsed_seconds",
                                                  round(time.monotonic() - connect_started, 6))
            else:
                reused = True
            report = await asyncio.wait_for(self.client.operate(operation, value), OPERATION_SECONDS)
            report.update(self.identity(), transaction_complete=True, session_healthy=True,
                          session_connected=True, authentication_verified=True, f0_verified=True,
                          connection_reused=reused, connect_auth_elapsed_seconds=connect_seconds,
                          elapsed_seconds=round(time.monotonic() - started, 6))
            report.update(read.connection_telemetry(self.result))
            return report
        except (Exception, asyncio.CancelledError) as error:
            report = {**self.identity(), "status": "error", "operation": operation,
                      "phase": self.result.get("phase", "session_operation"),
                      "transaction_complete": False, "session_healthy": False,
                      "relay_writes": self.result.get("relay_writes", 0),
                      "relay_write_attempts": self.result.get("relay_write_attempts", 0),
                      "maybe_executed": self.result.get("maybe_executed", False), **safe_error(error)}
            cleanup = await self.close()
            report.update(connection_attempted=self.result.get("connection_attempted", False),
                          disconnect_attempted=self.result.get("disconnect_attempted", False))
            report.update(read.connection_telemetry(self.result))
            for name in ("connected_after_cleanup", "cleanup_error_code", "cleanup_error_class"):
                if name in cleanup:
                    report[name] = cleanup[name]
            if isinstance(error, asyncio.CancelledError):
                raise
            return report


def decode_line(line):
    if len(line) > LINE_LIMIT:
        read.fail("input_too_large")
    return json.loads(line, object_pairs_hook=read.no_duplicate_keys)


def encoded_reply(reply, key):
    output = json.dumps(reply, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if key and key.hex() in output.lower():
        read.fail("credential_in_result")
    return output


async def serve(reader, emit, *, manager_factory=SessionManager):
    manager = None
    request_id = None
    key = b""
    try:
        line = await reader.readline()
        if not line:
            return
        target, idle_timeout = validated_init(decode_line(line))
        key = target.key
        del line
        manager = manager_factory(target, idle_timeout)
        emit(encoded_reply({"id": 0, "status": "ready", "address": target.address,
                            "model": target.model}, key))
        previous_id = 0
        while True:
            request_id = None
            timeout = manager.idle_remaining()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout) if timeout is not None else await reader.readline()
            except TimeoutError:
                cleanup = await manager.close()
                if cleanup["status"] == "error":
                    emit(encoded_reply({"id": None, "report": cleanup}, key))
                    return
                continue
            if not line:
                return
            data = decode_line(line)
            if type(data) is dict and type(data.get("id")) is int:
                request_id = data["id"]
            request_id, operation, value = validated_request(data, previous_id)
            previous_id = request_id
            del line, data
            report = await manager.close() if operation == "close" else await manager.execute(operation, value)
            emit(encoded_reply({"id": request_id, "report": report}, key))
            if operation == "close":
                return
    except Exception as error:
        report = {"status": "error", "phase": "session_worker", **safe_error(error)}
        if manager is not None:
            cleanup = await manager.close()
            report.update(manager.identity())
            for name in ("connected_after_cleanup", "cleanup_error_code", "cleanup_error_class"):
                if name in cleanup:
                    report[name] = cleanup[name]
        emit(encoded_reply({"id": request_id, "report": report}, key))
    finally:
        if manager is not None:
            await manager.close()
        key = b""


async def run_stdio():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    transport = None
    loop.set_exception_handler(lambda _loop, _context: None)
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass
        reader = asyncio.StreamReader(limit=LINE_LIMIT)
        transport, _protocol = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        await serve(reader, lambda output: print(output, flush=True))
    except asyncio.CancelledError:
        pass
    finally:
        if transport is not None:
            transport.close()
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main():
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 1:
            read.fail("command_line_arguments_not_allowed")
        asyncio.run(run_stdio())
    except Exception as error:
        print(json.dumps({"id": None, "report": {"status": "error", **safe_error(error)}}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

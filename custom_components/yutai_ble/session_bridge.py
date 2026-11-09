"""Bounded, privacy-safe transport for an authenticated session subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .models import DeviceConfig

_MAX_REPORT_BYTES = 1024 * 1024


class SessionBridgeError(Exception):
    """Transport failure without worker output or credentials."""

    def __init__(self, code: str, *, may_have_written: bool = False):
        self.code = code
        self.may_have_written = may_have_written
        super().__init__(code)


async def _finish_cleanup(task: asyncio.Future):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class SessionBridge:
    """Keep one worker per exact device/key, never replaying a dispatched request."""

    def __init__(self, worker_path: Path, *, timeout: float, idle_timeout: int):
        self.worker_path = worker_path
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self._process = None
        self._binding = None
        self._next_id = 0
        self._write_dispatched = False
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._process is not None

    def matches(self, config: DeviceConfig) -> bool:
        return self._binding is None or self._binding == (
            config.address, config.model, config.gatt_ltmk,
        )

    async def request(self, config: DeviceConfig, command: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._write_dispatched = False
            try:
                if not self.matches(config):
                    raise SessionBridgeError("invalid_worker_report")
                return await asyncio.wait_for(
                    self._request_locked(config, command), timeout=self.timeout,
                )
            except asyncio.CancelledError:
                await self._abort_locked()
                raise
            except BaseException as error:
                may_have_written = self._write_dispatched
                await self._abort_locked()
                if isinstance(error, SessionBridgeError):
                    raise SessionBridgeError(error.code, may_have_written=may_have_written) from None
                if isinstance(error, TimeoutError):
                    raise SessionBridgeError("worker_timeout", may_have_written=may_have_written) from None
                if isinstance(error, (OSError, ValueError, RuntimeError)):
                    raise SessionBridgeError("worker_unavailable", may_have_written=may_have_written) from None
                raise

    async def _request_locked(self, config: DeviceConfig, command: dict[str, Any]) -> dict[str, Any]:
        if self._process is None:
            await self._start_locked(config)
        elif self._process.returncode is not None:
            # An exited process may have consumed the last command. Never replay it.
            raise SessionBridgeError("worker_failed")
        self._next_id += 1
        envelope = await self._exchange_locked(
            {"id": self._next_id, **command}, writing=command["operation"] == "relay",
        )
        if set(envelope) != {"id", "report"} or not isinstance(envelope["report"], dict):
            raise SessionBridgeError("invalid_worker_report")
        report = envelope["report"]
        if report.get("address") != config.address or report.get("model") != config.model:
            raise SessionBridgeError("invalid_worker_report")
        if report.get("status") == "error":
            await self._abort_locked()
            return report
        self._validate_success(report, command)
        return report

    async def _start_locked(self, config: DeviceConfig) -> None:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-B", str(self.worker_path),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=_MAX_REPORT_BYTES,
        ))
        try:
            self._process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            try:
                self._process = await _finish_cleanup(spawn)
            except asyncio.CancelledError:
                self._process = spawn.result()
            raise
        self._binding = (config.address, config.model, config.gatt_ltmk)
        self._next_id = 0
        ready = await self._exchange_locked({
            "id": 0, "operation": "init", **config.worker_payload(),
            "idle_timeout": self.idle_timeout,
        })
        if (ready.get("status") != "ready" or ready.get("address") != config.address
                or ready.get("model") != config.model
                or set(ready) != {"id", "status", "address", "model"}):
            raise SessionBridgeError("invalid_worker_report")

    async def _exchange_locked(self, payload: dict[str, Any], *, writing: bool = False) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise SessionBridgeError("worker_unavailable")
        if writing:
            self._write_dispatched = True
        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        await process.stdin.drain()
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise SessionBridgeError("invalid_worker_report") from None
        if not line or not line.endswith(b"\n") or len(line) > _MAX_REPORT_BYTES:
            raise SessionBridgeError("invalid_worker_report")
        try:
            response = json.loads(line, object_pairs_hook=self._unique_object)
        except (ValueError, UnicodeError):
            raise SessionBridgeError("invalid_worker_report") from None
        if (not isinstance(response, dict) or type(response.get("id")) is not int
                or response["id"] != payload["id"]):
            raise SessionBridgeError("invalid_worker_report")
        return response

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    @staticmethod
    def _validate_success(report: dict[str, Any], command: dict[str, Any]) -> None:
        if (report.get("transport") != "retained_session"
                or "connected_after_cleanup" in report
                or any(report.get(field) is not True for field in (
                    "transaction_complete", "session_healthy", "session_connected",
                    "authentication_verified", "f0_verified",
                )) or type(report.get("connection_reused")) is not bool):
            raise SessionBridgeError("invalid_worker_report")
        if command["operation"] == "read":
            valid = (report.get("status") == "read_session_verified"
                     and report.get("operation") == "read" and report.get("verification_scope") == "full"
                     and type(report.get("relay_writes")) is int and report["relay_writes"] == 0)
        else:
            initial = report.get("initial_relay_state")
            expected_writes = int(initial is not command["value"])
            valid = (report.get("status") == "relay_session_verified"
                     and report.get("operation") == "relay_control"
                     and report.get("verification_scope") == "relay_only"
                     and report.get("verified") is True and report.get("initial_relay_verified") is True
                     and type(initial) is bool
                     and report.get("requested_value") is command["value"]
                     and report.get("observed_relay_state") is command["value"]
                     and type(report.get("relay_writes")) is int
                     and report["relay_writes"] == expected_writes
                     and type(report.get("relay_write_attempts")) is int
                     and report["relay_write_attempts"] == expected_writes
                     and report.get("maybe_executed") is bool(expected_writes))
        if not valid:
            raise SessionBridgeError("invalid_worker_report")

    async def async_close(self) -> bool:
        """Gracefully disconnect, then reap even on cancellation or invalid output."""
        async with self._lock:
            if self._process is None:
                return False
            try:
                if self._process.returncode is None:
                    await asyncio.wait_for(self._close_locked(), timeout=self.timeout)
            except asyncio.CancelledError:
                raise
            except (SessionBridgeError, TimeoutError, OSError, ValueError, RuntimeError):
                raise SessionBridgeError("cleanup_failed") from None
            finally:
                await self._abort_locked()
            return True

    async def _close_locked(self) -> None:
        self._next_id += 1
        envelope = await self._exchange_locked({"id": self._next_id, "operation": "close"})
        report = envelope.get("report")
        if (set(envelope) != {"id", "report"} or not isinstance(report, dict)
                or report.get("status") != "close_session_verified"
                or report.get("connected_after_cleanup") is not False
                or report.get("address") != self._binding[0]
                or report.get("model") != self._binding[1]):
            raise SessionBridgeError("cleanup_failed")
        await self._process.wait()

    async def _abort_locked(self) -> None:
        process = self._process
        try:
            if process is not None:
                await _finish_cleanup(asyncio.create_task(self._stop_process(process)))
        finally:
            self._process = None
            self._binding = None

    @staticmethod
    async def _stop_process(process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        else:
            await process.wait()

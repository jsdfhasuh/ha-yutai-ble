"""Run the vendor protocol in an isolated, serialized subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .models import DeviceConfig
from .request_queue import RequestInterrupted, RequestQueue

_LOCK = RequestQueue()
_NEXT_REQUEST_AT = 0.0
_MAX_REPORT_BYTES = 1024 * 1024
_CONNECT_RETRY_WINDOW = 30
_ERROR_CODES = frozenset({
    "worker_failed", "worker_timeout", "worker_unavailable", "invalid_worker_report",
    "ble_connect_failed", "ble_connect_timeout", "authentication_failed",
    "cleanup_failed", "protocol_failed", "validation_failed", "request_cancelled",
    "entry_closing", "write_result_uncertain",
})
_CONNECT_ERRORS = frozenset({"BleakError", "BleakDBusError", "BleakDeviceNotFoundError", "TimeoutError"})
_AUTH_PHASES = frozenset({
    "mtu_acquisition", "authentication_subscribe", "authentication_mcu",
    "authentication_a4", "authentication_pubkey", "authentication_proof", "protocol_exchange",
})


async def async_finish_cleanup(task: asyncio.Future):
    """Drain cleanup despite repeated caller cancellation, then propagate it."""
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


class BridgeError(Exception):
    """Privacy-safe worker failure; never includes subprocess output."""

    def __init__(self, code: str, *, retryable: bool = False, may_have_written: bool = False):
        self.code = code if type(code) is str and code in _ERROR_CODES else "worker_failed"
        self.retryable = retryable
        self.may_have_written = may_have_written
        super().__init__(self.code)


class ReadDeferred(Exception):
    """A not-yet-started read yields to a manual request, without a failure."""


async def _read_pause(delay: float, interrupt: asyncio.Event | None) -> None:
    if interrupt is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(interrupt.wait(), timeout=delay)
    except TimeoutError:
        return
    raise ReadDeferred


@asynccontextmanager
async def _request_slot(writing: bool, interrupt: asyncio.Event | None):
    try:
        while True:
            async with _LOCK.hold(manual=writing, interrupt=interrupt if not writing else None):
                if not writing and interrupt is not None and interrupt.is_set():
                    raise ReadDeferred
                while (delay := _NEXT_REQUEST_AT - time.monotonic()) > 0:
                    await _read_pause(delay, interrupt if not writing else None)
                # A manual request on another entry may have arrived during cooldown.
                if not writing and _LOCK.manual_waiting:
                    continue
                yield
                return
    except RequestInterrupted:
        raise ReadDeferred from None


def _worker_error(report: dict[str, Any], config: DeviceConfig, *, setting: bool, relay: bool = False) -> BridgeError:
    writing = setting or relay
    if report.get("address") != config.address or report.get("model") != config.model:
        return BridgeError("invalid_worker_report", may_have_written=writing)
    attempts = "relay_write_attempts" if relay else "setting_write_attempts"
    may_have_written = writing and not (
        report.get("maybe_executed") is False
        and type(report.get(attempts)) is int
        and report[attempts] == 0
        and type(report.get("relay_writes")) is int
        and report["relay_writes"] == 0
    )
    if report.get("cleanup_error_code"):
        return BridgeError("cleanup_failed", may_have_written=may_have_written)
    phase, error_class = report.get("phase"), report.get("error_class")
    if (phase == "ble_connect" and type(error_class) is str and error_class in _CONNECT_ERRORS
            and type(report.get("error_code")) is str
            and report["error_code"] in {"probe_failed", "phase_timeout"}):
        code = "ble_connect_timeout" if error_class == "TimeoutError" else "ble_connect_failed"
        return BridgeError(
            code,
            retryable=(not writing and report.get("status") == "error"
                       and report.get("connection_attempted") is True
                       and report.get("disconnect_attempted") is True
                       and type(report.get("relay_writes")) is int
                       and report["relay_writes"] == 0
                       and report.get("connected_after_cleanup") is False),
            may_have_written=may_have_written,
        )
    if type(phase) is str and phase in _AUTH_PHASES:
        return BridgeError("authentication_failed", may_have_written=may_have_written)
    if error_class == "ProbeError":
        return BridgeError("protocol_failed", may_have_written=may_have_written)
    return BridgeError("worker_failed", may_have_written=may_have_written)


class BridgeClient:
    """Serialize requests; retry only one safely disconnected, read-only connect."""

    def __init__(
        self,
        worker_path: Path | None = None,
        *,
        timeout: float = 100,
        cooldown: float = 3,
    ) -> None:
        self.worker_path = worker_path or Path(__file__).parent / "protocol" / "bridge_worker.py"
        self.timeout = timeout
        self.cooldown = cooldown
        self._closing = False
        self._inflight: set[asyncio.Task] = set()

    def _begin_request(self):
        if self._closing:
            raise BridgeError("entry_closing")
        task = asyncio.current_task()
        self._inflight.add(task)
        return task

    async def read(self, config: DeviceConfig, *, interrupt: asyncio.Event | None = None) -> dict[str, Any]:
        task = self._begin_request()
        try:
            for attempt in range(2):
                try:
                    return await self._request(config, {"operation": "read"}, interrupt=interrupt)
                except BridgeError as error:
                    if attempt or not error.retryable:
                        raise
                    if interrupt is not None and interrupt.is_set():
                        raise ReadDeferred from None
                    deadline = time.monotonic() + 5
                    while (delay := deadline - time.monotonic()) > 0:
                        await _read_pause(delay, interrupt)
        finally:
            self._inflight.discard(task)

    async def setting(self, config: DeviceConfig, field: str, value: Any) -> dict[str, Any]:
        task = self._begin_request()
        try:
            return await self._request(config, {"operation": "setting", "field": field, "value": value})
        finally:
            self._inflight.discard(task)

    async def relay(self, config: DeviceConfig, value: bool) -> dict[str, Any]:
        if type(value) is not bool:
            raise BridgeError("validation_failed")
        task = self._begin_request()
        try:
            return await self._request(config, {"operation": "relay", "value": value})
        finally:
            self._inflight.discard(task)

    async def async_close(self) -> None:
        self._closing = True
        tasks = [task for task in self._inflight if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        async def drain():
            await asyncio.gather(*tasks, return_exceptions=True)

        await async_finish_cleanup(asyncio.create_task(drain()))

    async def _request(self, config: DeviceConfig, command: dict[str, Any], *,
                       interrupt: asyncio.Event | None = None) -> dict[str, Any]:
        global _NEXT_REQUEST_AT
        payload = {**config.worker_payload(), **command}
        setting = command["operation"] == "setting"
        relay = command["operation"] == "relay"
        writing = setting or relay
        async with _request_slot(writing, interrupt):
            process = None
            attempt_started = time.monotonic()
            try:
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable,
                    "-B",
                    str(self.worker_path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_MAX_REPORT_BYTES,
                ))
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    try:
                        process = await async_finish_cleanup(spawn)
                    except asyncio.CancelledError:
                        process = spawn.result()
                    raise
                try:
                    stdout, _stderr = await asyncio.wait_for(
                        process.communicate(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
                        timeout=self.timeout,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    await self._stop(process)
                    raise
                if len(stdout) > _MAX_REPORT_BYTES:
                    raise BridgeError("invalid_worker_report", may_have_written=writing)
                try:
                    report = json.loads(stdout)
                except (ValueError, UnicodeError):
                    raise BridgeError("invalid_worker_report", may_have_written=writing) from None
                if not isinstance(report, dict):
                    raise BridgeError("invalid_worker_report", may_have_written=writing)
                if process.returncode != 0 or report.get("status") == "error":
                    error = _worker_error(report, config, setting=setting, relay=relay)
                    # Long worker timeouts are not transient connect failures.
                    if time.monotonic() - attempt_started > _CONNECT_RETRY_WINDOW:
                        error.retryable = False
                    raise error
                return report
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise BridgeError("worker_timeout", may_have_written=writing) from None
            except (OSError, ValueError):
                raise BridgeError("worker_unavailable", may_have_written=writing) from None
            finally:
                try:
                    if process is not None and process.returncode is None:
                        await self._stop(process)
                finally:
                    _NEXT_REQUEST_AT = time.monotonic() + self.cooldown

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        await async_finish_cleanup(asyncio.create_task(BridgeClient._stop_process(process)))

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
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

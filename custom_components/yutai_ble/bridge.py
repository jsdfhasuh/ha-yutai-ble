"""Run the vendor protocol in an isolated, serialized subprocess."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .models import DeviceConfig
from .request_queue import RequestInterrupted, RequestQueue
from .session_bridge import SessionBridge, SessionBridgeError

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
_DIAGNOSTIC_PHASES = _AUTH_PHASES | frozenset({
    "dependencies", "input_or_runtime", "bluez_cache", "ble_connect", "ble_connect_retry",
    "cleanup", "connect_retry_cooldown",
    "read_property", "read_settings_properties", "setting_initial_snapshot", "setting_set",
    "setting_verify_snapshot", "relay_initial_snapshot", "relay_set", "relay_verify_snapshot",
    "session_preflight", "session_operation", "session_initial_relay", "session_relay_set",
    "session_verify", "session_read", "session_worker",
})
_PROTOCOL_ERROR_CODES = frozenset({
    "probe_failed", "phase_timeout", "worker_failed", "session_failed", "input_or_runtime_failed",
    "connect_cleanup_failed", "timing_phase_invalid",
    "target_already_connected", "target_adapter_ambiguous", "target_adapter_changed",
    "target_absent_no_existing_discovery",
    "existing_discovery_stopped_or_changed", "target_not_seen", "target_disappeared", "bluez_query_failed",
    "required_characteristic_missing", "mtu_acquisition_backend_unsupported", "acquired_mtu_invalid",
    "auth_write_capacity_unknown", "auth_write_capacity_too_small", "auth_frame_exceeds_capacity",
    "auth_single_payload_invalid", "auth_single_payload_exceeds_dmtu", "auth_receiver_not_ready",
    "negotiation_not_complete", "negotiated_channel_dmtu_too_small", "mng1_ack_timeout", "auth_ready_timeout",
    "device_public_key_ambiguous", "device_public_key_missing", "public_key_complete_ack_missing",
    "authentication_not_confirmed", "authentication_rejected_or_invalid", "f0_response_invalid",
    "transport_ack_failed", "transport_ack_cancelled", "channel_task_stopped", "data_frame_invalid",
    "response_decrypt_or_validation_failed", "response_transaction_invalid", "frame_truncated",
    "frame_header_invalid", "protocol_phase_invalid", "read_response_mismatch", "property_count_invalid",
    "property_target_mismatch", "property_status_failed", "property_size_invalid", "property_value_invalid",
    "settings_response_mismatch", "settings_property_count_invalid", "settings_property_header_truncated",
    "settings_property_target_or_order_invalid", "settings_typelen_truncated", "settings_property_value_truncated",
    "settings_response_trailing_bytes", "settings_response_not_expected", "settings_request_in_progress",
    "setting_set_response_invalid", "setting_snapshot_status_failed", "setting_response_not_expected",
    "setting_relay_state_changed", "setting_readback_mismatch", "setting_decoded_value_mismatch",
    "setting_decoded_readback_mismatch", "relay_set_response_invalid", "relay_response_not_expected",
    "relay_readback_mismatch", "relay_settings_changed", "session_unusable", "session_disconnected",
    "session_not_authenticated", "session_lifetime_expired", "session_idle_expired", "session_request_in_progress",
    "session_packet_not_authorized", "session_response_not_expected", "session_command_budget_exhausted",
    "session_relay_readback_mismatch", "session_previous_cleanup_failed", "disconnect_failed_or_timed_out",
    "still_connected", "still_connected_or_unknown",
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

    def __init__(self, code: str, *, retryable: bool = False, may_have_written: bool = False,
                 phase: str = "unknown", protocol_error_code: str = "unknown",
                 connection_attempts: int | None = None, phase_timings_seconds: dict | None = None):
        self.code = code if type(code) is str and code in _ERROR_CODES else "worker_failed"
        self.retryable = retryable
        self.may_have_written = may_have_written
        self.phase = phase if type(phase) is str and phase in _DIAGNOSTIC_PHASES else "unknown"
        self.protocol_error_code = (protocol_error_code if type(protocol_error_code) is str
                                    and protocol_error_code in _PROTOCOL_ERROR_CODES else "unknown")
        self.connection_attempts = (connection_attempts if type(connection_attempts) is int
                                    and 0 <= connection_attempts <= 2 else None)
        self.phase_timings_seconds = {
            key: float(value) for key, value in (
                phase_timings_seconds.items() if isinstance(phase_timings_seconds, dict) else ())
            if type(key) is str and key in _DIAGNOSTIC_PHASES
            and type(value) in (int, float) and 0 <= value <= 120 and math.isfinite(value)
        }
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

    def failure(code, *, retryable=False, may_have_written=False):
        return BridgeError(
            code, retryable=retryable, may_have_written=may_have_written,
            phase=report.get("phase"), protocol_error_code=report.get("error_code"),
            connection_attempts=report.get("connection_attempts"),
            phase_timings_seconds=report.get("phase_timings_seconds"),
        )

    attempts = "relay_write_attempts" if relay else "setting_write_attempts"
    may_have_written = writing and not (
        report.get("maybe_executed") is False
        and type(report.get(attempts)) is int
        and report[attempts] == 0
        and type(report.get("relay_writes")) is int
        and report["relay_writes"] == 0
    )
    if report.get("cleanup_error_code"):
        return failure("cleanup_failed", may_have_written=may_have_written)
    phase, error_class = report.get("phase"), report.get("error_class")
    if (phase == "ble_connect" and type(error_class) is str and error_class in _CONNECT_ERRORS
            and type(report.get("error_code")) is str
            and report["error_code"] in {"probe_failed", "phase_timeout"}):
        code = "ble_connect_timeout" if error_class == "TimeoutError" else "ble_connect_failed"
        legacy_retry = "connection_attempts" not in report and "connect_retry_managed" not in report
        single_read_attempt = (report.get("connect_retry_managed") is False
                               and type(report.get("connection_attempts")) is int
                               and report["connection_attempts"] == 1)
        return failure(
            code,
            # Background reads yield between workers so queued manual commands
            # can go first. A worker-managed retry budget must never be restarted.
            retryable=((legacy_retry or single_read_attempt)
                       and not writing and report.get("status") == "error"
                       and report.get("connection_attempted") is True
                       and report.get("disconnect_attempted") is True
                       and type(report.get("relay_writes")) is int
                       and report["relay_writes"] == 0
                       and report.get("connected_after_cleanup") is False),
            may_have_written=may_have_written,
        )
    if type(phase) is str and phase in _AUTH_PHASES:
        return failure("authentication_failed", may_have_written=may_have_written)
    if error_class == "ProbeError":
        return failure("protocol_failed", may_have_written=may_have_written)
    return failure("worker_failed", may_have_written=may_have_written)


class BridgeClient:
    """Serialize requests; retry only one safely disconnected, read-only connect."""

    def __init__(
        self,
        worker_path: Path | None = None,
        *,
        timeout: float = 100,
        cooldown: float = 3,
        session_idle_timeout: int = 0,
    ) -> None:
        if type(session_idle_timeout) is not int or not 0 <= session_idle_timeout <= 120:
            raise ValueError("session_idle_timeout must be an integer from 0 to 120")
        self.worker_path = worker_path or Path(__file__).parent / "protocol" / "bridge_worker.py"
        self.timeout = timeout
        self.cooldown = cooldown
        self.session_idle_timeout = session_idle_timeout
        self._session = (SessionBridge(
            Path(__file__).parent / "protocol" / "session_worker.py",
            timeout=timeout, idle_timeout=session_idle_timeout,
        ) if session_idle_timeout else None)
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
            if self._session is not None and self._session.started:
                async with _request_slot(True, None):
                    await self._close_session_locked()

        await async_finish_cleanup(asyncio.create_task(drain()))

    async def async_reset_session(self) -> None:
        """Release this entry's idle connection without closing the bridge."""
        task = self._begin_request()
        try:
            if self._session is not None and self._session.started:
                async with _request_slot(True, None):
                    await self._close_session_locked()
        finally:
            self._inflight.discard(task)

    async def _close_session_locked(self) -> None:
        global _NEXT_REQUEST_AT
        if self._session is None or not self._session.started:
            return
        try:
            await self._session.async_close()
        except SessionBridgeError as error:
            raise BridgeError(error.code, may_have_written=error.may_have_written) from None
        finally:
            _NEXT_REQUEST_AT = time.monotonic() + self.cooldown

    async def _session_request(self, config: DeviceConfig, command: dict[str, Any], *,
                               interrupt: asyncio.Event | None) -> dict[str, Any]:
        global _NEXT_REQUEST_AT
        queued_at = time.monotonic()
        relay = command["operation"] == "relay"
        async with _request_slot(relay, interrupt):
            queue_wait = time.monotonic() - queued_at
            successful = False
            attempt_started = time.monotonic()
            try:
                if not self._session.matches(config):
                    await self._close_session_locked()
                    while (delay := _NEXT_REQUEST_AT - time.monotonic()) > 0:
                        await _read_pause(delay, interrupt if not relay else None)
                report = await self._session.request(config, command)
                if report.get("status") == "error":
                    error = _worker_error(report, config, setting=False, relay=relay)
                    if time.monotonic() - attempt_started > _CONNECT_RETRY_WINDOW:
                        error.retryable = False
                    raise error
                report["queue_wait_seconds"] = round(queue_wait, 6)
                report["worker_elapsed_seconds"] = round(time.monotonic() - attempt_started, 6)
                successful = True
                return report
            except SessionBridgeError as error:
                raise BridgeError(error.code, may_have_written=error.may_have_written) from None
            finally:
                if not successful:
                    _NEXT_REQUEST_AT = time.monotonic() + self.cooldown

    async def _request(self, config: DeviceConfig, command: dict[str, Any], *,
                       interrupt: asyncio.Event | None = None) -> dict[str, Any]:
        global _NEXT_REQUEST_AT
        if (self._session is not None and config.model == "yutai.plug.fsov8m"
                and command["operation"] in {"read", "relay"}):
            return await self._session_request(config, command, interrupt=interrupt)
        payload = {**config.worker_payload(), **command}
        setting = command["operation"] == "setting"
        relay = command["operation"] == "relay"
        writing = setting or relay
        queued_at = time.monotonic()
        async with _request_slot(writing, interrupt):
            queue_wait = time.monotonic() - queued_at
            # Settings use the independently verified one-shot path, never a second
            # connection alongside this entry's authenticated resident worker.
            if self._session is not None and self._session.started:
                await self._close_session_locked()
                while (delay := _NEXT_REQUEST_AT - time.monotonic()) > 0:
                    await _read_pause(delay, interrupt if not writing else None)
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
                report["queue_wait_seconds"] = round(queue_wait, 6)
                report["worker_elapsed_seconds"] = round(time.monotonic() - attempt_started, 6)
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

"""Read and verified setting writes, serialized before state publication."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import BridgeClient, BridgeError, ReadDeferred, async_finish_cleanup
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN, CONF_SESSION_IDLE_TIMEOUT, SUPPORTED_MODEL
from .models import Snapshot, ValidationError, normalize_report, normalize_relay_report, validate_config, validate_poll_interval, validate_session_idle_timeout, validate_setting
from .request_queue import RequestQueue

_LOGGER = logging.getLogger(__name__)


class YutaiCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.device_config = validate_config(entry.data)
        interval = validate_poll_interval(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        idle = validate_session_idle_timeout(entry.options.get(CONF_SESSION_IDLE_TIMEOUT, 0))
        self.session_idle_timeout = idle if self.device_config.model == SUPPORTED_MODEL else 0
        self.bridge = BridgeClient(session_idle_timeout=self.session_idle_timeout)
        self._request_lock = RequestQueue()
        self._manual_requested = asyncio.Event()
        self._manual_count = 0
        self._manual_sequence = 0
        self._last_manual_sequence = 0
        self._last_manual_command = None
        self._snapshot_revision = 0
        self._state_revision = 0
        self.relay_readback: bool | None = None
        self.last_relay_success_at: datetime | None = None
        self.last_operation_metrics: dict = {}
        self._verified_snapshot: Snapshot | None = None
        self._snapshot_valid = False
        self._base_interval = interval
        self._closing = False
        self._inflight: set[asyncio.Task] = set()
        self._recovery_task: asyncio.Task | None = None
        self._fast_refresh_task: asyncio.Task | None = None
        self.consecutive_failures = 0
        self.last_success_at: datetime | None = None
        self.last_error_code: str | None = None
        self.last_error_phase: str | None = None
        self.last_protocol_error: str | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            always_update=True,
        )

    def _begin_request(self):
        if self._closing:
            raise BridgeError("entry_closing")
        task = asyncio.current_task()
        self._inflight.add(task)
        return task

    def _record_success(self, snapshot: Snapshot):
        self.consecutive_failures = 0
        self.last_success_at = datetime.now(timezone.utc)
        self.last_error_code = None
        self.last_error_phase = None
        self.last_protocol_error = None
        self.update_interval = timedelta(seconds=self._base_interval)
        self._snapshot_revision += 1
        self._state_revision += 1
        self.relay_readback = None
        self._verified_snapshot = snapshot
        self._snapshot_valid = True

    def _record_failure(self, code, *, refresh=False, error=None):
        self._snapshot_valid = False
        self.relay_readback = None
        self.last_error_code = BridgeError(code).code
        self.last_error_phase = error.phase if isinstance(error, BridgeError) else None
        self.last_protocol_error = error.protocol_error_code if isinstance(error, BridgeError) else None
        if isinstance(error, BridgeError):
            self._record_metrics({"connection_attempts": error.connection_attempts,
                                  "phase_timings_seconds": error.phase_timings_seconds})
        if refresh:
            self.consecutive_failures += 1
            seconds = min(max(self._base_interval, 300),
                          self._base_interval * 2 ** min(self.consecutive_failures, 10))
            self.update_interval = timedelta(seconds=seconds)

    def _record_metrics(self, report):
        self.last_operation_metrics = {
            key: round(value, 4) for key in (
                "queue_wait_seconds", "worker_elapsed_seconds", "operation_elapsed_seconds",
                "connect_auth_elapsed_seconds")
            if type(value := report.get(key)) in (int, float) and math.isfinite(value) and value >= 0
        }
        if type(report.get("connection_reused")) is bool:
            self.last_operation_metrics["connection_reused"] = report["connection_reused"]
        if type(report.get("connection_attempts")) is int and 0 <= report["connection_attempts"] <= 2:
            self.last_operation_metrics["connection_attempts"] = report["connection_attempts"]
        phases = report.get("phase_timings_seconds")
        if isinstance(phases, dict):
            allowed = {"dependencies", "bluez_cache", "ble_connect", "mtu_acquisition",
                       "authentication_subscribe", "authentication_mcu", "authentication_a4",
                       "authentication_pubkey", "authentication_proof", "protocol_exchange",
                       "cleanup", "connect_retry_cooldown"}
            self.last_operation_metrics["phase_timings_seconds"] = {
                name: round(value, 4) for name, value in phases.items()
                if name in allowed and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 120
            }

    async def _async_reset_invalid_session(self):
        try:
            await self.bridge.async_reset_session()
        except BridgeError as error:
            # Reset already reaped the worker. Preserve the original validation
            # failure so stale state is invalidated and write recovery still runs.
            _LOGGER.warning("Could not gracefully close invalid Yutai BLE session: %s", error.code)

    def _schedule_full_refresh(self):
        if not self._closing and (self._fast_refresh_task is None or self._fast_refresh_task.done()):
            self._fast_refresh_task = asyncio.create_task(self._async_full_refresh())

    async def _async_full_refresh(self):
        try:
            while not self._closing:
                revision = self._state_revision
                await asyncio.sleep(1)
                if revision != self._state_revision:
                    continue
                if self.relay_readback is not None:
                    await self.async_request_refresh()
                break
        finally:
            self._fast_refresh_task = None

    def _publish_update_error(self, error: UpdateFailed) -> None:
        # HA only notifies on the transition to failure. Diagnostics can change
        # on later failures too, while the verified device data stays unavailable.
        already_failed = not self.last_update_success
        self.async_set_update_error(error)
        if already_failed:
            self.async_update_listeners()

    def _schedule_recovery(self):
        if not self._closing and (self._recovery_task is None or self._recovery_task.done()):
            self._recovery_task = asyncio.create_task(self._async_recover(self._snapshot_revision))

    async def _async_recover(self, revision):
        try:
            await asyncio.sleep(5)
            if (not self._closing and not
                    (self._snapshot_valid and self._snapshot_revision > revision)):
                await self.async_request_refresh()
        finally:
            self._recovery_task = None

    def _begin_manual(self):
        self._manual_count += 1
        self._manual_sequence += 1
        self._manual_requested.set()
        return self._manual_sequence, self._state_revision

    def _end_manual(self):
        self._manual_count -= 1
        if not self._manual_count:
            self._manual_requested.clear()

    def _duplicate_completed(self, sequence, revision, command):
        # Only consecutive, overlapping requests may share a verified result.
        # ON/OFF/ON stays three commands; an old cached ON never suppresses a new ON.
        if (self._state_revision > revision
                and self._last_manual_sequence == sequence - 1
                and self._last_manual_command == command):
            snapshot = self._verified_snapshot
            observed = self.relay_readback if command[0] == "relay" else None
            if observed is None and self._snapshot_valid and snapshot is not None:
                values = snapshot.measurements if command[0] == "relay" else snapshot.settings
                observed = values.get(command[1])
            if type(observed) is type(command[2]) and observed == command[2]:
                self._last_manual_sequence = sequence
                return True
        return False

    async def _async_update_data(self) -> Snapshot:
        try:
            task = self._begin_request()
        except BridgeError:
            raise UpdateFailed(
                "Yutai BLE entry is closing",
                translation_domain=DOMAIN,
                translation_key="entry_closing",
            ) from None
        refresh_started_failed = not self.last_update_success
        revision = self._snapshot_revision
        reading = False
        try:
            while True:
                async with self._request_lock.hold():
                    if self._closing:
                        raise UpdateFailed(
                            "Yutai BLE entry is closing",
                            translation_domain=DOMAIN,
                            translation_key="entry_closing",
                        )
                    if self._snapshot_valid and self._snapshot_revision > revision:
                        return self._verified_snapshot
                    try:
                        reading = True
                        report = await self.bridge.read(self.device_config, interrupt=self._manual_requested)
                        snapshot = normalize_report(self.device_config, report)
                    except ReadDeferred:
                        reading = False
                        continue
                    except (BridgeError, ValidationError) as error:
                        if isinstance(error, ValidationError):
                            await self._async_reset_invalid_session()
                        self._record_failure(error.code if isinstance(error, BridgeError) else "validation_failed",
                                             refresh=True, error=error)
                        failure = UpdateFailed(
                            "Yutai BLE read could not be verified",
                            translation_domain=DOMAIN,
                            translation_key="read_unverified",
                        )
                        # Match HA's state at refresh start, before a queued
                        # manual request can change it. HA publishes first failures.
                        if refresh_started_failed:
                            self._publish_update_error(failure)
                        raise failure from None
                    self._record_success(snapshot)
                    self._record_metrics(report)
                    return snapshot
        except asyncio.CancelledError:
            if reading:
                self._record_failure("request_cancelled")
                self._publish_update_error(UpdateFailed(
                    "Yutai BLE read was cancelled",
                    translation_domain=DOMAIN,
                    translation_key="read_cancelled",
                ))
            raise
        finally:
            self._inflight.discard(task)

    async def async_set_setting(self, field: str, value: object) -> None:
        try:
            task = self._begin_request()
        except BridgeError:
            raise HomeAssistantError(
                "Yutai BLE entry is closing",
                translation_domain=DOMAIN,
                translation_key="entry_closing",
            ) from None
        sequence, revision = self._begin_manual()
        command = ("setting", field, value)
        sending = False
        try:
            async with self._request_lock.hold(manual=True):
                if self._closing:
                    raise HomeAssistantError(
                        "Yutai BLE entry is closing",
                        translation_domain=DOMAIN,
                        translation_key="entry_closing",
                    )
                try:
                    validate_setting(field, value, self.data.capabilities)
                except ValidationError:
                    raise HomeAssistantError(
                        "Yutai BLE setting is not supported",
                        translation_domain=DOMAIN,
                        translation_key="unsupported_setting",
                    ) from None
                if self._duplicate_completed(sequence, revision, command):
                    return
                try:
                    sending = True
                    report = await self.bridge.setting(self.device_config, field, value)
                    snapshot = normalize_report(
                        self.device_config,
                        report,
                        operation="setting",
                        field_name=field,
                        expected_value=value,
                    )
                except (BridgeError, ValidationError) as error:
                    if isinstance(error, ValidationError):
                        await self._async_reset_invalid_session()
                    uncertain = not isinstance(error, BridgeError) or error.may_have_written
                    code = "write_result_uncertain" if uncertain else error.code
                    self._record_failure(code, error=error)
                    self._publish_update_error(UpdateFailed(
                        "Yutai BLE setting could not be verified",
                        translation_domain=DOMAIN,
                        translation_key="setting_unverified",
                    ))
                    self._schedule_recovery()
                    raise HomeAssistantError(
                        "Yutai BLE setting could not be verified",
                        translation_domain=DOMAIN,
                        translation_key="setting_unverified",
                    ) from None
                self._record_success(snapshot)
                self._record_metrics(report)
                self._last_manual_sequence, self._last_manual_command = sequence, command
                self.async_set_updated_data(snapshot)
        except asyncio.CancelledError:
            if sending:
                self._record_failure("write_result_uncertain")
                self._publish_update_error(UpdateFailed(
                    "Yutai BLE setting was cancelled",
                    translation_domain=DOMAIN,
                    translation_key="setting_cancelled",
                ))
                self._schedule_recovery()
            raise
        finally:
            self._end_manual()
            self._inflight.discard(task)

    async def async_set_relay(self, value: bool) -> None:
        if type(value) is not bool:
            raise HomeAssistantError(
                "Yutai BLE output requires a boolean value",
                translation_domain=DOMAIN,
                translation_key="invalid_relay_value",
            )
        try:
            task = self._begin_request()
        except BridgeError:
            raise HomeAssistantError(
                "Yutai BLE entry is closing",
                translation_domain=DOMAIN,
                translation_key="entry_closing",
            ) from None
        sequence, revision = self._begin_manual()
        command = ("relay", "relay_output", value)
        sending = False
        try:
            async with self._request_lock.hold(manual=True):
                if self._closing:
                    raise HomeAssistantError(
                        "Yutai BLE entry is closing",
                        translation_domain=DOMAIN,
                        translation_key="entry_closing",
                    )
                if self._duplicate_completed(sequence, revision, command):
                    return
                try:
                    sending = True
                    report = await self.bridge.relay(self.device_config, value)
                    partial = isinstance(report, dict) and report.get("transport") == "retained_session"
                    if partial:
                        result = normalize_relay_report(self.device_config, report, value)
                    else:
                        snapshot = normalize_report(self.device_config, report, operation="relay", expected_value=value)
                except (BridgeError, ValidationError) as error:
                    if isinstance(error, ValidationError):
                        await self._async_reset_invalid_session()
                    uncertain = not isinstance(error, BridgeError) or error.may_have_written
                    self._record_failure("write_result_uncertain" if uncertain else error.code, error=error)
                    self._publish_update_error(UpdateFailed(
                        "Yutai BLE output could not be verified",
                        translation_domain=DOMAIN,
                        translation_key="relay_unverified",
                    ))
                    self._schedule_recovery()
                    raise HomeAssistantError(
                        "Yutai BLE output could not be verified; readback will check its state",
                        translation_domain=DOMAIN,
                        translation_key="relay_unverified",
                    ) from None
                self._record_metrics(report)
                if partial:
                    self.relay_readback = result.value
                    self.last_relay_success_at = datetime.now(timezone.utc)
                    self._state_revision += 1
                    # The prior complete snapshot keeps its timestamp and validity.
                    self.async_update_listeners()
                    self._schedule_full_refresh()
                else:
                    self._record_success(snapshot)
                    self.async_set_updated_data(snapshot)
                self._last_manual_sequence, self._last_manual_command = sequence, command
        except asyncio.CancelledError:
            if sending:
                self._record_failure("write_result_uncertain")
                self._publish_update_error(UpdateFailed(
                    "Yutai BLE output request was cancelled",
                    translation_domain=DOMAIN,
                    translation_key="relay_cancelled",
                ))
                self._schedule_recovery()
            raise
        finally:
            self._end_manual()
            self._inflight.discard(task)

    async def async_shutdown(self) -> None:
        self._closing = True

        async def shutdown():
            current = asyncio.current_task()
            tasks = [task for task in self._inflight if task is not current]
            if self._recovery_task is not None:
                tasks.append(self._recovery_task)
            if self._fast_refresh_task is not None:
                tasks.append(self._fast_refresh_task)
            for task in tasks:
                task.cancel()
            try:
                await self.bridge.async_close()
            except BridgeError as error:
                _LOGGER.warning("Could not gracefully close Yutai BLE session during shutdown: %s", error.code)
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
                self._recovery_task = None
                self._fast_refresh_task = None
                await super(YutaiCoordinator, self).async_shutdown()

        await async_finish_cleanup(asyncio.create_task(shutdown()))

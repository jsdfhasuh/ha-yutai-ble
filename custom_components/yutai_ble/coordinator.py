"""Read and verified setting writes, serialized before state publication."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import BridgeClient, BridgeError, ReadDeferred, async_finish_cleanup
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN
from .models import Snapshot, ValidationError, normalize_report, validate_config, validate_poll_interval, validate_setting
from .request_queue import RequestQueue

_LOGGER = logging.getLogger(__name__)


class YutaiCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.device_config = validate_config(entry.data)
        interval = validate_poll_interval(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        self.bridge = BridgeClient()
        self._request_lock = RequestQueue()
        self._manual_requested = asyncio.Event()
        self._manual_count = 0
        self._manual_sequence = 0
        self._last_manual_sequence = 0
        self._last_manual_command = None
        self._snapshot_revision = 0
        self._verified_snapshot: Snapshot | None = None
        self._snapshot_valid = False
        self._base_interval = interval
        self._closing = False
        self._inflight: set[asyncio.Task] = set()
        self._recovery_task: asyncio.Task | None = None
        self.consecutive_failures = 0
        self.last_success_at: datetime | None = None
        self.last_error_code: str | None = None
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
        self.update_interval = timedelta(seconds=self._base_interval)
        self._snapshot_revision += 1
        self._verified_snapshot = snapshot
        self._snapshot_valid = True

    def _record_failure(self, code, *, refresh=False):
        self._snapshot_valid = False
        self.last_error_code = BridgeError(code).code
        if refresh:
            self.consecutive_failures += 1
            seconds = min(max(self._base_interval, 300),
                          self._base_interval * 2 ** min(self.consecutive_failures, 10))
            self.update_interval = timedelta(seconds=seconds)

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
        return self._manual_sequence, self._snapshot_revision

    def _end_manual(self):
        self._manual_count -= 1
        if not self._manual_count:
            self._manual_requested.clear()

    def _duplicate_completed(self, sequence, revision, command):
        # Only consecutive, overlapping requests may share a verified result.
        # ON/OFF/ON stays three commands; an old cached ON never suppresses a new ON.
        if (self._snapshot_valid and self._snapshot_revision > revision
                and self._last_manual_sequence == sequence - 1
                and self._last_manual_command == command):
            snapshot = self._verified_snapshot
            values = snapshot.measurements if command[0] == "relay" else snapshot.settings
            if values.get(command[1]) == command[2]:
                self._last_manual_sequence = sequence
                return True
        return False

    async def _async_update_data(self) -> Snapshot:
        try:
            task = self._begin_request()
        except BridgeError:
            raise UpdateFailed("Yutai BLE entry is closing") from None
        revision = self._snapshot_revision
        reading = False
        try:
            while True:
                async with self._request_lock.hold():
                    if self._closing:
                        raise UpdateFailed("Yutai BLE entry is closing")
                    if self._snapshot_valid and self._snapshot_revision > revision:
                        return self._verified_snapshot
                    try:
                        reading = True
                        snapshot = normalize_report(self.device_config, await self.bridge.read(
                            self.device_config, interrupt=self._manual_requested))
                    except ReadDeferred:
                        reading = False
                        continue
                    except (BridgeError, ValidationError) as error:
                        self._record_failure(error.code if isinstance(error, BridgeError) else "validation_failed",
                                             refresh=True)
                        raise UpdateFailed("Yutai BLE read could not be verified") from None
                    self._record_success(snapshot)
                    return snapshot
        except asyncio.CancelledError:
            if reading:
                self._record_failure("request_cancelled")
                self.async_set_update_error(UpdateFailed("Yutai BLE read was cancelled"))
            raise
        finally:
            self._inflight.discard(task)

    async def async_set_setting(self, field: str, value: object) -> None:
        try:
            task = self._begin_request()
        except BridgeError:
            raise HomeAssistantError("Yutai BLE entry is closing") from None
        sequence, revision = self._begin_manual()
        command = ("setting", field, value)
        sending = False
        try:
            async with self._request_lock.hold(manual=True):
                if self._closing:
                    raise HomeAssistantError("Yutai BLE entry is closing")
                try:
                    validate_setting(field, value, self.data.capabilities)
                except ValidationError:
                    raise HomeAssistantError("Yutai BLE setting is not supported") from None
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
                    uncertain = not isinstance(error, BridgeError) or error.may_have_written
                    code = "write_result_uncertain" if uncertain else error.code
                    self._record_failure(code)
                    self.async_set_update_error(UpdateFailed("Yutai BLE setting could not be verified"))
                    self._schedule_recovery()
                    raise HomeAssistantError("Yutai BLE setting could not be verified") from None
                self._record_success(snapshot)
                self._last_manual_sequence, self._last_manual_command = sequence, command
                self.async_set_updated_data(snapshot)
        except asyncio.CancelledError:
            if sending:
                self._record_failure("write_result_uncertain")
                self.async_set_update_error(UpdateFailed("Yutai BLE setting was cancelled"))
                self._schedule_recovery()
            raise
        finally:
            self._end_manual()
            self._inflight.discard(task)

    async def async_set_relay(self, value: bool) -> None:
        if type(value) is not bool:
            raise HomeAssistantError("Yutai BLE output requires a boolean value")
        try:
            task = self._begin_request()
        except BridgeError:
            raise HomeAssistantError("Yutai BLE entry is closing") from None
        sequence, revision = self._begin_manual()
        command = ("relay", "relay_output", value)
        sending = False
        try:
            async with self._request_lock.hold(manual=True):
                if self._closing:
                    raise HomeAssistantError("Yutai BLE entry is closing")
                if self._duplicate_completed(sequence, revision, command):
                    return
                try:
                    sending = True
                    report = await self.bridge.relay(self.device_config, value)
                    snapshot = normalize_report(self.device_config, report, operation="relay", expected_value=value)
                except (BridgeError, ValidationError) as error:
                    uncertain = not isinstance(error, BridgeError) or error.may_have_written
                    self._record_failure("write_result_uncertain" if uncertain else error.code)
                    self.async_set_update_error(UpdateFailed("Yutai BLE output could not be verified"))
                    self._schedule_recovery()
                    raise HomeAssistantError("Yutai BLE output could not be verified; readback will check its state") from None
                self._record_success(snapshot)
                self._last_manual_sequence, self._last_manual_command = sequence, command
                self.async_set_updated_data(snapshot)
        except asyncio.CancelledError:
            if sending:
                self._record_failure("write_result_uncertain")
                self.async_set_update_error(UpdateFailed("Yutai BLE output request was cancelled"))
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
            for task in tasks:
                task.cancel()
            await self.bridge.async_close()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._recovery_task = None
            await super(YutaiCoordinator, self).async_shutdown()

        await async_finish_cleanup(asyncio.create_task(shutdown()))

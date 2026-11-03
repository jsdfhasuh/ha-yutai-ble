"""Read and verified setting writes, serialized before state publication."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import BridgeClient, BridgeError, async_finish_cleanup
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN
from .models import Snapshot, ValidationError, normalize_report, validate_config, validate_poll_interval, validate_setting

_LOGGER = logging.getLogger(__name__)


class YutaiCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.device_config = validate_config(entry.data)
        interval = validate_poll_interval(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        self.bridge = BridgeClient()
        self._request_lock = asyncio.Lock()
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

    def _record_success(self):
        self.consecutive_failures = 0
        self.last_success_at = datetime.now(timezone.utc)
        self.last_error_code = None
        self.update_interval = timedelta(seconds=self._base_interval)

    def _record_failure(self, code, *, refresh=False):
        self.last_error_code = BridgeError(code).code
        if refresh:
            self.consecutive_failures += 1
            seconds = min(max(self._base_interval, 300),
                          self._base_interval * 2 ** min(self.consecutive_failures, 10))
            self.update_interval = timedelta(seconds=seconds)

    def _schedule_recovery(self):
        if not self._closing and (self._recovery_task is None or self._recovery_task.done()):
            self._recovery_task = asyncio.create_task(self._async_recover())

    async def _async_recover(self):
        try:
            await asyncio.sleep(5)
            if not self._closing:
                await self.async_request_refresh()
        finally:
            self._recovery_task = None

    async def _async_update_data(self) -> Snapshot:
        try:
            task = self._begin_request()
        except BridgeError:
            raise UpdateFailed("Yutai BLE entry is closing") from None
        try:
            async with self._request_lock:
                if self._closing:
                    raise UpdateFailed("Yutai BLE entry is closing")
                try:
                    snapshot = normalize_report(self.device_config, await self.bridge.read(self.device_config))
                except (BridgeError, ValidationError) as error:
                    self._record_failure(error.code if isinstance(error, BridgeError) else "validation_failed",
                                         refresh=True)
                    raise UpdateFailed("Yutai BLE read could not be verified") from None
                self._record_success()
                return snapshot
        except asyncio.CancelledError:
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
        try:
            async with self._request_lock:
                if self._closing:
                    raise HomeAssistantError("Yutai BLE entry is closing")
                try:
                    validate_setting(field, value, self.data.capabilities)
                except ValidationError:
                    raise HomeAssistantError("Yutai BLE setting is not supported") from None
                try:
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
                self._record_success()
                self.async_set_updated_data(snapshot)
        except asyncio.CancelledError:
            self._record_failure("request_cancelled")
            self.async_set_update_error(UpdateFailed("Yutai BLE setting was cancelled"))
            self._schedule_recovery()
            raise
        finally:
            self._inflight.discard(task)

    async def async_set_relay(self, value: bool) -> None:
        if type(value) is not bool:
            raise HomeAssistantError("Yutai BLE output requires a boolean value")
        try:
            task = self._begin_request()
        except BridgeError:
            raise HomeAssistantError("Yutai BLE entry is closing") from None
        sending = False
        try:
            async with self._request_lock:
                if self._closing:
                    raise HomeAssistantError("Yutai BLE entry is closing")
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
                self._record_success()
                self.async_set_updated_data(snapshot)
        except asyncio.CancelledError:
            self._record_failure("write_result_uncertain" if sending else "request_cancelled")
            self.async_set_update_error(UpdateFailed("Yutai BLE output request was cancelled"))
            self._schedule_recovery()
            raise
        finally:
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

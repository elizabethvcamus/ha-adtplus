"Live SRV1 push coordinator for ADT+."

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from typing import Any

from aiohttp import ClientSession

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import ADTPlusAPI, ADTPlusAuthError, ADTPlusConnectionError
from .const import (
    CONF_LOCATION,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    INITIAL_DATA_TIMEOUT,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
)
from .helpers import device_lock_mode, list_from_payload, object_id, payload_of

_LOGGER = logging.getLogger(__name__)


class ADTPlusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Maintain the authenticated ADT+ WebSocket and latest state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: ClientSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=None,
        )
        self.entry = entry
        self.location: dict[str, Any] = dict(
            entry.data[CONF_LOCATION]
        )
        self.api = ADTPlusAPI(
            session,
            entry.data[CONF_REFRESH_TOKEN],
            on_refresh_token=self._save_rotated_refresh_token,
        )
        self._runner: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._initial_ready = asyncio.Event()
        self._lock_confirmation_tasks: dict[str, asyncio.Task[None]] = {}
        self._state: dict[str, Any] = {
            "device_configs": {},
            "device_statuses": {},
            "partition_configs": {},
            "partition_statuses": {},
            "system_status": {},
            "login_response": {},
            "lock_diagnostics": {},
        }

    def _save_rotated_refresh_token(self, refresh_token: str) -> None:
        """Persist an Auth0 rotated refresh token immediately."""
        if refresh_token == self.entry.data.get(CONF_REFRESH_TOKEN):
            return

        updated = dict(self.entry.data)
        updated[CONF_REFRESH_TOKEN] = refresh_token

        # async_update_entry is intentionally called directly on HA's event
        # loop. We do NOT register an entry update listener, because an
        # integration-generated token rotation must not cause a reload loop.
        self.hass.config_entries.async_update_entry(
            self.entry,
            data=updated,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Authenticate first, then start the live push feed."""
        if self._runner is None or self._runner.done():
            # Make the initial refresh here so an invalid saved token is
            # surfaced to Home Assistant as an authentication failure rather
            # than a generic 35-second setup timeout.
            try:
                await self.api.async_refresh_access_token()
            except ADTPlusAuthError as err:
                raise ConfigEntryAuthFailed(
                    "ADT+ rejected the saved refresh token"
                ) from err
            except ADTPlusConnectionError as err:
                raise UpdateFailed(str(err)) from err

            self._runner = self.entry.async_create_background_task(
                self.hass,
                self._async_run(),
                "ADT+ push coordinator",
                eager_start=True,
            )

        try:
            async with asyncio.timeout(INITIAL_DATA_TIMEOUT):
                await self._initial_ready.wait()
        except TimeoutError as err:
            raise UpdateFailed(
                "Timed out waiting for initial ADT+ push state"
            ) from err

        return deepcopy(self._state)

    async def _async_run(self) -> None:
        delay = RECONNECT_MIN_SECONDS

        while not self._stop.is_set():
            try:
                async for message in self.api.async_push_messages(
                    self.location
                ):
                    changed = self._process_message(message)
                    if changed:
                        self._check_initial_ready()
                        self.async_set_updated_data(
                            deepcopy(self._state)
                        )

                if not self._stop.is_set():
                    raise ADTPlusConnectionError(
                        "ADT+ push connection closed"
                    )

            except asyncio.CancelledError:
                raise

            except ADTPlusAuthError as err:
                _LOGGER.error(
                    "ADT+ authentication failed: %s",
                    err,
                )
                self.async_set_update_error(err)
                return

            except Exception as err:  # noqa: BLE001
                if self._stop.is_set():
                    return

                _LOGGER.warning(
                    "ADT+ push connection lost: %s; "
                    "reconnecting in %ss",
                    err,
                    delay,
                )
                self.async_set_update_error(err)

                # Force a fresh access token on the next connection. If
                # Auth0 rotates the refresh token, ADTPlusAPI invokes our
                # callback before any WebSocket connection is attempted.
                self.api.access_token = None

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=delay,
                    )
                except TimeoutError:
                    pass

                delay = min(
                    delay * 2,
                    RECONNECT_MAX_SECONDS,
                )
                continue

            delay = RECONNECT_MIN_SECONDS

    async def async_arm_state_change(
        self,
        partition_id: str,
        arm_type: str,
        code: str | None,
    ) -> str:
        """Send an ADT+ alarm state-change request over the live push socket."""
        try:
            return await self.api.async_arm_state_change(
                partition_id,
                arm_type,
                code,
            )
        except ADTPlusAuthError as err:
            raise ConfigEntryAuthFailed(
                "ADT+ authentication failed while sending alarm command"
            ) from err


    async def async_set_door_lock(
        self,
        device_id: str,
        unit_id: int | None,
        locked: bool,
    ) -> str:
        """Lock or unlock an ADT-managed Z-Wave door lock."""
        try:
            request_id = await self.api.async_set_door_lock(
                device_id,
                unit_id,
                locked,
            )
        except ADTPlusAuthError as err:
            raise ConfigEntryAuthFailed(
                "ADT+ authentication failed while sending lock command"
            ) from err

        device_id = str(device_id)
        old_task = self._lock_confirmation_tasks.pop(
            device_id,
            None,
        )
        if old_task is not None:
            old_task.cancel()

        diagnostics = self._state.setdefault(
            "lock_diagnostics",
            {},
        )

        if locked:
            diagnostics[device_id] = {
                "jammed": False,
                "pending": True,
                "last_command": "lock",
                "last_result": "pending",
                "request_id": request_id,
            }
            self.async_set_updated_data(deepcopy(self._state))

            task = self.hass.async_create_task(
                self._async_confirm_lock_command(device_id),
            )
            self._lock_confirmation_tasks[device_id] = task
        else:
            # An unlock request clears an earlier inferred jam. The real
            # lock entity continues to show whatever ADT reports.
            diagnostics[device_id] = {
                "jammed": False,
                "pending": False,
                "last_command": "unlock",
                "last_result": "unlock_requested",
                "request_id": request_id,
            }
            self.async_set_updated_data(deepcopy(self._state))

        return request_id

    async def _async_confirm_lock_command(
        self,
        device_id: str,
    ) -> None:
        """Infer a jam/failure if ADT does not confirm secured in time."""
        try:
            await asyncio.sleep(8)

            status = self._state.get(
                "device_statuses",
                {},
            ).get(device_id)
            mode = device_lock_mode(status)

            diagnostics = self._state.setdefault(
                "lock_diagnostics",
                {},
            )
            current = diagnostics.get(device_id, {})

            # Ignore an obsolete timeout after a newer command.
            if (
                not isinstance(current, dict)
                or current.get("last_command") != "lock"
                or current.get("pending") is not True
            ):
                return

            if mode == "secured":
                diagnostics[device_id] = {
                    **current,
                    "jammed": False,
                    "pending": False,
                    "last_result": "locked",
                    "confirmed_mode": mode,
                }
            else:
                diagnostics[device_id] = {
                    **current,
                    "jammed": True,
                    "pending": False,
                    "last_result": "lock_not_confirmed",
                    "confirmed_mode": mode,
                }

            self.async_set_updated_data(deepcopy(self._state))
        except asyncio.CancelledError:
            raise
        finally:
            current_task = self._lock_confirmation_tasks.get(
                device_id
            )
            if current_task is asyncio.current_task():
                self._lock_confirmation_tasks.pop(
                    device_id,
                    None,
                )

    def _confirm_lock_if_secured(
        self,
        device_id: str,
        device: dict[str, Any],
    ) -> None:
        """Clear pending/jammed state as soon as ADT reports secured."""
        if device_lock_mode(device) != "secured":
            return

        diagnostics = self._state.setdefault(
            "lock_diagnostics",
            {},
        )
        current = diagnostics.get(device_id)
        if not isinstance(current, dict):
            return

        if (
            current.get("pending") is not True
            and current.get("jammed") is not True
        ):
            return

        diagnostics[device_id] = {
            **current,
            "jammed": False,
            "pending": False,
            "last_result": "locked",
            "confirmed_mode": "secured",
        }

        task = self._lock_confirmation_tasks.pop(
            device_id,
            None,
        )
        if task is not None:
            task.cancel()

    async def async_panic_request(
        self,
        partition_id: str,
        panic_type: str,
    ) -> str:
        """Send a monitored ADT+ emergency request."""
        try:
            return await self.api.async_panic_request(
                partition_id,
                panic_type,
            )
        except ADTPlusAuthError as err:
            raise ConfigEntryAuthFailed(
                "ADT+ authentication failed while sending emergency signal"
            ) from err

    def _process_message(
        self,
        message: dict[str, Any],
    ) -> bool:
        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            return False

        payload = payload_of(message)

        if msg_type == "loginResponse":
            self._state["login_response"] = payload
            return True

        if msg_type == "deviceConfigList":
            devices = list_from_payload(
                payload,
                "devices",
                "deviceConfigList",
                "deviceConfigs",
            )
            self._state["device_configs"] = {
                did: item
                for item in devices
                if (did := object_id(item)) is not None
            }
            return True

        if msg_type == "deviceStatusList":
            devices = list_from_payload(
                payload,
                "devices",
                "deviceStatusList",
                "deviceStatuses",
            )
            self._state["device_statuses"] = {
                did: item
                for item in devices
                if (did := object_id(item)) is not None
            }
            return True

        if msg_type == "partitionConfigList":
            parts = list_from_payload(
                payload,
                "partitions",
                "partitionConfigList",
                "partitionConfigs",
            )
            self._state["partition_configs"] = {
                pid: item
                for item in parts
                if (pid := object_id(item)) is not None
            }
            return True

        if msg_type in (
            "partitionStatusList",
            "partitionStatus",
        ):
            parts = list_from_payload(
                payload,
                "partitions",
                "partitionStatusList",
                "partitionStatuses",
            )
            if not parts and "armState" in payload:
                parts = [payload]

            self._state["partition_statuses"] = {
                pid: item
                for item in parts
                if (pid := object_id(item)) is not None
            }
            return True

        if msg_type == "systemStatus":
            self._state["system_status"] = payload
            return True

        if msg_type in (
            "deviceUpdate",
            "deviceStatus",
            "deviceUpdateResponse",
        ):
            device = (
                payload.get("device")
                if isinstance(payload.get("device"), dict)
                else payload
            )
            did = object_id(device)
            if did is not None:
                self._state["device_statuses"][did] = device
                self._confirm_lock_if_secured(did, device)
                return True

        if msg_type in (
            "partitionUpdate",
            "partitionUpdateResponse",
        ):
            part = (
                payload.get("partition")
                if isinstance(payload.get("partition"), dict)
                else payload
            )
            pid = object_id(part)
            if pid is not None:
                self._state["partition_statuses"][pid] = part
                return True

        return False

    def _check_initial_ready(self) -> None:
        if (
            self._state["device_configs"]
            and self._state["device_statuses"]
            and self._state["partition_statuses"]
        ):
            self._initial_ready.set()

    async def async_shutdown(self) -> None:
        self._stop.set()

        for task in self._lock_confirmation_tasks.values():
            task.cancel()
        self._lock_confirmation_tasks.clear()

        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None

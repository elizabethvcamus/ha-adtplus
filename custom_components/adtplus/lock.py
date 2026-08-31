"ADT+ Z-Wave door locks controlled through the live SRV1 connection."

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ADTPlusConnectionError
from .const import DOMAIN
from .coordinator import ADTPlusCoordinator
from .helpers import (
    device_name,
    device_type,
    find_service,
    find_service_any_unit,
    units,
)


_LOCK_SERVICE_TYPES = ("zwaveDoorLock", "doorLock")


def _lock_service(device: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the runtime lock service."""
    for service_type in _LOCK_SERVICE_TYPES:
        found = find_service_any_unit(device, service_type)
        if isinstance(found, dict):
            return found
    return None


def _lock_unit_id(
    config_device: dict[str, Any] | None,
    status_device: dict[str, Any] | None,
) -> int | None:
    """Return the unitId that owns the lock service."""
    for device in (config_device, status_device):
        for unit in units(device):
            has_lock = any(
                find_service(unit, service_type) is not None
                for service_type in _LOCK_SERVICE_TYPES
            )
            if not has_lock:
                continue

            value = unit.get("unitId", unit.get("id"))
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue

    return None


def _is_lock_device(
    config_device: dict[str, Any] | None,
    status_device: dict[str, Any] | None,
) -> bool:
    """Return True for ADT Z-Wave door locks."""
    if _lock_service(status_device) is not None:
        return True

    if isinstance(config_device, dict):
        dtype = str(
            config_device.get("deviceType")
            or config_device.get("translatedDeviceType")
            or config_device.get("type")
            or ""
        ).lower()
        if dtype == "lock" or "doorlock" in dtype:
            return True

        for service_type in _LOCK_SERVICE_TYPES:
            if find_service_any_unit(config_device, service_type) is not None:
                return True

    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: ADTPlusCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def add_new_entities() -> None:
        entities: list[LockEntity] = []

        configs = coordinator.data.get("device_configs", {})
        statuses = coordinator.data.get("device_statuses", {})
        all_ids = set(configs) | set(statuses)

        for device_id in sorted(all_ids):
            if device_id in added:
                continue

            config_device = configs.get(device_id, {})
            status_device = statuses.get(device_id, {})

            if not _is_lock_device(config_device, status_device):
                continue

            added.add(device_id)
            entities.append(
                ADTPlusDoorLock(
                    coordinator,
                    str(device_id),
                    config_device,
                )
            )

        if entities:
            async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(
        coordinator.async_add_listener(add_new_entities)
    )


class ADTPlusDoorLock(
    CoordinatorEntity[ADTPlusCoordinator],
    LockEntity,
):
    """ADT-managed Z-Wave lock."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.device_id = device_id
        self.config_device = config_device

        account = str(
            coordinator.location.get("accountNumber", "location")
        )
        self._attr_unique_id = (
            f"{account}_device_{device_id}_lock"
        )

    @property
    def _status_device(self) -> dict[str, Any]:
        value = self.coordinator.data.get(
            "device_statuses", {}
        ).get(self.device_id)
        return value if isinstance(value, dict) else {}

    @property
    def _current_config_device(self) -> dict[str, Any]:
        value = self.coordinator.data.get(
            "device_configs", {}
        ).get(self.device_id)
        return value if isinstance(value, dict) else self.config_device

    @property
    def device_info(self) -> DeviceInfo:
        config = self._current_config_device
        loc = self.coordinator.location
        account = str(loc.get("accountNumber", "location"))
        model = config.get("model") if isinstance(config, dict) else None

        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{account}_device_{self.device_id}",
                )
            },
            name=device_name(config),
            manufacturer="ADT",
            model=(
                str(model)
                if model
                else device_type(config)
            ),
            via_device=(DOMAIN, f"location_{account}"),
        )

    @property
    def is_locked(self) -> bool | None:
        service = _lock_service(self._status_device)
        if not isinstance(service, dict):
            return None

        mode = service.get("mode")
        if not isinstance(mode, str):
            return None

        normalized = mode.strip()
        if normalized == "secured":
            return True
        if normalized.startswith("unsecured"):
            return False
        return None

    @property
    def is_locking(self) -> bool:
        service = _lock_service(self._status_device)
        if not isinstance(service, dict):
            return False
        mode = service.get("mode")
        target = service.get("targetMode")
        return (
            isinstance(target, str)
            and target == "secured"
            and mode != "secured"
        )

    @property
    def is_unlocking(self) -> bool:
        service = _lock_service(self._status_device)
        if not isinstance(service, dict):
            return False
        mode = service.get("mode")
        target = service.get("targetMode")
        return (
            isinstance(target, str)
            and target.startswith("unsecured")
            and not (
                isinstance(mode, str)
                and mode.startswith("unsecured")
            )
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        service = _lock_service(self._status_device)
        if not isinstance(service, dict):
            return {}

        diagnostic = self.coordinator.data.get(
            "lock_diagnostics",
            {},
        ).get(self.device_id)
        if not isinstance(diagnostic, dict):
            diagnostic = {}

        attrs = {
            "adt_device_id": self.device_id,
            "adt_mode": service.get("mode"),
            "adt_target_mode": service.get("targetMode"),
            "door_closed": service.get("doorClosed"),
            "bolt_retracted": service.get("boltRetracted"),
            "latch_retracted": service.get("latchRetracted"),
            "lock_timeout": service.get("lockTimeout"),
            "lock_command_pending": diagnostic.get("pending"),
            "lock_jammed": diagnostic.get("jammed"),
            "last_lock_command": diagnostic.get("last_command"),
            "last_lock_result": diagnostic.get("last_result"),
        }
        return {
            key: value
            for key, value in attrs.items()
            if value is not None
        }

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_set_lock(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_set_lock(False)

    async def _async_set_lock(self, locked: bool) -> None:
        unit_id = _lock_unit_id(
            self._current_config_device,
            self._status_device,
        )

        try:
            await self.coordinator.async_set_door_lock(
                self.device_id,
                unit_id,
                locked,
            )
        except ADTPlusConnectionError as err:
            raise HomeAssistantError(str(err)) from err

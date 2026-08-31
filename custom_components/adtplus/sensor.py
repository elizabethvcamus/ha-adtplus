"Diagnostic numeric sensors for ADT+ devices."

from __future__ import annotations

from typing import Any
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ADTPlusCoordinator
from .helpers import (
    battery_level_text,
    battery_percentage,
    device_battery_service,
    device_name,
    device_type,
    find_service_any_unit,
    shock_last_alert,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: ADTPlusCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    # Remove stale Signal Strength entities when ADT exposes an RSSI service
    # wrapper but does not actually provide a numeric RSSI level. This keeps
    # device pages free of permanent "Unknown" diagnostics.
    account = str(
        coordinator.location.get("accountNumber", "location")
    )
    statuses = coordinator.data.get("device_statuses", {})
    valid_signal_unique_ids: set[str] = set()

    for device_id, status_device in statuses.items():
        rssi = find_service_any_unit(status_device, "rssi")
        level = rssi.get("level") if isinstance(rssi, dict) else None
        if (
            isinstance(level, (int, float))
            and not isinstance(level, bool)
        ):
            valid_signal_unique_ids.add(
                f"{account}_device_{device_id}_signal_strength"
            )

    registry = er.async_get(hass)
    for registry_entry in list(
        er.async_entries_for_config_entry(
            registry,
            entry.entry_id,
        )
    ):
        unique_id = registry_entry.unique_id
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.entity_id.startswith("sensor.")
            and isinstance(unique_id, str)
            and unique_id.startswith(f"{account}_device_")
            and unique_id.endswith("_signal_strength")
            and unique_id not in valid_signal_unique_ids
        ):
            registry.async_remove(registry_entry.entity_id)

    @callback
    def add_new_entities() -> None:
        entities: list[SensorEntity] = []

        for device_id, status_device in coordinator.data.get(
            "device_statuses", {}
        ).items():
            config_device = coordinator.data.get(
                "device_configs", {}
            ).get(device_id, {})

            if device_battery_service(status_device) is not None:
                key = f"{device_id}:battery"
                if key not in added:
                    added.add(key)

                    if battery_percentage(status_device) is not None:
                        entities.append(
                            ADTPlusBatteryPercentageSensor(
                                coordinator,
                                str(device_id),
                                config_device,
                            )
                        )
                    elif battery_level_text(status_device) is not None:
                        # Preserve the same unique ID used by the v0.3
                        # Battery entity so Home Assistant converts the
                        # existing Unknown entity in place rather than
                        # leaving a stale duplicate.
                        entities.append(
                            ADTPlusBatteryLevelSensor(
                                coordinator,
                                str(device_id),
                                config_device,
                            )
                        )

            # ADT's ShockStatusService is timestamp-only; it does not expose
            # a persistent Boolean shock state. Do not invent a Clear/Active
            # state. Add a Last Shock timestamp only after ADT reports one.
            if shock_last_alert(status_device) is not None:
                key = f"{device_id}:last_shock"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusLastShockSensor(
                            coordinator,
                            str(device_id),
                            config_device,
                        )
                    )

            rssi = find_service_any_unit(status_device, "rssi")
            rssi_level = (
                rssi.get("level")
                if isinstance(rssi, dict)
                else None
            )
            if (
                isinstance(rssi_level, (int, float))
                and not isinstance(rssi_level, bool)
            ):
                key = f"{device_id}:rssi"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusSignalStrengthSensor(
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


class _ADTPlusDiagnosticSensor(
    CoordinatorEntity[ADTPlusCoordinator],
    SensorEntity,
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
        suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self.device_id = device_id
        self.config_device = config_device
        account = str(
            coordinator.location.get("accountNumber", "location")
        )
        self._attr_unique_id = (
            f"{account}_device_{device_id}_{suffix}"
        )
        self._attr_name = name

    @property
    def _status_device(self) -> dict[str, Any]:
        value = self.coordinator.data.get(
            "device_statuses", {}
        ).get(self.device_id)
        return value if isinstance(value, dict) else {}

    @property
    def device_info(self) -> DeviceInfo:
        loc = self.coordinator.location
        account = str(loc.get("accountNumber", "location"))
        model = (
            self.config_device.get("model")
            if isinstance(self.config_device, dict)
            else None
        )
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{account}_device_{self.device_id}",
                )
            },
            name=device_name(self.config_device),
            manufacturer="ADT",
            model=(
                str(model)
                if model
                else device_type(self.config_device)
            ),
            via_device=(DOMAIN, f"location_{account}"),
        )


class ADTPlusBatteryPercentageSensor(_ADTPlusDiagnosticSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
    ) -> None:
        super().__init__(
            coordinator,
            device_id,
            config_device,
            "battery_percentage",
            "Battery",
        )

    @property
    def native_value(self) -> int | None:
        return battery_percentage(self._status_device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        found = device_battery_service(self._status_device)
        if found is None:
            return {}
        _, svc = found
        return {
            key: value
            for key, value in {
                "battery_level": svc.get("batteryLevel"),
                "battery_volts": svc.get("batteryVolts"),
                "battery_expected": svc.get("batteryExpected"),
                "needs_replacement": svc.get("needsReplacement"),
            }.items()
            if value is not None
        }


class ADTPlusBatteryLevelSensor(_ADTPlusDiagnosticSensor):
    """Categorical ADT battery level when no percentage is supplied."""

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
    ) -> None:
        # Intentionally reuse the v0.3 battery_percentage unique-id suffix.
        # That updates the existing Battery entity instead of leaving
        # "Battery: Unknown" behind in the registry.
        super().__init__(
            coordinator,
            device_id,
            config_device,
            "battery_percentage",
            "Battery",
        )

    @property
    def native_value(self) -> str | None:
        value = battery_level_text(self._status_device)
        if value is None:
            return None
        return value.replace("_", " ").title()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        found = device_battery_service(self._status_device)
        if found is None:
            return {}
        _, svc = found
        return {
            key: value
            for key, value in {
                "raw_battery_level": svc.get("batteryLevel"),
                "battery_volts": svc.get("batteryVolts"),
                "battery_expected": svc.get("batteryExpected"),
                "needs_replacement": svc.get("needsReplacement"),
            }.items()
            if value is not None
        }


class ADTPlusLastShockSensor(_ADTPlusDiagnosticSensor):
    """Timestamp of the most recent shock event reported by ADT."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
    ) -> None:
        super().__init__(
            coordinator,
            device_id,
            config_device,
            "last_shock",
            "Last Shock",
        )

    @property
    def native_value(self) -> datetime | None:
        value = shock_last_alert(self._status_device)
        if value is None:
            return None

        try:
            # ADT serializes java.util.Date in ISO-8601 form.
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None

        return parsed


class ADTPlusSignalStrengthSensor(_ADTPlusDiagnosticSensor):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        device_id: str,
        config_device: dict[str, Any],
    ) -> None:
        super().__init__(
            coordinator,
            device_id,
            config_device,
            "signal_strength",
            "Signal Strength",
        )

    @property
    def native_value(self) -> int | None:
        svc = find_service_any_unit(self._status_device, "rssi")
        if not isinstance(svc, dict):
            return None
        value = svc.get("level")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return int(round(value))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        svc = find_service_any_unit(self._status_device, "rssi")
        if not isinstance(svc, dict):
            return {}
        return {
            key: value
            for key, value in {
                "strength": svc.get("strength"),
                "last_heard_from": svc.get("lastHeardFrom"),
            }.items()
            if value is not None
        }

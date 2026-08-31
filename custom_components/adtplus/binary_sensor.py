"""ADT+ binary sensors backed by the live SRV1 push feed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ADTPlusCoordinator
from .helpers import (
    SensorDefinition,
    battery_level_text,
    battery_percentage,
    device_name,
    device_type,
    find_service_any_unit,
    device_battery_service,
    device_is_troubled,
    device_lock_service,
    device_low_battery,
    device_tamper_service,
    sensor_definitions,
    sensor_runtime,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: ADTPlusCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    # v0.3.1 cleanup:
    # Older development builds could register auxiliary Reed/Shock config
    # zones as binary sensors. They are not live Boolean states in SRV1 and
    # can remain in Home Assistant's entity registry even after the parser is
    # fixed. Remove only stale legacy ADT sensor entities that the current
    # source data no longer defines.
    account = str(
        coordinator.location.get("accountNumber", "location")
    )
    valid_sensor_unique_ids = {
        f"{account}_sensor_{definition.key}"
        for definition in sensor_definitions(coordinator.data)
    }

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
            and registry_entry.entity_id.startswith("binary_sensor.")
            and isinstance(unique_id, str)
            and unique_id.startswith(f"{account}_sensor_")
            and unique_id not in valid_sensor_unique_ids
        ):
            registry.async_remove(registry_entry.entity_id)

    # v0.3.4 diagnostic cleanup:
    # A dedicated Low Battery binary sensor is redundant when ADT already
    # supplies either a numeric battery percentage or a categorical
    # Full/Medium/Low battery value. Remove old registry entries so HA does
    # not leave a disabled/stale Unknown row after upgrading.
    statuses = coordinator.data.get("device_statuses", {})
    eligible_low_battery_unique_ids: set[str] = set()

    for device_id, status_device in statuses.items():
        has_primary_battery = (
            battery_percentage(status_device) is not None
            or battery_level_text(status_device) is not None
        )
        if (
            not has_primary_battery
            and device_low_battery(status_device) is not None
        ):
            eligible_low_battery_unique_ids.add(
                f"{account}_device_{device_id}_low_battery"
            )

    for registry_entry in list(
        er.async_entries_for_config_entry(
            registry,
            entry.entry_id,
        )
    ):
        unique_id = registry_entry.unique_id
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.entity_id.startswith("binary_sensor.")
            and isinstance(unique_id, str)
            and unique_id.startswith(f"{account}_device_")
            and unique_id.endswith("_low_battery")
            and unique_id not in eligible_low_battery_unique_ids
        ):
            registry.async_remove(registry_entry.entity_id)

    @callback
    def add_new_entities() -> None:
        entities: list[BinarySensorEntity] = []
        for definition in sensor_definitions(coordinator.data):
            if definition.key in added:
                continue

            try:
                entity = ADTPlusSecuritySensor(
                    coordinator,
                    definition,
                )
            except Exception:  # noqa: BLE001
                # Never let one unusual ADT service prevent every other
                # binary sensor from being created.
                import logging

                logging.getLogger(__name__).exception(
                    "Unable to create ADT+ binary sensor %s",
                    definition.key,
                )
                continue

            added.add(definition.key)
            entities.append(entity)

        # Device-level diagnostic binary sensors.
        for device_id, status_device in coordinator.data.get(
            "device_statuses", {}
        ).items():
            config_device = coordinator.data.get(
                "device_configs", {}
            ).get(device_id, {})

            # Only expose a separate Low Battery diagnostic when ADT
            # provides an explicit low/replacement flag but no useful primary
            # Battery percentage or Full/Medium/Low state.
            has_primary_battery = (
                battery_percentage(status_device) is not None
                or battery_level_text(status_device) is not None
            )
            if (
                not has_primary_battery
                and device_low_battery(status_device) is not None
            ):
                key = f"health:{device_id}:low_battery"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusLowBatterySensor(
                            coordinator,
                            str(device_id),
                            config_device,
                        )
                    )

            if device_tamper_service(status_device) is not None:
                key = f"health:{device_id}:tamper"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusTamperSensor(
                            coordinator,
                            str(device_id),
                            config_device,
                        )
                    )

            if device_is_troubled(status_device) is not None:
                key = f"health:{device_id}:trouble"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusTroubleSensor(
                            coordinator,
                            str(device_id),
                            config_device,
                        )
                    )

            if device_lock_service(status_device) is not None:
                key = f"health:{device_id}:lock_jam"
                if key not in added:
                    added.add(key)
                    entities.append(
                        ADTPlusLockJamSensor(
                            coordinator,
                            str(device_id),
                            config_device,
                        )
                    )

        connectivity_key = "__base_connectivity__"
        if connectivity_key not in added:
            added.add(connectivity_key)
            entities.append(ADTPlusBaseConnectivitySensor(coordinator))

        if entities:
            async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class ADTPlusSecuritySensor(CoordinatorEntity[ADTPlusCoordinator], BinarySensorEntity):
    """One SRV1 securitySensor/alert service pair."""

    # These names are already full ADT device/zone names (for example,
    # "Front Door" or "Smoke Detector Carbon Monoxide").
    _attr_has_entity_name = False

    def __init__(self, coordinator: ADTPlusCoordinator, definition: SensorDefinition) -> None:
        super().__init__(coordinator)
        self.definition = definition
        loc = coordinator.location
        account = str(loc.get("accountNumber", "location"))
        self._attr_unique_id = f"{account}_sensor_{definition.key}"
        self._attr_name = definition.name
        self._attr_device_class = _device_class(definition.sensor_type, definition.placement)

    @property
    def is_on(self) -> bool | None:
        _, alert, _ = sensor_runtime(self.coordinator.data, self.definition)
        if not isinstance(alert, dict):
            return None
        value = alert.get("isFaulted")
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config, alert, security = sensor_runtime(self.coordinator.data, self.definition)
        status_device = self.coordinator.data.get("device_statuses", {}).get(self.definition.device_id)
        battery = find_service_any_unit(status_device, "batteryPower")
        rssi = find_service_any_unit(status_device, "rssi")
        operational = find_service_any_unit(status_device, "operational")

        attrs: dict[str, Any] = {
            "device_id": self.definition.device_id,
            "zone_id": self.definition.zone_id,
            "sensor_type": self.definition.sensor_type,
            "placement": self.definition.placement,
        }
        if isinstance(config, dict):
            attrs.update(
                {
                    "alarm_config": config.get("alarmConfig"),
                    "entry_delay_seconds": config.get("entryDelaySeconds"),
                    "chime_enabled": config.get("chimeEnabled"),
                }
            )
        if isinstance(security, dict):
            attrs.update(
                {
                    "alarmed": security.get("alarmed"),
                    "bypassed": security.get("bypassed"),
                    "bypass_reason": security.get("bypassReason"),
                    "bypass_required": security.get("bypassRequired"),
                    "bypass_required_away": security.get("bypassRequiredAway"),
                    "troubled": security.get("troubled"),
                }
            )
        if isinstance(battery, dict):
            attrs["battery_level"] = battery.get("batteryLevel")
        if isinstance(rssi, dict):
            attrs["signal_level"] = rssi.get("level")
            attrs["last_heard_from"] = rssi.get("lastHeardFrom")
        if isinstance(operational, dict):
            attrs["operational_state"] = operational.get("state")
        if isinstance(alert, dict):
            attrs["is_faulted"] = alert.get("isFaulted")
        return {key: value for key, value in attrs.items() if value is not None}

    @property
    def device_info(self) -> DeviceInfo:
        config = self.coordinator.data.get("device_configs", {}).get(self.definition.device_id, {})
        loc = self.coordinator.location
        account = str(loc.get("accountNumber", "location"))
        model = config.get("model") if isinstance(config, dict) else None
        return DeviceInfo(
            identifiers={(DOMAIN, f"{account}_device_{self.definition.device_id}")},
            name=device_name(config),
            manufacturer="ADT",
            model=str(model) if model else device_type(config),
            via_device=(DOMAIN, f"location_{account}"),
        )


class _ADTPlusDeviceHealthSensor(
    CoordinatorEntity[ADTPlusCoordinator],
    BinarySensorEntity,
):
    """Base class for physical-device diagnostic binary sensors."""

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


class ADTPlusLowBatterySensor(_ADTPlusDeviceHealthSensor):
    """ADT explicit low-battery/replacement state."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

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
            "low_battery",
            "Low Battery",
        )

    @property
    def is_on(self) -> bool | None:
        return device_low_battery(self._status_device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        found = device_battery_service(self._status_device)
        if found is None:
            return {}
        _, svc = found
        return {
            key: value
            for key, value in {
                "battery_percentage": svc.get("batteryPercentage"),
                "battery_level": svc.get("batteryLevel"),
                "battery_volts": svc.get("batteryVolts"),
                "battery_expected": svc.get("batteryExpected"),
                "needs_replacement": svc.get("needsReplacement"),
            }.items()
            if value is not None
        }


class ADTPlusTamperSensor(_ADTPlusDeviceHealthSensor):
    """Physical tamper status reported by ADT."""

    _attr_device_class = getattr(
        BinarySensorDeviceClass,
        "TAMPER",
        BinarySensorDeviceClass.PROBLEM,
    )

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
            "tamper",
            "Sensor Tamper",
        )

    @property
    def is_on(self) -> bool | None:
        svc = device_tamper_service(self._status_device)
        if not isinstance(svc, dict):
            return None
        value = svc.get("state")
        return value if isinstance(value, bool) else None


class ADTPlusTroubleSensor(_ADTPlusDeviceHealthSensor):
    """ADT securitySensor trouble/lost diagnostic."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

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
            "trouble",
            "Device Health",
        )

    @property
    def is_on(self) -> bool | None:
        return device_is_troubled(self._status_device)


class ADTPlusLockJamSensor(_ADTPlusDeviceHealthSensor):
    """Lock command did not reach ADT's secured state in time."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

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
            "lock_jam",
            "Lock Jam",
        )

    @property
    def _diagnostic(self) -> dict[str, Any]:
        value = self.coordinator.data.get(
            "lock_diagnostics",
            {},
        ).get(self.device_id)
        return value if isinstance(value, dict) else {}

    @property
    def is_on(self) -> bool:
        return self._diagnostic.get("jammed") is True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        diagnostic = self._diagnostic
        lock_service = device_lock_service(self._status_device)

        attrs: dict[str, Any] = {
            "pending": diagnostic.get("pending"),
            "last_command": diagnostic.get("last_command"),
            "last_result": diagnostic.get("last_result"),
            "confirmed_mode": diagnostic.get("confirmed_mode"),
        }

        if isinstance(lock_service, dict):
            attrs.update(
                {
                    "adt_mode": lock_service.get("mode"),
                    "adt_target_mode": lock_service.get("targetMode"),
                    "adt_lock_timeout": lock_service.get("lockTimeout"),
                    "door_closed": lock_service.get("doorClosed"),
                    "bolt_retracted": lock_service.get("boltRetracted"),
                }
            )

        return {
            key: value
            for key, value in attrs.items()
            if value is not None
        }


class ADTPlusBaseConnectivitySensor(CoordinatorEntity[ADTPlusCoordinator], BinarySensorEntity):
    """Base-cloud connectivity reported by systemStatus."""

    _attr_has_entity_name = True
    _attr_name = "Base Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: ADTPlusCoordinator) -> None:
        super().__init__(coordinator)
        account = str(coordinator.location.get("accountNumber", "location"))
        self._attr_unique_id = f"{account}_base_connection"

    @property
    def is_on(self) -> bool | None:
        value = self._system_status.get("baseConnected")
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._system_status
        return {
            key: value
            for key, value in {
                "connection_type": status.get("connectionType"),
                "firmware_version": status.get("baseFirmwareVersion"),
                "api_version": status.get("baseApiVersion"),
                "firmware_upgrading": status.get("baseFirmwareUpgrading"),
                "last_connect_time": status.get("lastConnectTime"),
                "last_disconnect_time": status.get("lastDisconnectTime"),
            }.items()
            if value is not None
        }

    @property
    def device_info(self) -> DeviceInfo:
        loc = self.coordinator.location
        account = str(loc.get("accountNumber", "location"))
        location_name = str(loc.get("locationName") or "ADT+")
        return DeviceInfo(
            identifiers={(DOMAIN, f"location_{account}")},
            name=f"ADT+ {location_name}",
            manufacturer="ADT",
            model="ADT+ SRV1",
        )

    @property
    def _system_status(self) -> dict[str, Any]:
        value = self.coordinator.data.get("system_status")
        return value if isinstance(value, dict) else {}


def _device_class(
    sensor_type: str,
    placement: str | None,
) -> BinarySensorDeviceClass | None:
    """Return a Home Assistant device class without assuming enum members."""

    def cls(
        name: str,
        fallback: str | None = None,
    ) -> BinarySensorDeviceClass | None:
        value = getattr(BinarySensorDeviceClass, name, None)
        if value is not None:
            return value
        if fallback is not None:
            return getattr(BinarySensorDeviceClass, fallback, None)
        return None

    if sensor_type in ("openClose", "openCloseReed"):
        if placement == "door":
            return cls("DOOR", "OPENING")
        if placement == "window":
            return cls("WINDOW", "OPENING")
        return cls("OPENING")

    if sensor_type == "openCloseShock":
        return cls("VIBRATION", "PROBLEM")

    if sensor_type == "motion":
        return cls("MOTION", "OCCUPANCY")

    if sensor_type == "smoke":
        return cls("SMOKE", "SAFETY")

    if sensor_type == "carbonMonoxide":
        # Home Assistant names this enum member CO, while the serialized
        # device-class value remains "carbon_monoxide".
        return cls("CO", "SAFETY")

    if sensor_type == "heat":
        return cls("HEAT", "PROBLEM")

    if sensor_type == "tamper":
        return cls("TAMPER", "PROBLEM")

    if sensor_type == "siren":
        return cls("SOUND", "SAFETY")

    return None

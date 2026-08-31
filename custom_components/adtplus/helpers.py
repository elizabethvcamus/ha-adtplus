"""Helpers for turning ADT+ SRV1 JSON into Home Assistant entity data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def object_id(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("deviceId", obj.get("id"))
    return str(value) if value is not None else None


def payload_of(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload")
    return payload if isinstance(payload, dict) else message


def list_from_payload(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def units(obj: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(obj, dict):
        return []
    value = obj.get("units")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def services(unit: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(unit, dict):
        return []
    value = unit.get("services")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def service_type(wrapper: dict[str, Any]) -> str | None:
    value = wrapper.get("type")
    return str(value) if value is not None else None


def service_payload(wrapper: dict[str, Any]) -> dict[str, Any]:
    value = wrapper.get("service")
    return value if isinstance(value, dict) else wrapper


def find_service(unit: dict[str, Any] | None, wanted: str) -> dict[str, Any] | None:
    for wrapper in services(unit):
        if service_type(wrapper) == wanted:
            return service_payload(wrapper)
    return None


def find_service_any_unit(device: dict[str, Any] | None, wanted: str) -> dict[str, Any] | None:
    for unit in units(device):
        found = find_service(unit, wanted)
        if found is not None:
            return found
    return None


def device_name(device: dict[str, Any] | None) -> str:
    if isinstance(device, dict):
        value = device.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "ADT+ Device"


def device_type(device: dict[str, Any] | None) -> str | None:
    if not isinstance(device, dict):
        return None
    value = device.get("translatedDeviceType") or device.get("deviceType") or device.get("type")
    return str(value) if value is not None else None


@dataclass(frozen=True)
class SensorDefinition:
    """One security sensor zone inside a physical ADT+ device."""

    device_id: str
    unit_index: int
    sensor_service_index: int
    zone_id: str
    sensor_type: str
    placement: str | None
    name: str

    @property
    def key(self) -> str:
        return f"{self.device_id}:{self.unit_index}:{self.sensor_service_index}:{self.zone_id}"


def sensor_definitions(data: dict[str, Any]) -> list[SensorDefinition]:
    """Build one entity definition for each securitySensor service."""
    configs: dict[str, dict[str, Any]] = data.get("device_configs", {})
    result: list[SensorDefinition] = []

    for dev_id, config in configs.items():
        raw_defs: list[tuple[int, int, dict[str, Any]]] = []
        for unit_index, unit in enumerate(units(config)):
            for service_index, wrapper in enumerate(services(unit)):
                if service_type(wrapper) != "securitySensor":
                    continue
                svc = service_payload(wrapper)
                if svc.get("enabled") is False:
                    continue
                raw_defs.append((unit_index, service_index, svc))

        if not raw_defs:
            continue

        base_name = device_name(config)
        multiple = len(raw_defs) > 1

        for unit_index, service_index, svc in raw_defs:
            sensor_type = str(svc.get("sensorType") or "security")

            # Premium ADT contact sensors can advertise openCloseShock as a
            # configuration capability, but SRV1 does not provide a persistent
            # Boolean shock state for it. Creating a binary sensor therefore
            # leaves a permanent "Shock: Unknown" entity. Real shock history
            # is exposed separately through ShockStatusService as Last Shock.
            if sensor_type == "openCloseShock":
                continue

            zone_id = str(svc.get("zoneId") if svc.get("zoneId") is not None else unit_index)
            placement = svc.get("placement")
            suffix = _friendly_sensor_type(sensor_type)
            name = f"{base_name} {suffix}" if multiple else base_name
            result.append(
                SensorDefinition(
                    device_id=str(dev_id),
                    unit_index=unit_index,
                    sensor_service_index=service_index,
                    zone_id=zone_id,
                    sensor_type=sensor_type,
                    placement=str(placement) if placement is not None else None,
                    name=name,
                )
            )

    return result


def _friendly_sensor_type(sensor_type: str) -> str:
    mapping = {
        "openClose": "Contact",
        "openCloseReed": "Reed Contact",
        "openCloseShock": "Shock",
        "motion": "Motion",
        "smoke": "Smoke",
        "carbonMonoxide": "Carbon Monoxide",
        "heat": "Heat",
        "tamper": "Tamper",
        "siren": "Siren",
    }
    return mapping.get(sensor_type, sensor_type.replace("_", " ").title())


def sensor_runtime(
    data: dict[str, Any], definition: SensorDefinition
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return config service, alert status, and security status for one zone."""
    configs: dict[str, dict[str, Any]] = data.get("device_configs", {})
    statuses: dict[str, dict[str, Any]] = data.get("device_statuses", {})

    config = configs.get(definition.device_id)
    status = statuses.get(definition.device_id)

    config_units = units(config)
    status_units = units(status)

    config_unit = config_units[definition.unit_index] if definition.unit_index < len(config_units) else None
    status_unit = status_units[definition.unit_index] if definition.unit_index < len(status_units) else None

    config_sensor = find_service(config_unit, "securitySensor")
    alert_status = find_service(status_unit, "alert")
    security_status = find_service(status_unit, "securitySensor")
    return config_sensor, alert_status, security_status



def all_services(device: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Return all typed runtime services across a device's units."""
    result: list[tuple[str, dict[str, Any]]] = []
    for unit in units(device):
        for wrapper in services(unit):
            kind = service_type(wrapper)
            if kind is None:
                continue
            result.append((kind, service_payload(wrapper)))
    return result


def device_battery_service(
    device: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    """Return ADT batteryPower or zwaveBattery service if present."""
    for kind in ("batteryPower", "zwaveBattery"):
        service = find_service_any_unit(device, kind)
        if isinstance(service, dict):
            return kind, service
    return None


def device_tamper_service(
    device: dict[str, Any] | None,
) -> dict[str, Any] | None:
    return find_service_any_unit(device, "tamper")


def device_is_troubled(
    device: dict[str, Any] | None,
) -> bool | None:
    """Combine explicit ADT security trouble/lost state for diagnostics."""
    found = False
    troubled = False

    for kind, service in all_services(device):
        if kind == "securitySensor" and "troubled" in service:
            value = service.get("troubled")
            if isinstance(value, bool):
                found = True
                troubled = troubled or value

        if kind in ("lost", "zwaveLost"):
            value = service.get("isLost")
            if not isinstance(value, bool):
                value = service.get("state")
            if isinstance(value, bool):
                found = True
                troubled = troubled or value

    return troubled if found else None


def device_low_battery(
    device: dict[str, Any] | None,
) -> bool | None:
    """Use ADT's explicit low/critical/replacement battery indicators."""
    found = device_battery_service(device)
    if found is None:
        return None

    kind, service = found

    if kind == "zwaveBattery":
        value = service.get("needsReplacement")
        return value if isinstance(value, bool) else None

    level = service.get("batteryLevel")
    if isinstance(level, str):
        normalized = level.strip().lower()
        if normalized in {
            "low",
            "critical",
            "replace",
            "replacement",
            "needsreplacement",
        }:
            return True
        if normalized in {
            "normal",
            "ok",
            "good",
            "high",
            "medium",
            "full",
            "fullycharged",
            "fully_charged",
        }:
            return False

    # No arbitrary percentage threshold is guessed here; if ADT does not
    # provide an explicit low state, expose the percentage separately.
    return None



def device_shock_service(
    device: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the ADT shock runtime service, if the device has one."""
    return find_service_any_unit(device, "shock")


def battery_percentage(
    device: dict[str, Any] | None,
) -> int | None:
    """Return a real percentage only when ADT actually supplies one."""
    found = device_battery_service(device)
    if found is None:
        return None

    _, service = found
    value = service.get("batteryPercentage")

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = int(round(value))
        return max(0, min(100, number))

    return None


def battery_level_text(
    device: dict[str, Any] | None,
) -> str | None:
    """Return ADT's categorical battery level, e.g. full/medium/low."""
    found = device_battery_service(device)
    if found is None:
        return None

    _, service = found
    value = service.get("batteryLevel")
    if not isinstance(value, str) or not value.strip():
        return None

    return value.strip()


def shock_last_alert(
    device: dict[str, Any] | None,
) -> str | None:
    """Return ADT ShockStatusService.lastAlertTimestamp when present."""
    service = device_shock_service(device)
    if not isinstance(service, dict):
        return None

    value = service.get("lastAlertTimestamp")
    if isinstance(value, str) and value.strip():
        return value.strip()

    return None



def device_lock_service(
    device: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the ADT Z-Wave door-lock runtime service, if present."""
    for kind in ("zwaveDoorLock", "doorLock"):
        service = find_service_any_unit(device, kind)
        if isinstance(service, dict):
            return service
    return None


def device_lock_mode(
    device: dict[str, Any] | None,
) -> str | None:
    """Return ADT's raw door-lock mode."""
    service = device_lock_service(device)
    if not isinstance(service, dict):
        return None

    value = service.get("mode")
    if not isinstance(value, str) or not value.strip():
        return None

    return value.strip()

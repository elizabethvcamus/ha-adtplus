"ADT+ custom integration."

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ADTPlusConnectionError
from .const import DOMAIN
from .coordinator import ADTPlusCoordinator

PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.SENSOR,
]

EMERGENCY_SERVICES = {
    "police_emergency": "police",
    "medical_emergency": "medical",
    "fire_emergency": "fire",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up ADT+ from a config entry."""
    session = async_get_clientsession(hass)
    coordinator = ADTPlusCoordinator(
        hass,
        entry,
        session,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[
        entry.entry_id
    ] = coordinator

    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    _register_emergency_services(hass)
    return True


def _register_emergency_services(hass: HomeAssistant) -> None:
    """Register monitored emergency actions once."""

    for service_name, panic_type in EMERGENCY_SERVICES.items():
        if hass.services.has_service(DOMAIN, service_name):
            continue

        async def handler(
            call: ServiceCall,
            requested_type: str = panic_type,
        ) -> None:
            if call.data.get("confirm") is not True:
                raise HomeAssistantError(
                    "Emergency signal was NOT sent. "
                    "Set confirm=true to intentionally send it."
                )

            coordinators = hass.data.get(DOMAIN, {})
            entry_id = call.data.get("config_entry_id")

            if entry_id:
                coordinator = coordinators.get(str(entry_id))
                if coordinator is None:
                    raise HomeAssistantError(
                        "The selected ADT+ config entry is not loaded"
                    )
            else:
                loaded = list(coordinators.values())
                if len(loaded) != 1:
                    raise HomeAssistantError(
                        "More than one ADT+ hub is loaded; "
                        "specify config_entry_id"
                    )
                coordinator = loaded[0]

            partition_id = call.data.get("partition_id")
            if partition_id is None:
                parts = sorted(
                    coordinator.data.get(
                        "partition_statuses", {}
                    )
                )
                if not parts:
                    raise HomeAssistantError(
                        "No ADT+ partition is available"
                    )
                partition_id = parts[0]

            try:
                await coordinator.async_panic_request(
                    str(partition_id),
                    requested_type,
                )
            except ADTPlusConnectionError as err:
                raise HomeAssistantError(str(err)) from err

        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            schema=vol.Schema(
                {
                    vol.Required("confirm"): bool,
                    vol.Optional("partition_id"): str,
                    vol.Optional("config_entry_id"): str,
                }
            ),
        )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload ADT+."""
    coordinator: ADTPlusCoordinator = hass.data[
        DOMAIN
    ][entry.entry_id]

    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unloaded:
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id)

        if not hass.data[DOMAIN]:
            for service_name in EMERGENCY_SERVICES:
                hass.services.async_remove(
                    DOMAIN,
                    service_name,
                )

    return unloaded

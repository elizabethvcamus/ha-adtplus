"ADT+ alarm-control-panel entities with SRV1 arm/disarm control."

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ADTPlusConnectionError
from .const import DOMAIN
from .coordinator import ADTPlusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one alarm-control-panel entity per ADT+ partition."""
    coordinator: ADTPlusCoordinator = hass.data[DOMAIN][entry.entry_id]
    partition_ids = sorted(coordinator.data.get("partition_statuses", {}))
    async_add_entities(
        ADTPlusAlarmEntity(coordinator, pid) for pid in partition_ids
    )


class ADTPlusAlarmEntity(
    CoordinatorEntity[ADTPlusCoordinator],
    AlarmControlPanelEntity,
):
    """Representation of one ADT+ security partition."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    _attr_code_format = CodeFormat.NUMBER

    # Requiring the code for arm actions makes the first control release work
    # consistently even on accounts where ADT one-touch arming is disabled.
    # The integration does not save the code.
    _attr_code_arm_required = True

    def __init__(
        self,
        coordinator: ADTPlusCoordinator,
        partition_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.partition_id = partition_id
        loc = coordinator.location
        account = str(loc.get("accountNumber", "location"))
        self._attr_unique_id = f"{account}_partition_{partition_id}"

        # Friendly default requested for the primary partition. The raw ADT
        # partition id is retained internally for state updates and commands.
        if str(partition_id) == "0":
            self._attr_name = "ADT+ Base"
        else:
            config = coordinator.data.get(
                "partition_configs", {}
            ).get(partition_id, {})
            config_name = (
                config.get("name")
                if isinstance(config, dict)
                else None
            )
            self._attr_name = (
                str(config_name)
                if config_name
                else f"ADT+ Base {partition_id}"
            )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Translate the ADT partition state into Home Assistant."""
        part = self._status

        # ADT exposes this independently of its textual armState.
        if part.get("localAlarming") is True:
            return AlarmControlPanelState.TRIGGERED

        raw = part.get("armState")
        if not isinstance(raw, str):
            return None

        mapping = {
            "ready": AlarmControlPanelState.DISARMED,
            "notReady": AlarmControlPanelState.DISARMED,
            "notready": AlarmControlPanelState.DISARMED,
            "notArmed": AlarmControlPanelState.DISARMED,
            "allArmedDisarmed": AlarmControlPanelState.DISARMED,
            "disarm": AlarmControlPanelState.DISARMED,
            "armedStay": AlarmControlPanelState.ARMED_HOME,
            "armstay": AlarmControlPanelState.ARMED_HOME,
            "armedStayInstant": AlarmControlPanelState.ARMED_HOME,
            "armstayinst": AlarmControlPanelState.ARMED_HOME,
            "armedAway": AlarmControlPanelState.ARMED_AWAY,
            "armaway": AlarmControlPanelState.ARMED_AWAY,
            "armedAwayInstant": AlarmControlPanelState.ARMED_AWAY,
            "armawayinst": AlarmControlPanelState.ARMED_AWAY,
            "armedNight": AlarmControlPanelState.ARMED_NIGHT,
            "entryDelay": AlarmControlPanelState.PENDING,
            "entrydelay": AlarmControlPanelState.PENDING,
            "exitDelay": AlarmControlPanelState.ARMING,
            "exitdelay": AlarmControlPanelState.ARMING,
            "exitErrorEntryDelay": AlarmControlPanelState.PENDING,
            "exiterrordelay": AlarmControlPanelState.PENDING,
            "alarmed": AlarmControlPanelState.TRIGGERED,
            "alarm": AlarmControlPanelState.TRIGGERED,
        }
        return mapping.get(raw, mapping.get(raw.lower()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful raw ADT partition state without sensitive data."""
        part = self._status
        config = self.coordinator.data.get(
            "partition_configs", {}
        ).get(self.partition_id, {})
        return {
            "adt_partition_id": self.partition_id,
            "adt_arm_state": part.get("armState"),
            "future_arm_state": part.get("futureArmState"),
            "ready_to_arm": part.get("armState") == "ready",
            "local_alarming": part.get("localAlarming"),
            "trouble_beeping": part.get("troubleBeeping"),
            "exit_delay_seconds": (
                config.get("exitDelaySeconds")
                if isinstance(config, dict)
                else None
            ),
            "chime_enabled": (
                config.get("chimeEnabled")
                if isinstance(config, dict)
                else None
            ),
        }

    @property
    def device_info(self) -> DeviceInfo:
        loc = self.coordinator.location
        account = str(loc.get("accountNumber", "location"))
        location_name = str(loc.get("locationName") or "Home")
        return DeviceInfo(
            identifiers={(DOMAIN, f"location_{account}")},
            name=f"ADT+ {location_name}",
            manufacturer="ADT",
            model="ADT+ SRV1",
        )

    async def async_alarm_arm_home(
        self,
        code: str | None = None,
    ) -> None:
        """Arm ADT+ Stay."""
        await self._async_send_arm_action("stay", code)

    async def async_alarm_arm_away(
        self,
        code: str | None = None,
    ) -> None:
        """Arm ADT+ Away."""
        await self._async_send_arm_action("away", code)

    async def async_alarm_arm_night(
        self,
        code: str | None = None,
    ) -> None:
        """Arm ADT+ Night."""
        await self._async_send_arm_action("night", code)

    async def async_alarm_disarm(
        self,
        code: str | None = None,
    ) -> None:
        """Disarm ADT+."""
        await self._async_send_arm_action("disarm", code)

    async def _async_send_arm_action(
        self,
        arm_type: str,
        code: str | None,
    ) -> None:
        """Validate the one-time code and send the command."""
        clean_code = str(code).strip() if code is not None else ""
        if not clean_code:
            raise HomeAssistantError(
                "ADT+ requires the alarm code for this action"
            )

        if not clean_code.isdigit():
            raise HomeAssistantError(
                "ADT+ alarm code must contain only numbers"
            )

        try:
            await self.coordinator.async_arm_state_change(
                self.partition_id,
                arm_type,
                clean_code,
            )
        except ADTPlusConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def _status(self) -> dict[str, Any]:
        value = self.coordinator.data.get(
            "partition_statuses", {}
        ).get(self.partition_id)
        return value if isinstance(value, dict) else {}

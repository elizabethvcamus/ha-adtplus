"Config flow for ADT+."

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ADTPlusAPI,
    ADTPlusAuthError,
    ADTPlusConnectionError,
)
from .const import (
    CONF_LOCATION,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)


def _token_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_REFRESH_TOKEN
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD
                )
            )
        }
    )


class ADTPlusConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Set up ADT+ from an Auth0 refresh token."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str | None = None
        self._locations: list[dict[str, Any]] = []
        self._reauth_entry: ConfigEntry | None = None

    async def _validate_token(
        self,
        refresh_token: str,
    ) -> tuple[
        ADTPlusAPI,
        list[dict[str, Any]],
    ]:
        session = async_get_clientsession(self.hass)
        api = ADTPlusAPI(
            session,
            refresh_token,
        )
        await api.async_refresh_access_token()
        locations = await api.async_get_locations()
        return api, locations

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = str(
                user_input[CONF_REFRESH_TOKEN]
            ).strip()

            try:
                api, locations = await self._validate_token(
                    refresh_token
                )
            except ADTPlusAuthError:
                errors["base"] = "invalid_auth"
            except ADTPlusConnectionError:
                errors["base"] = "cannot_connect"
            else:
                if not locations:
                    errors["base"] = "no_locations"
                else:
                    # Auth0 may rotate during validation. Save the token
                    # actually returned by Auth0, never the consumed input.
                    self._refresh_token = api.refresh_token
                    self._locations = locations

                    if len(locations) == 1:
                        return await self._create_for_location(
                            locations[0]
                        )

                    return await self.async_step_location()

        return self.async_show_form(
            step_id="user",
            data_schema=_token_schema(),
            errors=errors,
        )

    async def async_step_location(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        if (
            not self._locations
            or self._refresh_token is None
        ):
            return self.async_abort(
                reason="missing_auth"
            )

        choices = {
            str(index): _location_label(location)
            for index, location in enumerate(
                self._locations
            )
        }

        if user_input is not None:
            index = int(user_input["location"])
            return await self._create_for_location(
                self._locations[index]
            )

        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Required("location"): vol.In(
                        choices
                    )
                }
            ),
        )

    async def _create_for_location(
        self,
        location: dict[str, Any],
    ) -> FlowResult:
        assert self._refresh_token is not None

        account = str(
            location.get("accountNumber")
            or location.get("laneId")
            or "adtplus"
        )

        await self.async_set_unique_id(account)
        self._abort_if_unique_id_configured()

        title = str(
            location.get("locationName")
            or "ADT+"
        )

        return self.async_create_entry(
            title=title,
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_LOCATION: location,
            },
        )

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> FlowResult:
        """Start Home Assistant reauthentication."""
        entry_id = self.context.get("entry_id")
        if not isinstance(entry_id, str):
            return self.async_abort(
                reason="missing_auth"
            )

        self._reauth_entry = (
            self.hass.config_entries.async_get_entry(
                entry_id
            )
        )

        if self._reauth_entry is None:
            return self.async_abort(
                reason="missing_auth"
            )

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Accept and validate a replacement refresh token."""
        errors: dict[str, str] = {}

        if self._reauth_entry is None:
            return self.async_abort(
                reason="missing_auth"
            )

        if user_input is not None:
            refresh_token = str(
                user_input[CONF_REFRESH_TOKEN]
            ).strip()

            try:
                api, locations = await self._validate_token(
                    refresh_token
                )
            except ADTPlusAuthError:
                errors["base"] = "invalid_auth"
            except ADTPlusConnectionError:
                errors["base"] = "cannot_connect"
            else:
                if not locations:
                    errors["base"] = "no_locations"
                else:
                    old_location = dict(
                        self._reauth_entry.data.get(
                            CONF_LOCATION,
                            {},
                        )
                    )
                    old_account = str(
                        old_location.get(
                            "accountNumber"
                        )
                        or ""
                    )

                    matched = None
                    if old_account:
                        for location in locations:
                            if str(
                                location.get(
                                    "accountNumber"
                                )
                                or ""
                            ) == old_account:
                                matched = location
                                break

                    if matched is None:
                        # One-location accounts are safe to recover even if
                        # ADT changed metadata such as the lane.
                        if len(locations) == 1:
                            matched = locations[0]
                        else:
                            errors["base"] = (
                                "location_mismatch"
                            )

                    if matched is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data_updates={
                                CONF_REFRESH_TOKEN:
                                    api.refresh_token,
                                CONF_LOCATION: matched,
                            },
                            reason="reauth_successful",
                        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_token_schema(),
            errors=errors,
        )


def _location_label(
    location: dict[str, Any],
) -> str:
    name = str(
        location.get("locationName")
        or "ADT+ Location"
    )

    city = location.get("city")
    state = location.get("state")
    area = ", ".join(
        str(value)
        for value in (city, state)
        if value
    )

    return f"{name} — {area}" if area else name

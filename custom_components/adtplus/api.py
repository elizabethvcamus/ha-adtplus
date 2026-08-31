"Small async client for the ADT+ endpoints used by this integration."

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import logging
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError, ClientSession, WSMsgType

from .const import (
    APP_NAME,
    APP_VERSION,
    AUTH0_CLIENT_ID,
    AUTH0_TOKEN_URL,
    LOCATIONS_URL,
    LS_APP_VERSION,
    PUSH_API_VERSION,
    PUSH_BASE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class ADTPlusError(Exception):
    """Base ADT+ error."""


class ADTPlusAuthError(ADTPlusError):
    """Authentication failed."""


class ADTPlusConnectionError(ADTPlusError):
    """Connection to ADT+ failed."""


RefreshTokenCallback = Callable[[str], None]


class ADTPlusAPI:
    """ADT+ API client using the app's Auth0 refresh token and SRV1 push feed."""

    def __init__(
        self,
        session: ClientSession,
        refresh_token: str,
        on_refresh_token: RefreshTokenCallback | None = None,
    ) -> None:
        self._session = session
        self._on_refresh_token = on_refresh_token
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self._ws = None
        self._send_lock = asyncio.Lock()

    async def async_refresh_access_token(self) -> str:
        """Exchange the saved refresh token for a fresh access token."""
        data = {
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": self.refresh_token,
        }

        try:
            async with self._session.post(
                AUTH0_TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            ) as response:
                text = await response.text()
                if response.status in (400, 401, 403):
                    raise ADTPlusAuthError(
                        f"Auth0 refresh rejected the token (HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise ADTPlusConnectionError(
                        f"Auth0 refresh failed (HTTP {response.status}): {text[:300]}"
                    )
                payload = json.loads(text)
        except ADTPlusError:
            raise
        except (ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to refresh ADT+ token: {err}"
            ) from err

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ADTPlusAuthError(
                "Auth0 response did not include an access token"
            )

        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != self.refresh_token:
            self.refresh_token = rotated

            # Persist the rotated token immediately. Do not wait for the
            # WebSocket to connect or yield its first message: Auth0 may have
            # already invalidated the token that was just exchanged.
            if self._on_refresh_token is not None:
                self._on_refresh_token(rotated)

        self.access_token = access_token
        return access_token

    async def async_get_locations(self) -> list[dict[str, Any]]:
        """Return the account's ADT+ locations."""
        token = self.access_token or await self.async_refresh_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "LsAppVersion": LS_APP_VERSION,
            "x-adt-rid": str(uuid4()),
        }

        try:
            async with self._session.get(
                LOCATIONS_URL,
                headers=headers,
                timeout=30,
            ) as response:
                text = await response.text()
                if response.status == 401:
                    token = await self.async_refresh_access_token()
                    return await self._async_get_locations_with_token(token)
                if response.status in (403,):
                    raise ADTPlusAuthError(
                        "ADT+ rejected the authenticated location request "
                        f"(HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise ADTPlusConnectionError(
                        f"Location lookup failed (HTTP {response.status}): "
                        f"{text[:300]}"
                    )
                payload = json.loads(text)
        except ADTPlusError:
            raise
        except (ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to load ADT+ locations: {err}"
            ) from err

        return _extract_locations(payload)

    async def _async_get_locations_with_token(
        self,
        token: str,
    ) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "LsAppVersion": LS_APP_VERSION,
            "x-adt-rid": str(uuid4()),
        }
        try:
            async with self._session.get(
                LOCATIONS_URL,
                headers=headers,
                timeout=30,
            ) as response:
                text = await response.text()
                if response.status in (401, 403):
                    raise ADTPlusAuthError(
                        "ADT+ rejected the authenticated location request "
                        f"(HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise ADTPlusConnectionError(
                        f"Location lookup failed (HTTP {response.status}): "
                        f"{text[:300]}"
                    )
                return _extract_locations(json.loads(text))
        except ADTPlusError:
            raise
        except (ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to load ADT+ locations: {err}"
            ) from err

    async def async_push_messages(
        self,
        location: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Connect to the SRV1 push feed and yield decoded server messages."""
        # Reuse a current access token when one was obtained during setup.
        # On reconnect the coordinator clears access_token, causing exactly
        # one fresh Auth0 exchange here.
        token = self.access_token or await self.async_refresh_access_token()

        lane_id = str(location.get("laneId") or "").strip()
        vin = location.get("accountNumber")
        if not lane_id or vin is None:
            raise ADTPlusConnectionError(
                "Selected ADT+ location has no lane/account identifier"
            )

        try:
            vin_value = int(vin)
        except (TypeError, ValueError) as err:
            raise ADTPlusConnectionError(
                "ADT+ account identifier was not numeric"
            ) from err

        url = f"{PUSH_BASE}/{lane_id}/push/{PUSH_API_VERSION}"
        headers = {"User-Agent": USER_AGENT}

        try:
            ws = await self._session.ws_connect(
                url,
                headers=headers,
                heartbeat=30,
                autoping=True,
                timeout=30,
            )
        except (ClientError, asyncio.TimeoutError) as err:
            raise ADTPlusConnectionError(
                f"Unable to open ADT+ push WebSocket: {err}"
            ) from err

        self._ws = ws

        login = {
            "requestId": str(uuid4()),
            "payload": {
                "applicationName": APP_NAME,
                "applicationVersion": APP_VERSION,
                "vin": vin_value,
                "jwt": token,
                "hppConfigTimestamp": 0,
                "ibpConfigTimestamp": 0,
                "ruleConfigTimestamp": 0,
                "lastEventTimestamp": 0,
                "lastMediaId": 0,
            },
            "type": "loginWithJWT",
        }

        try:
            # Never log this object: it contains the bearer JWT.
            await ws.send_json(login)

            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        decoded = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(decoded, dict):
                        yield decoded
                elif message.type in (WSMsgType.CLOSED, WSMsgType.CLOSE):
                    break
                elif message.type == WSMsgType.ERROR:
                    err = ws.exception()
                    raise ADTPlusConnectionError(
                        "ADT+ push WebSocket error: "
                        f"{err or 'unknown error'}"
                    )
        except ClientResponseError as err:
            raise ADTPlusConnectionError(
                f"ADT+ push connection failed: {err}"
            ) from err
        finally:
            if self._ws is ws:
                self._ws = None
            await ws.close()

    async def async_arm_state_change(
        self,
        partition_id: str | int,
        arm_type: str,
        code: str | None,
    ) -> str:
        """Send the SRV1 armStateChange request used by the ADT+ app.

        arm_type must be one of: stay, away, disarm.
        The security code is never stored by this API client.
        """
        if arm_type not in {"stay", "away", "night", "disarm"}:
            raise ValueError(f"Unsupported ADT+ arm type: {arm_type}")

        try:
            partition = int(partition_id)
        except (TypeError, ValueError) as err:
            raise ADTPlusConnectionError(
                f"Invalid ADT+ partition id: {partition_id}"
            ) from err

        ws = self._ws
        if ws is None or ws.closed:
            raise ADTPlusConnectionError(
                "ADT+ push connection is not currently available"
            )

        user_code = None
        if code is not None:
            code = str(code).strip()
            if code:
                user_code = code

        request_id = str(uuid4())
        request = {
            "requestId": request_id,
            "payload": {
                "partitions": [partition],
                "armType": arm_type,
                "userCode": user_code,
                "userConfigId": None,
                "bypassAll": True,
                "local": False,
                "exitDelayEnabled": True,
                "usernameOverride": None,
            },
            "type": "armStateChange",
        }

        try:
            async with self._send_lock:
                await ws.send_json(request)
        except (ClientError, RuntimeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to send ADT+ alarm command: {err}"
            ) from err

        return request_id


    async def async_set_door_lock(
        self,
        device_id: str | int,
        unit_id: str | int | None,
        locked: bool,
        user_config_id: str | int | None = None,
    ) -> str:
        """Send the SRV1 Z-Wave doorLockMode haCommand used by ADT+.

        Decompiled ADT+ app flow:
          HaCommandRequest.DoorLock(
              userConfigId,
              deviceId,
              unitId,
              None,
              None,
              HaCommandDeviceMode("secured"|"unsecured", None),
          ).toSrv1Template()

        The security lock remains joined to ADT's Z-Wave network; this only
        asks ADT's live SRV1 connection to perform the same command as the app.
        """
        try:
            device = int(device_id)
        except (TypeError, ValueError) as err:
            raise ADTPlusConnectionError(
                f"Invalid ADT+ lock device id: {device_id}"
            ) from err

        unit: int | None = None
        if unit_id is not None:
            try:
                unit = int(unit_id)
            except (TypeError, ValueError) as err:
                raise ADTPlusConnectionError(
                    f"Invalid ADT+ lock unit id: {unit_id}"
                ) from err

        # ADT's app uses the logged-in user's userConfigId. Its own fallback
        # is 1 if a current user object is unavailable.
        user_config = 1
        if user_config_id is not None:
            try:
                user_config = int(user_config_id)
            except (TypeError, ValueError) as err:
                raise ADTPlusConnectionError(
                    f"Invalid ADT+ user config id: {user_config_id}"
                ) from err

        ws = self._ws
        if ws is None or ws.closed:
            raise ADTPlusConnectionError(
                "ADT+ push connection is not currently available"
            )

        request_id = str(uuid4())
        request = {
            "requestId": request_id,
            "payload": {
                "userConfigId": user_config,
                "type": "doorLockMode",
                "deviceId": device,
                "unitId": unit,
                "roomId": None,
                "partitionId": None,
                "commandData": {
                    "mode": "secured" if locked else "unsecured",
                    "manufacturerSpecific": None,
                },
            },
            "type": "haCommand",
        }

        try:
            async with self._send_lock:
                await ws.send_json(request)
        except (ClientError, RuntimeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to send ADT+ lock command: {err}"
            ) from err

        return request_id

    async def async_panic_request(
        self,
        partition_id: str | int,
        panic_type: str,
    ) -> str:
        """Send the SRV1 panicRequest used by the ADT+ app.

        Supported panic types are police, medical, and fire.
        """
        if panic_type not in {"police", "medical", "fire"}:
            raise ValueError(
                f"Unsupported ADT+ panic type: {panic_type}"
            )

        try:
            partition = int(partition_id)
        except (TypeError, ValueError) as err:
            raise ADTPlusConnectionError(
                f"Invalid ADT+ partition id: {partition_id}"
            ) from err

        ws = self._ws
        if ws is None or ws.closed:
            raise ADTPlusConnectionError(
                "ADT+ push connection is not currently available"
            )

        request_id = str(uuid4())
        request = {
            "requestId": request_id,
            "payload": {
                "type": panic_type,
                "partitions": [partition],
                "userConfigId": None,
            },
            "type": "panicRequest",
        }

        try:
            async with self._send_lock:
                await ws.send_json(request)
        except (ClientError, RuntimeError) as err:
            raise ADTPlusConnectionError(
                f"Unable to send ADT+ emergency signal: {err}"
            ) from err

        return request_id


def _extract_locations(payload: Any) -> list[dict[str, Any]]:
    """Extract locationList from the observed Pacecar response shapes."""
    if isinstance(payload, dict):
        locations = payload.get("locationList")
        if isinstance(locations, list):
            return [
                item for item in locations if isinstance(item, dict)
            ]
        nested = payload.get("data")
        if isinstance(nested, dict):
            locations = nested.get("locationList")
            if isinstance(locations, list):
                return [
                    item for item in locations if isinstance(item, dict)
                ]
    return []

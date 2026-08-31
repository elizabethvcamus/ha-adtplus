"""Constants for the ADT+ custom integration."""

DOMAIN = "adtplus"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_LOCATION = "location"

AUTH0_DOMAIN = "auth.prod.acs.adt.com"
AUTH0_CLIENT_ID = "ulXpiQCqkyGiRCTZYauYjvUv46M8DeT4"
AUTH0_TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"

PACE_CAR_BASE = "https://racecar.platform.adt.com"
LOCATIONS_URL = f"{PACE_CAR_BASE}/pacecar/v202.47/user/locations"

PUSH_BASE = "wss://racecar.platform.adt.com"
PUSH_API_VERSION = "202.52"
REST_API_VERSION = "v202.47"

APP_NAME = "ADT+ A Play"
APP_VERSION = "4.6.0"
APP_BUILD_VERSION = "4.6.0.329951"
USER_AGENT = f"adtplusA|{APP_BUILD_VERSION}"
LS_APP_VERSION = USER_AGENT

INITIAL_DATA_TIMEOUT = 35
RECONNECT_MIN_SECONDS = 5
RECONNECT_MAX_SECONDS = 60

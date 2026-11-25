DOMAIN = "linktap"
STATUS_CMD = 3
START_CMD = 6
STOP_CMD = 7
CONFIG_CMD = 16
DISMISS_ALERT_CMD = 10
PAUSE_CMD = 18
TAP_ID = "ID of Tap"
GW_ID = "ID of Gateway"
GW_IP = "Local IP Address of Gateway"
NAME = "Friendly Name of Tap"
DEFAULT_TIME = 15
DEFAULT_NAME = "Linktap Local Integration"
DEFAULT_VOL = 0
PLATFORMS = ['number', 'binary_sensor', 'sensor', 'switch', 'valve']
ATTR_DEFAULT_TIME = 'Default Time'
ATTR_VOL = "Watering by Volume"
ATTR_DURATION = "Watering Duration"
ATTR_VOLUME = "Watering Volume"
ATTR_STATE = "is_watering"
MANUFACTURER = "Linktap"

# Local push. The gateway's "HTTP Client" POSTs status changes (plus a periodic
# keepalive) to a user-configured Server URL. Point it at this path on Home
# Assistant, e.g. http://<home-assistant>:8123/api/linktap/push
PUSH_URL = "/api/linktap/push"
# hass.data key for the push routing table (kept separate from per-entry data).
PUSH_DATA = "linktap_push"
# Gateway frames that carry the local date/time in their reply.
HANDSHAKE_CMD = 0
DATETIME_SYNC_CMD = 13
# Polling stays on as a fallback. While pushes keep arriving it is relaxed to
# PUSH_POLL_INTERVAL; if none arrives within PUSH_WATCHDOG the coordinator goes
# back to its normal polling cadence.
PUSH_POLL_INTERVAL_SECONDS = 60
PUSH_WATCHDOG_SECONDS = 180

# Optional per-device Home Assistant safety ceilings for instant watering.
# These do not alter LinkTap's own limits; they constrain requests made by
# this Home Assistant integration.
CONF_WATERING_SAFETY_LIMITS = "watering_safety_limits"
CONF_MAX_WATERING_DURATION = "max_watering_duration"
CONF_MAX_WATERING_VOLUME = "max_watering_volume"

# Existing integration/native entity maxima. Leaving an option unset preserves
# these existing limits and therefore preserves current behaviour.
NATIVE_MAX_WATERING_DURATION = 120
NATIVE_MAX_WATERING_VOLUME = 2000

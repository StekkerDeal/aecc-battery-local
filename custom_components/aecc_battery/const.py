"""Constants for the AECC Battery (Local TCP) integration."""

DOMAIN = "aecc_battery"

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"
# Legacy symmetric 800/2400 toggle; read for backward compatibility, never
# written since v1.5.2 (superseded by the per-direction limits below).
CONF_EXTENDED_POWER = "extended_power"
CONF_MAX_CHARGE_POWER = "max_charge_power"
CONF_MAX_DISCHARGE_POWER = "max_discharge_power"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"

# Default connection values
DEFAULT_HOST = "192.168.0.1"
DEFAULT_PORT = 8080
DEFAULT_NAME = "AECC Battery"
DEFAULT_TIMEOUT = 5  # seconds
POLL_INTERVAL = 5  # seconds - change this to update faster/slower
MIN_POLL_INTERVAL = 2  # seconds - hard floor to avoid flooding the device

# TCP reconnect backoff. On a connection error the client waits, then reconnects.
# The cooldown escalates per consecutive failure (base, base*2, base*4, …) capped
# at max, so a device that drops its single-session TCP server is not hammered
# every 2s for the whole outage. The first retry uses base, matching the previous
# flat behaviour, so a single transient blip is not slowed down.
RECONNECT_BASE_COOLDOWN = 2  # seconds - first retry delay
RECONNECT_MAX_COOLDOWN = 60  # seconds - cap on the escalating cooldown
READ_TIMEOUT_SUSPECT_THRESHOLD = 3  # consecutive GET read timeouts before recycling the socket
MAX_BATTERY_POWER_W = 2400  # watts - hardware rated max
MAX_REGISTER_POWER_DEFAULT = 800  # watts - default local TCP limit without extended power
# How often to re-read the WiFi RSSI (DeviceManagement reg 76). It is a separate
# TCP command with its own timeout, so we throttle it well below POLL_INTERVAL to
# avoid doubling traffic; RSSI changes slowly so 60s granularity is plenty.
WIFI_RSSI_REFRESH_INTERVAL = 60  # seconds

# Modbus TCP telemetry. The device answers plain Modbus frames on the same socket
# as the JSON protocol (dispatch is per frame). Read-only: no write function
# codes exist in this integration. The map is disjoint from the 3000+ JSON
# control registers and not every brand is known to expose it, so it is probed
# once at setup and the entities only exist when the probe answers.
MODBUS_UNIT_ID = 1
MODBUS_REFRESH_INTERVAL = 30  # seconds - temperatures and counters move slowly
MODBUS_READ_TIMEOUT = 3  # seconds - same budget as the DeviceManagement probe
# (start, count) blocks; every register below falls inside one of them.
MODBUS_BLOCKS: tuple[tuple[int, int], ...] = ((65030, 23), (30073, 3), (52050, 4), (52080, 2))
MB_TEMP_1, MB_TEMP_2, MB_TEMP_3 = 30073, 30074, 30075
MB_ENERGY_CHARGED, MB_ENERGY_DISCHARGED, MB_ENERGY_TO_GRID = 52050, 52052, 52080
MB_AVAILABLE_CHARGE_POWER = 65033
MB_NOMINAL_POWER, MB_NOMINAL_BATTERY_POWER = 65035, 65037
MB_DEVICE_STATUS = 65039  # 0 normal, 1 fault
MB_ALARM_FLAGS = 65051  # bit meanings unknown
# Decode table: register -> (kind, factor). kind is "i16", "u16" or "u32" (low word
# first, the opposite of the usual Modbus word order). Adding a register is a row here.
MODBUS_REGISTERS: dict[int, tuple[str, float]] = {
    MB_TEMP_1: ("i16", 0.1),
    MB_TEMP_2: ("i16", 0.1),
    MB_TEMP_3: ("i16", 0.1),
    MB_ENERGY_CHARGED: ("u32", 0.1),
    MB_ENERGY_DISCHARGED: ("u32", 0.1),
    MB_ENERGY_TO_GRID: ("u32", 0.1),
    MB_AVAILABLE_CHARGE_POWER: ("i16", 1),
    MB_NOMINAL_POWER: ("i16", 1),
    MB_NOMINAL_BATTERY_POWER: ("i16", 1),
    MB_DEVICE_STATUS: ("u16", 1),
    MB_ALARM_FLAGS: ("u32", 1),
}

# Known AECC brands (for config flow dropdown)
KNOWN_BRANDS = [
    "Lunergy",
    "Sunpura",
    "Voltdeer",
    "AEG",
    "AFERIY",
    "AccuMate",
    "JET",
    "Oscal",
    "Fossibot",
    "TSUN",
    "Other",
]

# Brands whose hardware is rated above the common 2400W. The options form offers
# the highest of these and rejects anything over the selected brand's own
# ceiling, so a 2400W unit can never be asked for more.
BRAND_MAX_POWER_W: dict[str, int] = {"TSUN": 2500}
MAX_BRAND_POWER_W = max([MAX_BATTERY_POWER_W, *BRAND_MAX_POWER_W.values()])


def max_power_for_brand(manufacturer: str | None) -> int:
    """Highest power (W) the integration will offer for this brand."""
    return BRAND_MAX_POWER_W.get(manufacturer or "", MAX_BATTERY_POWER_W)


# Brand whose firmware needs a different control-slot encoding (see REG_CONTROL_TIME1
# comment below and async_set_battery_control). Gate AEG-specific behaviour on this.
BRAND_AEG = "AEG"

# ─── Sensor cleaning profile ─────────────────────────────────────────────────
# Per-brand thresholds for the physics-aware SOC cleaner.
# - soc_zero_reject_during_active_w: reject SOC=0 readings when the absolute
#   wall-side power exceeds this threshold (battery is clearly in motion, so
#   SOC cannot have collapsed to 0 instantaneously).
# - soc_max_rate_pct_per_min: discard SOC readings whose change rate from the
#   last accepted sample exceeds this. Catches BMS step-jump glitches.
# - hold_last_value_seconds: how long an entity may keep returning its last
#   accepted value after readings start being rejected before going
#   "unavailable". Hybrid pattern: smooth charts for transient blips, honest
#   signal for prolonged sensor failure.
#
# Lunergy is the known-bad device (sustained SOC=0 lockups during active
# discharge). Sunpura / others are stable and get a permissive profile that
# only catches obvious physical impossibilities.
# ──────────────────────────────────────────────────────────────────────────────

CONF_BRAND_PROFILE_KEY = "brand_profile"

BRAND_PROFILES: dict[str, dict[str, float | int]] = {
    "Lunergy": {
        "soc_zero_reject_during_active_w": 50,
        "soc_max_rate_pct_per_min": 5.0,
        "hold_last_value_seconds": 120,
    },
    "Sunpura": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "Voltdeer": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "AEG": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "AFERIY": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "AccuMate": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "JET": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "Oscal": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "Fossibot": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "TSUN": {
        "soc_zero_reject_during_active_w": 200,
        "soc_max_rate_pct_per_min": 10.0,
        "hold_last_value_seconds": 120,
    },
    "Other": {
        "soc_zero_reject_during_active_w": 100,
        "soc_max_rate_pct_per_min": 8.0,
        "hold_last_value_seconds": 120,
    },
}

DEFAULT_BRAND_PROFILE: dict[str, float | int] = BRAND_PROFILES["Other"]

# ─── Control register addresses (confirmed by register scan) ─────────────────
REG_EMS_ENABLE = "3000"  # 0 = off, 1 = on
REG_SCHEDULE_MODE = "3020"  # Schedule mode (6 = custom schedule)
REG_AI_SMART_CHARGE = "3021"  # 0 = off, 1 = on
REG_AI_SMART_DISC = "3022"  # 0 = off, 1 = on
REG_CUSTOM_MODE = "3030"  # 0 = off, 1 = on

# Power setpoint, time-slot format (confirmed from scan):
#   "timeSwitch,startHH:MM,endHH:MM,powerW,0,mode,0,0,0,chargingSOC,dischargingSOC"
#   e.g. "1,00:00,23:59,2400,0,6,0,0,0,100,10"    (discharge at 2400 W)
#        "1,00:00,23:59,-2400,0,6,0,0,0,100,10"   (charge at 2400 W)
#        "0,00:00,00:00,0,0,0,0,0,0,100,10"       (idle / disabled)
#
# AEG (Solarcube) variant: same signed-power slot as every other brand, differing
# only in field 6, which AEG wants as 0 (other brands use field7 = 5/4 there). A live
# capture of the AEG app's own write (issue #8) confirmed the signed field and field 6 = 0:
#        "1,00:00,23:59,-500,0,6,0,0,0,100,10"   (AEG charge at 500 W)
#        "1,00:00,23:59,800,0,6,0,0,0,100,10"    (AEG discharge at 800 W)
# Gated on BRAND_AEG; see _encode_active_slot in coordinator.
REG_CONTROL_TIME1 = "3003"  # First active time slot

REG_MIN_SOC = "3023"  # Minimum discharge SOC  (confirmed: currently 10)
REG_MAX_SOC = "3024"  # Maximum charge SOC     (confirmed: currently 98)
REG_MAX_FEED_POWER = "3039"  # Max feed power in W    (confirmed: currently 2400)

# Empty schedule slot - clears the active time slot so the firmware won't
# auto-re-enable EMS after a disable.
SLOT_DISABLED = "0,00:00,00:00,0,0,0,0,0,0,100,10"

# Work modes (human-readable names for the Select entity)
# Note: there is deliberately no "Disabled" mode. Disabling EMS (3000=0)
# does not reliably stop the battery, it hands control back to the device's
# own logic. To stop the battery, set Power Setpoint to 0 (or Battery
# Direction to Idle), which clears the schedule slot while leaving EMS on,
# custom mode on and both AI flags off, so nothing drives the battery.
MODE_SELF_CONSUMPTION = "Self-Consumption (AI)"
MODE_CUSTOM = "Custom / Manual"

WORK_MODES = [MODE_SELF_CONSUMPTION, MODE_CUSTOM]

# Schedule-mode value (3020) that accompanies a manual setpoint.
#
# 6 = custom schedule, confirmed on AFERIY and the brands behind #2/#3.
#
# AEG (Solarcube) is the exception: its app's own Customized mode leaves 3020
# at 3 (#16 register captures), so on AEG we mirror the app. This was tried as
# a fix for the secondary unit ignoring a manual setpoint on a two-unit stack.
# It is not one: with 3020=3 confirmed on the device the secondary still does
# not follow (#16, two stacks). Manual control reaches the master only, on any
# brand, because the master does not forward a local write to the other units.
# The value stays because it is what the AEG app writes and it is harmless.
SCHEDULE_MODE_CUSTOM = "6"
SCHEDULE_MODE_CUSTOM_AEG = "3"

# Register sets for each mode
MODE_REGISTERS = {
    MODE_SELF_CONSUMPTION: {
        REG_EMS_ENABLE: "1",
        # Reset the schedule mode back to self-gen (3). The battery-control
        # path sets this to 6 (custom schedule) and never resets it, so
        # without this line switching back to Self-Consumption left the
        # device running the previous custom schedule (issues #2, #3).
        # Value 3 confirmed on AFERIY PS240; 6 = custom is confirmed on all.
        REG_SCHEDULE_MODE: "3",
        REG_AI_SMART_CHARGE: "1",
        REG_AI_SMART_DISC: "1",
        REG_CUSTOM_MODE: "0",
        # Clear the leftover manual time slot so the firmware stops executing
        # the previous custom setpoint and hands control back to the AI.
        REG_CONTROL_TIME1: SLOT_DISABLED,
    },
    MODE_CUSTOM: {
        REG_EMS_ENABLE: "1",
        # Switch the schedule to custom. Without this, selecting Custom /
        # Manual coming from Self-Consumption left the schedule on self-gen
        # (3020=3) so the device ignored the manual setpoint and did nothing
        # until a Direction/Power command (which writes the schedule) was sent.
        # That is why users had to go via Idle first to make Manual respond.
        # Mirror of the 3020=3 reset added to Self-Consumption for #2/#3.
        # AEG overrides this per SCHEDULE_MODE_CUSTOM_AEG, see the coordinator.
        REG_SCHEDULE_MODE: SCHEDULE_MODE_CUSTOM,
        REG_AI_SMART_CHARGE: "0",
        REG_AI_SMART_DISC: "0",
        REG_CUSTOM_MODE: "1",
    },
}

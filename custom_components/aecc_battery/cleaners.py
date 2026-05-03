"""Physics-aware sensor value cleaners.

The AECC TCP integration on some devices (notably Lunergy) emits ``0`` for
SOC and power fields when the underlying datalog gateway loses sync with
the BMS for tens of seconds to minutes. The JSON response defaults missing
fields to ``0`` rather than marking them unavailable, so the value passes
through ``coordinator.get_value`` and is published to Home Assistant, which
then pollutes Energy Dashboard accumulators and triggers automations on
bogus thresholds.

This module provides per-key cleaning functions that the coordinator can
invoke after extracting the raw value. Each cleaner gets a small context
object describing the current poll's environment (last accepted value,
elapsed time, current wall-side power) and returns either the value to
publish or ``None`` to indicate the reading should be rejected. Rejection
flows downstream as a ``None`` from ``get_value``, which the existing
hold-last-value logic in ``AeccSensor`` already understands.

Design constraints:
- One cleaner per logical sensor, not per AECC field. Multiple AECC fields
  may map to the same cleaner via ``_FIELD_MAP`` in ``coordinator.py``.
- Cleaners are stateless functions; the coordinator owns and passes the
  per-cleaner state via ``CleanerContext``.
- Conservative defaults: when in doubt, accept the value. We would rather
  publish a slightly-noisy reading than reject a legitimate one. The
  thresholds in ``BRAND_PROFILES`` are tuned for known-bad devices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CleanerContext:
    """Environment passed to a cleaner for one (key, value) decision.

    All fields are nullable because the coordinator may not have data for
    every signal on every poll (e.g., first poll after restart has no
    last_accepted_value, no elapsed_seconds).
    """

    key: str
    raw_value: float
    last_accepted_value: float | None
    last_accepted_at: float | None  # epoch seconds
    now: float  # epoch seconds of the current poll
    wall_power_w: float | None  # signed; positive = charging, negative = discharging
    profile: dict[str, Any]


def clean_soc(ctx: CleanerContext) -> float | None:
    """Reject SOC readings that contradict observable physics.

    Two checks, both relative to the per-brand profile:

    1. **Zero-during-active-flow**: a SOC of 0 while the wall-side power
       shows the battery actively cycling (above the configured threshold)
       is a sensor glitch. The cell does not collapse to 0 in a single poll
       interval. Reject and let the entity hold its previous value.

    2. **Impossible rate of change**: a SOC change exceeding
       ``soc_max_rate_pct_per_min`` since the last accepted sample is
       physically impossible (a 2.4 kWh battery at full 2.4 kW shifts SOC
       by ~1.7%/min at most; >5%/min is always a glitch). Reject.

    Returns ``ctx.raw_value`` to accept, ``None`` to reject.
    """
    raw = ctx.raw_value
    profile = ctx.profile
    threshold_w = float(profile.get("soc_zero_reject_during_active_w", 100))
    max_rate = float(profile.get("soc_max_rate_pct_per_min", 8.0))

    if raw == 0 and ctx.wall_power_w is not None:
        if abs(ctx.wall_power_w) > threshold_w:
            return None

    if ctx.last_accepted_value is not None and ctx.last_accepted_at is not None and ctx.now > ctx.last_accepted_at:
        elapsed_seconds = ctx.now - ctx.last_accepted_at
        # Skip the rate check for sub-poll-interval calls. Multiple sensor
        # entities (e.g. AeccBatteryPowerSensor) call get_value several
        # times per coordinator update and the second call would otherwise
        # see "huge" pp/min from the microsecond gap. Real polls are ≥ 2s
        # apart per MIN_POLL_INTERVAL, so a 1-second floor is safe.
        if elapsed_seconds >= 1.0:
            elapsed_min = elapsed_seconds / 60.0
            change_per_min = abs(raw - ctx.last_accepted_value) / elapsed_min
            if change_per_min > max_rate:
                return None

    return raw


def clean_charge_power(ctx: CleanerContext) -> float | None:
    """Reject 0-watt charge readings when the battery is actively charging.

    Conservative: only fires when ``power_zero_reject_during_status_active``
    is enabled in the brand profile (default off). When enabled, rejects
    raw=0 readings ONLY when the wall-side power signal indicates active
    charging (positive flow above the configured threshold).

    Crucially, we do NOT reject charge=0 when the battery is discharging
    or idle: that's the correct, expected reading. The previous non-
    directional check produced false rejections that made AC Charging
    Power flicker to "unavailable" during normal discharge operation.

    Applies to: ``ac_charging_power``, ``battery_charging_power``.
    """
    if not ctx.profile.get("power_zero_reject_during_status_active", False):
        return ctx.raw_value
    if ctx.raw_value != 0:
        return ctx.raw_value
    if ctx.wall_power_w is None:
        return ctx.raw_value
    threshold_w = float(ctx.profile.get("soc_zero_reject_during_active_w", 100))
    # Charge sensors only get rejected when wall is actively CHARGING.
    if ctx.wall_power_w > threshold_w:
        return None
    return ctx.raw_value


def clean_discharge_power(ctx: CleanerContext) -> float | None:
    """Reject 0-watt discharge readings when the battery is actively discharging.

    Mirror of ``clean_charge_power`` for the opposite direction. Only
    rejects raw=0 when the wall-side power signal indicates active
    discharging (negative flow below the negated threshold). Accepts 0
    during charging or idle, since that's the correct reading there.

    Applies to: ``battery_discharging_power``.
    """
    if not ctx.profile.get("power_zero_reject_during_status_active", False):
        return ctx.raw_value
    if ctx.raw_value != 0:
        return ctx.raw_value
    if ctx.wall_power_w is None:
        return ctx.raw_value
    threshold_w = float(ctx.profile.get("soc_zero_reject_during_active_w", 100))
    # Discharge sensors only get rejected when wall is actively DISCHARGING.
    if ctx.wall_power_w < -threshold_w:
        return None
    return ctx.raw_value


# Map canonical sensor key -> cleaner function. Keys not in this map go
# unfiltered. New cleaners register here without touching the coordinator.
#
# PV sensors deliberately have no cleaner: a 0 reading there is genuinely
# ambiguous (panels not producing vs sensor glitch) and the wall-power
# signal can't disambiguate, since PV can be active while the battery is
# either charging or discharging.
CLEANERS: dict[str, Any] = {
    "battery_soc": clean_soc,
    "ac_charging_power": clean_charge_power,
    "battery_charging_power": clean_charge_power,
    "battery_discharging_power": clean_discharge_power,
}

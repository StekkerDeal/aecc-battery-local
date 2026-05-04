"""Tests for the physics-aware sensor cleaners."""

from __future__ import annotations

import pytest

from custom_components.aecc_battery.cleaners import (
    CleanerContext,
    clean_soc,
)
from custom_components.aecc_battery.const import BRAND_PROFILES, DEFAULT_BRAND_PROFILE


def _ctx(
    *,
    key: str = "battery_soc",
    raw_value: float,
    last_accepted_value: float | None = None,
    last_accepted_at: float | None = None,
    now: float = 1_000_000.0,
    wall_power_w: float | None = None,
    profile: dict | None = None,
) -> CleanerContext:
    return CleanerContext(
        key=key,
        raw_value=raw_value,
        last_accepted_value=last_accepted_value,
        last_accepted_at=last_accepted_at,
        now=now,
        wall_power_w=wall_power_w,
        profile=profile or BRAND_PROFILES["Lunergy"],
    )


# ── clean_soc ────────────────────────────────────────────────────────────────


class TestCleanSoc:
    def test_accepts_normal_reading_with_no_history(self) -> None:
        """First reading after startup is accepted (no history to validate against)."""
        result = clean_soc(_ctx(raw_value=42.0))
        assert result == 42.0

    def test_rejects_zero_during_active_discharge(self) -> None:
        """The Lunergy regression: SOC=0 while battery actively discharging."""
        result = clean_soc(
            _ctx(
                raw_value=0.0,
                last_accepted_value=70.0,
                last_accepted_at=1_000_000.0 - 60,
                wall_power_w=-800.0,  # discharging at 800W
            )
        )
        assert result is None

    def test_rejects_zero_during_active_charge(self) -> None:
        """SOC=0 while charging is also impossible."""
        result = clean_soc(
            _ctx(
                raw_value=0.0,
                last_accepted_value=20.0,
                wall_power_w=1500.0,
            )
        )
        assert result is None

    def test_accepts_zero_during_idle(self) -> None:
        """SOC=0 with idle power may be a real depleted-battery reading."""
        result = clean_soc(
            _ctx(
                raw_value=0.0,
                last_accepted_value=2.0,
                last_accepted_at=1_000_000.0 - 60,
                wall_power_w=5.0,  # near idle, below threshold
            )
        )
        assert result == 0.0

    def test_accepts_zero_when_wall_power_unknown(self) -> None:
        """No wall power signal: don't reject, fall through to other checks."""
        result = clean_soc(_ctx(raw_value=0.0, wall_power_w=None, last_accepted_value=2.0))
        # Without active-power signal we can't reject by direction; rate-of-change
        # is the only fallback. last_accepted_at is None, so rate check skips.
        assert result == 0.0

    def test_rejects_impossible_rate_of_change(self) -> None:
        """SOC change exceeding max rate per minute is a glitch (BMS step jump)."""
        # Lunergy profile: 5%/min cap. 30pp jump in 1 minute is way over.
        result = clean_soc(
            _ctx(
                raw_value=80.0,
                last_accepted_value=50.0,
                last_accepted_at=1_000_000.0 - 60,  # 1 min ago
                wall_power_w=0.0,
            )
        )
        assert result is None

    def test_accepts_legitimate_fast_change_within_threshold(self) -> None:
        """A 4pp/min change is below the 5pp/min Lunergy threshold."""
        result = clean_soc(
            _ctx(
                raw_value=54.0,
                last_accepted_value=50.0,
                last_accepted_at=1_000_000.0 - 60,
                wall_power_w=2400.0,
            )
        )
        assert result == 54.0

    def test_sunpura_profile_more_lenient_on_zero(self) -> None:
        """Sunpura's higher zero-reject threshold lets minor flow through."""
        sunpura = BRAND_PROFILES["Sunpura"]
        # Power 100W is below Sunpura's 200W threshold, so SOC=0 is allowed.
        result = clean_soc(
            _ctx(
                raw_value=0.0,
                last_accepted_value=70.0,
                wall_power_w=-100.0,
                profile=sunpura,
            )
        )
        assert result == 0.0

    def test_default_profile_used_when_unset(self) -> None:
        """DEFAULT_BRAND_PROFILE applies the conservative 'Other' thresholds."""
        result = clean_soc(
            _ctx(
                raw_value=0.0,
                last_accepted_value=70.0,
                wall_power_w=-150.0,  # > 100W threshold
                profile=DEFAULT_BRAND_PROFILE,
            )
        )
        assert result is None


# ── BRAND_PROFILES sanity ────────────────────────────────────────────────────


@pytest.mark.parametrize("brand", list(BRAND_PROFILES.keys()))
def test_brand_profile_has_required_keys(brand: str) -> None:
    """Every brand profile must define every cleaner-relevant key."""
    profile = BRAND_PROFILES[brand]
    assert "soc_zero_reject_during_active_w" in profile
    assert "soc_max_rate_pct_per_min" in profile
    assert "hold_last_value_seconds" in profile

# Lunergy Local TCP Control

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/slaapyhoofd/Lunergy-Local-TCP.svg)](https://github.com/slaapyhoofd/Lunergy-Local-TCP/releases)
![Maintained](https://img.shields.io/badge/maintained-yes-brightgreen.svg)

A Home Assistant custom integration that provides **direct local TCP control** of Lunergy EMS battery systems — no cloud, no latency, no dependency on external servers.

Based on the AECC platform protocol, compatible with Lunergy, Sunpura, and other AECC-based batteries.

---

## Features

- **Local-only** — communicates directly with your battery over your LAN
- **Fast polling** — 5-second update interval with intelligent failure tolerance
- **Energy Dashboard ready** — accumulated kWh sensors for the built-in Home Assistant Energy Dashboard
- **Battery control** — direction select (Charge/Discharge/Idle) with power slider (0-800 W, extendable to 2400 W)
- **SOC limits** — set minimum discharge and maximum charge percentages
- **Battery sensors** — SOC, signed battery power, status, AC charging, PV, grid, backup, home consumption
- **Work mode selector** — switch between Self-Consumption (AI), Custom/Manual, and Disabled
- **EMS switch** — master enable/disable for the energy management system
- **Device info** — automatic serial number and firmware detection (Sunpura; graceful fallback on Lunergy)

---

## Requirements

- Home Assistant **2024.1.0** or newer
- Your Lunergy battery must be on the **same local network** as Home Assistant
- You need the battery's **static IP address** and **TCP port** (typically 8080)

---

## Installation via HACS

1. Open **HACS** in Home Assistant
2. Go to **Integrations**
3. Click the three-dot menu > **Custom repositories**
4. Add `https://github.com/slaapyhoofd/Lunergy-Local-TCP` as an **Integration**
5. Search for **Lunergy** and click **Download**
6. Restart Home Assistant

---

## Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/slaapyhoofd/Lunergy-Local-TCP/releases)
2. Copy the `custom_components/lunergy_local` folder into your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Lunergy Battery (Local TCP)**
3. Enter your battery's **IP address**, **TCP port** (default 8080), and a **friendly name**

> You can update the IP/port at any time via the integration's **Configure** button — Home Assistant will reconnect immediately.

### Extended Power Range

By default, local TCP control is limited to **800 W**. This is not a hardware limit; the battery hardware supports up to 2400 W. The 800 W default exists because the battery's firmware requires register 3039 (maxFeedPower) to be set before accepting higher power values on register 3003.

To unlock the full 2400 W range:

1. Go to the integration's **Configure** button
2. Enable **Extended power range (up to 2400W)**
3. The power slider will increase from 0-800 W to 0-2400 W

When enabled, the integration automatically writes register 3039 alongside every power command. This is the same mechanism the official AECC app uses when you configure power above 800 W.

---

## Entities

### Sensors

| Entity | Type | Description |
|---|---|---|
| `Battery SOC` | Sensor (%) | State of charge |
| `Battery Power` | Sensor (W) | Signed power: positive = charging, negative = discharging |
| `Battery Status` | Sensor | Text: Charging, Discharging, or Idle |
| `Energy Charged` | Sensor (kWh) | Accumulated energy charged (AC + PV), `total_increasing` |
| `Energy Discharged` | Sensor (kWh) | Accumulated energy discharged, `total_increasing` |
| `AC Charging Power` | Sensor (W) | Current AC charging power |
| `Battery Discharging Power` | Sensor (W) | Current discharge power |
| `PV Power` | Sensor (W) | Total solar power |
| `PV Charging Power` | Sensor (W) | Solar power charging battery |
| `Grid / Meter Power` | Sensor (W) | Smart meter power |
| `Grid Export Power` | Sensor (W) | Power exported to grid |
| `Backup Power` | Sensor (W) | Backup/off-grid load power |
| `Home Consumption` | Sensor (W) | Total home consumption |
| `Firmware Version` | Sensor | Diagnostic; only available on Sunpura |

### Controls

| Entity | Type | Description |
|---|---|---|
| `Battery Direction` | Select | Charge, Discharge, or Idle |
| `Battery Power` | Number (slider) | Power target: 0-800 W (or 0-2400 W with extended power) |
| `Discharge Limit` | Number (slider) | Minimum SOC before discharging stops (5-50%) |
| `Charge Limit` | Number (slider) | Maximum SOC before charging stops (50-100%) |
| `Work Mode` | Select | Self-Consumption (AI), Custom/Manual, Disabled |
| `EMS Enabled` | Switch | Master on/off for energy management |

---

## Energy Dashboard Setup

This integration provides the sensors needed for the Home Assistant Energy Dashboard. The energy sensors use Riemann sum integration to compute kWh locally, since the AECC protocol does not expose cumulative energy counters over TCP.

### How to add battery storage to the Energy Dashboard

1. Go to **Settings > Dashboards > Energy**
2. In the **Battery Systems** section, click **Add Battery System**
3. For **Energy going in to the battery**, select `Energy Charged`
4. For **Energy coming out of the battery**, select `Energy Discharged`
5. Click **Save**

The energy sensors are `total_increasing` and persist across Home Assistant restarts (values are restored automatically). They accumulate from zero when first created and increase continuously from there.

### Notes on energy accuracy

- Energy is computed by integrating power over time at each poll interval (default 5 seconds)
- If the battery is unreachable for more than 60 seconds, the integration skips that time gap to avoid phantom energy spikes
- For best accuracy, keep the poll interval at 5 seconds (the default)
- The `Energy Charged` sensor sums both AC and PV charging power, so it captures all charging sources regardless of whether you charge from grid, solar, or both

---

## Battery Control

### Direction + Power

The battery is controlled with two entities working together:

1. **Battery Direction** (select) — sets *what* the battery does: Charge, Discharge, or Idle
2. **Battery Power** (slider) — sets *how much* power to use: 0-800 W (or 0-2400 W with extended power enabled)

When you select a direction, the integration automatically switches the battery to Custom mode and writes the appropriate schedule register. Setting direction to Idle stops the battery.

### Work Mode

The Work Mode select controls the battery's operating strategy:

| Mode | Description |
|---|---|
| `Self-Consumption (AI)` | Battery charges/discharges automatically based on solar and consumption |
| `Custom / Manual` | Manual control via Battery Direction + Power slider |
| `Disabled` | EMS is turned off entirely |

### SOC Limits

- **Discharge Limit** — the battery stops discharging when SOC drops to this level (default 10%)
- **Charge Limit** — the battery stops charging when SOC reaches this level (default 98%)

---

## Compatibility

This integration works with batteries based on the AECC platform protocol. Tested with:

| Brand | Data source | DeviceManagement |
|---|---|---|
| **Lunergy** | SSumInfoList | Not available (graceful skip) |
| **Sunpura** | Storage_list + SSumInfoList | Serial + firmware detected |

Other AECC-based brands (same protocol, different branding) may also work. If you test with another brand, please open an issue to let us know.

---

## Troubleshooting

**Entities show "Unavailable"**
- Verify the battery IP and port are reachable: `ping <battery-ip>` from your HA host
- Check Home Assistant logs for connection errors

**Battery direction/power has no effect**
- The integration automatically sets Custom mode when you pick a direction, so no manual mode switching is needed
- Check the HA logs for `SET battery_control` entries to confirm the command was sent

**Energy sensors show 0 kWh after restart**
- On first install, energy sensors start at 0 and accumulate from there
- After a restart, the last known value is restored automatically
- If values reset to 0, check that your HA recorder is working correctly

**Slow response after setting power**
- The firmware has a brief processing pause after a SET command (~2 s)
- The integration tolerates 5 consecutive polling failures before marking entities unavailable

---

## Known Register Map

| Register | Description | Confirmed |
|---|---|---|
| `3000` | EMS enable (0=off, 1=on) | Yes |
| `3003` | controlTime1 — power schedule slot | Yes |
| `3020` | Energy mode (6=custom) | Yes |
| `3021` | AI smart charge (0=off, 1=on) | Yes |
| `3022` | AI smart discharge (0=off, 1=on) | Yes |
| `3023` | Min discharge SOC (%) | Yes, 10% |
| `3024` | Max charge SOC (%) | Yes, 98% |
| `3030` | Custom mode (0=off, 1=on) | Yes |
| `3039` | Max feed power (W) — gates local power cap | Yes, 2400 W |

---

## Credits

Forked from [Mathieuleysen/Sunpura-Local-TCP](https://github.com/Mathieuleysen/Sunpura-Local-TCP). Extended with multi-brand AECC compatibility, energy dashboard sensors, improved battery control UX, and Lunergy branding.

## License

MIT — see [LICENSE](LICENSE)

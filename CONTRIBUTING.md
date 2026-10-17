# Contributing

Thanks for wanting to help. Reports from batteries we have not tested are the
most valuable contribution there is, so please do share them.

## Open an issue first

For a new feature or a change in behaviour, open an issue before writing code,
so we can agree on the design before you put work into it. A pull request for a
change that was not discussed first will usually be closed.

Typo and documentation fixes can go straight to a pull request.

## What decides whether a change fits

- **Home Assistant or the vendor app, never both.** The integration either
  controls the battery or hands it back to the device's own logic with
  Self-Consumption (AI). Features that mirror or change the vendor app's
  settings from Home Assistant do not fit.
- **No EMS on/off switch.** Writing EMS off (register 3000) locally does not
  stick, the firmware turns it back on within a poll (#6). Stop the battery
  with Power Setpoint 0.
- **One integration for every AECC brand.** Brand-specific extensions belong
  in their own project.
- **Proven on a real battery.** A change to control or telemetry is tested on
  hardware, not only in pytest. Say which battery and firmware you tested on.

## Before you open a pull request

- Run `.\scripts\check.ps1` (Docker Desktop required). It runs ruff, pytest
  and hassfest, the same checks as CI.
- Update the README when entities or behaviour change, and the translations
  for all four languages when user-visible strings change.
- Leave version numbers and releases to us: do not bump the version in
  `manifest.json`, tag, or write release notes.
- No secrets or real device serial numbers in code, tests or logs.

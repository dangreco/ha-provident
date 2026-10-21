# ha-provident

[![CI](https://github.com/dangreco/ha-provident/actions/workflows/ci.yml/badge.svg)](https://github.com/dangreco/ha-provident/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/dangreco/ha-provident)](https://github.com/dangreco/ha-provident/releases)
[![HACS Default](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License](https://img.shields.io/github/license/dangreco/ha-provident)](LICENSE)

Home Assistant integration for the [Provident](https://provident.meterconnex.com)
(MeterConnex) utility portal via [`provident`](https://pypi.org/project/provident/).
Not affiliated with or endorsed by Provident.

## Supported meters

Provident meters on your account:
- Electricity
- Cold water and hot water
- Gas
- Heating and cooling
- Electric vehicle (EV) charging

## Supported functionality

Each meter creates a device with three sensors:
- **Total usage**: cumulative register reading (state class `total_increasing`). The portal updates it once a day, so the Energy dashboard would put a whole day in one hour; use the hourly statistic instead.
- **Usage today**: current day's consumption (`total_increasing`).
- **Usage yesterday**: previous day's consumption (`total_increasing`, diagnostic).

Up to 30 days of hourly usage is imported into the recorder as an external statistic per meter, named "<meter> <utility> hourly usage" (ID `provident:<serial>_hourly_consumption`). Use this statistic in the Energy dashboard.

## Data updates

Data is polled every 15 minutes. The portal updates meters on different schedules:
- **Electricity**: published once daily after the day ends. Today's usage reads 0 until the next day.
- **Water**: published hourly.
- **Registers**: reflect the end of the last complete day.
- **Hourly statistics**: each sync refetches a trailing 2-day window to pick up late or restated hours.

## Installation

### HACS

1. Open HACS → Integrations → Custom repositories.
2. Add `https://github.com/dangreco/ha-provident` as an **Integration**.
3. Install **Provident** and restart Home Assistant.

### Manual

Copy `custom_components/provident` into your `config/custom_components/` directory and restart Home Assistant.

Requires Home Assistant 2026.10 or newer.

## Configuration

Go to Settings → Devices & services → Add integration → **Provident**.

Required parameters:
- **Username**: portal login username or email.
- **Password**: portal password.

One config entry is created per portal account. Use **Reconfigure** to update credentials.

## Removal

1. Go to Settings → Devices & services → **Provident**.
2. Open the three dots menu and select **Delete**.

Deleting the entry removes all associated devices, entities, and background polling.

## Known limitations

- Only kWh and volume units are mapped (`kWh`, `Wh`, `MWh`, `m³`, `L`, `gal`, `ft³`, `ccf`). Unmapped units create plain sensors without statistics.
- Electricity readings are delayed up to 24 hours by the portal.

## Troubleshooting

- **Sensors unavailable / cannot connect**: verify the portal is reachable in a browser. The integration retries automatically during portal downtime.
- **Re-authentication requested**: the portal rejected the stored password. Click the notification or integration card to re-enter your password.
- **Diagnostics**: select **Download diagnostics** on the integration page for a redacted debug export.

## Examples and use cases

### Energy dashboard

Under Settings → Dashboards → Energy:
- Electricity: **Electricity grid** → **Add consumption** → pick "<meter> Electricity hourly usage".
- Water: **Water consumption** → **Add water source** → pick "<meter> <utility> hourly usage".
- Don't also add the **Total usage** sensor for the same meter, or that meter's usage is counted twice.

### Daily notification automation

```yaml
automation:
  - alias: "Daily Electricity Summary"
    trigger:
      - trigger: time
        at: "08:00:00"
    action:
      - action: notify.persistent_notification
        data:
          title: "Electricity Yesterday"
          message: "Usage: {{ states('sensor.3105_electricity_usage_yesterday') }} kWh"
```

## Development

```sh
nix develop
make py-dev
make py-lint
make py-test
make hassfest
make verify-container
```

Pull requests target `dev` and require [Conventional Commits](https://www.conventionalcommits.org).
Releasable commits open a calver release PR (`YEAR.MONTH.PATCH`). Merging it tags the release and updates `main`.

## License

[GPL-3.0-only](LICENSE)

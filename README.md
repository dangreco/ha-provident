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
- **Total usage**: cumulative register reading. The portal updates it once a day, so the Energy dashboard would put a whole day in one hour; use the hourly statistic instead.
- **Usage today**: current day's consumption.
- **Usage yesterday**: previous day's consumption (diagnostic).

These sensors have no state class on purpose, so only the hourly statistics are selectable in the Energy dashboard.

Hourly usage is imported into the recorder as an external statistic per meter, named "<meter> <utility> hourly usage" (ID `provident:<serial>_hourly_consumption`). The default is 30 days of history; choose 1 to 365 days when you add the account, or change it later under **Reconfigure** (a larger value imports the older days on the next update). Use this statistic in the Energy dashboard.

### Rates and cost

With a price plan set in the options, electricity meters also get:
- **Hourly cost statistic**: "<meter> Electricity hourly cost" (ID `provident:<serial>_hourly_cost`, CAD), priced hour by hour from the rates Provident publishes for your building.
- **Current price**: energy price in effect now (CAD/kWh).
- **Price period**: off-peak, mid-peak, on-peak, ultra-low overnight, weekend off-peak, tier 1, tier 2, or market price.

Prices follow the Ontario Energy Board's rules: summer and winter Time-of-Use hours, Ultra-Low Overnight hours, weekends and OEB holidays (including the next-weekday rule when a holiday falls on a weekend), and the tiered threshold (600 kWh per 30 days in summer, 1,000 in winter, counted per calendar month). Each hour uses the price that was in effect that day, so history spanning a November 1 price change is priced correctly.

Buildings not on a Regulated Price Plan pay the market price plus the Global Adjustment, billed as a single rate that changes every bill and isn't on the rates page. For those, choose **Market price** and enter the rate from each bill; every hour of a bill's period is priced at that bill's rate, so the energy cost matches the bill's Electricity Charge line.

The cost covers **energy plus, optionally, Provident's administration and meter fees** (spread evenly over every hour of a 30-day month). It **excludes HST**, delivery, regulatory charges, and rebates, which Provident does not publish, so it will be lower than your bill. At low usage the fees can be most of the hourly cost.

## Data updates

Data is polled every 15 minutes. The portal updates meters on different schedules:
- **Electricity**: published once daily after the day ends. Today's usage reads 0 until the next day.
- **Water**: published hourly.
- **Registers**: reflect the end of the last complete day.
- **Hourly statistics**: each sync refetches the last 2 days plus today to pick up late or restated hours.
- **Rates**: fetched once a day. If the rates site is down, the last rates are kept (also across restarts) and usage keeps updating. When Provident changes a price, the cost history is re-priced from the day it takes effect.

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

### Options

Under Settings → Devices & services → **Provident** → **Configure**:
- **Price plan**: None (default, no cost tracking), Time-of-Use, Ultra-Low Overnight, Tiered, or Market price. Use the plan on your Provident bill; the rates page lists every regulated plan and doesn't say which one your building is on. Under "Your Electricity Charges", separate On-Peak/Mid-Peak/Off-Peak lines mean Time-of-Use; a single "kWh @ rate" line whose rate changes from bill to bill means Market price. Changing the plan re-prices the whole imported cost history.
- **Include Provident fees**: add the administration and meter fees to each hour (default on).
- **Rates from your bills** (Market price only): for each bill, the first reading date and the rate on its Electricity Charge line in cents (`269.00 kWh @ 0.095` is 9.5 ¢/kWh). A rate applies until the next one starts, and the latest keeps applying until you add the next bill. Adding or correcting a bill re-prices only the days it covers. Re-enter a date to correct its rate, or remove one.

## Removal

1. Go to Settings → Devices & services → **Provident**.
2. Open the three dots menu and select **Delete**.

Deleting the entry removes all associated devices, entities, and background polling.

## Known limitations

- Only kWh and volume units are mapped (`kWh`, `Wh`, `MWh`, `m³`, `L`, `gal`, `ft³`, `ccf`). Unmapped units create plain sensors without statistics.
- Electricity readings are delayed up to 24 hours by the portal.
- Cost excludes HST, delivery, regulatory charges, and rebates (see [Rates and cost](#rates-and-cost)).
- Only electricity is priced. EV charging meters are priced only if your rates page has its own EV charging section; they never borrow suite electricity prices. Water, gas, heating, and cooling rates aren't priced yet; if your rates page lists them, please share **Download diagnostics** in an issue.
- Hours before the earliest price shown on the rates page (or, for Market price, the first entered bill) get no cost row.
- Market price lags by a bill: until you enter the latest bill, recent hours use the previous bill's rate. Adding the bill corrects them. The price sensors show that rate too, not the live market price.
- Daylight saving: the portal returns 24 hourly values for every local day, including the 23- and 25-hour days. The values are stored by wall-clock hour, so on the spring-forward day the 02:00 and 03:00 values share one hour in the statistics, and on the fall-back day the repeated hour has no row. Day totals are unaffected.

## Troubleshooting

- **Sensors unavailable / cannot connect**: verify the portal is reachable in a browser. A failed poll is retried every 15 minutes. If one meter's data can't be read, a transport error is retried once and that meter keeps its last data from the same day while the other meters update.
- **Re-authentication requested**: the portal rejected the stored password. Click the notification or integration card to re-enter your password.
- **Diagnostics**: select **Download diagnostics** on the integration page for a redacted debug export. It includes the rates read from the rates page and any rows that weren't recognized.
- **No cost statistic or price sensors**: check that a price plan is set in the options, and that diagnostics show the plan's rates. A warning is logged if the rates page couldn't be read.

## Examples and use cases

### Energy dashboard

Under Settings → Dashboards → Energy:
- Electricity: **Electricity grid** → **Add consumption** → pick "<meter> Electricity hourly usage".
- Water: **Water consumption** → **Add water source** → pick "<meter> <utility> hourly usage".
- Don't also add the **Total usage** sensor for the same meter, or that meter's usage is counted twice.
- Cost: with a price plan set, choose **Use an entity tracking the total costs** for the electricity consumption and pick "<meter> Electricity hourly cost".

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

### Run appliances off-peak

```yaml
automation:
  - alias: "Dishwasher off-peak"
    trigger:
      - trigger: state
        entity_id: sensor.3105_electricity_price_period
        to: "off_peak"
    action:
      - action: switch.turn_on
        target:
          entity_id: switch.dishwasher
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

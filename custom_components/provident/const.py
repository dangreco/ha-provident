# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from provident import Utility

if TYPE_CHECKING:
    from provident import Meter

DOMAIN = "provident"
MANUFACTURER = "Provident (MeterConnex)"

# Keys are lower-cased on lookup.
PORTAL_UNIT_MAP: dict[str, tuple[str, str]] = {
    "kwh": (UnitOfEnergy.KILO_WATT_HOUR, "energy"),
    "wh": (UnitOfEnergy.WATT_HOUR, "energy"),
    "mwh": (UnitOfEnergy.MEGA_WATT_HOUR, "energy"),
    "m3": (UnitOfVolume.CUBIC_METERS, "volume"),
    "l": (UnitOfVolume.LITERS, "volume"),
    "gal": (UnitOfVolume.GALLONS, "volume"),
    "ft3": (UnitOfVolume.CUBIC_FEET, "volume"),
    "ccf": (UnitOfVolume.CENTUM_CUBIC_FEET, "volume"),
}

UTILITY_DEVICE_CLASS: dict[Utility, SensorDeviceClass] = {
    Utility.ELECTRICITY: SensorDeviceClass.ENERGY,
    Utility.ELECTRIC_VEHICLE: SensorDeviceClass.ENERGY,
    Utility.HEATING: SensorDeviceClass.ENERGY,
    Utility.COOLING: SensorDeviceClass.ENERGY,
    Utility.COLD_WATER: SensorDeviceClass.WATER,
    Utility.HOT_WATER: SensorDeviceClass.WATER,
    Utility.GAS: SensorDeviceClass.GAS,
}

UTILITY_LABEL = {
    Utility.ELECTRICITY: "Electricity",
    Utility.ELECTRIC_VEHICLE: "EV",
    Utility.HEATING: "Heating",
    Utility.COOLING: "Cooling",
    Utility.COLD_WATER: "Cold Water",
    Utility.HOT_WATER: "Hot Water",
    Utility.GAS: "Gas",
}

UPDATE_INTERVAL = timedelta(minutes=15)

# The portal serves hourly series for at most 7 days per request.
HOUR_CHUNK_DAYS = 7

CONF_BACKFILL_DAYS = "backfill_days"
DEFAULT_BACKFILL_DAYS = 30
MAX_BACKFILL_DAYS = 365

# Trailing window refetched each sync for late or restated hours.
REFETCH_DAYS = 2


def meter_unit_info(meter: Meter) -> tuple[SensorDeviceClass | None, str, str]:
    """Return (device_class, unit, unit_class); unknown units give a plain sensor."""
    mapped = PORTAL_UNIT_MAP.get(meter.unit.strip().lower())
    if mapped is None:
        return None, meter.unit, ""
    unit, unit_class = mapped
    return UTILITY_DEVICE_CLASS.get(meter.utility), unit, unit_class

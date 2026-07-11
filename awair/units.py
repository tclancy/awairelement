"""Temperature unit conversion for display.

Storage is always Celsius (Awair Local API native, NOAA convention);
this module converts at the boundary between the DB and any surface a
human sees — dashboard JSON, ntfy notifications.
"""

import os

VALID_UNITS = frozenset({"C", "F", "K"})
DEFAULT_UNIT = "C"
SYMBOLS = {"C": "°C", "F": "°F", "K": "K"}


def get_temperature_unit(environ=None):
    """Read TEMPERATURE_UNIT (C, F, or K). Default C. Case-insensitive.

    Raises ValueError on an unrecognized value so a typo fails at startup
    rather than silently reverting to Celsius.
    """
    source = os.environ if environ is None else environ
    value = source.get("TEMPERATURE_UNIT", DEFAULT_UNIT).upper()
    if value not in VALID_UNITS:
        raise ValueError(
            f"TEMPERATURE_UNIT must be one of {sorted(VALID_UNITS)}, got {value!r}"
        )
    return value


def symbol(unit):
    """Display symbol for a unit ('°C', '°F', 'K')."""
    return SYMBOLS[unit]


def from_celsius(value, unit):
    """Convert a Celsius value to `unit`. None passes through unchanged.

    Rounded to 2 decimals to keep JSON payloads tidy — matches `series.bucket`.
    """
    if value is None:
        return None
    if unit == "F":
        return round(value * 9 / 5 + 32, 2)
    if unit == "K":
        return round(value + 273.15, 2)
    return value

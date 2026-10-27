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


def to_celsius(value, unit):
    """Convert a value already in `unit` to Celsius. None passes through.

    The inverse of `from_celsius`, and it exists for exactly one caller: the
    mini split feed, whose upstream bridge works in Fahrenheit while every
    temperature column in this database is Celsius (#139). That makes it the
    one conversion in this project that runs *into* storage, so it lives here
    beside its inverse rather than as a `* 5 / 9` in a poller — a bare
    multiply there is how a database grows one column in a second unit.

    Rounded to 2 decimals for the same reason `from_celsius` is: this is the
    value that gets stored, and a 16-digit artefact of the division is noise
    the source never claimed. A round-trip through both functions is therefore
    accurate to the rounding, not exact.
    """
    if value is None:
        return None
    if unit == "F":
        return round((value - 32) * 5 / 9, 2)
    if unit == "K":
        return round(value - 273.15, 2)
    return value

"""Temperature unit conversions and env-var parsing."""

import pytest

from awair import units


class TestGetTemperatureUnit:
    def test_default_is_celsius_when_unset(self):
        assert units.get_temperature_unit(environ={}) == "C"

    def test_reads_env_var(self):
        assert units.get_temperature_unit(environ={"TEMPERATURE_UNIT": "F"}) == "F"

    def test_case_insensitive(self):
        assert units.get_temperature_unit(environ={"TEMPERATURE_UNIT": "f"}) == "F"
        assert units.get_temperature_unit(environ={"TEMPERATURE_UNIT": "k"}) == "K"

    def test_kelvin_accepted(self):
        assert units.get_temperature_unit(environ={"TEMPERATURE_UNIT": "K"}) == "K"

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="TEMPERATURE_UNIT"):
            units.get_temperature_unit(environ={"TEMPERATURE_UNIT": "R"})

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            units.get_temperature_unit(environ={"TEMPERATURE_UNIT": ""})


class TestFromCelsius:
    def test_celsius_passthrough(self):
        assert units.from_celsius(22.5, "C") == 22.5

    def test_fahrenheit_conversion(self):
        assert units.from_celsius(0, "F") == 32.0
        assert units.from_celsius(100, "F") == 212.0
        assert units.from_celsius(22.5, "F") == 72.5

    def test_kelvin_conversion(self):
        assert units.from_celsius(0, "K") == 273.15
        assert units.from_celsius(-273.15, "K") == 0.0

    def test_none_passes_through(self):
        assert units.from_celsius(None, "F") is None
        assert units.from_celsius(None, "K") is None
        assert units.from_celsius(None, "C") is None

    def test_rounds_to_two_decimals(self):
        # 20 C = 68.0 F exactly; use a value that fractionates
        assert units.from_celsius(1.234, "F") == 34.22


class TestSymbol:
    def test_symbols(self):
        assert units.symbol("C") == "°C"
        assert units.symbol("F") == "°F"
        assert units.symbol("K") == "K"

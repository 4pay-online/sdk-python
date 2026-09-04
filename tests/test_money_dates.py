import pytest

from fourpay import format_amount, from_minor_units, from_unix_seconds, parse_timestamp, to_minor_units


def test_major_unit_strings_convert_with_the_currencys_scale():
    assert to_minor_units("10.00", "USD") == "1000"
    assert to_minor_units("10.5", "USD") == "1050"
    assert to_minor_units("1000", "JPY") == "1000"
    assert to_minor_units("1.234567", "USDT") == "1234567"


def test_a_float_amount_is_refused_outright():
    with pytest.raises(TypeError, match="float"):
        to_minor_units(10.5, "USD")


def test_more_decimals_than_the_currency_has_is_refused():
    with pytest.raises(ValueError, match="decimal places"):
        to_minor_units("10.001", "USD")


def test_an_unknown_currency_is_refused_rather_than_assumed():
    with pytest.raises(ValueError, match="Unknown minor-unit scale"):
        to_minor_units("10.00", "XYZ")


def test_eighteen_decimal_assets_survive_the_round_trip():
    wei = "1234567890123456789"
    assert to_minor_units(from_minor_units(wei, "ETH"), "ETH") == wei


def test_display_formatting():
    assert from_minor_units("1000", "USD") == "10.00"
    assert from_minor_units("-1050", "USD") == "-10.50"
    assert format_amount("2500", "EUR") == "25.00 EUR"


def test_the_platforms_own_timestamp_format_parses_as_utc():
    parsed = parse_timestamp("2026-09-03 11:18:30.101839 Etc/UTC")
    assert parsed.isoformat() == "2026-09-03T11:18:30.101839+00:00"


def test_fromisoformat_alone_cannot_do_it_which_is_why_this_exists():
    from datetime import datetime
    with pytest.raises(ValueError):
        datetime.fromisoformat("2026-09-03 11:18:30.101839 Etc/UTC")


def test_a_bare_timestamp_is_read_as_utc_not_local_time():
    assert parse_timestamp("2026-09-03 11:18:30").isoformat() == "2026-09-03T11:18:30+00:00"


def test_nonsense_gives_none_rather_than_a_wrong_date():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not a date") is None


def test_unix_seconds_from_sessions_and_webhooks():
    assert from_unix_seconds(1788434490).isoformat() == "2026-09-03T11:21:30+00:00"
    assert from_unix_seconds(None) is None

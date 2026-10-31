from datetime import date

import pytest

from jbt.domain.errors import TemporalError
from jbt.domain.time import (
    DateMode,
    Instant,
    LegDate,
    Precision,
    SourceTimeObservation,
    TimeObservation,
    ZoneDecision,
    ZoneResolution,
    civil_time,
    resolve_instant,
    shifted_date,
    source_instant,
    validate_interval,
)


def test_source_second_offset_and_long_fraction_convert_without_rounding() -> None:
    observation = SourceTimeObservation(
        "1970-01-01",
        "00:00:36.12345678901234567890123456789012345678",
        36,
        None,
        Precision.FRACTIONAL_SECOND,
        38,
    )
    instant = source_instant(observation, offset_seconds=36)
    assert instant == Instant(0, "12345678901234567890123456789012345678")
    converted = civil_time(
        instant,
        offset_seconds=-18000,
        zone="America/New_York",
        precision=Precision.FRACTIONAL_SECOND,
    )
    assert converted.date == date(1969, 12, 31)
    assert converted.local == "19:00:00.12345678901234567890123456789012345678"
    assert converted.offset_minutes == -300
    with pytest.raises(TemporalError, match="zone_offset_disagreement"):
        source_instant(observation, offset_seconds=0)


def test_civil_conversion_rejects_unrepresentable_target_offset_and_date() -> None:
    with pytest.raises(TemporalError, match="model_offset_unsupported"):
        civil_time(
            Instant(0, None),
            offset_seconds=36,
            zone="Synthetic/Zone",
            precision=Precision.SECOND,
        )
    with pytest.raises(TemporalError, match="civil_date_range"):
        civil_time(
            Instant(253402300800, None),
            offset_seconds=0,
            zone="Etc/UTC",
            precision=Precision.SECOND,
        )


@pytest.mark.parametrize(
    ("civil", "local", "offset", "precision", "digits"),
    [
        ("0000-02-29", None, None, Precision.DATE, None),
        ("0400-02-29", "23:59:60", 18036, Precision.SECOND, None),
        ("2026-01-01", "23:59:60.124", -18036, Precision.FRACTIONAL_SECOND, 3),
        ("2026-01-01", "00:00", -36, Precision.MINUTE, None),
    ],
)
def test_source_time_preserves_calendar_leap_second_and_exact_offset(
    civil: str,
    local: str | None,
    offset: int | None,
    precision: Precision,
    digits: int | None,
) -> None:
    value = SourceTimeObservation(civil, local, offset, "StatedZone", precision, digits)
    assert (value.date, value.local, value.offset_seconds, value.zone) == (
        civil,
        local,
        offset,
        "StatedZone",
    )


@pytest.mark.parametrize(
    "civil",
    [
        "0000-02-30",
        "0100-02-29",
        "2026-00-01",
        "2026-01-00",
        "2026-1-01",
        "10000-01-01",
    ],
)
def test_source_calendar_rejects_invalid_dates(civil: str) -> None:
    with pytest.raises(TemporalError):
        SourceTimeObservation(civil, None, None, None, Precision.DATE, None)


@pytest.mark.parametrize(
    "local", ["24:00:00", "23:60:00", "23:59:61", "23:59", "23:59:60.1"]
)
def test_source_clock_keeps_precision_and_range_strict(local: str) -> None:
    with pytest.raises(TemporalError):
        SourceTimeObservation("0000-01-01", local, None, None, Precision.SECOND, None)


@pytest.mark.parametrize("offset", [-86400, 86400, True])
def test_source_offset_requires_bounded_integer_seconds(offset: int) -> None:
    with pytest.raises(TemporalError, match="offset_seconds_range"):
        SourceTimeObservation(
            "2026-01-01", "00:00:00", offset, None, Precision.SECOND, None
        )


def test_leg_unknown_does_not_inherit_or_invent_another_role() -> None:
    parent = date(2026, 9, 29)
    override = date(2026, 9, 30)
    assert LegDate(DateMode.INHERIT, None).resolve(parent) == parent
    assert LegDate(DateMode.INHERIT, None).resolve(None) is None
    assert LegDate(DateMode.UNKNOWN, None).resolve(parent) is None
    assert LegDate(DateMode.VALUE, override).resolve(parent) == override
    with pytest.raises(TemporalError, match="date_mode_value_combination"):
        LegDate(DateMode.VALUE, None)
    with pytest.raises(TemporalError, match="date_mode_value_combination"):
        LegDate(DateMode.INHERIT, parent)


def test_date_only_with_zone_is_not_midnight() -> None:
    observation = TimeObservation(
        date(2026, 9, 29), None, None, "Europe/Paris", Precision.DATE, None
    )
    with pytest.raises(TemporalError, match="date_only_has_no_instant"):
        resolve_instant(observation, resolution=None)
    with pytest.raises(TemporalError, match="date_only_fields"):
        TimeObservation(date(2026, 9, 29), "00:00", None, None, Precision.DATE, None)


def test_offset_observation_resolves_exact_submicrosecond_precision() -> None:
    observation = TimeObservation(
        date(1970, 1, 1),
        "01:00:00.123456789012345678",
        60,
        None,
        Precision.FRACTIONAL_SECOND,
        18,
    )
    assert resolve_instant(observation, resolution=None) == Instant(
        0, "123456789012345678"
    )
    prior = TimeObservation(date(1970, 1, 1), "00:00", 1, None, Precision.MINUTE, None)
    assert resolve_instant(prior, resolution=None) == Instant(-60, None)


@pytest.mark.parametrize("offset", [-1439, 1439])
def test_offset_limits_are_admitted(offset: int) -> None:
    value = TimeObservation(
        date(1970, 1, 1), "00:00", offset, None, Precision.MINUTE, None
    )
    assert resolve_instant(value, resolution=None).unix_seconds == -offset * 60


@pytest.mark.parametrize("offset", [-1440, 1440, True, False])
def test_offset_limits_and_integer_types_are_strict(offset: int) -> None:
    with pytest.raises(TemporalError, match="offset_minutes_range"):
        TimeObservation(date(1970, 1, 1), "00:00", offset, None, Precision.MINUTE, None)


@pytest.mark.parametrize("local", ["24:00", "12:60", "1:00", "00:00:00", "00:00Z"])
def test_wall_time_precision_and_ranges_are_strict(local: str) -> None:
    with pytest.raises(TemporalError):
        TimeObservation(date(2026, 1, 1), local, None, None, Precision.MINUTE, None)


def test_fraction_digits_require_exact_integer_precision() -> None:
    with pytest.raises(TemporalError, match="fraction_precision_mismatch"):
        TimeObservation(
            date(2026, 1, 1),
            "00:00:00.1",
            None,
            None,
            Precision.FRACTIONAL_SECOND,
            fraction_digits=True,
        )
    with pytest.raises(TemporalError, match="fraction_precision_mismatch"):
        TimeObservation(
            date(2026, 1, 1), "00:00:00.123", None, None, Precision.FRACTIONAL_SECOND, 2
        )
    value = TimeObservation(
        date(2026, 1, 1),
        "00:00:00." + "1" * 38,
        0,
        None,
        Precision.FRACTIONAL_SECOND,
        38,
    )
    assert resolve_instant(value, resolution=None).fraction == "1" * 38
    with pytest.raises(TemporalError, match="time_precision_mismatch"):
        TimeObservation(
            date(2026, 1, 1),
            "00:00:00." + "1" * 39,
            0,
            None,
            Precision.FRACTIONAL_SECOND,
            39,
        )


def test_unzoned_and_unresolved_zone_only_observations_have_no_instant() -> None:
    for zone in (None, "Synthetic/Zone"):
        value = TimeObservation(
            date(2026, 1, 1), "00:00", None, zone, Precision.MINUTE, None
        )
        with pytest.raises(TemporalError, match="required_instant_unresolved"):
            resolve_instant(value, resolution=None)


def test_pinned_overlap_resolution_distinguishes_earlier_and_later() -> None:
    observation = TimeObservation(
        date(1970, 1, 1), "02:00", None, "Synthetic/Zone", Precision.MINUTE, None
    )
    earlier = ZoneResolution(
        observation, "synthetic-1", (60, 120), ZoneDecision.EARLIER, 120, 0
    )
    later = ZoneResolution(
        observation, "synthetic-1", (60, 120), ZoneDecision.LATER, 60, 0
    )
    assert resolve_instant(observation, resolution=earlier) == Instant(0, None)
    assert resolve_instant(observation, resolution=later) == Instant(3600, None)
    with pytest.raises(TemporalError, match="zone_candidate_decision_mismatch"):
        ZoneResolution(
            observation, "synthetic-1", (60, 120), ZoneDecision.UNIQUE, 60, 0
        )
    with pytest.raises(TemporalError, match="zone_candidate_decision_mismatch"):
        ZoneResolution(
            observation, "synthetic-1", (60, 120), ZoneDecision.EARLIER, 60, 0
        )


def test_gap_requires_explicit_adjustment_and_offset_zone_disagreement_fails() -> None:
    observation = TimeObservation(
        date(1970, 1, 1), "02:00", None, "Synthetic/Zone", Precision.MINUTE, None
    )
    with pytest.raises(TemporalError, match="explicit_gap_decision_required"):
        ZoneResolution(observation, "synthetic-1", (), ZoneDecision.UNIQUE, 120, 0)
    forward = ZoneResolution(
        observation, "synthetic-1", (), ZoneDecision.GAP_FORWARD, 120, 60
    )
    assert resolve_instant(observation, resolution=forward) == Instant(3600, None)
    explicit = TimeObservation(
        date(1970, 1, 1), "02:00", 60, "Synthetic/Zone", Precision.MINUTE, None
    )
    conflicting = ZoneResolution(
        explicit, "synthetic-1", (120,), ZoneDecision.UNIQUE, 120, 0
    )
    with pytest.raises(TemporalError, match="zone_offset_disagreement"):
        resolve_instant(explicit, resolution=conflicting)


def test_civil_intervals_and_shifts_preserve_date_conventions() -> None:
    start = date(2026, 9, 29)
    validate_interval(start, start, inclusive=True)
    validate_interval(start, None, inclusive=False)
    with pytest.raises(TemporalError, match="interval_order"):
        validate_interval(start, start, inclusive=False)
    with pytest.raises(TemporalError, match="interval_order"):
        validate_interval(start, date(2026, 9, 28), inclusive=True)
    assert shifted_date(start, 1) == date(2026, 9, 30)
    with pytest.raises(TemporalError, match="civil_date_range"):
        shifted_date(date.max, 1)

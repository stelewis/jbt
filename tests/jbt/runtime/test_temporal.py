import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jbt.artifacts.integrity import byte_digest
from jbt.domain.errors import TemporalError
from jbt.domain.time import Precision, SourceTimeObservation
from jbt.extraction import ExtractionContext, extract
from jbt.importers.ofx import importer
from jbt.runtime.resolution import model_time
from jbt.runtime.temporal import convert_time, timezone_provenance, validate_zone

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_unqualified_ofx_clock_uses_gmt_before_account_civil_conversion() -> None:
    fixture = Path(__file__).parents[1] / "importers/fixtures_synthetic/main.ofx"
    raw = fixture.read_bytes().replace(
        b"<DTPOSTED>20260112</DTPOSTED>",
        b"<DTPOSTED>20260101235000</DTPOSTED>",
        1,
    )
    wire = json.loads(
        extract(
            importer,
            raw,
            ExtractionContext("bank-feed", byte_digest(raw), "ofx"),
        )
    )
    transaction = next(
        record for record in wire["records"] if record["kind"] == "transaction"
    )
    posted = next(
        time
        for time in transaction["payload"]["times"]
        if time["date_role"] == "posted"
    )
    assert posted["timestamp_date"] == "2026-01-01"
    assert posted["timestamp_offset_seconds"] == 0
    converted = model_time(posted, civil_zone="Europe/Paris")
    assert (converted.date, converted.local, converted.offset_minutes) == (
        date(2026, 1, 2),
        "00:50:00",
        60,
    )


@pytest.mark.parametrize(
    ("civil", "local", "offset", "expected"),
    [
        ("2026-02-01", "04:50:00", 0, (date(2026, 1, 31), "23:50:00", -300)),
        ("2026-07-01", "03:50:00", 0, (date(2026, 6, 30), "23:50:00", -240)),
    ],
)
def test_account_civil_zone_not_utc_date_drives_conversion(
    civil: str,
    local: str,
    offset: int,
    expected: tuple[date, str, int],
) -> None:
    observation = SourceTimeObservation(
        civil, local, offset, None, Precision.SECOND, None
    )
    converted = convert_time(observation, civil_zone="America/New_York")
    assert (converted.date, converted.local, converted.offset_minutes) == expected
    assert observation.date == civil
    assert observation.local == local
    assert observation.offset_seconds == offset


def test_zone_only_unique_clock_uses_locked_seasonal_rules_and_exact_fraction() -> None:
    observation = SourceTimeObservation(
        "2026-01-31",
        "23:50:00.123456789012345678",
        None,
        "Europe/Paris",
        Precision.FRACTIONAL_SECOND,
        18,
    )
    converted = convert_time(observation, civil_zone="Etc/UTC")
    assert (converted.date, converted.local, converted.offset_minutes) == (
        date(2026, 1, 31),
        "22:50:00.123456789012345678",
        0,
    )


@pytest.mark.parametrize(
    ("civil", "local", "constraint"),
    [
        ("2026-03-08", "02:30:00", "source_clock_gap"),
        ("2026-11-01", "01:30:00", "source_clock_ambiguous"),
    ],
)
def test_zone_only_gap_or_overlap_requires_disambiguation(
    civil: str, local: str, constraint: str
) -> None:
    observation = SourceTimeObservation(
        civil, local, None, "America/New_York", Precision.SECOND, None
    )
    with pytest.raises(TemporalError, match=constraint):
        convert_time(observation, civil_zone="Etc/UTC")
    date_only = SourceTimeObservation(
        civil, None, None, "America/New_York", Precision.DATE, None
    )
    converted = convert_time(date_only, civil_zone="Etc/UTC")
    assert converted.date.isoformat() == civil
    assert converted.local is None
    assert converted.offset_minutes is None
    assert converted.zone == "America/New_York"


@pytest.mark.parametrize(
    ("offset", "expected_clock"), [(-14400, "05:30:00"), (-18000, "06:30:00")]
)
def test_explicit_offset_resolves_each_overlap_occurrence(
    offset: int, expected_clock: str
) -> None:
    observation = SourceTimeObservation(
        "2026-11-01",
        "01:30:00",
        offset,
        "America/New_York",
        Precision.SECOND,
        None,
    )
    converted = convert_time(observation, civil_zone="Etc/UTC")
    assert converted.local == expected_clock


def test_zone_offset_disagreement_is_not_silently_rewritten() -> None:
    observation = SourceTimeObservation(
        "2026-07-01", "12:00", -18000, "America/New_York", Precision.MINUTE, None
    )
    with pytest.raises(TemporalError, match="zone_offset_disagreement"):
        convert_time(observation, civil_zone="Etc/UTC")


@pytest.mark.parametrize("zone", ["../UTC", "/Etc/UTC", "Synthetic/Absent"])
def test_rules_reject_paths_and_unknown_zones(zone: str) -> None:
    with pytest.raises(TemporalError, match="timezone_rules_unavailable"):
        validate_zone(zone)


def test_same_version_rule_byte_changes_invalidate_provenance(
    mocker: MockerFixture,
) -> None:
    package, zones, root, rule = (mocker.Mock() for _ in range(4))
    zones.read_text.return_value = "Etc/UTC\n"
    package.joinpath.side_effect = lambda name: root if name == "zoneinfo" else zones
    root.joinpath.return_value = rule
    mocker.patch("jbt.runtime.temporal.files", return_value=package)
    rule.read_bytes.return_value = b"synthetic-rule-v1"
    before = timezone_provenance()
    rule.read_bytes.return_value = b"synthetic-rule-v2"
    after = timezone_provenance()
    assert before["version"] == after["version"]
    assert before["iana_version"] == after["iana_version"]
    assert before["rules_sha256"] != after["rules_sha256"]

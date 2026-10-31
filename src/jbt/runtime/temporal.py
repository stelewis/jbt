"""Civil conversion using only the locked tzdata distribution's rules."""

from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from importlib.resources import files
from zoneinfo import ZoneInfo

import tzdata

from jbt.domain.errors import TemporalError
from jbt.domain.time import (
    Precision,
    SourceTimeObservation,
    TimeObservation,
    civil_time,
    source_instant,
)


def timezone_provenance() -> dict[str, object]:
    """Fingerprint installed rules and the policy required for replay."""
    package = files("tzdata")
    root = package.joinpath("zoneinfo")
    digest = sha256()
    for key in sorted(package.joinpath("zones").read_text().splitlines()):
        payload = root.joinpath(*key.split("/")).read_bytes()
        digest.update(key.encode("utf-8") + b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return {
        "distribution": "tzdata",
        "version": tzdata.__version__,
        "iana_version": tzdata.IANA_VERSION,
        "rules_sha256": digest.hexdigest(),
        "gap_policy": "reject",
        "overlap_policy": "require_explicit_offset",
    }


def _zone(key: str) -> ZoneInfo:
    package = files("tzdata")
    if key not in package.joinpath("zones").read_text().splitlines():
        raise TemporalError(constraint="timezone_rules_unavailable", location="time")
    with package.joinpath("zoneinfo", *key.split("/")).open("rb") as stream:
        return ZoneInfo.from_file(stream, key=key)


def validate_zone(key: str) -> None:
    """Require a civil zone supplied by the locked distribution."""
    _zone(key)


def _wall_clock(observation: SourceTimeObservation) -> datetime:
    if observation.local is None:
        raise TemporalError(constraint="date_only_has_no_instant", location="time")
    hour, minute, *seconds = [
        int(part) for part in observation.local.partition(".")[0].split(":")
    ]
    try:
        return datetime.combine(
            date.fromisoformat(observation.date),
            time(hour, minute, seconds[0] if seconds else 0),
        )
    except ValueError as error:
        raise TemporalError(
            constraint="source_clock_unsupported", location="time"
        ) from error


def _offset_seconds(value: datetime) -> int:
    offset = value.utcoffset()
    if offset is None:
        raise TemporalError(constraint="required_instant_unresolved", location="time")
    return offset.days * 86400 + offset.seconds


def _source_offset(observation: SourceTimeObservation) -> int:
    if observation.zone is None:
        if observation.offset_seconds is None:
            raise TemporalError(
                constraint="required_instant_unresolved", location="time"
            )
        return observation.offset_seconds
    zone = _zone(observation.zone)
    wall = _wall_clock(observation)
    try:
        candidates = {
            _offset_seconds(aware)
            for fold in (0, 1)
            if (aware := wall.replace(tzinfo=zone, fold=fold))
            .astimezone(UTC)
            .astimezone(zone)
            .replace(tzinfo=None)
            == wall
        }
    except OverflowError as error:
        raise TemporalError(constraint="civil_date_range", location="time") from error
    if observation.offset_seconds is not None:
        if observation.offset_seconds not in candidates:
            raise TemporalError(constraint="zone_offset_disagreement", location="time")
        return observation.offset_seconds
    if len(candidates) != 1:
        raise TemporalError(
            constraint="source_clock_ambiguous" if candidates else "source_clock_gap",
            location="time",
        )
    return next(iter(candidates))


def convert_time(
    observation: SourceTimeObservation, *, civil_zone: str
) -> TimeObservation:
    """Convert required clocks; never turn a civil date into an instant.

    Gaps are rejected. Overlaps require an explicit agreeing source offset.
    """
    try:
        civil = date.fromisoformat(observation.date)
    except ValueError as error:
        raise TemporalError(
            constraint="source_calendar_unsupported", location="time"
        ) from error
    if observation.precision == Precision.DATE:
        return TimeObservation(
            civil, None, None, observation.zone, Precision.DATE, None
        )
    instant = source_instant(observation, offset_seconds=_source_offset(observation))
    zone = _zone(civil_zone)
    try:
        utc = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=instant.unix_seconds)
        offset = _offset_seconds(utc.astimezone(zone))
    except (OverflowError, ValueError) as error:
        raise TemporalError(constraint="civil_date_range", location="time") from error
    return civil_time(
        instant,
        offset_seconds=offset,
        zone=civil_zone,
        precision=observation.precision,
    )

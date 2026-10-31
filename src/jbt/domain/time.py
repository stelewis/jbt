"""Precision-preserving civil observations and explicitly resolved instants."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from jbt.domain.errors import TemporalError
from jbt.domain.ids import validate_key

_MINUTE = re.compile(r"([0-9]{2}):([0-9]{2})\Z", re.ASCII)
_SECOND = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})\Z", re.ASCII)
_FRACTION = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{1,38})\Z", re.ASCII)
_MAX_OFFSET = 1439
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
_SECONDS_PER_DAY = 86400
_SECOND_COMPONENTS = 3
_SOURCE_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)


class Precision(StrEnum):
    """The observation's stated temporal precision."""

    DATE = "date"
    MINUTE = "minute"
    SECOND = "second"
    FRACTIONAL_SECOND = "fractional_second"


class TimeRole(StrEnum):
    """The fact whose exact civil time is observed."""

    AUTHORIZED = "authorized"
    TRADED = "traded"
    POSTED = "posted"
    SETTLED = "settled"
    VALUE = "value"
    OCCURRED = "occurred"
    INITIATED = "initiated"
    PENDING_AS_OF = "pending_as_of"
    HOLD_EXPIRES = "hold_expires"


def is_midnight(local: str | None) -> bool:
    """Recognize midnight without discarding the clock's stated precision."""
    clock, _, fraction = (local or "").partition(".")
    return clock in {"00:00", "00:00:00"} and not fraction.strip("0")


def validate_source_date(value: str) -> None:
    """Validate a proleptic Gregorian source date, including year zero."""
    if not isinstance(value, str) or _SOURCE_DATE.fullmatch(value) is None:
        raise TemporalError(constraint="source_date_required", location="source_time")
    year, month, day = (int(part) for part in value.split("-"))
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not 1 <= month <= len(month_days) or not 1 <= day <= month_days[month - 1]:
        raise TemporalError(constraint="source_calendar_range", location="source_time")


def _validate_local_time(
    local: str | None,
    precision: Precision,
    fraction_digits: int | None,
    *,
    last_second: int,
) -> None:
    if not isinstance(local, str):
        raise TemporalError(constraint="local_time_required", location="time")
    patterns = {
        Precision.MINUTE: _MINUTE,
        Precision.SECOND: _SECOND,
        Precision.FRACTIONAL_SECOND: _FRACTION,
    }
    match = patterns[precision].fullmatch(local)
    if match is None:
        raise TemporalError(constraint="time_precision_mismatch", location="time")
    parts = match.groups()
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) >= _SECOND_COMPONENTS else 0
    if hour >= _HOURS_PER_DAY or minute >= _MINUTES_PER_HOUR or second > last_second:
        raise TemporalError(constraint="civil_time_range", location="time")
    if precision == Precision.FRACTIONAL_SECOND:
        if type(fraction_digits) is not int or fraction_digits != len(parts[3]):
            raise TemporalError(
                constraint="fraction_precision_mismatch", location="time"
            )
    elif fraction_digits is not None:
        raise TemporalError(constraint="unexpected_fraction_digits", location="time")


@dataclass(frozen=True, slots=True)
class SourceTimeObservation:
    """Exact source civil evidence; not a resolved model time or instant.

    A lexical leap second does not establish a valid UTC leap event.
    Offset seconds preserve fractional-hour offsets without rounding.
    """

    date: str
    local: str | None
    offset_seconds: int | None
    zone: str | None
    precision: Precision
    fraction_digits: int | None

    def __post_init__(self) -> None:
        """Validate source ranges without Python calendar/POSIX conversion."""
        validate_source_date(self.date)
        if not isinstance(self.precision, Precision):
            raise TemporalError(
                constraint="date_and_precision_required", location="source_time"
            )
        if self.zone is not None:
            validate_key(self.zone, "source_time.zone")
        if self.offset_seconds is not None and (
            type(self.offset_seconds) is not int
            or not -_SECONDS_PER_DAY < self.offset_seconds < _SECONDS_PER_DAY
        ):
            raise TemporalError(
                constraint="offset_seconds_range", location="source_time"
            )
        if self.precision == Precision.DATE:
            if any(
                item is not None
                for item in (
                    self.local,
                    self.offset_seconds,
                    self.fraction_digits,
                )
            ):
                raise TemporalError(
                    constraint="date_only_fields", location="source_time"
                )
            return
        _validate_local_time(
            self.local,
            self.precision,
            self.fraction_digits,
            last_second=60,
        )


class DateMode(StrEnum):
    """A leg can inherit, override, or explicitly suppress a parent date."""

    INHERIT = "inherit"
    VALUE = "value"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LegDate:
    """One posting-level date role, with explicit inheritance semantics."""

    mode: DateMode
    value: date | None

    def __post_init__(self) -> None:
        """Require a date only for the explicit-value mode."""
        if not isinstance(self.mode, DateMode):
            raise TemporalError(constraint="date_mode_required", location="leg_date")
        if (self.mode == DateMode.VALUE) != (type(self.value) is date):
            raise TemporalError(
                constraint="date_mode_value_combination", location="leg_date"
            )
        if self.mode != DateMode.VALUE and self.value is not None:
            raise TemporalError(constraint="absent_date_required", location="leg_date")

    def resolve(self, parent: date | None) -> date | None:
        """Resolve only the same temporal role; unknown never inherits."""
        if parent is not None and type(parent) is not date:
            raise TemporalError(
                constraint="civil_date_required", location="leg_date.parent"
            )
        return parent if self.mode == DateMode.INHERIT else self.value


def _offset(value: int) -> None:
    if type(value) is not int or not -_MAX_OFFSET <= value <= _MAX_OFFSET:
        raise TemporalError(
            constraint="offset_minutes_range", location="time.offset_minutes"
        )


@dataclass(frozen=True, slots=True)
class TimeObservation:
    """A present date/time bundle, preserving offset and zone independently."""

    date: date
    local: str | None
    offset_minutes: int | None
    zone: str | None
    precision: Precision
    fraction_digits: int | None

    def __post_init__(self) -> None:
        """Reject invented precision and inconsistent time bundle fields."""
        if type(self.date) is not date or not isinstance(self.precision, Precision):
            raise TemporalError(
                constraint="date_and_precision_required", location="time"
            )
        if self.zone is not None:
            validate_key(self.zone, "time.zone")
        if self.offset_minutes is not None:
            _offset(self.offset_minutes)
        if self.precision == Precision.DATE:
            if any(
                item is not None
                for item in (self.local, self.offset_minutes, self.fraction_digits)
            ):
                raise TemporalError(constraint="date_only_fields", location="time")
            return
        self._validate_local()

    def _validate_local(self) -> None:
        _validate_local_time(
            self.local,
            self.precision,
            self.fraction_digits,
            last_second=59,
        )


class ZoneDecision(StrEnum):
    """A recorded choice against caller-supplied pinned timezone rules."""

    UNIQUE = "unique"
    EARLIER = "earlier"
    LATER = "later"
    GAP_FORWARD = "gap_forward"
    GAP_BACKWARD = "gap_backward"


@dataclass(frozen=True, slots=True)
class ZoneResolution:
    """Explicit rule evidence; obtaining tzdb data belongs outside the domain.

    Candidate offsets describe this observation under the named pinned
    ruleset. For a gap, the adapter supplies the selected shift and offset.
    This pure type validates the decision, not the authenticity of tzdb.
    """

    observation: TimeObservation
    tzdb_version: str
    candidate_offsets: tuple[int, ...]
    decision: ZoneDecision
    offset_minutes: int
    shift_minutes: int

    def __post_init__(self) -> None:
        """Reject ambiguous selections and undocumented gap adjustments."""
        validate_key(self.tzdb_version, "time.tzdb_version")
        if self.observation.zone is None or self.observation.local is None:
            raise TemporalError(
                constraint="zoned_wall_time_required", location="zone_resolution"
            )
        if not isinstance(self.decision, ZoneDecision):
            raise TemporalError(
                constraint="zone_decision_required", location="zone_resolution"
            )
        _offset(self.offset_minutes)
        if type(self.shift_minutes) is not int or abs(self.shift_minutes) > _MAX_OFFSET:
            raise TemporalError(
                constraint="bounded_gap_shift_required", location="zone_resolution"
            )
        if not isinstance(self.candidate_offsets, tuple):
            raise TemporalError(
                constraint="candidate_offsets_tuple_required",
                location="zone_resolution",
            )
        for offset in self.candidate_offsets:
            _offset(offset)
        if len(set(self.candidate_offsets)) != len(self.candidate_offsets):
            raise TemporalError(
                constraint="unique_candidate_offsets", location="zone_resolution"
            )
        self._validate_selection()

    def _validate_selection(self) -> None:
        if not self.candidate_offsets:
            valid = (
                self.decision == ZoneDecision.GAP_FORWARD and self.shift_minutes > 0
            ) or (self.decision == ZoneDecision.GAP_BACKWARD and self.shift_minutes < 0)
            if not valid:
                raise TemporalError(
                    constraint="explicit_gap_decision_required",
                    location="zone_resolution",
                )
            return
        if self.shift_minutes:
            raise TemporalError(
                constraint="unexpected_gap_shift", location="zone_resolution"
            )
        if len(self.candidate_offsets) == 1:
            valid = (
                self.decision == ZoneDecision.UNIQUE
                and self.offset_minutes == self.candidate_offsets[0]
            )
        else:
            valid = (
                self.decision == ZoneDecision.EARLIER
                and self.offset_minutes == max(self.candidate_offsets)
            ) or (
                self.decision == ZoneDecision.LATER
                and self.offset_minutes == min(self.candidate_offsets)
            )
        if not valid:
            raise TemporalError(
                constraint="zone_candidate_decision_mismatch",
                location="zone_resolution",
            )


@dataclass(frozen=True, slots=True)
class Instant:
    """UTC integer seconds plus exact stated fractional digits, never floats."""

    unix_seconds: int
    fraction: str | None

    def __post_init__(self) -> None:
        """Keep the resolved representation bounded and exact."""
        if (
            type(self.unix_seconds) is not int
            or not -(2**63) <= self.unix_seconds < 2**63
        ):
            raise TemporalError(
                constraint="instant_integer_seconds_required", location="instant"
            )
        if self.fraction is not None and (
            not isinstance(self.fraction, str)
            or re.fullmatch(r"[0-9]{1,38}", self.fraction, flags=re.ASCII) is None
        ):
            raise TemporalError(
                constraint="instant_fraction_required", location="instant"
            )


def resolve_instant(
    observation: TimeObservation, *, resolution: ZoneResolution | None
) -> Instant:
    """Require actual offset evidence; date-only/unzoned data has no instant."""
    if observation.local is None:
        raise TemporalError(constraint="date_only_has_no_instant", location="time")
    offset = observation.offset_minutes
    shift = 0
    if resolution is not None:
        if resolution.observation != observation:
            raise TemporalError(
                constraint="zone_resolution_observation_mismatch", location="time"
            )
        if offset is not None and offset != resolution.offset_minutes:
            raise TemporalError(constraint="zone_offset_disagreement", location="time")
        offset = resolution.offset_minutes
        shift = resolution.shift_minutes
    if offset is None:
        raise TemporalError(constraint="required_instant_unresolved", location="time")
    clock, separator, fraction = observation.local.partition(".")
    parts = tuple(int(part) for part in clock.split(":"))
    hour, minute = parts[:2]
    second = parts[2] if len(parts) >= _SECOND_COMPONENTS else 0
    days = (observation.date - date(1970, 1, 1)).days
    seconds = (
        days * _SECONDS_PER_DAY
        + hour * _MINUTES_PER_HOUR * _SECONDS_PER_MINUTE
        + (minute + shift - offset) * _SECONDS_PER_MINUTE
        + second
    )
    return Instant(seconds, fraction if separator else None)


def source_instant(
    observation: SourceTimeObservation, *, offset_seconds: int
) -> Instant:
    """Resolve an ordinary source clock with an exact supplied offset."""
    if observation.local is None:
        raise TemporalError(constraint="date_only_has_no_instant", location="time")
    if (
        type(offset_seconds) is not int
        or not -_SECONDS_PER_DAY < offset_seconds < _SECONDS_PER_DAY
    ):
        raise TemporalError(constraint="offset_seconds_range", location="time")
    if (
        observation.offset_seconds is not None
        and observation.offset_seconds != offset_seconds
    ):
        raise TemporalError(constraint="zone_offset_disagreement", location="time")
    try:
        civil = date.fromisoformat(observation.date)
    except ValueError as error:
        raise TemporalError(
            constraint="source_calendar_unsupported", location="time"
        ) from error
    clock, separator, fraction = observation.local.partition(".")
    parts = tuple(int(part) for part in clock.split(":"))
    hour, minute = parts[:2]
    second = parts[2] if len(parts) >= _SECOND_COMPONENTS else 0
    if second == _SECONDS_PER_MINUTE:
        raise TemporalError(
            constraint="source_leap_second_requires_rules", location="time"
        )
    return Instant(
        (civil - date(1970, 1, 1)).days * _SECONDS_PER_DAY
        + hour * 3600
        + minute * 60
        + second
        - offset_seconds,
        fraction if separator else None,
    )


def civil_time(
    instant: Instant, *, offset_seconds: int, zone: str, precision: Precision
) -> TimeObservation:
    """Render an instant without rounding its fractional-second evidence.

    Minute source precision expands to seconds only when required by a
    historical second-resolution offset.
    """
    if (
        type(offset_seconds) is not int
        or not -_SECONDS_PER_DAY < offset_seconds < _SECONDS_PER_DAY
    ):
        raise TemporalError(constraint="offset_seconds_range", location="time")
    if offset_seconds % 60:
        raise TemporalError(constraint="model_offset_unsupported", location="time")
    if precision == Precision.DATE:
        raise TemporalError(constraint="date_only_has_no_instant", location="time")
    days, seconds = divmod(instant.unix_seconds + offset_seconds, _SECONDS_PER_DAY)
    civil = shifted_date(date(1970, 1, 1), days)
    hour, seconds = divmod(seconds, 3600)
    minute, second = divmod(seconds, 60)
    if precision == Precision.MINUTE and second:
        precision = Precision.SECOND
    local = f"{hour:02}:{minute:02}"
    if precision != Precision.MINUTE:
        local += f":{second:02}"
    if instant.fraction is not None:
        local += f".{instant.fraction}"
    return TimeObservation(
        civil,
        local,
        offset_seconds // 60,
        zone,
        precision,
        len(instant.fraction) if instant.fraction is not None else None,
    )


def validate_interval(start: date, end: date | None, *, inclusive: bool) -> None:
    """Validate civil validity or inclusive statement intervals explicitly."""
    if type(start) is not date or (end is not None and type(end) is not date):
        raise TemporalError(constraint="civil_date_required", location="interval")
    if type(inclusive) is not bool:
        raise TemporalError(
            constraint="interval_convention_required", location="interval"
        )
    if end is not None and (end < start or (end == start and not inclusive)):
        raise TemporalError(constraint="interval_order", location="interval")


def shifted_date(value: date, days: int) -> date:
    """Shift a civil date with bounded integer day arithmetic."""
    if (
        type(value) is not date
        or type(days) is not int
        or abs(days) > date.max.toordinal()
    ):
        raise TemporalError(constraint="bounded_civil_shift_required", location="date")
    try:
        return value + timedelta(days=days)
    except OverflowError as error:
        raise TemporalError(constraint="civil_date_range", location="date") from error

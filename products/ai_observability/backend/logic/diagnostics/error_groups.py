from collections.abc import Mapping, Sequence
from datetime import datetime

from posthog.dataclasses import frozen
from posthog.enums import LabeledStrEnum

from .trends import Trend


class ErrorOrder(LabeledStrEnum):
    OCCURRENCES = "occurrences"
    CHANGE = "change"
    TRACES = "traces"
    USERS = "users"
    LAST_SEEN = "last_seen", "Last seen"


class ErrorView(LabeledStrEnum):
    INCREASING = "increasing"
    DECREASING = "decreasing"
    NEW = "new"


@frozen
class ErrorGroupRow:
    """One normalized error message over the selection's range. `previous_occurrences` is None when the range has no
    previous window, and `series` counts the occurrences in each of the range's bucket starts."""

    message: str
    occurrences: int
    previous_occurrences: int | None
    traces: int
    users: int
    models: tuple[str, ...]
    last_seen: datetime
    is_new: bool
    trend: Trend
    series: tuple[int, ...]


@frozen
class ErrorCountsRow:
    total: int
    increasing: int
    decreasing: int
    new: int


def series_of(counts: Mapping[int, int], starts: Sequence[datetime]) -> tuple[int, ...]:
    """The count in each bucket, from counts keyed by each bucket's start in Unix seconds. A start with no count had no
    occurrence."""
    # Unix seconds name one instant in any zone, so the two buckets of the hour a clock goes back stay apart.
    return tuple(counts.get(int(start.timestamp()), 0) for start in starts)


def first_seen_of(earliest: datetime, lookback_start: datetime) -> datetime | None:
    """None when the earliest occurrence read is at or before the lookback start, as nothing older was read and the
    message may have occurred before."""
    return earliest if earliest > lookback_start else None

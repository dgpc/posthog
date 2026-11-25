from datetime import datetime

from posthog.dataclasses import frozen
from posthog.enums import LabeledStrEnum


class ToolOrder(LabeledStrEnum):
    CALLS = "calls"
    CHANGE = "change"
    TRACES = "traces"
    FAILURE_RATE = "failure_rate", "Failure rate"
    LAST_SEEN = "last_seen", "Last seen"


class ToolView(LabeledStrEnum):
    FAILING = "failing"


@frozen
class ToolGroupRow:
    """One called tool over the selection's range. `previous_calls` is None when the range has no previous window.
    `results` and `failures` count the tool's captured results and failed results in the range, and `series` counts
    the calls in each of the range's bucket starts."""

    name: str
    calls: int
    previous_calls: int | None
    traces: int
    results: int
    failures: int
    only_tool_calls: int
    last_seen: datetime
    is_failing: bool
    series: tuple[int, ...]


@frozen
class ToolCountsRow:
    total: int
    failing: int

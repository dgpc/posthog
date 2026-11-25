from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

from posthog.hogql import ast
from posthog.hogql.parser import parse_expr, parse_select

from posthog.models import Team
from posthog.paging import PageRequest

from ..traces.lookback import lookback_selection
from ..traces.trace_metrics import BucketSize, has_no_previous_window, previous_window
from ..traces.trace_metrics_queries import last_moment, period_bucket
from ..traces.trace_selection import TraceSelection, TraceWindow
from ..traces.trace_selection_queries import grouped_traces

# ClickHouse refuses a groupArraySorted size above this, so a page read sorts at most this many groups. An offset past
# it reads an empty page, and the counts still cover every group.
MAX_SORTED_GROUPS: Final = 16_777_215


def groups_to_sort(page: PageRequest) -> int:
    """How many of the first groups a page read sorts, from which it keeps the page's own."""
    return min(page.offset + page.limit, MAX_SORTED_GROUPS)


def change_order(current: str, previous: str) -> tuple[str, ...]:
    """The sort keys of the change order, each ascending in the page's sort, over two group columns."""
    # A group absent last period has no percent change. It counts as the largest rise, so these groups lead, the larger
    # count first.
    return (
        f"{previous} != 0",
        f"negate(if({previous} = 0, 0, ({current} - {previous}) / {previous}))",
        f"negate({current})",
    )


def contains_search(value: ast.Expr, search: str | None) -> ast.Expr:
    term = (search or "").strip()
    if not term:
        return ast.Constant(value=True)
    return parse_expr(
        "positionCaseInsensitiveUTF8({value}, {term}) > 0",
        placeholders={"value": value, "term": ast.Constant(value=term)},
    )


def fields(names: Sequence[str]) -> list[ast.Expr]:
    return [ast.Field(chain=[name]) for name in names]


def traces_in_periods(team: Team, selection: TraceSelection, items: Callable[[bool], ast.Expr]) -> ast.SelectQuery:
    """One row per trace of the range and of its previous window, with `trace_id`, `in_current` and `items`, the
    per-trace array `items(current)` aggregates over the trace's events in that period."""
    # Each period reads its traces exactly as the traces list reads that range, so a group's Traces is the count of the
    # list scoped to it. A trace in both reads keeps only its current row, so the periods never share a trace.
    reads: list[ast.SelectQuery] = [
        grouped_traces(team, selection, {"current_read": ast.Constant(value=True), "period_items": items(True)})
    ]
    if not has_no_previous_window(selection.window):
        previous = replace(selection, window=previous_window(selection.window))
        reads.append(
            grouped_traces(team, previous, {"current_read": ast.Constant(value=False), "period_items": items(False)})
        )
    source = reads[0] if len(reads) == 1 else ast.SelectSetQuery.create_from_queries(reads, "UNION ALL")
    query = parse_select(
        """
        SELECT
            trace_id,
            max(current_read) AS in_current,
            tupleElement(argMax(tuple(period_items), current_read), 1) AS items
        FROM {source}
        GROUP BY trace_id
        """,
        placeholders={"source": source},
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def bucket_seconds(size: BucketSize, window: TraceWindow) -> ast.Expr:
    """The Unix seconds of the bucket a row's `timestamp` falls in, within the period its `in_current` names."""
    previous = window if has_no_previous_window(window) else previous_window(window)
    period_start = parse_expr(
        "if(in_current, {current}, {previous})",
        placeholders={"current": ast.Constant(value=window.start), "previous": ast.Constant(value=previous.start)},
    )
    period_last_moment = parse_expr(
        "if(in_current, {current}, {previous})",
        placeholders={"current": last_moment(window), "previous": last_moment(previous)},
    )
    bucket = period_bucket(size, ast.Field(chain=["timestamp"]), period_start, period_last_moment)
    return ast.Call(name="toUnixTimestamp", args=[bucket])


def lookback_through_range(selection: TraceSelection) -> TraceSelection:
    lookback = lookback_selection(selection)
    return replace(lookback, window=TraceWindow(start=lookback.window.start, end=selection.window.end))

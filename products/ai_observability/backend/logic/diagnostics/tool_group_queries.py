from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from posthog.hogql import ast
from posthog.hogql.parser import parse_expr, parse_select

from posthog.dataclasses import frozen
from posthog.models import Team, User
from posthog.paging import PageRequest

from ..traces.trace_expression_queries import EVENTS_COLUMNS, tool_names
from ..traces.trace_metrics import BucketSize, bucket_size_for, bucket_starts, has_no_previous_window, previous_window
from ..traces.trace_selection import TraceSelection
from ..traces.trace_selection_queries import execute_selection, grouped_traces
from .group_page_queries import (
    bucket_seconds,
    change_order,
    contains_search,
    fields,
    groups_to_sort,
    lookback_through_range,
    traces_in_periods,
)
from .tool_groups import ToolCountsRow, ToolGroupRow, ToolOrder, ToolView
from .trends import failing_expression, series_of

TOOLS_PAGE_QUERY_NAME = "ai_diagnostics_tools_page"
TOOL_GROUP_QUERY_NAME = "ai_diagnostics_tool_group"
TOOLS_CAPTURED_QUERY_NAME = "ai_diagnostics_tools_captured"

# Each value is ascending in the page's sort, so a descending order negates its value.
_ORDER_KEYS: dict[ToolOrder, tuple[str, ...]] = {
    ToolOrder.CALLS: ("negate(calls)",),
    ToolOrder.CHANGE: change_order("calls", "previous_calls"),
    ToolOrder.TRACES: ("negate(traces)",),
    # A tool with no captured result has no failure rate, so it follows every tool that has one.
    ToolOrder.FAILURE_RATE: ("results = 0", "negate(if(results = 0, 0, failures / results))", "negate(calls)"),
    ToolOrder.LAST_SEEN: ("negate(toUnixTimestamp64Milli(last_seen))",),
}
_VIEWS: dict[ToolView, str] = {ToolView.FAILING: "is_failing"}
# The columns of one group, in the order a page row and a group read return them.
_GROUP_COLUMNS = (
    "name",
    "calls",
    "previous_calls",
    "traces",
    "results",
    "failures",
    "only_tool_calls",
    "last_seen",
    "is_failing",
    "series",
)


class _Part(StrEnum):
    CALL = "call"
    RESULT = "result"
    FAILURE = "failure"
    TRACE = "trace"


# Tool names are never empty, so the group of the empty name can hold one trace part per trace of the range. It counts
# the range's traces in the same pass as the tools, traces without a tool call included.
_TRACES_GROUP_NAME = ""


@frozen
class ToolPageRead:
    rows: tuple[ToolGroupRow, ...]
    count: int
    counts: ToolCountsRow
    total_traces: int
    bucket_starts: tuple[datetime, ...]


@frozen
class ToolGroupDetailRow:
    row: ToolGroupRow
    previous_series: tuple[int, ...]
    total_traces: int


def _parts(property_name: str, part: _Part, *, single: ast.Expr) -> ast.Expr:
    return parse_expr(
        "arrayMap(name -> tuple(name, {part}, timestamp, {single}), {names})",
        placeholders={
            "names": tool_names(EVENTS_COLUMNS, property_name),
            "part": ast.Constant(value=part.value),
            "single": single,
        },
    )


def _tool_parts(current: bool) -> ast.Expr:
    """Per trace, one `(name, part, timestamp, single)` tuple for each name a generation calls, and in the range also
    for each result and failed result it receives and one trace part. `single` marks a call from a generation that
    calls one tool."""
    # Results only feed the range's columns, so the previous window skips parsing them.
    called = _parts(
        "$ai_tools_called",
        _Part.CALL,
        single=parse_expr(
            "length({names}) = 1", placeholders={"names": tool_names(EVENTS_COLUMNS, "$ai_tools_called")}
        ),
    )
    if not current:
        return parse_expr(
            "arrayFlatten(groupArrayIf({called}, event = '$ai_generation'))", placeholders={"called": called}
        )
    return parse_expr(
        """
        arrayConcat(
            arrayFlatten(groupArrayIf(arrayConcat({called}, {results}, {failures}), event = '$ai_generation')),
            [tuple({traces_group}, {trace}, max(timestamp), 0)]
        )
        """,
        placeholders={
            "called": called,
            "results": _parts("$ai_tool_results", _Part.RESULT, single=ast.Constant(value=0)),
            "failures": _parts("$ai_tool_errors", _Part.FAILURE, single=ast.Constant(value=0)),
            "traces_group": ast.Constant(value=_TRACES_GROUP_NAME),
            "trace": ast.Constant(value=_Part.TRACE.value),
        },
    )


def _tool_events(team: Team, selection: TraceSelection, match: ast.Expr) -> ast.SelectQuery:
    query = parse_select(
        """
        SELECT
            trace_id,
            in_current,
            tupleElement(tool_part, 1) AS name,
            tupleElement(tool_part, 2) AS part,
            tupleElement(tool_part, 3) AS timestamp,
            tupleElement(tool_part, 4) AS single
        FROM (SELECT trace_id, in_current, arrayJoin(items) AS tool_part FROM {traces})
        WHERE part = {trace} OR {match}
        """,
        placeholders={
            "traces": traces_in_periods(team, selection, _tool_parts),
            "trace": ast.Constant(value=_Part.TRACE.value),
            "match": match,
        },
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _groups(team: Team, selection: TraceSelection, size: BucketSize, match: ast.Expr) -> ast.SelectQuery:
    """One row per tool a generation in the range calls, over the names `match` keeps, and the traces group, the one
    row that is not a tool, whose `range_traces` is the range's trace count."""
    query = parse_select(
        """
        SELECT
            name,
            countIf(part = {call} AND in_current) AS calls,
            countIf(part = {call} AND NOT in_current) AS previous_calls,
            uniqIf(trace_id, part = {call} AND in_current) AS traces,
            countIf(part = {result} AND in_current) AS results,
            countIf(part = {failure} AND in_current) AS failures,
            countIf(part = {call} AND in_current AND single) AS only_tool_calls,
            maxIf(timestamp, part = {call} AND in_current) AS last_seen,
            {failing} AS is_failing,
            countIf(part = {trace}) AS range_traces,
            name != {traces_group} AS is_tool,
            sumMapIf([bucket], [1], part = {call} AND in_current) AS series,
            sumMapIf([bucket], [1], part = {call} AND NOT in_current) AS previous_series
        FROM (SELECT *, {bucket} AS bucket FROM {events})
        GROUP BY name
        HAVING calls > 0 OR NOT is_tool
        """,
        placeholders={
            "call": ast.Constant(value=_Part.CALL.value),
            "result": ast.Constant(value=_Part.RESULT.value),
            "failure": ast.Constant(value=_Part.FAILURE.value),
            "trace": ast.Constant(value=_Part.TRACE.value),
            "traces_group": ast.Constant(value=_TRACES_GROUP_NAME),
            "failing": failing_expression(ast.Field(chain=["results"]), ast.Field(chain=["failures"])),
            "bucket": bucket_seconds(size, selection.window),
            "events": _tool_events(team, selection, match),
        },
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _named(name: str) -> ast.Expr:
    return parse_expr("name = {name}", placeholders={"name": ast.Constant(value=name)})


def _page_query(
    team: Team,
    selection: TraceSelection,
    size: BucketSize,
    *,
    search: str | None,
    view: ToolView | None,
    order: ToolOrder,
    page: PageRequest,
) -> ast.SelectQuery:
    sort_key = ast.Tuple(exprs=[*(parse_expr(key) for key in _ORDER_KEYS[order]), ast.Field(chain=["name"])])
    query = parse_select(
        """
        SELECT
            countIf(is_tool) AS total,
            countIf(is_tool AND is_failing) AS failing,
            countIf(is_tool AND {in_view}) AS matching,
            sumIf(range_traces, NOT is_tool) AS total_traces,
            groupArraySortedIf({take})(tuple({sort_key}, {row}), is_tool AND {in_view}) AS page
        FROM {groups}
        """,
        placeholders={
            "in_view": parse_expr(_VIEWS[view]) if view is not None else ast.Constant(value=True),
            "take": ast.Constant(value=groups_to_sort(page)),
            "sort_key": sort_key,
            "row": ast.Tuple(exprs=fields(_GROUP_COLUMNS)),
            "groups": _groups(team, selection, size, contains_search(ast.Field(chain=["name"]), search)),
        },
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _row(values: Sequence[Any], starts: Sequence[datetime], *, has_previous: bool) -> ToolGroupRow:
    name, calls, previous_calls, traces, results, failures, only_tool_calls, last_seen, is_failing, series = values
    return ToolGroupRow(
        name=name,
        calls=calls,
        previous_calls=previous_calls if has_previous else None,
        traces=traces,
        results=results,
        failures=failures,
        only_tool_calls=only_tool_calls,
        last_seen=last_seen.astimezone(UTC),
        is_failing=bool(is_failing),
        series=series_of(dict(zip(*series)), starts),
    )


def load_tool_page(
    team: Team,
    user: User | None,
    selection: TraceSelection,
    *,
    search: str | None,
    view: ToolView | None,
    order: ToolOrder,
    page: PageRequest,
) -> ToolPageRead:
    """A page of the tools the selection's generations call, the number of tools in `view`, the counts of every view
    and the range's trace count. `search` narrows the tools and the counts, and `view` only the page. The selection's
    window must be in the project's zone, where ClickHouse floors the buckets."""
    size = bucket_size_for(selection.window)
    starts = bucket_starts(selection.window, size)
    has_previous = not has_no_previous_window(selection.window)
    query = _page_query(team, selection, size, search=search, view=view, order=order, page=page)
    [[total, failing, matching, total_traces, rows]] = execute_selection(
        team, user, selection, query, TOOLS_PAGE_QUERY_NAME
    )
    return ToolPageRead(
        rows=tuple(_row(values, starts, has_previous=has_previous) for _, values in rows[page.offset :]),
        count=matching,
        counts=ToolCountsRow(total=total, failing=failing),
        total_traces=total_traces,
        bucket_starts=tuple(starts),
    )


def load_tool_group(team: Team, user: User | None, selection: TraceSelection, name: str) -> ToolGroupDetailRow | None:
    """The tool's group over the selection, with its series over the previous window's buckets and the range's trace
    count, or None when no generation in the range calls the tool."""
    window = selection.window
    size = bucket_size_for(window)
    query = parse_select(
        """
        SELECT
            countIf(is_tool) > 0 AS found,
            anyIf(tuple({row}, previous_series), is_tool) AS tool,
            sumIf(range_traces, NOT is_tool) AS total_traces
        FROM {groups}
        """,
        placeholders={
            "row": ast.Tuple(exprs=fields(_GROUP_COLUMNS)),
            "groups": _groups(team, selection, size, _named(name)),
        },
    )
    [[found, (values, previous_series), total_traces]] = execute_selection(
        team, user, selection, query, TOOL_GROUP_QUERY_NAME
    )
    if not found:
        return None
    has_previous = not has_no_previous_window(window)
    return ToolGroupDetailRow(
        row=_row(values, bucket_starts(window, size), has_previous=has_previous),
        previous_series=series_of(dict(zip(*previous_series)), bucket_starts(previous_window(window), size))
        if has_previous
        else (),
        total_traces=total_traces,
    )


def has_any_tool_call(team: Team, user: User | None, selection: TraceSelection) -> bool:
    """Whether any generation called a tool from the lookback start to the range end, under the lookback's rules."""
    widened = lookback_through_range(selection)
    calls = parse_expr(
        "countIf(event = '$ai_generation' AND notEmpty({names}))",
        placeholders={"names": tool_names(EVENTS_COLUMNS, "$ai_tools_called")},
    )
    query = parse_select(
        "SELECT trace_id FROM {traces} WHERE calls > 0 LIMIT 1",
        placeholders={"traces": grouped_traces(team, widened, {"calls": calls})},
    )
    return bool(execute_selection(team, user, widened, query, TOOLS_CAPTURED_QUERY_NAME))

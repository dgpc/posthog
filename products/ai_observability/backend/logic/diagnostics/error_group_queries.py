from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from posthog.hogql import ast
from posthog.hogql.parser import parse_expr, parse_select

from posthog.dataclasses import frozen
from posthog.models import Team, User
from posthog.paging import PageRequest

from ..traces.lookback import lookback_selection
from ..traces.trace_expression_queries import EVENTS_COLUMNS, error_message, event_anonymous, event_errored
from ..traces.trace_metrics import BucketSize, bucket_size_for, bucket_starts, has_no_previous_window, previous_window
from ..traces.trace_metrics_queries import last_moment, period_bucket
from ..traces.trace_selection import TraceSelection, TraceWindow
from ..traces.trace_selection_queries import execute_selection, grouped_traces
from .error_groups import ErrorCountsRow, ErrorGroupRow, ErrorOrder, ErrorView, first_seen_of, series_of
from .group_page_queries import groups_to_sort
from .trends import Trend, trend_expression

ERRORS_PAGE_QUERY_NAME = "ai_diagnostics_errors_page"
ERROR_GROUP_QUERY_NAME = "ai_diagnostics_error_group"
ERROR_FIRST_SEEN_QUERY_NAME = "ai_diagnostics_error_first_seen"
ERRORS_CAPTURED_QUERY_NAME = "ai_diagnostics_errors_captured"

# Each value is ascending in the page's sort, so a descending order negates its value.
_ORDER_KEYS: dict[ErrorOrder, tuple[str, ...]] = {
    ErrorOrder.OCCURRENCES: ("negate(occurrences)",),
    # A message absent last period has no percent change. It counts as the largest rise, so these messages lead, the
    # larger count first.
    ErrorOrder.CHANGE: (
        "previous_occurrences != 0",
        "negate(if(previous_occurrences = 0, 0, (occurrences - previous_occurrences) / previous_occurrences))",
        "negate(occurrences)",
    ),
    ErrorOrder.TRACES: ("negate(traces)",),
    ErrorOrder.USERS: ("negate(users)",),
    ErrorOrder.LAST_SEEN: ("negate(toUnixTimestamp64Milli(last_seen))",),
}
_VIEWS: dict[ErrorView, str] = {
    ErrorView.INCREASING: f"trend = '{Trend.INCREASING.value}'",
    ErrorView.DECREASING: f"trend = '{Trend.DECREASING.value}'",
    ErrorView.NEW: "is_new",
}
# The columns of one group, in the order a page row and a group read return them.
_GROUP_COLUMNS = (
    "message",
    "occurrences",
    "previous_occurrences",
    "traces",
    "users",
    "models",
    "last_seen",
    "is_new",
    "trend",
    "series",
)


@frozen
class ErrorPageRead:
    rows: tuple[ErrorGroupRow, ...]
    count: int
    counts: ErrorCountsRow
    bucket_starts: tuple[datetime, ...]


@frozen
class ErrorGroupDetailRow:
    row: ErrorGroupRow
    previous_series: tuple[int, ...]


def _message() -> ast.Expr:
    return error_message(EVENTS_COLUMNS)


def _errored_matching(match: ast.Expr) -> ast.Expr:
    return parse_expr(
        "{errored} AND isNotNull({message}) AND {match}",
        placeholders={"errored": event_errored(EVENTS_COLUMNS), "message": _message(), "match": match},
    )


def _searched(search: str | None) -> ast.Expr:
    term = (search or "").strip()
    if not term:
        return ast.Constant(value=True)
    return parse_expr(
        "positionCaseInsensitiveUTF8({message}, {term}) > 0",
        placeholders={"message": _message(), "term": ast.Constant(value=term)},
    )


def _named(messages: Sequence[str]) -> ast.Expr:
    return ast.CompareOperation(op=ast.CompareOperationOp.In, left=_message(), right=ast.Constant(value=list(messages)))


def _period_read(team: Team, selection: TraceSelection, match: ast.Expr, *, current: bool) -> ast.SelectQuery:
    errors = parse_expr(
        "groupArrayIf(tuple({message}, timestamp, ifNull({model}, ''), {person}, {anonymous}), {errored})",
        placeholders={
            "message": _message(),
            "model": ast.Field(chain=["properties", "$ai_model"]),
            "person": EVENTS_COLUMNS.person,
            "anonymous": event_anonymous(EVENTS_COLUMNS),
            "errored": _errored_matching(match),
        },
    )
    return grouped_traces(team, selection, {"current_read": ast.Constant(value=current), "errors": errors})


def _errored_events(team: Team, selection: TraceSelection, match: ast.Expr) -> ast.SelectQuery:
    # Each period reads its traces exactly as the traces list reads that range, so a group's Traces is the count of the
    # list scoped to its message. A trace in both reads keeps only its current row, so the periods never share a trace.
    reads: list[ast.SelectQuery] = [_period_read(team, selection, match, current=True)]
    if not has_no_previous_window(selection.window):
        previous = replace(selection, window=previous_window(selection.window))
        reads.append(_period_read(team, previous, match, current=False))
    source = reads[0] if len(reads) == 1 else ast.SelectSetQuery.create_from_queries(reads, "UNION ALL")
    query = parse_select(
        """
        SELECT
            trace_id,
            in_current,
            tupleElement(error, 1) AS message,
            tupleElement(error, 2) AS timestamp,
            tupleElement(error, 3) AS model,
            tupleElement(error, 4) AS person_id,
            tupleElement(error, 5) AS anonymous
        FROM (
            SELECT
                trace_id,
                max(current_read) AS in_current,
                arrayJoin(tupleElement(argMax(tuple(errors), current_read), 1)) AS error
            FROM {source}
            GROUP BY trace_id
        )
        """,
        placeholders={"source": source},
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _seen_in_lookback(team: Team, selection: TraceSelection, match: ast.Expr) -> ast.SelectQuery:
    """The messages of flagged events in the lookback before the range, which ignores the selection's property
    filters, scopes and search."""
    messages = parse_expr(
        "groupUniqArrayIf({message}, {errored} AND timestamp < {start})",
        placeholders={
            "message": _message(),
            "errored": _errored_matching(match),
            "start": ast.Constant(value=selection.window.start),
        },
    )
    query = parse_select(
        "SELECT arrayJoin(messages) FROM {traces}",
        placeholders={"traces": grouped_traces(team, lookback_selection(selection), {"messages": messages})},
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _bucket(size: BucketSize, window: TraceWindow) -> ast.Expr:
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


def _groups(team: Team, selection: TraceSelection, size: BucketSize, match: ast.Expr) -> ast.SelectQuery:
    """One row per message with a flagged event in the range, over the events `match` keeps."""
    has_previous = not has_no_previous_window(selection.window)
    query = parse_select(
        """
        SELECT
            message,
            countIf(in_current) AS occurrences,
            countIf(NOT in_current) AS previous_occurrences,
            uniqIf(trace_id, in_current) AS traces,
            uniqIf(person_id, in_current AND NOT anonymous) AS users,
            topK(3)(if(in_current AND model != '', model, NULL)) AS models,
            maxIf(timestamp, in_current) AS last_seen,
            message NOT IN {seen_in_lookback} AS is_new,
            {trend} AS trend,
            sumMapIf([bucket], [1], in_current) AS series,
            sumMapIf([bucket], [1], NOT in_current) AS previous_series
        FROM (SELECT *, {bucket} AS bucket FROM {events})
        GROUP BY message
        HAVING occurrences > 0
        """,
        placeholders={
            "seen_in_lookback": _seen_in_lookback(team, selection, match),
            "trend": trend_expression(
                ast.Field(chain=["occurrences"]),
                ast.Field(chain=["previous_occurrences"]) if has_previous else None,
                ast.Field(chain=["is_new"]),
            ),
            "bucket": _bucket(size, selection.window),
            "events": _errored_events(team, selection, match),
        },
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _fields(names: Sequence[str]) -> list[ast.Expr]:
    return [ast.Field(chain=[name]) for name in names]


def _page_query(
    team: Team,
    selection: TraceSelection,
    size: BucketSize,
    *,
    search: str | None,
    view: ErrorView | None,
    order: ErrorOrder,
    page: PageRequest,
) -> ast.SelectQuery:
    sort_key = ast.Tuple(exprs=[*(parse_expr(key) for key in _ORDER_KEYS[order]), ast.Field(chain=["message"])])
    query = parse_select(
        """
        SELECT
            count() AS total,
            countIf(trend = {increasing}) AS increasing,
            countIf(trend = {decreasing}) AS decreasing,
            countIf(is_new) AS new,
            countIf({in_view}) AS matching,
            groupArraySortedIf({take})(tuple({sort_key}, {row}), {in_view}) AS page
        FROM {groups}
        """,
        placeholders={
            "increasing": ast.Constant(value=Trend.INCREASING.value),
            "decreasing": ast.Constant(value=Trend.DECREASING.value),
            "in_view": parse_expr(_VIEWS[view]) if view is not None else ast.Constant(value=True),
            "take": ast.Constant(value=groups_to_sort(page)),
            "sort_key": sort_key,
            "row": ast.Tuple(exprs=_fields(_GROUP_COLUMNS)),
            "groups": _groups(team, selection, size, _searched(search)),
        },
    )
    assert isinstance(query, ast.SelectQuery)
    return query


def _row(values: Sequence[Any], starts: Sequence[datetime], *, has_previous: bool) -> ErrorGroupRow:
    message, occurrences, previous_occurrences, traces, users, models, last_seen, is_new, trend, series = values
    return ErrorGroupRow(
        message=message,
        occurrences=occurrences,
        previous_occurrences=previous_occurrences if has_previous else None,
        traces=traces,
        users=users,
        models=tuple(models),
        last_seen=last_seen.astimezone(UTC),
        is_new=bool(is_new),
        trend=Trend(trend),
        series=series_of(dict(zip(*series)), starts),
    )


def load_error_page(
    team: Team,
    user: User | None,
    selection: TraceSelection,
    *,
    search: str | None,
    view: ErrorView | None,
    order: ErrorOrder,
    page: PageRequest,
) -> ErrorPageRead:
    """A page of the messages of the selection's flagged events, the number of messages in `view`, and the counts of
    every view. `search` narrows the messages and the counts, and `view` only the page. The selection's window must be
    in the project's zone, where ClickHouse floors the buckets."""
    size = bucket_size_for(selection.window)
    starts = bucket_starts(selection.window, size)
    has_previous = not has_no_previous_window(selection.window)
    query = _page_query(team, selection, size, search=search, view=view, order=order, page=page)
    [[total, increasing, decreasing, new, matching, rows]] = execute_selection(
        team, user, selection, query, ERRORS_PAGE_QUERY_NAME
    )
    return ErrorPageRead(
        rows=tuple(_row(values, starts, has_previous=has_previous) for _, values in rows[page.offset :]),
        count=matching,
        counts=ErrorCountsRow(total=total, increasing=increasing, decreasing=decreasing, new=new),
        bucket_starts=tuple(starts),
    )


def load_error_group(
    team: Team, user: User | None, selection: TraceSelection, message: str
) -> ErrorGroupDetailRow | None:
    """The message's group over the selection, with its series over the previous window's buckets, or None when the
    message has no flagged event in the range."""
    window = selection.window
    size = bucket_size_for(window)
    query = parse_select(
        "SELECT {columns}, previous_series FROM {groups} LIMIT 1",
        placeholders={
            "columns": ast.Tuple(exprs=_fields(_GROUP_COLUMNS)),
            "groups": _groups(team, selection, size, _named([message])),
        },
    )
    results = execute_selection(team, user, selection, query, ERROR_GROUP_QUERY_NAME)
    if not results:
        return None
    [[values, previous_series]] = results
    has_previous = not has_no_previous_window(window)
    return ErrorGroupDetailRow(
        row=_row(values, bucket_starts(window, size), has_previous=has_previous),
        previous_series=series_of(dict(zip(*previous_series)), bucket_starts(previous_window(window), size))
        if has_previous
        else (),
    )


def _lookback_through_range(selection: TraceSelection) -> TraceSelection:
    lookback = lookback_selection(selection)
    return replace(lookback, window=TraceWindow(start=lookback.window.start, end=selection.window.end))


def load_error_first_seen(
    team: Team, user: User | None, selection: TraceSelection, messages: Sequence[str]
) -> dict[str, datetime | None]:
    """Each message's earliest flagged event from the lookback start to the range end, None when the message already
    occurs at the lookback start. A message with no flagged event there has no entry."""
    if not messages:
        return {}
    widened = _lookback_through_range(selection)
    earliest = parse_expr(
        "groupArrayIf(tuple({message}, timestamp), {errored})",
        placeholders={"message": _message(), "errored": _errored_matching(_named(messages))},
    )
    query = parse_select(
        """
        SELECT tupleElement(occurrence, 1) AS message, min(tupleElement(occurrence, 2)) AS earliest
        FROM (SELECT arrayJoin(earliest) AS occurrence FROM {traces})
        GROUP BY message
        """,
        placeholders={"traces": grouped_traces(team, widened, {"earliest": earliest})},
    )
    results = execute_selection(team, user, widened, query, ERROR_FIRST_SEEN_QUERY_NAME)
    return {message: first_seen_of(earliest.astimezone(UTC), widened.window.start) for message, earliest in results}


def has_any_error(team: Team, user: User | None, selection: TraceSelection) -> bool:
    """Whether any flagged event with a message was captured from the lookback start to the range end, under the
    lookback's rules."""
    widened = _lookback_through_range(selection)
    errored = parse_expr("countIf({errored})", placeholders={"errored": _errored_matching(ast.Constant(value=True))})
    query = parse_select(
        "SELECT trace_id FROM {traces} WHERE errored > 0 LIMIT 1",
        placeholders={"traces": grouped_traces(team, widened, {"errored": errored})},
    )
    return bool(execute_selection(team, user, widened, query, ERRORS_CAPTURED_QUERY_NAME))

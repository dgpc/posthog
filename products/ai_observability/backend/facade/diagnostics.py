from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Final

from posthog.models import Team, User
from posthog.paging import Page, PageRequest

from ..logic.diagnostics.entity_keys import (
    MalformedEntityKeyError as MalformedEntityKeyError,
    decode_entity_key,
    encode_entity_key,
)
from ..logic.diagnostics.error_group_queries import (
    ErrorPageRead,
    has_any_error,
    load_error_first_seen as load_error_first_seen_rows,
    load_error_group,
    load_error_page,
)
from ..logic.diagnostics.error_groups import (
    ErrorGroupRow,
    ErrorOrder as ErrorOrder,
    ErrorView as ErrorView,
)
from ..logic.diagnostics.tool_group_queries import ToolPageRead, has_any_tool_call, load_tool_group, load_tool_page
from ..logic.diagnostics.tool_groups import (
    ToolGroupRow,
    ToolOrder as ToolOrder,
    ToolView as ToolView,
)
from ..logic.diagnostics.trends import change_label, error_change_tone, failure_rate
from ..logic.traces.lookback import has_no_lookback, seen_before
from ..logic.traces.trace_metrics import bucket_size_for, bucket_starts, has_too_many_buckets
from ..logic.traces.trace_selection import (
    InvalidTraceWindowError as InvalidTraceWindowError,
    TraceSelection,
    TraceWindow,
)
from .contracts import TraceMetricChangeTone
from .diagnostics_contracts import (
    ErrorFirstSeen,
    ErrorFirstSeenList,
    ErrorGroupDetail,
    ErrorListCounts,
    ErrorListItem,
    ErrorPage as ErrorPage,
    ErrorTrend,
    ToolFailureState,
    ToolGroupDetail,
    ToolListCounts,
    ToolListItem,
    ToolPage as ToolPage,
)

ERROR_FIRST_SEEN_MAX_KEYS: Final = 100


class DiagnosticsRangeTooLongError(Exception):
    pass


class ErrorNotFoundError(Exception):
    pass


class ErrorFirstSeenTooManyKeysError(ValueError):
    pass


class ToolNotFoundError(Exception):
    pass


def load_error_list(
    team: Team,
    user: User | None,
    selection: TraceSelection,
    *,
    search: str | None,
    view: ErrorView | None,
    order: ErrorOrder,
    page: PageRequest,
) -> Callable[[], ErrorPage]:
    """Reads a page of the selection's errors, grouped by normalized message, and returns the step that builds it.
    `search` narrows the errors and the counts, and `view` only the page. Raises `DiagnosticsRangeTooLongError` and
    `InvalidTraceWindowError` for a range it cannot read.

    On the first page of a selection with no error, a second read checks whether the project captured any error from
    the lookback start, so the client can tell a quiet range from a project that sends no errors. The step runs no
    ClickHouse query.
    """
    zoned = _readable(team, selection)
    read = load_error_page(team, user, zoned, search=search, view=view, order=order, page=page)
    nothing_captured = page.offset == 0 and read.counts.total == 0 and not has_any_error(team, user, zoned)
    return lambda: _to_error_page(read, nothing_captured)


def get_error(team: Team, user: User | None, selection: TraceSelection, key: str) -> Callable[[], ErrorGroupDetail]:
    """Reads the error `key` names over the selection, with its series over the previous period, and returns the step
    that builds it. Raises `MalformedEntityKeyError` for a key that names no message, `ErrorNotFoundError` when the
    message has no occurrence in the selection, and the range errors of `load_error_list`. The step runs no ClickHouse
    query."""
    message = decode_entity_key(key)
    zoned = _readable(team, selection)
    detail = load_error_group(team, user, zoned, message)
    if detail is None:
        raise ErrorNotFoundError(key)
    return lambda: ErrorGroupDetail(
        item=_to_list_item(detail.row),
        bucket_starts=bucket_starts(zoned.window, bucket_size_for(zoned.window)),
        previous_series=list(detail.previous_series),
    )


def load_error_first_seen(
    team: Team, user: User | None, selection: TraceSelection, keys: Sequence[str]
) -> ErrorFirstSeenList:
    """When each error first occurred, checked from the lookback start to the range end, one entry per distinct key in
    request order. Raises `ErrorFirstSeenTooManyKeysError` past `ERROR_FIRST_SEEN_MAX_KEYS` keys,
    `MalformedEntityKeyError` for a key that names no message, and the range errors of `load_error_list`."""
    requested = list(dict.fromkeys(keys))
    if len(requested) > ERROR_FIRST_SEEN_MAX_KEYS:
        raise ErrorFirstSeenTooManyKeysError(f"Send at most {ERROR_FIRST_SEEN_MAX_KEYS} keys.")
    messages = {key: decode_entity_key(key) for key in requested}
    zoned = _readable(team, selection)
    earliest = load_error_first_seen_rows(team, user, zoned, list(messages.values()))
    bound = seen_before(zoned.window)
    return ErrorFirstSeenList(
        results=[_to_first_seen(key, earliest, message, bound) for key, message in messages.items()]
    )


def load_tool_list(
    team: Team,
    user: User | None,
    selection: TraceSelection,
    *,
    search: str | None,
    view: ToolView | None,
    order: ToolOrder,
    page: PageRequest,
) -> Callable[[], ToolPage]:
    """Reads a page of the tools the selection's generations call, and returns the step that builds it. `search`
    narrows the tools and the counts, and `view` only the page. Raises the range errors of `load_error_list`.

    On the first page of a selection with no tool, a second read checks whether any generation called a tool from the
    lookback start, so the client can tell a quiet range from a project that sends no tool calls. The step runs no
    ClickHouse query.
    """
    zoned = _readable(team, selection)
    read = load_tool_page(team, user, zoned, search=search, view=view, order=order, page=page)
    nothing_captured = page.offset == 0 and read.counts.total == 0 and not has_any_tool_call(team, user, zoned)
    return lambda: _to_tool_page(read, nothing_captured)


def get_tool(team: Team, user: User | None, selection: TraceSelection, key: str) -> Callable[[], ToolGroupDetail]:
    """Reads the tool `key` names over the selection, with its series over the previous period, and returns the step
    that builds it. Raises `MalformedEntityKeyError` for a key that names no tool, `ToolNotFoundError` when no
    generation in the selection calls the tool, and the range errors of `load_error_list`. The step runs no ClickHouse
    query."""
    name = decode_entity_key(key)
    zoned = _readable(team, selection)
    detail = load_tool_group(team, user, zoned, name)
    if detail is None:
        raise ToolNotFoundError(key)
    return lambda: ToolGroupDetail(
        item=_to_tool_item(detail.row, detail.total_traces),
        bucket_starts=bucket_starts(zoned.window, bucket_size_for(zoned.window)),
        previous_series=list(detail.previous_series),
    )


def _readable(team: Team, selection: TraceSelection) -> TraceSelection:
    # Buckets start at midnight in the project's zone, so every step works on the window in that zone.
    zone = team.timezone_info
    zoned = replace(
        selection,
        window=TraceWindow(start=selection.window.start.astimezone(zone), end=selection.window.end.astimezone(zone)),
    )
    if has_too_many_buckets(zoned.window, bucket_size_for(zoned.window)):
        raise DiagnosticsRangeTooLongError()
    if has_no_lookback(zoned.window):
        raise InvalidTraceWindowError("The date range is outside the dates that can be read")
    return zoned


def _to_first_seen(key: str, earliest: Mapping[str, datetime | None], message: str, bound: datetime) -> ErrorFirstSeen:
    if message not in earliest:
        return ErrorFirstSeen(key=key, first_seen=None, seen_before=None)
    first_seen = earliest[message]
    return ErrorFirstSeen(key=key, first_seen=first_seen, seen_before=bound if first_seen is None else None)


def _to_error_page(read: ErrorPageRead, nothing_captured: bool) -> ErrorPage:
    return ErrorPage(
        page=Page(results=[_to_list_item(row) for row in read.rows], count=read.count),
        counts=ErrorListCounts(
            total=read.counts.total,
            increasing=read.counts.increasing,
            decreasing=read.counts.decreasing,
            new=read.counts.new,
        ),
        bucket_starts=list(read.bucket_starts),
        nothing_captured=nothing_captured,
    )


def _to_list_item(row: ErrorGroupRow) -> ErrorListItem:
    return ErrorListItem(
        key=encode_entity_key(row.message),
        message=row.message,
        occurrences=row.occurrences,
        change_label=change_label(row.occurrences, row.previous_occurrences),
        change_tone=TraceMetricChangeTone(error_change_tone(row.trend).value),
        trend=ErrorTrend(row.trend.value),
        is_new=row.is_new,
        traces=row.traces,
        users=row.users,
        models=list(row.models),
        last_seen=row.last_seen,
        series=list(row.series),
    )


def _to_tool_page(read: ToolPageRead, nothing_captured: bool) -> ToolPage:
    return ToolPage(
        page=Page(results=[_to_tool_item(row, read.total_traces) for row in read.rows], count=read.count),
        counts=ToolListCounts(total=read.counts.total, failing=read.counts.failing),
        bucket_starts=list(read.bucket_starts),
        nothing_captured=nothing_captured,
    )


def _to_tool_item(row: ToolGroupRow, total_traces: int) -> ToolListItem:
    rate = failure_rate(row.results, row.failures)
    return ToolListItem(
        key=encode_entity_key(row.name),
        name=row.name,
        calls=row.calls,
        change_label=change_label(row.calls, row.previous_calls),
        change_tone=TraceMetricChangeTone.NEUTRAL,
        traces=row.traces,
        trace_share=_share(row.traces, total_traces),
        failure_rate=rate,
        failures=None if rate is None else row.failures,
        failure_state=ToolFailureState.NOT_CAPTURED if rate is None else ToolFailureState.CAPTURED,
        is_failing=row.is_failing,
        only_tool_share=_share(row.only_tool_calls, row.calls),
        last_seen=row.last_seen,
        series=list(row.series),
    )


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0

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
from ..logic.diagnostics.trends import change_label, error_change_tone
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
)

ERROR_FIRST_SEEN_MAX_KEYS: Final = 100


class DiagnosticsRangeTooLongError(Exception):
    pass


class ErrorNotFoundError(Exception):
    pass


class ErrorFirstSeenTooManyKeysError(ValueError):
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

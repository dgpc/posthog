from datetime import UTC, datetime, timedelta

from posthog.schema import EventPropertyFilter, PropertyOperator

from products.ai_observability.backend.logic.traces.lookback import (
    LOOKBACK,
    lookback_selection,
    lookback_window,
    seen_before,
)
from products.ai_observability.backend.logic.traces.trace_selection import TraceGroupScope, TraceSelection, TraceWindow

WINDOW = TraceWindow(start=datetime(2026, 11, 18, tzinfo=UTC), end=datetime(2026, 11, 25, tzinfo=UTC))


def test_lookback_window_ends_at_the_range_start_and_spans_thirty_days():
    assert lookback_window(WINDOW) == TraceWindow(
        start=datetime(2026, 10, 19, tzinfo=UTC), end=datetime(2026, 11, 18, tzinfo=UTC)
    )
    assert LOOKBACK == timedelta(days=30)


def test_lookback_selection_keeps_only_the_test_account_and_support_switches():
    selection = TraceSelection(
        window=WINDOW,
        properties=(EventPropertyFilter(key="plan", value="pro", operator=PropertyOperator.EXACT),),
        filter_test_accounts=True,
        include_support_traces=True,
        search="refund",
        person_id="person-1",
        error="Rate limit reached",
        tool="search_docs",
        failed_tool="run_query",
        group=TraceGroupScope(key="acme", type_index=0),
        quick_filter="errors",
    )

    assert lookback_selection(selection) == TraceSelection(
        window=lookback_window(WINDOW), filter_test_accounts=True, include_support_traces=True
    )


def test_seen_before_is_the_lookback_start():
    assert seen_before(WINDOW) == datetime(2026, 10, 19, tzinfo=UTC)

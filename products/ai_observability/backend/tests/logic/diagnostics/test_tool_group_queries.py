from typing import Any

from posthog.test.base import BaseTest, ClickhouseTestMixin

from parameterized import parameterized

from posthog.paging import PageRequest

from products.ai_observability.backend.logic.diagnostics.tool_group_queries import (
    ToolPageRead,
    has_any_tool_call,
    load_tool_group,
    load_tool_page,
)
from products.ai_observability.backend.logic.diagnostics.tool_groups import (
    ToolCountsRow,
    ToolGroupRow,
    ToolOrder,
    ToolView,
)
from products.ai_observability.backend.logic.diagnostics.trends import failure_rate
from products.ai_observability.backend.logic.traces.trace_metrics import BucketSize, bucket_starts
from products.ai_observability.backend.logic.traces.trace_selection import TraceSelection

from ..traces.test_trace_selection_queries import at, seed, to_selection

DAY = 24 * 60

FETCH_PAGE = "fetch_page"
LOOKUP = "lookup"
PARSE_PDF = "parse_pdf"
SEND_EMAIL = "send_email"


def _generation(
    minute: int,
    trace_id: str,
    *,
    called: str | None = None,
    results: str | None = None,
    errors: str | None = None,
    event: str = "$ai_generation",
) -> dict[str, Any]:
    properties = {
        name: value
        for name, value in (("$ai_tools_called", called), ("$ai_tool_results", results), ("$ai_tool_errors", errors))
        if value is not None
    }
    return {"event": event, "trace_id": trace_id, "minutes": minute, "properties": properties}


def _calls(name: str, minutes: range, *, prefix: str, traces: int) -> list[dict[str, Any]]:
    return [_generation(minute, f"{prefix}-{index % traces}", called=name) for index, minute in enumerate(minutes)]


def _repeated(name: str, times: int) -> str:
    return ",".join([name] * times)


class TestToolGroupQueries(ClickhouseTestMixin, BaseTest):
    def setUp(self) -> None:
        super().setUp()
        # Away from UTC, a bucket floored in UTC instead of the project's zone lands half an hour off.
        self.team.timezone = "Asia/Kolkata"
        self.team.save()

    def _selection(self, **overrides: Any) -> TraceSelection:
        return to_selection(self.team, {"date_from": 0, "date_to": 120, **overrides})

    def _page(
        self,
        *,
        search: str | None = None,
        view: ToolView | None = None,
        order: ToolOrder = ToolOrder.CALLS,
        limit: int = 50,
        offset: int = 0,
    ) -> ToolPageRead:
        return load_tool_page(
            self.team,
            self.user,
            self._selection(),
            search=search,
            view=view,
            order=order,
            page=PageRequest(limit=limit, offset=offset),
        )

    def _seed_tools(self) -> None:
        seed(
            self.team,
            [
                *_calls(FETCH_PAGE, range(10, 31, 10), prefix="fetch", traces=3),
                _generation(11, "fetch-0", results=_repeated(FETCH_PAGE, 20), errors=FETCH_PAGE),
                *_calls(FETCH_PAGE, range(-100, -79, 10), prefix="fetch-before", traces=1),
                *_calls(SEND_EMAIL, range(56, 61), prefix="email", traces=1),
                _generation(61, "email-0", results=_repeated(SEND_EMAIL, 20)),
                *_calls(PARSE_PDF, range(100, 111, 10), prefix="pdf", traces=2),
                _generation(101, "pdf-0", results=_repeated(PARSE_PDF, 19), errors=_repeated(PARSE_PDF, 19)),
                *_calls(PARSE_PDF, range(-50, -49), prefix="pdf-before", traces=1),
                *_calls(LOOKUP, range(80, 91, 3), prefix="lookup", traces=2),
                *_calls(LOOKUP, range(-115, -44, 10), prefix="lookup-before", traces=1),
            ],
        )

    def test_calls_count_each_trimmed_part_of_a_generation(self) -> None:
        seed(
            self.team,
            [
                _generation(10, "plan-1", called="search_docs, run_query"),
                _generation(40, "plan-2", called="lookup,, run_query,"),
                _generation(100, "plan-3", called="run_query,run_query"),
                _generation(50, "plan-4", called="run_query", event="$ai_span"),
                _generation(51, "plan-4"),
                _generation(-60, "plan-before", called="run_query"),
            ],
        )

        read = self._page()

        assert read.rows[0] == ToolGroupRow(
            name="run_query",
            calls=4,
            previous_calls=1,
            traces=3,
            results=0,
            failures=0,
            only_tool_calls=0,
            last_seen=at(100),
            is_failing=False,
            series=(1, 1, 2),
        )
        assert [row.name for row in read.rows] == ["run_query", "lookup", "search_docs"]
        assert read.total_traces == 4
        assert read.bucket_starts == tuple(bucket_starts(self._selection().window, BucketSize.HOUR))

    def test_results_and_failures_come_from_later_generations_by_name(self) -> None:
        seed(
            self.team,
            [
                _generation(10, "pay-1", called="charge_card, refund"),
                _generation(11, "pay-1", results="charge_card, refund ", errors=" charge_card"),
                _generation(20, "pay-2", called="charge_card,lookup"),
                _generation(21, "pay-2", results="charge_card"),
                _generation(-50, "pay-before", called="charge_card"),
                _generation(-49, "pay-before", results="charge_card", errors="charge_card"),
            ],
        )

        rows = {row.name: row for row in self._page().rows}

        assert {name: (row.results, row.failures) for name, row in rows.items()} == {
            "charge_card": (2, 1),
            "refund": (1, 0),
            "lookup": (0, 0),
        }
        assert failure_rate(rows["lookup"].results, rows["lookup"].failures) is None

    def test_only_tool_counts_single_tool_generations(self) -> None:
        seed(
            self.team,
            [
                _generation(10, "only-1", called="search"),
                _generation(11, "only-1", called=" search , "),
                _generation(12, "only-2", called="search, fetch"),
                _generation(13, "only-2", called="search,search"),
            ],
        )

        [row] = [row for row in self._page().rows if row.name == "search"]

        assert (row.calls, row.only_tool_calls) == (5, 2)

    def test_failing_view_and_count_agree_with_is_failing(self) -> None:
        self._seed_tools()

        everything = self._page()
        failing = self._page(view=ToolView.FAILING)

        assert {row.name: row.is_failing for row in everything.rows} == {
            FETCH_PAGE: True,
            SEND_EMAIL: False,
            PARSE_PDF: False,
            LOOKUP: False,
        }
        assert [row.name for row in failing.rows] == [FETCH_PAGE]
        assert failing.count == 1
        assert failing.counts == everything.counts == ToolCountsRow(total=4, failing=1)

    @parameterized.expand(
        [
            ("calls", [SEND_EMAIL, LOOKUP, FETCH_PAGE, PARSE_PDF]),
            ("change", [SEND_EMAIL, PARSE_PDF, FETCH_PAGE, LOOKUP]),
            ("traces", [FETCH_PAGE, LOOKUP, PARSE_PDF, SEND_EMAIL]),
            ("failure_rate", [PARSE_PDF, FETCH_PAGE, SEND_EMAIL, LOOKUP]),
            ("last_seen", [PARSE_PDF, LOOKUP, SEND_EMAIL, FETCH_PAGE]),
        ]
    )
    def test_order_then_name_is_stable_across_pages(self, order: ToolOrder, expected: list[str]) -> None:
        self._seed_tools()

        pages = [self._page(order=order, limit=2, offset=offset) for offset in (0, 2)]

        assert [row.name for page in pages for row in page.rows] == expected
        assert {page.count for page in pages} == {4}

    def test_group_reads_one_tool_with_the_previous_series_and_the_trace_count(self) -> None:
        self._seed_tools()
        page = self._page()
        [page_row] = [row for row in page.rows if row.name == LOOKUP]

        detail = load_tool_group(self.team, self.user, self._selection(), LOOKUP)

        assert detail is not None
        assert detail.row == page_row
        assert detail.previous_series == (3, 5, 0)
        assert detail.total_traces == page.total_traces == 8
        assert load_tool_group(self.team, self.user, self._selection(), "never_called") is None

    @parameterized.expand(
        [
            ("ascii", "SEARCH", ["SearchDocs", "web_search"]),
            ("accented", "ÍNDICE", ["búsqueda_índice"]),
            ("no_match", "translate", []),
        ]
    )
    def test_search_matches_tool_name_ignoring_case(self, _name: str, search: str, expected: list[str]) -> None:
        seed(
            self.team,
            [
                _generation(10 + index, f"search-{index}", called=name)
                for index, name in enumerate(["SearchDocs", "web_search", "búsqueda_índice", "fetch"])
            ],
        )

        read = self._page(search=search)

        assert [row.name for row in read.rows] == expected
        assert read.counts.total == len(expected)
        assert read.total_traces == 4

    @parameterized.expand([("inside_the_lookback", 20, True), ("past_the_lookback", 40, False)])
    def test_any_tool_call_looks_back_from_the_range(self, _name: str, days_before: int, expected: bool) -> None:
        seed(
            self.team,
            [
                _generation(-days_before * DAY, "called", called="run_query"),
                _generation(10, "not-called", results="run_query"),
            ],
        )

        assert has_any_tool_call(self.team, self.user, self._selection()) is expected

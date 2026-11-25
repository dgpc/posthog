from typing import Any

from posthog.test.base import BaseTest, ClickhouseTestMixin

from parameterized import parameterized

from posthog.hogql import ast
from posthog.hogql.query import execute_hogql_query

from products.ai_observability.backend.logic.diagnostics.trends import (
    Trend,
    change_label,
    error_change_tone,
    failing_expression,
    failure_rate,
    trend_expression,
)
from products.ai_observability.backend.logic.traces.trace_metrics import ChangeTone


class TestTrendExpressions(ClickhouseTestMixin, BaseTest):
    def _select(self, expr: ast.Expr) -> Any:
        response = execute_hogql_query(query=ast.SelectQuery(select=[expr]), team=self.team)
        assert response.results is not None
        [(value,)] = response.results
        return value

    @parameterized.expand(
        [
            ("rise_of_exactly_the_minimum_percent", 12, 10, False, Trend.INCREASING),
            ("rise_under_the_minimum_percent", 11, 10, False, Trend.STEADY),
            ("from_zero_at_the_minimum_occurrences", 10, 0, False, Trend.INCREASING),
            ("from_zero_under_the_minimum_occurrences", 9, 0, False, Trend.STEADY),
            ("new_wins_over_a_rise_from_zero", 10, 0, True, Trend.STEADY),
            ("fall_of_exactly_the_minimum_percent", 8, 10, False, Trend.DECREASING),
            ("fall_from_under_the_minimum_occurrences", 7, 9, False, Trend.STEADY),
            ("no_previous_window", 5, None, False, Trend.STEADY),
        ]
    )
    def test_trend(self, _name: str, current: int, previous: int | None, is_new: bool, expected: Trend) -> None:
        expr = trend_expression(
            ast.Constant(value=current),
            ast.Constant(value=previous) if previous is not None else None,
            ast.Constant(value=is_new),
        )

        assert Trend(self._select(expr)) == expected

    def test_a_group_absent_from_the_previous_window_rises_from_zero(self) -> None:
        expr = trend_expression(ast.Constant(value=10), ast.Constant(value=None), ast.Constant(value=False))

        assert Trend(self._select(expr)) == Trend.INCREASING

    @parameterized.expand(
        [
            ("at_the_minimum_rate_and_results", 20, 1, True),
            ("under_the_minimum_results", 19, 5, False),
            ("under_the_minimum_rate", 100, 4, False),
        ]
    )
    def test_failing(self, _name: str, results: int, failures: int, expected: bool) -> None:
        expr = failing_expression(ast.Constant(value=results), ast.Constant(value=failures))

        assert bool(self._select(expr)) is expected


@parameterized.expand(
    [
        ("rise", 412, 251, "+64%"),
        ("fall_with_a_minus_sign", 121, 125, "−3%"),
        ("rise_just_under_the_increase_minimum", 1195, 1000, "+19%"),
        ("rise_of_exactly_the_increase_minimum", 12, 10, "+20%"),
        ("fall_of_exactly_the_decrease_minimum", 8, 10, "−20%"),
        ("fall_just_under_the_decrease_minimum", 805, 1000, "−19%"),
        ("from_zero", 84, 0, "was 0"),
        ("no_previous_window", 5, None, None),
        ("nothing_in_either_period", 0, 0, "0%"),
    ]
)
def test_change_label(_name: str, current: int, previous: int | None, expected: str | None) -> None:
    assert change_label(current, previous) == expected


@parameterized.expand([("some_results", 40, 3, 0.075), ("no_results", 0, 0, None)])
def test_failure_rate(_name: str, results: int, failures: int, expected: float | None) -> None:
    assert failure_rate(results, failures) == expected


@parameterized.expand(
    [
        ("increasing", Trend.INCREASING, ChangeTone.BAD),
        ("decreasing", Trend.DECREASING, ChangeTone.GOOD),
        ("steady", Trend.STEADY, ChangeTone.NEUTRAL),
    ]
)
def test_error_change_tone(_name: str, trend: Trend, expected: ChangeTone) -> None:
    assert error_change_tone(trend) == expected

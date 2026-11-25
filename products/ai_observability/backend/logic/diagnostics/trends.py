from enum import StrEnum

from posthog.hogql import ast
from posthog.hogql.parser import parse_expr

from products.ai_observability.backend.logic.traces.trace_metrics import ChangeTone

INCREASE_MIN_PERCENT = 20
DECREASE_MIN_PERCENT = 20
TREND_MIN_OCCURRENCES = 10
FAILING_MIN_RATE = 0.05
FAILING_MIN_RESULTS = 20

MINUS_SIGN = "−"


class Trend(StrEnum):
    INCREASING = "increasing"
    DECREASING = "decreasing"
    STEADY = "steady"


def trend_expression(current: ast.Expr, previous: ast.Expr | None, is_new: ast.Expr) -> ast.Expr:
    """The trend's string value from this period's occurrences against the previous window's, `previous` being None
    when the range has no previous window. A null `previous` is a group that did not occur in the previous window."""
    if previous is None:
        return ast.Constant(value=Trend.STEADY.value)
    return parse_expr(
        """
        multiIf(
            {is_new}, {steady},
            {current} >= {min_occurrences} AND {current} * 100 >= {previous} * {increase_ratio}, {increasing},
            {previous} >= {min_occurrences} AND {current} * 100 <= {previous} * {decrease_ratio}, {decreasing},
            {steady}
        )
        """,
        placeholders={
            "current": current,
            "previous": ast.Call(name="ifNull", args=[previous, ast.Constant(value=0)]),
            "is_new": is_new,
            "min_occurrences": ast.Constant(value=TREND_MIN_OCCURRENCES),
            "increase_ratio": ast.Constant(value=100 + INCREASE_MIN_PERCENT),
            "decrease_ratio": ast.Constant(value=100 - DECREASE_MIN_PERCENT),
            "increasing": ast.Constant(value=Trend.INCREASING.value),
            "decreasing": ast.Constant(value=Trend.DECREASING.value),
            "steady": ast.Constant(value=Trend.STEADY.value),
        },
    )


def failing_expression(results: ast.Expr, failures: ast.Expr) -> ast.Expr:
    return parse_expr(
        "{results} >= {min_results} AND {failures} / {results} >= {min_rate}",
        placeholders={
            "results": results,
            "failures": failures,
            "min_results": ast.Constant(value=FAILING_MIN_RESULTS),
            "min_rate": ast.Constant(value=FAILING_MIN_RATE),
        },
    )


def change_label(current: int, previous: int | None) -> str | None:
    if previous is None:
        return None
    if previous == 0:
        return "was 0" if current else "0%"
    # Truncating toward zero in integers keeps the label under a threshold whenever the trend is, as +19.5% reads
    # "+19%" and not "+20%" beside a steady trend.
    percent = abs(current - previous) * 100 // previous
    if percent == 0:
        return "0%"
    return f"+{percent}%" if current > previous else f"{MINUS_SIGN}{percent}%"


def failure_rate(results: int, failures: int) -> float | None:
    return failures / results if results else None


def error_change_tone(trend: Trend) -> ChangeTone:
    match trend:
        case Trend.INCREASING:
            return ChangeTone.BAD
        case Trend.DECREASING:
            return ChangeTone.GOOD
        case Trend.STEADY:
            return ChangeTone.NEUTRAL

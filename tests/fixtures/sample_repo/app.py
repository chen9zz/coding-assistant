from decimal import Decimal


class Calculator:
    def divide(self, numerator: Decimal, denominator: Decimal) -> Decimal:
        return numerator / denominator


def format_result(value: Decimal) -> str:
    return f"{value:.2f}"


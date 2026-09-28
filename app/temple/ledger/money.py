from __future__ import annotations

from decimal import Decimal

CENT = Decimal("0.01")


def to_cents(value: Decimal | int | str) -> int:
    """Convert a yuan amount to integer cents, rejecting sub-cent precision."""
    if isinstance(value, bool):
        raise ValueError("金额格式不正确")
    if isinstance(value, int):
        cents = value * 100
    else:
        amount = Decimal(str(value))
        quantized = amount.quantize(CENT)
        if quantized != amount:
            raise ValueError("金额最多精确到分")
        cents = int(quantized * 100)
    if cents <= 0:
        raise ValueError("金额必须大于零")
    return cents


def yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}{value // 100}.{value % 100:02d}"

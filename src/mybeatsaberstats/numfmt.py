"""画面に出す数値の整形（四捨五入ではなく切り捨て）。

四捨五入だと、まだ届いていない値が届いたように見える（例: 99.96% → 100.0%、★9.996 → 10.00）。
画面の数値はすべてここで 0 に向かって切り捨てる。
"""
from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Optional

# float の計算誤差を消すための桁。0.29 * 100 = 28.999999999999996 を 29 として扱う。
_NOISE_PLACES = Decimal("1e-9")


def _to_decimal(value: float, digits: int) -> Optional[Decimal]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    try:
        d = Decimal(repr(f)).quantize(_NOISE_PLACES, rounding=ROUND_HALF_EVEN)
        d = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)
    except InvalidOperation:
        return None
    if d.is_zero():
        # -0.00 を 0.00 にする
        d = d.copy_abs()
    return d


def trunc(value: float, digits: int = 0) -> float:
    """value を小数 digits 桁で 0 に向かって切り捨てた値を返す（比較・差分用）。"""
    d = _to_decimal(value, digits)
    if d is None:
        return float(value)
    return float(d)


def trunc_int(value: float) -> int:
    """value を整数に切り捨てる（0 に向かって）。"""
    d = _to_decimal(value, 0)
    if d is None:
        return int(value)
    return int(d)


def fmt(value: float, digits: int = 0, *, comma: bool = False, sign: bool = False) -> str:
    """value を小数 digits 桁で切り捨てて文字列にする。

    comma=True で 3 桁区切り、sign=True で +/- を付ける（差分の表示用）。
    """
    d = _to_decimal(value, digits)
    if d is None:
        return str(value)
    spec = ("+" if sign else "") + ("," if comma else "") + f".{digits}f"
    return format(d, spec)

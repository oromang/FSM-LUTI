"""Explicit continuation of positive demographic and employment series.

The default continuation matches the Zurich FSM's scenario_generation.py:
use the recent log trend, then multiply each successive annual log increment
by damping**step. Fractional dates interpolate the resulting annual stocks.
Published years always retain linear interpolation of the source values.
"""
from dataclasses import asdict, dataclass
import math
import numpy as np


@dataclass(frozen=True)
class ProjectionExtension:
    method: str = "error"
    lookback_years: float = 10.0
    damping: float = 0.8
    before_source: str = "error"

    @classmethod
    def from_value(cls, value=None):
        result = value if isinstance(value, cls) else cls(**(value or {}))
        if result.method not in {"error", "hold", "damped_log"}:
            raise ValueError("projection_extension.method must be error, hold or damped_log")
        if result.before_source not in {"error", "hold"}:
            raise ValueError("projection_extension.before_source must be error or hold")
        if not np.isfinite(result.lookback_years) or result.lookback_years <= 0:
            raise ValueError("Projection lookback_years must be positive and finite")
        if not np.isfinite(result.damping) or not 0 <= result.damping <= 1:
            raise ValueError("Projection damping must be between zero and one")
        return result

    def as_dict(self):
        return asdict(self)


def evaluate_series(years, values, year, options=None):
    """Return a stock without silently extrapolating or inventing zero support."""
    opts = ProjectionExtension.from_value(options)
    x, y = np.asarray(years, dtype=float), np.asarray(values, dtype=float)
    if x.ndim != 1 or y.shape != x.shape or not len(x):
        raise ValueError("Projection series requires aligned, nonempty one-dimensional arrays")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or (y < 0).any():
        raise ValueError("Invalid projection values or years")
    if not np.isfinite(year):
        raise ValueError("Invalid projection year")
    order = np.argsort(x)
    x, y = x[order], y[order]
    if (np.diff(x) <= 0).any():
        raise ValueError("Duplicate projection years")
    if year < x[0]:
        if opts.before_source == "hold":
            return float(y[0])
        raise ValueError(f"Year {year} outside projection support {x[0]:g}..{x[-1]:g}; historical backcasting is disabled")
    if year <= x[-1]:
        return float(np.interp(year, x, y))
    if opts.method == "error":
        raise ValueError(f"Year {year} outside projection support {x[0]:g}..{x[-1]:g}")
    if opts.method == "hold" or len(x) == 1 or y[-1] == 0:
        return float(y[-1])
    positive = np.flatnonzero(y > 0)
    recent = positive[x[positive] >= max(x[-1] - opts.lookback_years, x[0])]
    if len(recent) < 2:
        recent = positive[-2:]
    if len(recent) < 2:
        return float(y[-1])
    first, last = recent[0], recent[-1]
    rate = (math.log(y[last]) - math.log(y[first])) / max(x[last] - x[first], 1.0)
    extra = float(year - x[-1])
    whole = int(math.floor(extra))

    def annual_stock(n):
        # fsum follows the original FSM's annual damping convention exactly;
        # damping=0 holds the terminal stock, damping=1 gives constant CAGR.
        effective_years = math.fsum(opts.damping ** step for step in range(1, n + 1))
        try:
            result = math.exp(math.log(y[-1]) + rate * effective_years)
        except OverflowError as exc:
            raise ValueError("Projection continuation overflow; revise horizon or damping") from exc
        if not np.isfinite(result):
            raise ValueError("Projection continuation is not finite")
        return result

    a = annual_stock(whole)
    fraction = extra - whole
    return float(a if fraction == 0 else a + fraction * (annual_stock(whole + 1) - a))

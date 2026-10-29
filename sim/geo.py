"""Great-circle helpers (lon/lat degrees, spherical earth)."""
from __future__ import annotations

import math

R_KM = 6371.0088


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_KM * math.asin(min(1.0, math.sqrt(h)))


def interpolate(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    """Point at fraction t in [0,1] along the great circle a->b (slerp)."""
    d = haversine_km(a, b) / R_KM
    if d < 1e-9:
        return a
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    A, B = math.sin((1 - t) * d) / math.sin(d), math.sin(t * d) / math.sin(d)
    x = A * math.cos(la1) * math.cos(lo1) + B * math.cos(la2) * math.cos(lo2)
    y = A * math.cos(la1) * math.sin(lo1) + B * math.cos(la2) * math.sin(lo2)
    z = A * math.sin(la1) + B * math.sin(la2)
    return (round(math.degrees(math.atan2(y, x)), 5),
            round(math.degrees(math.atan2(z, math.hypot(x, y))), 5))


class Path:
    """Piecewise great-circle path through waypoints with cumulative distances."""

    def __init__(self, pts: list[tuple[float, float]]):
        self.pts = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + haversine_km(a, b))
        self.total = self.cum[-1]

    def frac(self, i: int) -> float:
        return self.cum[i] / self.total if self.total else 1.0

    def at(self, progress: float) -> tuple[float, float]:
        progress = min(1.0, max(0.0, progress))
        d = progress * self.total
        for i in range(1, len(self.pts)):
            if d <= self.cum[i] or i == len(self.pts) - 1:
                span = self.cum[i] - self.cum[i - 1]
                t = 0.0 if span <= 0 else (d - self.cum[i - 1]) / span
                return interpolate(self.pts[i - 1], self.pts[i], min(1.0, max(0.0, t)))
        return self.pts[-1]

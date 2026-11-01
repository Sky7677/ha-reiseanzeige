"""Kleine Geometrie-Helfer (Kugel, lokale Ebene). Ohne Abhängigkeiten."""

from __future__ import annotations

import math

R = 6371008.8


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Abstand in Metern (äquirektangulär, auf Gleisabstände genau genug)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    x = (lon2 - lon1) * math.cos((lat1 + lat2) / 2)
    return math.hypot(x, lat2 - lat1) * R


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Richtung von a nach b in Grad, 0 = Nord, im Uhrzeigersinn."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    x = math.sin(lon2 - lon1) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def turn(a: float, b: float) -> float:
    """Betrag der Richtungsänderung zwischen zwei Richtungen (0…180 Grad)."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def simplify(points: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker in einer lokalen Ebene (Meter); Endpunkte bleiben."""
    if len(points) <= 2:
        return list(points)
    lat0 = math.radians(points[0][0])
    kx, ky = R * math.cos(lat0) * math.pi / 180, R * math.pi / 180
    xy = [(p[1] * kx, p[0] * ky) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
            d = math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
            if d > best:
                best, idx = d, k
        if best > tol_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]

"""Coordinate validation and JSON results."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import numpy as np
import s2geometry as s2
import s2sphere
from .geometry import unit
from .calculator import mu_field


def parse_coordinates(values):
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("Supply exactly three coordinates.")
    points = []
    for i, value in enumerate(values, 1):
        if isinstance(value, dict) and set(value) == {"lat", "lng"}:
            value = str(value["lat"]) + "," + str(value["lng"])
        if not isinstance(value, str) or len(value) > 100 or not value.isascii():
            raise ValueError(f"Coordinate {i}: use latitude, longitude.")
        parts = value.split(",")
        if len(parts) != 2:
            raise ValueError(f"Coordinate {i}: use latitude, longitude.")
        try:
            lat, lng = (Decimal(p.strip()) for p in parts)
            if not lat.is_finite() or not lng.is_finite():
                raise ValueError(f"Coordinate {i}: numbers must be finite.")
            if not -90 <= lat <= 90 or not -180 <= lng <= 180:
                raise ValueError(
                    f"Coordinate {i}: latitude must be -90 to 90; longitude -180 to 180."
                )
            points.append(
                tuple(
                    (
                        int((v * 1000000).to_integral_value(rounding=ROUND_HALF_UP))
                        for v in (lat, lng)
                    )
                )
            )
        except InvalidOperation:
            raise ValueError(
                f"Coordinate {i}: use decimal numbers, such as 52.245944, 10.539506."
            ) from None
    if len(set(points)) != 3:
        raise ValueError(
            "The three coordinates must be distinct at six decimal places."
        )
    loop = s2.S2Loop([s2.S2LatLng.FromE6(*p).ToPoint() for p in points])
    if not loop.IsValid():
        raise ValueError("These coordinates do not form a valid spherical triangle.")
    loop.Normalize()
    vectors = [unit(*p) for p in points]
    if (
        abs(float(np.dot(vectors[0], np.cross(vectors[1], vectors[2])))) < 1e-18
        or loop.GetArea() <= 0
    ):
        raise ValueError("The triangle must have a nonzero area.")
    return points


def calculate(grump, values):
    points = parse_coordinates(values)
    result = mu_field(grump, points)
    return {
        "coordinates": [
            f"{lat / 1000000.0:.6f}, {lng / 1000000.0:.6f}" for (lat, lng) in points
        ],
        "mu": result["mu"],
        "area_km2": result["area"],
        "raw_mu": result["total"],
        "regional_total_mu": result["regional_total"],
        "regions": [
            {
                "s2_token": s2sphere.CellId(cid).to_token(),
                "raw_mu": raw,
                "minimum_applied_mu": max(1, raw),
            }
            for (cid, raw) in sorted(result["regions"].items())
        ],
        "cells": [
            {
                "s2_id": str(cid),
                "s2_token": s2sphere.CellId(cid).to_token(),
                "level": level,
                "overlap_km2": area,
                "density": density,
                "raw_mu": area * density,
            }
            for (cid, level, area, density) in result["cells"]
        ],
    }

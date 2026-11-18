"""MU calculation and level-six regional minimum."""

import collections
import math
import s2sphere
from .covering import cover
from .geometry import overlaps, density

MODEL = "grump2000-s2java2011-min6-l6min1"


def round_mu(total):
    return max(1, math.floor(total + 0.5))


def region_result(pts, result):
    groups = collections.defaultdict(float)
    for cid, level, area, density in result["cells"]:
        cell = s2sphere.CellId(cid)
        groups[cell.parent(6).id()] += area * density
    return round_mu(sum(max(1, value) for value in groups.values())), dict(groups)


def mu_field(grump, points):
    cell_ids = cover(points)
    intersections, area = overlaps(points, cell_ids)
    cells = []
    total = 0.0
    for cell_id, level, overlap in intersections:
        population = density(grump, cell_id)
        cells.append((cell_id, level, overlap, population))
        total += overlap * population
    result = dict(total=total, area=area, cells=cells)
    result["mu"], result["regions"] = region_result(points, result)
    result["regional_total"] = sum(
        max(1, value) for value in result["regions"].values()
    )
    return result

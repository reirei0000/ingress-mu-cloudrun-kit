"""Population mosaic, descendant sampling and spherical polygon intersections."""

import glob
import math
import os
import numpy as np
import s2sphere

RE = 6371.01
CACHE_LIMIT = 100_000
D = 1 / 120


class Grump:
    """Memory-mapped mosaic; sum valid counts where country rasters overlap."""

    def __init__(self, folder=None):
        folder = folder or os.environ.get("GRUMP_DIR", "/data/grump/bil")
        self.folder = folder
        self.density_table = {}
        self.r = []
        self.density_cache = {}
        for h in sorted(glob.glob(os.path.join(folder, "*up00ag.hdr"))):
            iso = os.path.basename(h)[:3]
            with open(h) as header:
                hd = {
                    line.split()[0]: line.split()[1] for line in header if line.strip()
                }
            nr, nc = (int(hd["NROWS"]), int(hd["NCOLS"]))
            a = np.memmap(h[:-4] + ".bil", dtype="<f4", mode="r", shape=(nr, nc))
            gi0 = round((float(hd["ULXMAP"]) - D / 2 + 180) / D)
            gj0 = round((90 - (float(hd["ULYMAP"]) + D / 2)) / D)
            self.r.append((iso, gi0, gj0, a))

    def pix(self, lat, lng):
        """Global pixel indices for samples (half-pixel offset; see docs/METHOD.md)."""
        return (
            np.floor((lng - D / 2 + 180) / D).astype(np.int64),
            np.floor((90 - (lat + D / 2)) / D).astype(np.int64),
        )

    def sample(self, lat, lng):
        lat = np.atleast_1d(np.asarray(lat, float))
        lng = np.atleast_1d(np.asarray(lng, float))
        gi, gj = self.pix(lat, lng)
        tot = np.zeros(len(lat))
        lo_i, hi_i, lo_j, hi_j = gi.min(), gi.max(), gj.min(), gj.max()
        for iso, gi0, gj0, a in self.r:
            # Skip rasters whose extent cannot contain any sample.
            if gi0 > hi_i or gi0 + a.shape[1] <= lo_i or gj0 > hi_j or gj0 + a.shape[0] <= lo_j:
                continue
            i, j = (gi - gi0, gj - gj0)
            m = (j >= 0) & (j < a.shape[0]) & (i >= 0) & (i < a.shape[1])
            if not m.any():
                continue
            v = np.asarray(a[j[m], i[m]], float)
            ok = v > -1e30
            idx = np.nonzero(m)[0][ok]
            tot[idx] += v[ok]
        return tot


def l15_centres(cell_id, level=15):
    c = s2sphere.CellId(cell_id)
    L = c.level()
    if L >= level:
        ll = s2sphere.CellId(cell_id).parent(level).to_lat_lng()
        return (np.array([ll.lat().degrees]), np.array([ll.lng().degrees]))
    face, i, j, _ = c.to_face_ij_orientation()
    sz = 1 << 30 - level
    n = 1 << level - L
    base = 1 << 30 - L
    i0, j0 = (i // base * base, j // base * base)
    a = np.arange(n)
    ii = (i0 + a * sz)[:, None] * np.ones((1, n), dtype=np.int64)
    jj = (j0 + a * sz)[None, :] * np.ones((n, 1), dtype=np.int64)
    si = (2 * ii.ravel() + sz) / 2.0**31
    ti = (2 * jj.ravel() + sz) / 2.0**31
    u = np.where(si >= 0.5, (4 * si * si - 1) / 3, (1 - 4 * (1 - si) ** 2) / 3)
    v = np.where(ti >= 0.5, (4 * ti * ti - 1) / 3, (1 - 4 * (1 - ti) ** 2) / 3)
    one = np.ones_like(u)
    x, y, z = {
        0: (one, u, v),
        1: (-u, one, v),
        2: (-u, -v, one),
        3: (-one, -v, -u),
        4: (v, -one, -u),
        5: (v, u, -one),
    }[face]
    return (np.degrees(np.arctan2(z, np.hypot(x, y))), np.degrees(np.arctan2(y, x)))


def density(g, cell_id, level=15):
    """Mean raw count at descendant centers, cached separately for each mosaic."""
    cell_level = s2sphere.CellId(cell_id).level()
    if level == 15 and cell_level in g.density_table:
        from .tables import lookup
        return lookup(g.density_table[cell_level], cell_id)
    k =(cell_id, level)
    if k not in g.density_cache:
        if len(g.density_cache) >= CACHE_LIMIT:
            g.density_cache.clear()  # Full flush keeps the cache bounded.
        lat, lng = l15_centres(cell_id, level)
        g.density_cache[k] = float(g.sample(lat, lng).mean())
    return g.density_cache[k]


def unit(la, ln):
    la, ln = (math.radians(la / 1000000.0), math.radians(ln / 1000000.0))
    return np.array(
        [math.cos(la) * math.cos(ln), math.cos(la) * math.sin(ln), math.sin(la)]
    )


def tri_area_km2(vs):
    return (
        2
        * math.atan2(
            abs(np.dot(vs[0], np.cross(vs[1], vs[2]))),
            1 + np.dot(vs[0], vs[1]) + np.dot(vs[1], vs[2]) + np.dot(vs[2], vs[0]),
        )
        * RE**2
    )


def _normals(vs):
    c = sum(vs)
    n = []
    for i in range(len(vs)):
        x = np.cross(vs[i], vs[(i + 1) % len(vs)])
        n.append(x if np.dot(x, c) > 0 else -x)
    return n


def _clip(poly, n):
    out = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        da, db = (np.dot(n, a), np.dot(n, b))
        if da >= 0:
            out.append(a)
        if (da >= 0) != (db >= 0):
            p = a + (b - a) * (da / (da - db))
            out.append(p / np.linalg.norm(p))
    return out


def sph_area(poly):
    if len(poly) < 3:
        return 0.0
    t = 0.0
    for i in range(1, len(poly) - 1):
        a, b, c = (poly[0], poly[i], poly[i + 1])
        t += 2 * math.atan2(
            abs(np.dot(a, np.cross(b, c))),
            1 + np.dot(a, b) + np.dot(b, c) + np.dot(c, a),
        )
    return t


def overlaps(pts_e6, cids):
    vs = [unit(*p) for p in pts_e6]
    area = tri_area_km2(vs)
    out = []
    tn = _normals(vs)
    interiors = {}
    if len(cids) >= 1000:
        # Clip only boundary cells. Bulk interior areas use the same two
        # spherical triangles, avoiding Python clipping on thousands of cells.
        import s2geometry as s2
        vertices = np.empty((len(cids), 4, 3))
        for i, cid in enumerate(cids):
            cell = s2.S2Cell(s2.S2CellId(cid))
            for k in range(4):
                v = cell.GetVertex(k)
                vertices[i, k] = (v.x(), v.y(), v.z())
        inside = np.all(vertices @ np.asarray(tn).T >= 0, axis=(1, 2))
        indices = np.flatnonzero(inside)
        v = vertices[indices]
        areas = np.zeros(len(indices))
        for k in (1, 2):
            a, b, c = v[:, 0], v[:, k], v[:, k + 1]
            dot = lambda x, y: np.sum(x * y, axis=-1)
            areas += 2 * np.arctan2(abs(dot(a, np.cross(b, c))),
                                   1 + dot(a, b) + dot(b, c) + dot(c, a))
        interiors = dict(zip((cids[i] for i in indices), areas * RE**2))
    for cid in cids:
        if cid in interiors:
            out.append((cid, s2sphere.CellId(cid).level(), float(interiors[cid])))
            continue
        cell = s2sphere.Cell(s2sphere.CellId(cid))
        poly = [np.array(list(cell.get_vertex(k))) for k in range(4)]
        for n in tn:
            poly = _clip(poly, n)
            if not poly:
                break
        out.append((cid, s2sphere.CellId(cid).level(), sph_area(poly) * RE**2))
    return (out, area)

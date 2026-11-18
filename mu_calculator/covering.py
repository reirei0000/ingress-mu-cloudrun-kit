"""2011 S2 region covering with Java priority queue tie ordering.

Derived from S2RegionCoverer and S2CellUnion in google/s2-geometry-library-java
(Apache-2.0, Copyright 2005 Google Inc.). Modified: ported to Python on top of
the s2geometry bindings, with fixed parameters and Java PriorityQueue ordering.
See NOTICE.
"""

import math
import s2geometry as s2

KMINWIDTH = 2 * math.sqrt(2) / 3


def max_level_for(value):
    if value <= 0:
        return 30
    return max(0, min(30, math.frexp(KMINWIDTH / value)[1] - 1))


class Region:

    def __init__(self, points):
        self.loop = s2.S2Loop([s2.S2LatLng.FromE6(a, b).ToPoint() for (a, b) in points])
        self.loop.Normalize()
        self.cap = self.loop.GetCapBound()

    def may_intersect(self, cell):
        return self.loop.MayIntersect(cell)

    def contains(self, cell):
        return self.loop.Contains(cell)


class JHeap:
    """Java PriorityQueue sift rules, including unstable equal-priority ties."""

    def __init__(self):
        self.q = []

    def __len__(self):
        return len(self.q)

    @staticmethod
    def cmp(a, b):
        return 1 if a[0] < b[0] else -1 if a[0] > b[0] else 0

    def push(self, x):
        q = self.q
        q.append(x)
        k = len(q) - 1
        while k > 0:
            p = k - 1 >> 1
            e = q[p]
            if self.cmp(x, e) >= 0:
                break
            q[k] = e
            k = p
        q[k] = x

    def pop(self):
        q = self.q
        res = q[0]
        x = q.pop()
        n = len(q)
        if n:
            k = 0
            half = n >> 1
            while k < half:
                child = 2 * k + 1
                c = q[child]
                r = child + 1
                if r < n and self.cmp(c, q[r]) > 0:
                    child = r
                    c = q[child]
                if self.cmp(x, c) <= 0:
                    break
                q[k] = c
                k = child
            q[k] = x
        return res


def cover(pts_e6, max_cells=20, max_level=13, min_level=6, level_mod=1, observer=None):
    """Cover a triangle with the 2011 Java candidate queue ordering.

    observer, if given, is called as observer(event, **data) while the covering
    is built (events: start, push, poll, merge, result, normalize). It only
    reports; the result does not depend on it.
    """
    R = Region(pts_e6)
    result = []
    pq = JHeap()
    mcs = 2 * level_mod

    def new_candidate(cid):
        cell = s2.S2Cell(cid)
        if not R.may_intersect(cell):
            return None
        lvl = cid.level()
        terminal = False
        why = None
        if lvl >= min_level:
            if lvl + level_mod > max_level or R.contains(cell):
                terminal = True
                if observer:
                    why = "inside" if R.contains(cell) else "max_level"
        return dict(cid=cid, cell=cell, terminal=terminal, children=[], nterm=0, why=why)

    def add(cand):
        if cand is None:
            return 0
        if cand["terminal"]:
            result.append(cand["cid"])
            if observer:
                observer("result", cid=cand["cid"].id(), why=cand["why"])
            return 1
        num_levels = 1 if cand["cid"].level() < min_level else level_mod

        def rec(cid, lv):
            if lv == 0:
                ch = new_candidate(cid)
                if ch is None:
                    return 0
                cand["children"].append(ch)
                return 1 if ch["terminal"] else 0
            return sum((rec(cid.child(k), lv - 1) for k in range(4)))

        nterm = rec(cand["cid"], num_levels)
        cand["num_children"] = len(cand["children"])
        cand["nterm"] = nterm
        if cand["num_children"] == 0:
            return 0
        if nterm == 1 << mcs and cand["cid"].level() >= min_level:
            cand["terminal"] = True
            if observer:
                cand["why"] = "merged"
                observer("merge", cid=cand["cid"].id(),
                         children=[c["cid"].id() for c in cand["children"]])
            return add(cand)
        priority = -(
            ((cand["cid"].level() << mcs) + cand["num_children"] << mcs) + nterm
        )
        pq.push((priority, cand))
        if observer:
            observer("push", cid=cand["cid"].id(), priority=priority,
                     children=[(c["cid"].id(), c["terminal"]) for c in cand["children"]])
        return 0

    started = False
    if max_cells >= 4:
        cap = R.cap
        ang = math.acos(max(-1.0, min(1.0, 1 - cap.height())))
        level = min(max_level_for(2 * ang), min(max_level, 29))
        if level_mod > 1 and level > min_level:
            level -= (level - min_level) % level_mod
        if level > 0:
            axis = cap.center()
            leaf = s2.S2CellId(axis)
            if observer:
                observer("start", mode="neighbors", level=level, angle=ang,
                         x=KMINWIDTH / (2 * ang), leaf=leaf.id(),
                         initial=[c.id() for c in leaf.GetVertexNeighbors(level)])
            for cid in leaf.GetVertexNeighbors(level):
                add(new_candidate(cid))
            started = True
    if not started:
        if observer:
            observer("start", mode="faces", level=0, angle=None, x=None, leaf=None,
                     initial=list(range(6)))
        for face in range(6):
            add(new_candidate(s2.S2CellId.FromFacePosLevel(face, 0, 0)))
    while pq:
        priority, cand = pq.pop()
        n = len(cand["children"])
        expand = (
            cand["cid"].level() < min_level
            or n == 1
            or len(result) + len(pq) + n <= max_cells
        )
        if observer:
            observer("poll", cid=cand["cid"].id(), priority=priority,
                     children=[(c["cid"].id(), c["terminal"]) for c in cand["children"]],
                     nterm=cand["nterm"], result=len(result), queue=len(pq),
                     expand=expand,
                     forced=cand["cid"].level() < min_level)
        if expand:
            for ch in cand["children"]:
                add(ch)
        else:
            cand["terminal"] = True
            cand["why"] = "budget"
            add(cand)
    final = normalize([c.id() for c in result], min_level, level_mod)
    if observer:
        observer("normalize", before=[c.id() for c in result], after=final)
    return final


def normalize(ids, min_level=0, level_mod=1):
    ids = sorted(set(ids))
    out = []
    for cid in ids:
        c = s2.S2CellId(cid)
        if out and s2.S2CellId(out[-1]).contains(c):
            continue
        while out and c.contains(s2.S2CellId(out[-1])):
            out.pop()
        while len(out) >= 3 and c.level() > min_level:
            p = c.parent()
            if (
                all(
                    (
                        s2.S2CellId(o).level() == c.level()
                        and s2.S2CellId(o).parent().id() == p.id()
                        for o in out[-3:]
                    )
                )
                and len({o for o in out[-3:]}) == 3
            ):
                del out[-3:]
                c = p
            else:
                break
        out.append(c.id())
    return out

#!/usr/bin/env python3
"""Bake Natural Earth 110m land polygons into a compact flat-array world file.

The page draws its own equirectangular Earth texture from these numbers, so the
whole thing stays offline and dependency-free (no image downloads, no CDN).

Output: data/world.json  {"polys": [[lon,lat,lon,lat,...], ...]}
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "scripts", "ne_110m_land.geojson")
OUT = os.path.join(ROOT, "data", "world.json")

TOL = 0.08  # degrees; ~9 km at the equator, invisible at globe scale


def dp(pts, tol):
    """Douglas-Peucker on [lon,lat] pairs."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        den = (dx * dx + dy * dy) ** 0.5
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if den == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / den
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def main():
    if not os.path.exists(SRC):
        print(f"missing {SRC}: fetch with\n  curl -sL https://raw.githubusercontent.com/"
              "nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson -o "
              + SRC, file=sys.stderr)
        return 1
    with open(SRC, encoding="utf-8") as fh:
        gj = json.load(fh)

    polys, n_in, n_out = [], 0, 0
    for feature in gj["features"]:
        g = feature["geometry"]
        rings = (
            [g["coordinates"][0]]
            if g["type"] == "Polygon"
            else [p[0] for p in g["coordinates"]]
        )
        for ring in rings:
            n_in += len(ring)
            simp = dp([[round(x, 3), round(y, 3)] for x, y in ring], TOL)
            if len(simp) < 4:
                continue
            flat = []
            for x, y in simp:
                flat.append(round(x, 2))
                flat.append(round(y, 2))
            n_out += len(simp)
            polys.append(flat)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"polys": polys}, fh, separators=(",", ":"))
    size = os.path.getsize(OUT)
    print(f"polygons={len(polys)} points {n_in} -> {n_out}  {size/1024:.1f} KB -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

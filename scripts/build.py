#!/usr/bin/env python3
"""Inline the baked data into template.html -> index.html (single file, offline).

The page must render from a file:// open with no network, so nothing may be
loaded externally. This asserts that.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "template.html")
OUT = os.path.join(ROOT, "index.html")


def read_json(name):
    path = os.path.join(ROOT, "data", name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    with open(TPL, encoding="utf-8") as fh:
        html = fh.read()

    storms = read_json("storms.json")
    world = read_json("world.json")

    # Trim what the renderer never reads so the page stays lean.
    keep = {
        "id", "name", "designation", "basin", "source", "source_short", "source_url",
        "secondary_url", "lat", "lon", "wind_kt", "wind_avg", "wind_kt_1min", "gust_kt",
        "pressure_hpa", "cat", "cat_label", "cat_class", "status", "intensity",
        "motion_dir", "motion_deg", "motion_kt", "updated", "alert", "countries",
        "radius_km", "track", "forecast", "cone", "active", "gdacs_url",
    }
    slim = {
        "generated": storms.get("generated"),
        "counts": storms.get("counts"),
        "errors": storms.get("errors", []),
        "storms": [{k: v for k, v in s.items() if k in keep} for s in storms.get("storms", [])],
    }

    blob = (
        "window.__STORM_DATA__="
        + json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
        + ";\nwindow.__WORLD__="
        + json.dumps(world, separators=(",", ":"))
        + ";"
    )

    if "/*__DATA__*/" not in html:
        print("template is missing the /*__DATA__*/ marker", file=sys.stderr)
        return 1
    out = html.replace("/*__DATA__*/", blob)

    # ---- self-checks ----
    problems = []
    if "__DATA__" in out or "__WORLD__" in out.replace("window.__WORLD__", ""):
        problems.append("unreplaced placeholder left in output")
    for m in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', out):
        url = m.group(1)
        if url.startswith(("http://", "https://", "//")) and "github.com/nwfella" not in url \
                and "gdacs.org" not in url and "nhc.noaa.gov" not in url \
                and "jma.go.jp" not in url:
            problems.append(f"external resource reference: {url}")
    if '"storms":[]' in out:
        problems.append("no storms in payload")
    if problems:
        for p in problems:
            print("BUILD FAIL:", p, file=sys.stderr)
        return 1

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)

    n = len(slim["storms"])
    act = sum(1 for s in slim["storms"] if s.get("active"))
    print(f"index.html {os.path.getsize(OUT)/1024:.1f} KB · {n} systems ({act} active) "
          f"· {len(world['polys'])} land polygons")
    return 0


if __name__ == "__main__":
    sys.exit(main())

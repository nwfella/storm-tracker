#!/usr/bin/env python3
"""
storm-tracker data collector.

Pulls every active tropical cyclone on Earth from three authoritative feeds and
merges them into data/storms.json:

  * NHC  (NOAA)        - Atlantic + East/Central Pacific, 1-min sustained wind
  * JMA  (RSMC Tokyo)  - West Pacific, 10-min sustained wind, 5-day forecast
  * GDACS (EU/JRC)     - global catch-all (JTWC/BoM/IMD sourced), wind radii + cone

No API keys. Stdlib only.
"""

import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

UA = "storm-tracker/1.0 (+https://github.com/nwfella/storm-tracker)"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

KT_PER_KMH = 1.0 / 1.852
KM_PER_DEG = 111.32

# WMO-recommended 10-min -> 1-min sustained wind conversion for WPac systems
# (Harper et al. 2010). Applied ONLY to derive the Saffir-Simpson equivalent;
# the reported value is always kept verbatim next to it.
TENMIN_TO_ONEMIN = 1.12


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def get(url, timeout=45, json_body=False, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
            text = raw.decode("utf-8", "replace")
            return json.loads(text) if json_body else text
        except Exception as exc:  # noqa: BLE001 - network soup
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


# --------------------------------------------------------------------------- #
# classification helpers
# --------------------------------------------------------------------------- #
def saffir_simpson(kt_1min):
    """Return (cat_int, label, css_class) from 1-minute sustained wind in kt."""
    if kt_1min is None:
        return (None, "Unknown", "unk")
    if kt_1min < 34:
        return (-1, "Tropical Depression", "td")
    if kt_1min < 64:
        return (0, "Tropical Storm", "ts")
    if kt_1min < 83:
        return (1, "Category 1", "cat1")
    if kt_1min < 96:
        return (2, "Category 2", "cat2")
    if kt_1min < 113:
        return (3, "Category 3", "cat3")
    if kt_1min < 137:
        return (4, "Category 4", "cat4")
    return (5, "Category 5", "cat5")


COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def compass(deg):
    if deg is None:
        return None
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def basin_of(lat, lon):
    """Coarse WMO-style basin label from a position."""
    lon = ((lon + 180) % 360) - 180
    if lat >= 0:
        if lon >= -100 and lon < 0:
            return "North Atlantic"
        if lon >= -140 and lon < -100:
            return "East Pacific"
        if lon >= -180 and lon < -140:
            return "Central Pacific"
        if lon >= 100 and lon < 180:
            return "West Pacific"
        if lon >= 30 and lon < 100:
            return "North Indian"
        return "Western Hemisphere"
    if lon >= 20 and lon < 90:
        return "South Indian"
    if lon >= 90 and lon < 160:
        return "Australian Region"
    return "South Pacific"


def haversine_km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def ring_center(ring):
    """Centroid of a lon/lat ring, dateline-aware enough for TC scale work."""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    # unwrap longitudes around the first point so a dateline crossing does not
    # average to the middle of the planet
    base = xs[0]
    un = [((x - base + 180) % 360) - 180 + base for x in xs]
    return (sum(ys) / len(ys), ((sum(un) / len(un) + 180) % 360) - 180)


def ring_radius_km(ring, center):
    ds = sorted(haversine_km(center, (p[1], p[0])) for p in ring)
    return ds[len(ds) // 2] if ds else None


def downsample(pts, n=72, closed=True):
    if not pts:
        return []
    if len(pts) <= n:
        return [[round(p[1], 2), round(p[0], 2)] for p in pts]
    step = len(pts) / float(n)
    out = [pts[int(i * step)] for i in range(n)]
    if closed:
        out.append(out[0])
    return [[round(p[1], 2), round(p[0], 2)] for p in out]


def iso(dt):
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# NHC
# --------------------------------------------------------------------------- #
NHC_FORECAST_RE = re.compile(
    r"(?:FORECAST|OUTLOOK)\s+VALID\s+(\d{2})/(\d{4})Z\s+"
    r"(\d+(?:\.\d+)?)\s*([NS])\s+(\d+(?:\.\d+)?)\s*([EW])",
    re.I,
)
NHC_WIND_RE = re.compile(r"MAX\s+WIND\s+(\d+)\s*KT", re.I)
NHC_GUST_RE = re.compile(r"GUSTS\s+(\d+)\s*KT", re.I)
NHC_34_RE = re.compile(
    r"34\s*KT\.\.\.\s*(\d+)\s*NE\s+(\d+)\s*SE\s+(\d+)\s*SW\s+(\d+)\s*NW", re.I
)
NHC_INIT_RE = re.compile(
    r"^\s*INIT\s+(\d{2})/(\d{4})Z\s+(\d+(?:\.\d+)?)\s*([NS])\s+"
    r"(\d+(?:\.\d+)?)\s*([EW])\s+(\d+)\s*KT\s+(\d+)\s*MB",
    re.I | re.M,
)


def strip_html(text):
    m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
    if m:
        text = m.group(1)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(a, b)
    return text


def parse_nhc_tcm(text, year, prev_wind):
    """Parse an NHC Forecast/Advisory (TCM) into a list of forecast points."""
    txt = strip_html(text)
    pts = []
    # Reference clock comes from the advisory header, e.g. "2100 UTC TUE OCT 06 2026".
    # Forecast points carry only day-of-month + hhmm, so the month/year must come
    # from here or the timestamps are unusable downstream.
    head = re.search(
        r"(\d{2})(\d{2})\s+UTC\s+\w{3}\s+([A-Z]{3})\s+(\d{2})\s+(\d{4})", txt, re.I
    )
    ref = None
    if head:
        try:
            ref = datetime(
                int(head.group(5)),
                datetime.strptime(head.group(3).title(), "%b").month,
                int(head.group(4)),
                int(head.group(1)),
                int(head.group(2)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            ref = None
    blocks = list(NHC_FORECAST_RE.finditer(txt))
    for i, m in enumerate(blocks):
        dd, hhmm = int(m.group(1)), m.group(2)
        lat = float(m.group(3)) * (1 if m.group(4).upper() == "N" else -1)
        lon = float(m.group(5)) * (1 if m.group(6).upper() == "E" else -1)
        tail = txt[m.end(): blocks[i + 1].start() if i + 1 < len(blocks) else len(txt)]
        wm = NHC_WIND_RE.search(tail)
        gm = NHC_GUST_RE.search(tail)
        r34 = NHC_34_RE.search(tail)
        when = None
        if ref is not None:
            try:
                when = ref.replace(day=dd, hour=int(hhmm[:2]), minute=int(hhmm[2:]))
                if when < ref - timedelta(days=200):
                    when = when.replace(year=when.year + 1)
            except ValueError:
                when = None
        pts.append(
            {
                "lat": lat,
                "lon": lon,
                "ts": iso(when) if when else None,
                "wind_kt": int(wm.group(1)) if wm else None,
                "gust_kt": int(gm.group(1)) if gm else None,
                "radius_km": (max(int(x) for x in r34.groups()) * 1.852) if r34 else None,
                "kind": "forecast",
            }
        )
    # extended outlook points beyond 120h
    return pts


def fetch_nhc(errors):
    out = []
    try:
        raw = get("https://www.nhc.noaa.gov/CurrentStorms.json", json_body=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"nhc: {exc}")
        return out

    storms = raw.get("activeStorms") or []
    prefix_basin = {
        "al": "North Atlantic",
        "ep": "East Pacific",
        "cp": "Central Pacific",
    }

    def one(s):
        sid = (s.get("id") or "").lower()
        basin = prefix_basin.get(sid[:2], "North Atlantic")
        try:
            wind = int(float(s.get("intensity") or 0))
        except (TypeError, ValueError):
            wind = None
        try:
            pressure = int(float(s.get("pressure") or 0)) or None
        except (TypeError, ValueError):
            pressure = None
        lat = s.get("latitudeNumeric")
        lon = s.get("longitudeNumeric")
        if lat is None or lon is None:
            return None

        forecast = []
        cone = None
        adv = s.get("forecastAdvisory") or {}
        url = adv.get("url")
        if url:
            try:
                tcm = get(url)
                forecast = parse_nhc_tcm(tcm, None, wind)[:8]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"nhc-tcm {sid}: {exc}")

        cat, label, css = saffir_simpson(wind)
        return {
            "id": f"nhc-{sid}",
            "name": (s.get("name") or sid.upper()).title(),
            "designation": (s.get("binNumber") or sid.upper()),
            "basin": basin,
            "source": "NHC",
            "source_short": "NOAA NHC",
            "source_url": (s.get("publicAdvisory") or {}).get("url")
            or "https://www.nhc.noaa.gov/",
            "secondary_url": (s.get("forecastGraphics") or {}).get("url"),
            "lat": float(lat),
            "lon": float(lon),
            "wind_kt": wind,
            "wind_avg": "1min",
            "wind_kt_1min": wind,
            "gust_kt": None,
            "pressure_hpa": pressure,
            "cat": cat,
            "cat_label": label,
            "cat_class": css,
            "status": s.get("classification") or "",
            "motion_deg": s.get("movementDir"),
            "motion_dir": compass(s.get("movementDir")),
            "motion_kt": s.get("movementSpeed"),
            "updated": s.get("lastUpdate"),
            "alert": None,
            "countries": [],
            "radius_km": None,
            "track": [],
            "forecast": [p for p in forecast],
            "cone": cone,
            "active": True,
        }

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(one, storms):
            if r:
                out.append(r)
    return out


# --------------------------------------------------------------------------- #
# JMA (RSMC Tokyo)
# --------------------------------------------------------------------------- #
JP_DIR = {
    "北": "N", "北北東": "NNE", "北東": "NE", "東北東": "ENE", "東": "E",
    "東南東": "ESE", "南東": "SE", "南南東": "SSE", "南": "S",
    "南南西": "SSW", "南西": "SW", "西南西": "WSW", "西": "W",
    "西北西": "WNW", "北西": "NW", "北北西": "NNW",
}
JP_DIR_DEG = {v: i * 22.5 for i, v in enumerate(COMPASS)}
JP_INTENSITY = {
    "猛烈な": "Violent",
    "非常に強い": "Very strong",
    "強い": "Strong",
}
JP_CAT = {"TY": "Typhoon", "STS": "Severe tropical storm", "TS": "Tropical storm", "TD": "Tropical depression"}


def jp_dir(text):
    if not text:
        return None, None
    t = text.strip()
    if "停滞" in t:
        return "Stationary", None
    for k, v in JP_DIR.items():
        if t == k:
            return v, JP_DIR_DEG.get(v)
    for k, v in JP_DIR.items():
        if k in t:
            return v, JP_DIR_DEG.get(v)
    return None, None


def fetch_jma(errors):
    out = []
    try:
        targets = get("https://www.jma.go.jp/bosai/typhoon/data/targetTc.json", json_body=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"jma-list: {exc}")
        return out
    if not isinstance(targets, list):
        targets = []

    def one(t):
        tcid = t.get("tropicalCyclone")
        if not tcid:
            return None
        try:
            fc = get(f"https://www.jma.go.jp/bosai/typhoon/data/{tcid}/forecast.json", json_body=True)
            spec = get(f"https://www.jma.go.jp/bosai/typhoon/data/{tcid}/specifications.json", json_body=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"jma {tcid}: {exc}")
            return None
        if not isinstance(fc, list) or not fc:
            return None
        title = fc[0] if isinstance(fc[0], dict) else {}
        name = ((title.get("name") or {}).get("en")) or tcid
        number = title.get("typhoonNumber") or t.get("typhoonNumber") or ""
        parts = [p for p in fc[1:] if isinstance(p, dict)]
        sparts = [p for p in (spec[1:] if isinstance(spec, list) else []) if isinstance(p, dict)]

        analysis = next((p for p in parts if p.get("advancedHours") == 0), None)
        if not analysis or not analysis.get("center"):
            return None
        lat, lon = analysis["center"]

        def wind_of(p):
            for s in sparts:
                if s.get("advancedHours") == p.get("advancedHours"):
                    try:
                        return int((s.get("maximumWind") or {}).get("sustained", {}).get("kt"))
                    except (TypeError, ValueError):
                        return None
            return None

        def gust_of(p):
            for s in sparts:
                if s.get("advancedHours") == p.get("advancedHours"):
                    try:
                        return int((s.get("maximumWind") or {}).get("gust", {}).get("kt"))
                    except (TypeError, ValueError):
                        return None
            return None

        def pressure_of(p):
            for s in sparts:
                if s.get("advancedHours") == p.get("advancedHours"):
                    v = s.get("pressure")
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return None
            return None

        sp0 = next((s for s in sparts if s.get("advancedHours") == 0), {})
        wind10 = wind_of(analysis)
        wind1 = int(round(wind10 * TENMIN_TO_ONEMIN)) if wind10 else None
        cat, label, css = saffir_simpson(wind1)

        # official past track (JMA publishes the analysed path as polylines)
        track = []
        tr = (analysis.get("track") or {})
        for key in ("preTyphoon", "typhoon"):
            for pt in tr.get(key) or []:
                if isinstance(pt, list) and len(pt) == 2:
                    track.append([round(pt[0], 2), round(pt[1], 2), None])
        if track and (track[-1][0] != lat or track[-1][1] != lon):
            track.append([round(lat, 2), round(lon, 2), (analysis.get("validtime") or {}).get("UTC")])

        forecast = []
        for p in parts:
            if p.get("advancedHours") in (None, 0) or not p.get("center"):
                continue
            plat, plon = p["center"]
            rad = ((p.get("probabilityCircle") or {}).get("radius") or 0) / 1000.0
            forecast.append(
                {
                    "lat": round(plat, 2),
                    "lon": round(plon, 2),
                    "ts": (p.get("validtime") or {}).get("UTC"),
                    "wind_kt": wind_of(p),
                    "gust_kt": gust_of(p),
                    "pressure_hpa": pressure_of(p),
                    "radius_km": round(rad) if rad else None,
                    "kind": "forecast",
                }
            )

        radius_km = None
        for key in ("stormWarningArea", "galeWarningArea"):
            area = analysis.get(key) or {}
            for arc in area.get("arc") or []:
                if isinstance(arc, list) and len(arc) >= 2 and isinstance(arc[1], (int, float)):
                    radius_km = max(radius_km or 0, arc[1] / 1000.0)
        if not radius_km:
            for w in sp0.get("stormWarning") or []:
                r = (w.get("range") or {}).get("km")
                if isinstance(r, (int, float)):
                    radius_km = max(radius_km or 0, float(r))

        intensity_jp = sp0.get("intensity") or ""
        dir_txt, dir_deg = jp_dir(sp0.get("course"))
        speed = (sp0.get("speed") or {})
        try:
            speed_kmh = float(speed.get("km/h"))
        except (TypeError, ValueError):
            speed_kmh = None

        return {
            "id": f"jma-{tcid}",
            "name": name,
            "designation": f"T{number}" if number else tcid,
            "basin": basin_of(lat, lon),
            "source": "JMA",
            "source_short": "RSMC Tokyo (JMA)",
            "source_url": f"https://www.jma.go.jp/bosai/typhoon/#/detail/{tcid}",
            "secondary_url": "https://www.jma.go.jp/bosai/typhoon/",
            "lat": round(lat, 2),
            "lon": round(lon, 2),
            "wind_kt": wind10,
            "wind_avg": "10min",
            "wind_kt_1min": wind1,
            "gust_kt": gust_of(analysis),
            "pressure_hpa": pressure_of(analysis),
            "cat": cat,
            "cat_label": label,
            "cat_class": css,
            "status": (sp0.get("category") or {}).get("en") or t.get("category") or "TY",
            "status_long": JP_CAT.get((sp0.get("category") or {}).get("en"), "Typhoon"),
            "intensity": JP_INTENSITY.get(intensity_jp, None),
            "motion_deg": dir_deg,
            "motion_dir": dir_txt,
            "motion_kmh": speed_kmh,
            "motion_kt": round(speed_kmh / 1.852) if speed_kmh else None,
            "updated": (analysis.get("validtime") or {}).get("UTC"),
            "alert": None,
            "countries": [],
            "radius_km": round(radius_km) if radius_km else None,
            "track": track[-40:],
            "forecast": forecast,
            "cone": None,
            "active": True,
        }

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(one, targets):
            if r:
                out.append(r)
    return out


# --------------------------------------------------------------------------- #
# GDACS (global catch-all)
# --------------------------------------------------------------------------- #
def _gdacs_ts(props, year):
    """GDACS stamps polygons with a `key` of DDMMHHMM in UTC plus a DD/MM HH:MM
    label; `polygondate` is NOT per-point (every point in one response carries the
    episode date), so it cannot be used to order a track."""
    # The label ("01/11 06:00") is DD/MM; the `key` field is MMDDHHMM. Both are
    # unambiguous once you know the order -- getting it wrong silently turns a
    # 5-day forecast into a January track.
    lbl = props.get("polygonlabel") or ""
    m = re.match(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", lbl)
    if m:
        try:
            return datetime(
                year, int(m.group(2)), int(m.group(1)), int(m.group(3)), int(m.group(4)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass
    key = props.get("key")
    if key:
        s = str(key)
        if len(s) >= 8 and s[:8].isdigit():
            try:
                dt = datetime(
                    year, int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]),
                    tzinfo=timezone.utc,
                )
                # December storms roll the year forward when the label says so
                return dt
            except ValueError:
                pass
    pd = props.get("polygondate")
    if pd:
        try:
            dt = datetime.fromisoformat(str(pd).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _iso_z(v):
    """GDACS timestamps are naive UTC; a bare 'YYYY-MM-DDTHH:MM:SS' is read as
    LOCAL time by JS `new Date()`, which shifts every displayed clock."""
    if not v:
        return None
    v = str(v).strip()
    if v.endswith("Z") or "+" in v[10:]:
        return v
    return v + "Z"


def fetch_gdacs(errors, days_back=30):
    out = []
    today = datetime.now(timezone.utc)
    frm = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to = (today + timedelta(days=10)).strftime("%Y-%m-%d")
    url = (
        "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
        f"?eventlist=TC&fromDate={frm}&toDate={to}"
    )
    try:
        raw = get(url, json_body=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gdacs-list: {exc}")
        return out

    feats = raw.get("features") or []

    def geometry(eid, epid):
        try:
            return get(
                "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"
                f"?eventtype=TC&eventid={eid}&episodeid={epid}",
                json_body=True,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gdacs-geom {eid}: {exc}")
            return None

    def one(f):
        p = f.get("properties") or {}
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
        except Exception:  # noqa: BLE001
            return None
        kmh = ((p.get("severitydata") or {}).get("severity"))
        wind1 = int(round(float(kmh) * KT_PER_KMH)) if kmh else None
        cat, label, css = saffir_simpson(wind1)
        eid, epid = p.get("eventid"), p.get("episodeid")

        track, forecast, cone, radius = [], [], None, None
        geo = geometry(eid, epid) if eid and epid else None
        year = int(str(p.get("fromdate") or "2026")[:4])
        analysis_ts = _gdacs_ts({"polygondate": p.get("todate") or p.get("fromdate")}, year)
        if geo and geo.get("features"):
            centers = {}
            for gf in geo["features"]:
                gp = gf.get("properties") or {}
                gt = (gf.get("geometry") or {}).get("type")
                coords = (gf.get("geometry") or {}).get("coordinates")
                if gp.get("episodeid") not in (None, epid):
                    continue  # older episodes carry their own positions
                if gt == "Point" and gp.get("Class") == "Point_Centroid":
                    continue
                if gt == "Polygon" and gp.get("featuretype") == "PointRadii" and coords:
                    clat, clon = ring_center(coords[0])
                    ts = _gdacs_ts(gp, year)
                    centers[(ts or "", gp.get("key") or gp.get("polygonlabel") or "")] = (
                        clat,
                        clon,
                        ts,
                    )
                elif gt == "Polygon" and gp.get("Class") == "Poly_Cones" and coords:
                    cone = downsample(coords[0], 96)
            for _k, (clat, clon, ts) in sorted(centers.items(), key=lambda kv: kv[0][0]):
                entry = {
                    "lat": round(clat, 2),
                    "lon": round(clon, 2),
                    "ts": iso(ts) if ts else None,
                    "wind_kt": None,
                    "pressure_hpa": None,
                    "radius_km": None,
                    "kind": "forecast",
                }
                if ts and analysis_ts and ts <= analysis_ts:
                    entry["kind"] = "history"
                    track.append([entry["lat"], entry["lon"], iso(ts)])
                else:
                    forecast.append(entry)
            # Current gale radius: outer edge of the 60 km/h wind-radii ring,
            # measured from the storm centre. GDACS ships zero-radius rings when
            # a threshold is not reached, and the ring is often asymmetric (a
            # landfalling system), so a centroid-based median understates it.
            for gf in geo["features"]:
                gp = gf.get("properties") or {}
                if (
                    gp.get("featuretype") == "WindRadii"
                    and gp.get("Class") == "Poly_Green"
                    and _gdacs_ts(gp, year) == analysis_ts
                ):
                    ring = (gf.get("geometry") or {}).get("coordinates", [[]])[0]
                    if not ring:
                        continue
                    ds = [haversine_km((lat, lon), (pt[1], pt[0])) for pt in ring]
                    if ds and max(ds) > 5:
                        radius = max(ds)

        name = (p.get("eventname") or "").strip()
        pretty = name.title() if name else "Tropical Cyclone"
        # GDACS "FOUR-26" style designations are JTWC storm numbers
        return {
            "id": f"gdacs-{eid}",
            "name": pretty,
            "designation": name.upper(),
            "basin": basin_of(lat, lon),
            "source": "GDACS",
            "source_short": f"GDACS / {p.get('source') or 'JTWC'}",
            "source_url": ((p.get("url") or {}).get("report"))
            or "https://www.gdacs.org/",
            "secondary_url": "https://www.gdacs.org/",
            "lat": round(lat, 2),
            "lon": round(lon, 2),
            "wind_kt": wind1,
            "wind_avg": "1min" if (p.get("source") == "JTWC") else "10min",
            "wind_kt_1min": wind1 if p.get("source") == "JTWC" else (
                int(round(wind1 * TENMIN_TO_ONEMIN)) if wind1 else None
            ),
            "gust_kt": None,
            "pressure_hpa": None,
            "cat": cat,
            "cat_label": label,
            "cat_class": css,
            "status": ((p.get("severitydata") or {}).get("severitytext") or "").split("(")[0].strip(),
            "motion_deg": None,
            "motion_dir": None,
            "motion_kt": None,
            "updated": _iso_z(p.get("todate") or p.get("datemodified")),
            "alert": p.get("alertlevel"),
            "alert_score": p.get("alertscore"),
            "countries": [
                c.get("countryname") for c in (p.get("affectedcountries") or []) if c.get("countryname")
            ][:6],
            "radius_km": round(radius) if radius else None,
            "track": track,
            "forecast": forecast,
            "cone": cone,
            "active": str(p.get("iscurrent", "")).lower() == "true",
        }

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(one, feats):
            if r:
                out.append(r)
    return out


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def merge(nhc, jma, gdacs):
    """Prefer the basin authority's record; keep GDACS attributes as a sidecar."""
    storms = list(jma) + list(nhc)
    for g in gdacs:
        dup = None
        for s in storms:
            if haversine_km((s["lat"], s["lon"]), (g["lat"], g["lon"])) < 600:
                dup = s
                break
            a, b = s["name"].lower().replace("-", " "), g["name"].lower().replace("-", " ")
            if a and (a in b or b in a):
                dup = s
                break
        if dup:
            dup.setdefault("alert", None)
            if not dup.get("alert"):
                dup["alert"] = g.get("alert")
            if not dup.get("countries"):
                dup["countries"] = g.get("countries") or []
            if not dup.get("cone") and g.get("cone"):
                dup["cone"] = g["cone"]
            dup["gdacs_url"] = g.get("source_url")
            dup["gdacs_alert_score"] = g.get("alert_score")
            # fill any forecast points the authority feed lacked
            if not dup.get("forecast") and g.get("forecast"):
                dup["forecast"] = g["forecast"]
            if not dup.get("radius_km") and g.get("radius_km"):
                dup["radius_km"] = g["radius_km"]
        else:
            storms.append(g)
    storms.sort(key=lambda s: (not s.get("active", False), -(s.get("wind_kt") or 0)))
    return storms


def main():
    errors = []
    t0 = time.time()
    nhc, jma, gdacs = [], [], []
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_nhc = ex.submit(fetch_nhc, errors)
        f_jma = ex.submit(fetch_jma, errors)
        f_gdacs = ex.submit(fetch_gdacs, errors)
        nhc, jma, gdacs = f_nhc.result(), f_jma.result(), f_gdacs.result()

    storms = merge(nhc, jma, gdacs)
    active = [s for s in storms if s.get("active")]
    os.makedirs(DATA, exist_ok=True)
    payload = {
        "generated": iso(datetime.now(timezone.utc)),
        "counts": {
            "total": len(storms),
            "active": len(active),
            "nhc": len(nhc),
            "jma": len(jma),
            "gdacs": len(gdacs),
        },
        "sources": {
            "nhc": "https://www.nhc.noaa.gov/CurrentStorms.json",
            "jma": "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json",
            "gdacs": "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC",
        },
        "errors": errors,
        "storms": storms,
    }
    path = os.path.join(DATA, "storms.json")
    if not nhc and not jma and not gdacs:
        print("ALL SOURCES FAILED - not writing", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 2
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(
        f"storms={len(storms)} active={len(active)} "
        f"(nhc={len(nhc)} jma={len(jma)} gdacs={len(gdacs)}) "
        f"in {time.time() - t0:.1f}s -> {path}"
    )
    for e in errors:
        print("  warn: " + e, file=sys.stderr)
    for s in active:
        print(
            f"  {s['name']:<14} {s['basin']:<16} {s['lat']:>6.1f},{s['lon']:>7.1f} "
            f"{s['wind_kt']}kt {s['cat_label']:<20} fc={len(s.get('forecast') or [])} "
            f"trk={len(s.get('track') or [])} src={s['source']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

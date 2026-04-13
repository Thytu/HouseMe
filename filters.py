"""Filtering and scam detection for Craigslist listings."""

import re
from collections import Counter
from datetime import datetime, timezone
from statistics import median

COMPANY_ZONE = [
    (37.800, -122.441), (37.806, -122.422), (37.808, -122.418),
    (37.808, -122.410), (37.808, -122.403), (37.798, -122.398),
    (37.792, -122.394), (37.787, -122.391), (37.781, -122.390),
    (37.778, -122.392), (37.776, -122.405), (37.775, -122.420),
    (37.774, -122.435), (37.775, -122.449),
]

EXCLUDE_HOODS_BY_NAME = {
    "tenderloin", "mid-market", "bayview", "civic center",
    "excelsior / outer mission", "visitacion valley",
}

EXCLUDE_GEO_ZONES = [
    (37.7805, 37.7870, -122.4190, -122.4070),
    (37.7790, 37.7845, -122.4110, -122.4040),
    (37.7785, 37.7815, -122.4200, -122.4130),
    (37.7270, 37.7420, -122.3970, -122.3720),
    (37.7760, 37.7810, -122.4200, -122.4100),
]


def point_in_polygon(lat, lon, polygon):
    """Ray-casting algorithm for point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def is_excluded_area(post):
    """Check if a listing is in a neighborhood we want to skip."""
    hood = (post.get("neighborhood") or "").lower()
    loc = (post.get("location") or "").lower()
    for bad in EXCLUDE_HOODS_BY_NAME:
        if bad in hood or bad in loc:
            return True
    lat, lon = post.get("lat"), post.get("lon")
    if lat and lon:
        for lat_min, lat_max, lon_min, lon_max in EXCLUDE_GEO_ZONES:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return True
    return False


def _normalize_title(title):
    if not title:
        return ""
    t = re.sub(r'[^a-z0-9 ]', '', title.lower())
    return re.sub(r'\s+', ' ', t).strip()


def detect_scam_flags(results):
    """Add a 'flags' list to each result with scam indicators.

    Flags: LOW $, NO IMG, REPOST, STALE.
    """
    prices_by_br = {}
    for r in results:
        price, br = r.get("price"), r.get("bedrooms")
        if price and price > 0 and br is not None:
            prices_by_br.setdefault(br, []).append(price)

    median_by_br = {br: median(p) for br, p in prices_by_br.items() if len(p) >= 3}
    all_prices = [r["price"] for r in results if r.get("price") and r["price"] > 0]
    overall_median = median(all_prices) if len(all_prices) >= 3 else None

    title_counts = Counter()
    for r in results:
        norm = _normalize_title(r.get("title"))
        if norm and len(norm) > 10:
            title_counts[norm] += 1
    repost_titles = {t for t, c in title_counts.items() if c > 1}

    now = datetime.now(timezone.utc)
    for r in results:
        flags = []
        price, br = r.get("price"), r.get("bedrooms")
        norm_title = _normalize_title(r.get("title"))
        if price and price > 0:
            ref = median_by_br.get(br, overall_median)
            if ref and price < ref * 0.5:
                flags.append("LOW $")
        if not r.get("image_count"):
            flags.append("NO IMG")
        if norm_title and len(norm_title) > 10 and norm_title in repost_titles:
            flags.append("REPOST")
        posted = r.get("posted_date")
        if posted and (now - posted).days > 14:
            flags.append("STALE")
        r["flags"] = flags

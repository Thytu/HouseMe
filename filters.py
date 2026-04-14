"""Filtering and scam detection for Craigslist listings."""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

DB_FILE = Path(__file__).parent / ".houseme_listings.json"

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


def _load_listings_db() -> dict[str, dict]:
    """Load the historical listings database.

    Returns:
        Mapping of PID (str) to listing snapshot with keys:
        price, bedrooms, sqft, title_norm, neighborhood, location,
        image_count, posted_date (ISO str).
    """
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def _save_listings_db(db: dict[str, dict]) -> None:
    """Persist the listings database to disk."""
    DB_FILE.write_text(json.dumps(db, indent=2))


def _store_listings(db: dict[str, dict], results: list[dict]) -> None:
    """Add or update listings in the database.

    Creates new entries for unseen PIDs. Updates existing entries with
    image hash data when it becomes available (set by imgdb.check_and_store).
    """
    for r in results:
        pid_str = str(r["pid"])
        if pid_str not in db:
            posted = r.get("posted_date")
            db[pid_str] = {
                "price": r.get("price"),
                "bedrooms": r.get("bedrooms"),
                "sqft": r.get("sqft"),
                "title_norm": _normalize_title(r.get("title")),
                "neighborhood": r.get("neighborhood"),
                "location": r.get("location"),
                "image_count": r.get("image_count", 0),
                "posted_date": posted.isoformat() if posted else None,
                "image_hash": r.get("_image_hash"),
            }
        elif r.get("_image_hash") and not db[pid_str].get("image_hash"):
            db[pid_str]["image_hash"] = r["_image_hash"]


def _compute_medians(db: dict[str, dict]) -> tuple[dict[int, float], float | None]:
    """Compute per-bedroom and overall median prices from the full historical DB.

    Returns:
        (median_by_br, overall_median) where median_by_br maps bedroom count
        to median price, and overall_median is the median across all listings.
    """
    prices_by_br: dict[int, list[int]] = {}
    all_prices: list[int] = []

    for entry in db.values():
        price = entry.get("price")
        br = entry.get("bedrooms")
        if price and price > 0:
            all_prices.append(price)
            if br is not None:
                prices_by_br.setdefault(br, []).append(price)

    median_by_br = {br: median(p) for br, p in prices_by_br.items() if len(p) >= 3}
    overall_median = median(all_prices) if len(all_prices) >= 3 else None

    return median_by_br, overall_median


def _build_repost_titles(db: dict[str, dict], current_pids: set[str]) -> set[str]:
    """Find normalized titles that appear across multiple PIDs in the DB.

    A title is a repost if it appears on 2+ different PIDs — whether from
    this batch, previous runs, or both.
    """
    title_pids: dict[str, list[str]] = {}
    for pid_str, entry in db.items():
        norm = entry.get("title_norm", "")
        if norm and len(norm) > 10:
            title_pids.setdefault(norm, []).append(pid_str)

    return {t for t, pids in title_pids.items() if len(pids) > 1}


STOCK_IMG_THRESHOLD = 5


def detect_scam_flags(results: list[dict]) -> None:
    """Add a 'flags' list to each result with scam indicators.

    Flags:
        LOW $     — price below 50% of historical median for bedroom count
        NO IMG    — zero images
        REPOST    — normalized title seen on 2+ PIDs across all runs
        STALE     — posted more than 14 days ago
        DUPE IMG  — first image perceptual-hash matches another listing
        STOCK IMG — first image seen on 5+ different listings (broker/stock photo)

    Must be called AFTER imgdb.check_and_store() so that _image_hash,
    img_reuse_pids, and _image_reuse_count are populated on results.

    Stats are computed from the full historical listings database,
    which grows more accurate with every run.
    """
    db = _load_listings_db()
    _store_listings(db, results)
    _save_listings_db(db)

    median_by_br, overall_median = _compute_medians(db)
    current_pids = {str(r["pid"]) for r in results}
    repost_titles = _build_repost_titles(db, current_pids)

    now = datetime.now(timezone.utc)
    for r in results:
        flags: list[str] = []
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

        # Image-based flags (populated by imgdb.check_and_store)
        reuse_count = r.get("_image_reuse_count", 0)
        if reuse_count >= STOCK_IMG_THRESHOLD:
            flags.append("STOCK IMG")
        elif r.get("img_reuse_pids"):
            flags.append("DUPE IMG")

        r["flags"] = flags

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

FLARESOLVERR_URL = "http://localhost:8191/v1"

# Tag IDs used in Craigslist's compressed item format (from search JS)
_TAG_IMAGE_IDS = 4
_TAG_HOUSING = 5
_TAG_SEO = 6
_TAG_PRICE_STR = 10


def flaresolverr_get(url, max_timeout=60000):
    """Fetch a URL through FlareSolverr, bypassing Cloudflare."""
    resp = requests.post(FLARESOLVERR_URL, json={
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max_timeout,
    })
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr failed: {data.get('message')}")
    return data["solution"]["response"]


@lru_cache(maxsize=16)
def _get_area_id(site, area=None):
    """Get the areaId by loading a search page and extracting it from the init script."""
    path = f"/search/{area}/sss" if area else "/search/sss"
    url = f"https://{site}.craigslist.org{path}"
    html = flaresolverr_get(url)
    m = re.search(r'"areaId"\s*:\s*(\d+)', html)
    if m:
        return int(m.group(1))
    raise RuntimeError(f"Could not find areaId for {site}")


def _decode_items(data, category_abbr="apa"):
    """Decode Craigslist's compressed search API response into readable dicts."""
    decode = data["decode"]
    min_posting_id = decode["minPostingId"]
    min_posted_date = decode["minPostedDate"]
    locations_table = decode["locations"]
    descriptions_table = decode["locationDescriptions"]
    neighborhoods_table = decode.get("neighborhoods", [])

    parsed_locations = []
    for loc in locations_table:
        if isinstance(loc, list):
            parsed_locations.append({"areaId": loc[0], "hostname": loc[1], "subareaAbbr": loc[2]})
        else:
            parsed_locations.append(None)

    results = []
    for item in data["items"]:
        item = list(item)

        pid_offset = item[0]
        posted_date_offset = item[1]
        price_cents = item[3]
        location_str = item[4]

        pid = min_posting_id + pid_offset
        posted_ts = min_posted_date + posted_date_offset
        posted_date = datetime.fromtimestamp(posted_ts, tz=timezone.utc)

        loc_parts = location_str.split("~")
        loc_indices = loc_parts[0].split(":")
        lat = float(loc_parts[1]) if len(loc_parts) > 1 else None
        lon = float(loc_parts[2]) if len(loc_parts) > 2 else None

        desc_idx = int(loc_indices[1]) if len(loc_indices) > 1 else 0
        neigh_idx = int(loc_indices[2]) if len(loc_indices) > 2 else None

        location_desc = descriptions_table[desc_idx] if desc_idx < len(descriptions_table) else ""
        neighborhood = neighborhoods_table[neigh_idx] if neigh_idx is not None and neigh_idx < len(neighborhoods_table) else None

        loc_idx = int(loc_indices[0])
        loc_info = parsed_locations[loc_idx] if loc_idx < len(parsed_locations) else None

        result = {
            "pid": pid,
            "posted_date": posted_date,
            "price": price_cents if price_cents != -1 else None,
            "location": location_desc,
            "neighborhood": neighborhood,
            "lat": lat,
            "lon": lon,
        }

        title = None
        for field in item[6:]:
            if isinstance(field, str):
                title = field
            elif isinstance(field, list) and len(field) >= 1:
                tag = field[0]
                if tag == _TAG_HOUSING:
                    result["bedrooms"] = field[1] if len(field) > 1 else None
                    result["sqft"] = field[2] if len(field) > 2 else None
                elif tag == _TAG_PRICE_STR:
                    result["price_str"] = field[1] if len(field) > 1 else None
                elif tag == _TAG_SEO:
                    result["seo"] = field[1] if len(field) > 1 else None
                elif tag == _TAG_IMAGE_IDS:
                    result["image_count"] = len(field) - 1
                    # Store raw image IDs (strip "3:" prefix)
                    result["image_ids"] = [
                        img_id.split(":", 1)[-1] for img_id in field[1:]
                        if isinstance(img_id, str)
                    ]

        result["title"] = title

        # Build posting URL
        seo = result.get("seo", "listing")
        hostname = loc_info["hostname"] if loc_info else "sfbay"
        subarea = loc_info["subareaAbbr"] if loc_info else ""
        result["url"] = f"https://{hostname}.craigslist.org/{subarea}/{category_abbr}/d/{seo}/{pid}.html"

        results.append(result)

    return results, data.get("totalResultCount", len(results))


def search(site="sfbay", area=None, category="apa", query=None, limit=None, offset=0, **params):
    """Search Craigslist via FlareSolverr using the internal search API.

    Returns (results, total_count) where results is a list of dicts.
    offset: skip this many results (for pagination).
    """
    area_id = _get_area_id(site, area)

    search_path = f"{area}/{category}" if area else category
    batch = f"{area_id}-{offset}-360-0-0"

    api_params = {
        "batch": batch,
        "searchPath": search_path,
        "cc": "US",
        "lang": "en",
    }
    if query:
        api_params["query"] = query
    api_params.update(params)

    api_url = f"https://sapi.craigslist.org/web/v8/postings/search/full?{urlencode(api_params)}"
    html = flaresolverr_get(api_url)

    soup = BeautifulSoup(html, "html.parser")
    pre = soup.find("pre")
    api_data = json.loads(pre.text if pre else html)

    if api_data.get("errors"):
        msgs = [e.get("message", "") for e in api_data["errors"]]
        raise RuntimeError(f"Craigslist API errors: {'; '.join(msgs)}")

    items_data = api_data.get("data", {})
    if not items_data.get("items"):
        return [], 0

    results, total = _decode_items(items_data, category_abbr=category)

    if limit:
        results = results[:limit]

    return results, total

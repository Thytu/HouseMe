"""Zillow rental listing search via __NEXT_DATA__ extraction.

Fetches Zillow rental search pages with plain HTTP requests and extracts
structured listing data from the embedded __NEXT_DATA__ JSON. No browser
automation, no proxies, no API keys required.

Zillow's PerimeterX bot detection blocks ~10% of stateless requests, so
a simple retry loop achieves near-100% reliability.
"""

import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# Zillow image URL template — photoKey slots in for {photoKey}
_ZILLOW_IMG_URL = "https://photos.zillowstatic.com/fp/{}-p_e.jpg"

# SF region ID (Zillow's internal ID for San Francisco)
_SF_REGION_ID = 20330
_SF_REGION_TYPE = 6

# Retry / rate-limit settings
_MAX_RETRIES = 3
_RETRY_DELAY = 3.0


def _fetch_page(url: str) -> dict | None:
    """Fetch a Zillow page and extract __NEXT_DATA__, with retries.

    Args:
        url: Full Zillow URL to fetch.

    Returns:
        Parsed __NEXT_DATA__ dict, or None if all retries fail.
    """
    for attempt in range(_MAX_RETRIES):
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        if resp.status_code == 200:
            match = _NEXT_DATA_RE.search(resp.text)
            if match:
                return json.loads(match.group(1))
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY)
    return None


def _build_search_url(
    region_id: int = _SF_REGION_ID,
    region_type: int = _SF_REGION_TYPE,
    min_price: int | None = None,
    max_price: int | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = None,
    page: int = 1,
) -> str:
    """Build a Zillow rental search URL with filters encoded in searchQueryState.

    Args:
        region_id: Zillow's internal region identifier.
        region_type: Zillow region type (6 = city).
        min_price: Minimum monthly rent in dollars.
        max_price: Maximum monthly rent in dollars.
        min_bedrooms: Minimum bedroom count (0 = studio).
        max_bedrooms: Maximum bedroom count.
        page: Page number (1-indexed).

    Returns:
        Full Zillow search URL with encoded query parameters.
    """
    filter_state: dict = {
        "isForRent": {"value": True},
        "isForSaleByAgent": {"value": False},
        "isForSaleByOwner": {"value": False},
        "isNewConstruction": {"value": False},
        "isComingSoon": {"value": False},
        "isAuction": {"value": False},
        "isForSaleForeclosure": {"value": False},
    }

    if min_price is not None or max_price is not None:
        price_filter: dict = {}
        if min_price is not None:
            price_filter["min"] = min_price
        if max_price is not None:
            price_filter["max"] = max_price
        filter_state["price"] = price_filter
        filter_state["monthlyPayment"] = price_filter

    if min_bedrooms is not None or max_bedrooms is not None:
        beds_filter: dict = {}
        if min_bedrooms is not None:
            beds_filter["min"] = min_bedrooms
        if max_bedrooms is not None:
            beds_filter["max"] = max_bedrooms
        filter_state["beds"] = beds_filter

    query_state: dict = {
        "filterState": filter_state,
        "regionSelection": [{"regionId": region_id, "regionType": region_type}],
    }

    if page > 1:
        query_state["pagination"] = {"currentPage": page}

    encoded = urllib.parse.quote(json.dumps(query_state, separators=(",", ":")))
    base = "https://www.zillow.com/san-francisco-ca/rentals/"

    if page > 1:
        return f"{base}{page}_p/?searchQueryState={encoded}"
    return f"{base}?searchQueryState={encoded}"


def _parse_listing(raw: dict) -> dict:
    """Convert a Zillow listResult into HouseMe's listing dict format.

    Args:
        raw: A single entry from cat1.searchResults.listResults.

    Returns:
        Normalized listing dict compatible with the HouseMe pipeline.
    """
    home_info = raw.get("hdpData", {}).get("homeInfo", {})
    lat_long = raw.get("latLong", {})
    is_building = raw.get("isBuilding", False)

    # Price: prefer unformattedPrice (int), fall back to homeInfo.price
    price = raw.get("unformattedPrice") or home_info.get("price")

    # Bedrooms: individual listings have top-level "beds", buildings have "units"
    bedrooms = raw.get("beds")
    if bedrooms is None:
        bedrooms = home_info.get("bedrooms")

    # Bathrooms
    baths = raw.get("baths") or home_info.get("bathrooms")

    # Square footage
    sqft = raw.get("area") or home_info.get("livingArea")

    # For building listings, extract price/beds from the cheapest unit
    units = raw.get("units", [])
    if is_building and units:
        if price is None:
            price = raw.get("minBaseRent")
            if price is None:
                # Parse from unit price string like "$3,596+"
                for unit in units:
                    unit_price_str = unit.get("price", "")
                    cleaned = re.sub(r"[^0-9]", "", unit_price_str)
                    if cleaned:
                        price = int(cleaned)
                        break

        if bedrooms is None and units:
            # Use the first unit's bed count
            first_beds = units[0].get("beds", "")
            if first_beds != "":
                try:
                    bedrooms = int(first_beds)
                except (ValueError, TypeError):
                    pass

    # Posted date: derive from daysOnZillow
    days_on = home_info.get("daysOnZillow")
    posted_date: datetime | None = None
    if days_on is not None:
        posted_date = datetime.now(timezone.utc) - timedelta(days=days_on)

    # Images: extract photoKeys from carousel data
    photos_data = raw.get("carouselPhotosComposable", {})
    photo_entries = photos_data.get("photoData", [])
    photo_keys = [p["photoKey"] for p in photo_entries if p.get("photoKey")]

    # Build image URLs (used as image_ids — the detail screen will detect full URLs)
    image_urls = [_ZILLOW_IMG_URL.format(key) for key in photo_keys]

    # Title: use statusText (e.g. "Apartment for rent") + buildingName if available
    building_name = raw.get("buildingName")
    address_street = raw.get("addressStreet", "")
    status_text = raw.get("statusText", "")
    if building_name:
        title = f"{building_name} — {address_street}"
    elif status_text and status_text != "FOR_RENT":
        title = f"{status_text} — {address_street}"
    else:
        title = address_street

    # Neighbourhood: use addressCity + zipcode as location
    location = ", ".join(
        s for s in [raw.get("addressCity"), raw.get("addressState")] if s
    )
    zipcode = raw.get("addressZipcode", "")

    # zpid: Zillow's unique ID — use as pid (convert to int if possible)
    zpid = raw.get("zpid", "")
    try:
        pid = int(zpid)
    except (ValueError, TypeError):
        # Building listings use "lat--lon" as zpid — hash it for a stable int
        pid = hash(zpid) & 0x7FFFFFFFFFFFFFFF

    # Listing URL
    detail_url = raw.get("detailUrl", "")
    if detail_url and not detail_url.startswith("http"):
        detail_url = f"https://www.zillow.com{detail_url}"

    # Phone number from CTA recommendations
    phone = None
    cta_recs = raw.get("listCardRecommendation", {}).get("ctaRecommendations", [])
    for cta in cta_recs:
        if cta.get("contentType") == "PHONE":
            phone = cta.get("displayString")
            break

    # Units info for building listings
    units = raw.get("units", [])

    # Rent zestimate
    rent_zestimate = home_info.get("rentZestimate")

    # Facts and features
    facts = raw.get("factsAndFeatures", {})

    return {
        "pid": pid,
        "zpid": zpid,
        "posted_date": posted_date,
        "price": price,
        "price_str": raw.get("price"),  # Formatted string like "$2,195/mo"
        "title": title,
        "bedrooms": bedrooms,
        "bathrooms": baths,
        "sqft": sqft,
        "lat": lat_long.get("latitude"),
        "lon": lat_long.get("longitude"),
        "location": location,
        "neighborhood": zipcode,
        "url": detail_url,
        "image_count": len(photo_keys),
        "image_ids": image_urls,  # Full URLs (not CL-style bare IDs)
        "source": "zillow",
        "is_building": raw.get("isBuilding", False),
        "units": units,
        "phone": phone,
        "rent_zestimate": rent_zestimate,
        "home_type": home_info.get("homeType"),
        "facts": facts,
    }


def search(
    min_price: int | None = None,
    max_price: int | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = None,
    limit: int = 41,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Search Zillow rentals in San Francisco.

    Args:
        min_price: Minimum monthly rent in dollars.
        max_price: Maximum monthly rent in dollars.
        min_bedrooms: Minimum bedroom count (0 = studio).
        max_bedrooms: Maximum bedroom count.
        limit: Maximum number of listings to return.
        offset: Number of listings to skip (for pagination).

    Returns:
        (results, total_count) where results is a list of normalized listing
        dicts and total_count is the total number of matching listings on Zillow.
    """
    # Convert offset to page number (Zillow serves ~41 listings per page)
    page_size = 41
    start_page = (offset // page_size) + 1
    skip_on_first_page = offset % page_size

    collected: list[dict] = []
    total_count = 0
    page = start_page

    while len(collected) < limit:
        url = _build_search_url(
            min_price=min_price,
            max_price=max_price,
            min_bedrooms=min_bedrooms,
            max_bedrooms=max_bedrooms,
            page=page,
        )

        data = _fetch_page(url)
        if not data:
            break

        search_state = data.get("props", {}).get("pageProps", {}).get("searchPageState", {})
        cat1 = search_state.get("cat1", {})
        search_list = cat1.get("searchList", {})
        list_results = cat1.get("searchResults", {}).get("listResults", [])

        total_count = search_list.get("totalResultCount", 0)
        total_pages = search_list.get("totalPages", 0)

        if not list_results:
            break

        parsed = [_parse_listing(r) for r in list_results]

        # On the first page, skip listings to honor the offset
        if page == start_page and skip_on_first_page > 0:
            parsed = parsed[skip_on_first_page:]

        collected.extend(parsed)

        if page >= total_pages:
            break

        page += 1
        time.sleep(_RETRY_DELAY)

    return collected[:limit], total_count

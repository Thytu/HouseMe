# HouseMe

I moved to SF and needed an apartment. Craigslist and Zillow have the inventory but the experience is painful: walls of text, scam listings everywhere, and copy-pasting the same "Hi, I'm interested..." email 40 times.

So I built this. It pulls listings from both Craigslist and Zillow into a terminal UI, flags the scams, drafts a personalized visit request email for every listing, and opens it in Gmail with one keystroke. It remembers what you've already seen so you never waste time on the same listing twice.

![Listing table](screenshots/tui.png)

## What a session looks like

```bash
# Start FlareSolverr (one-time, runs in background — needed for Craigslist)
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr

# Search both sources (default)
uv run python main.py search \
  --min-price 2200 --max-price 3750 \
  --min-bedrooms 1 --max-age 7 \
  --exclude-drug-houses \
  --has-images

# Or just one source
uv run python main.py search --source zillow --min-price 2000 --max-price 3500
uv run python main.py search --source craigslist --min-price 2200 --max-price 3750
```

You get a navigable table with results from both sources interleaved. Arrow keys to browse, Enter to see photos, `e` to fire off an email, `d` to never see it again. Done in 10 minutes instead of an hour.

## Setup

```bash
git clone https://github.com/Thytu/HouseMe.git
cd HouseMe
uv sync
```

Needs **Python 3.10+**, `ANTHROPIC_API_KEY` set for email drafting, and **FlareSolverr** on port 8191 for Craigslist (Zillow works without it).

First run asks your name, job, and move-in date — saved locally, used to personalize emails.

## Keybindings

**Listing table:**

| Key | What it does |
|-----|-------------|
| `Enter` | Open detail view with photos |
| `e` | Open Gmail with AI-drafted visit request |
| `o` | Open listing in browser |
| `d` | Dislike — gone forever |
| `c` | Mark as contacted — gone forever |
| `s` | Cycle sort: price, date, sqft |
| `f` | Toggle hiding flagged listings |
| `m` | Open map of all visible listings in browser |
| `l` | Load more results |
| `q` | Quit |

**Detail view:**

| Key | What it does |
|-----|-------------|
| `Left` / `Right` | Browse photos |
| `d` / `c` / `e` / `o` | Same as table |
| `Escape` | Back |

Select a listing and hit Enter to see its photos full-screen:

![Detail view with inline image](screenshots/details.png)

## Scam detection

Every listing gets flagged automatically. The detection improves over time — it builds a local database of every listing and image hash it's ever seen.

| Flag | What it means |
|------|--------------|
| `LOW $` | Price is less than half the median for that bedroom count (computed from all listings ever seen, not just the current batch) |
| `NO IMG` | Zero photos |
| `REPOST` | Same title already appeared under a different listing ID — across runs, not just within one search |
| `STALE` | Posted more than 14 days ago |
| `DUPE IMG` | First photo matches another listing's photo (perceptual hash, tolerates crops and slight edits) |

Press `f` to hide all flagged listings at once.

## Email drafting

Every listing gets a short, personalized visit request drafted by Claude. No "Dear Sir/Madam", no regurgitating the listing description. It reads like a human wrote it because the prompt forces a specific structure.

Press `e` and Gmail opens with subject, body, and (if `--fetch-emails` is on) the To: field pre-filled.

## Map view

Press `m` to open a browser map with all visible listings plotted. Green = clean, red = flagged. Click a marker for price, title, and a link to the listing. Respects current sort/filter.

![Map view with subsidy zone](screenshots/map.png)

## Subsidy zone

If your company subsidizes rent in a specific area, use `--delphi-pays-rent` to filter listings to that zone. First time you use it, a browser-based editor opens where you draw the polygon yourself. Redraw anytime with:

```bash
uv run python main.py edit-zone
```

## All search options

```
--source            Listing source: both (default), craigslist, or zillow
--site              Craigslist site (default: sfbay, CL only)
--area              Sub-area (default: sfc, CL only)
-q / --query        Search text (CL only)
-n / --limit        Results per batch (default: 25)
--min-price         Minimum rent
--max-price         Maximum rent
--min-bedrooms      Min BR count
--max-bedrooms      Max BR count
--min-sqft          Min square footage (CL only)
--max-sqft          Max square footage (CL only)
--max-age           Max posting age in days
--has-images        Only listings with photos
--exclude-drug-houses   Skip Tenderloin, Bayview, Mid-Market, Civic Center, etc.
--delphi-pays-rent      Only the company rent subsidy zone ($750/mo off)
--fetch-emails          Extract CL reply email via Chrome CDP (CL only)
--exclude-scams         Hide listings flagged as potential scams
```

## Managing state

```bash
# See everything you've contacted
uv run python main.py contacted

# Un-mark listings to bring them back
```

Disliked and contacted listings are stored in `.houseme_state.json`. Image hashes live in `.houseme_images.json`. Listing stats (prices, titles, neighborhoods) accumulate in `.houseme_listings.json` — this is what makes scam detection get smarter over time. The subsidy zone polygon lives in `.houseme_zone.json`.

## Architecture

| File | What it does |
|------|-------------|
| `main.py` | CLI, TUI, email drafting, map generation, zone editor |
| `craigslist.py` | CL search API client via FlareSolverr |
| `zillow.py` | Zillow search via `__NEXT_DATA__` extraction (no API key, no browser) |
| `filters.py` | Scam detection, geo filtering, historical listings DB |
| `imgdb.py` | Perceptual image hashing and duplicate detection |
| `approach_cdp.py` | Chrome CDP for extracting CL reply emails (optional, needs `playwright`) |

## How Zillow works

Zillow pages embed all listing data in a `<script id="__NEXT_DATA__">` tag. Plain HTTP requests with a browser-like User-Agent fetch the page, and the JSON is extracted without any browser automation or proxies. PerimeterX blocks ~10% of requests; a simple retry loop handles it. Building listings with multiple unit types (studio, 1bd, 2bd) are expanded into separate rows so filters apply per-unit accurately.

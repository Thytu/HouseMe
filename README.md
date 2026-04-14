# HouseMe

I moved to SF and needed an apartment. Craigslist has the inventory but the experience is painful: walls of text, scam listings everywhere, and copy-pasting the same "Hi, I'm interested..." email 40 times.

So I built this. It pulls Craigslist listings into a terminal UI, flags the scams, drafts a personalized visit request email for every listing, and opens it in Gmail with one keystroke. It remembers what you've already seen so you never waste time on the same listing twice.

## What a session looks like

```bash
# Start FlareSolverr (one-time, runs in background)
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr

# Search
uv run python main.py search \
  --min-price 2200 --max-price 3750 \
  --min-bedrooms 1 --max-age 7 \
  --exclude-drug-houses \
  --has-images
```

You get a navigable table. Arrow keys to browse, Enter to see photos, `e` to fire off an email, `d` to never see it again. Done in 10 minutes instead of an hour.

## Setup

```bash
git clone https://github.com/Thytu/HouseMe.git
cd HouseMe
uv sync
```

Needs **Python 3.10+**, **FlareSolverr** on port 8191, and `ANTHROPIC_API_KEY` set for email drafting.

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

## All search options

```
--site              Craigslist site (default: sfbay)
--area              Sub-area (default: sfc)
-q / --query        Search text (e.g. "pet friendly")
-n / --limit        Results per batch (default: 25)
--min-price         Minimum rent
--max-price         Maximum rent
--min-bedrooms      Min BR count
--max-bedrooms      Max BR count
--min-sqft          Min square footage
--max-sqft          Max square footage
--max-age           Max posting age in days
--has-images        Only listings with photos
--exclude-drug-houses   Skip Tenderloin, Bayview, Mid-Market, Civic Center, etc.
--delphi-pays-rent      Only the company rent subsidy zone ($750/mo off)
--fetch-emails          Extract CL reply email via Chrome CDP (fills the To: field)
```

## Managing state

```bash
# See everything you've contacted
uv run python main.py contacted

# Un-mark listings to bring them back
```

Disliked and contacted listings are stored in `.houseme_state.json`. Image hashes live in `.houseme_images.json`. Listing stats (prices, titles, neighborhoods) accumulate in `.houseme_listings.json` — this is what makes scam detection get smarter over time.

## Architecture

| File | What it does |
|------|-------------|
| `main.py` | CLI, TUI, email drafting, map generation |
| `craigslist.py` | CL search API client via FlareSolverr |
| `filters.py` | Scam detection, geo filtering, historical listings DB |
| `imgdb.py` | Perceptual image hashing and duplicate detection |
| `approach_cdp.py` | Chrome CDP for extracting CL reply emails (optional, needs `playwright`) |

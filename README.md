# HouseMe

Craigslist apartment hunter with a TUI, scam detection, and AI-drafted visit request emails.

Built for people relocating to SF who don't want to waste time on scams or copy-pasting emails.

## What it does

- Searches Craigslist apartments via their internal API (bypasses Cloudflare with FlareSolverr)
- Shows results in a navigable terminal UI (arrow keys, not a wall of text)
- Flags scams: below-market pricing, no images, reposts, stale listings, reused photos across listings
- Drafts a visit request email for every listing using Claude Haiku and opens it in Gmail
- Remembers what you've already contacted or disliked — never shows them again
- Filters by price, bedrooms, sqft, posting age, and geographic zones
- Detects duplicate photos across listings using perceptual hashing (builds up over time)

## Prerequisites

- **Python 3.10+**
- **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** running on port 8191 (Cloudflare bypass):
  ```bash
  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr
  ```
- **Anthropic API key** set as `ANTHROPIC_API_KEY` (for email drafting with Haiku)

## Setup

```bash
git clone https://github.com/Thytu/HouseMe.git
cd HouseMe
uv sync
```

On first run, you'll be prompted for your name, occupation, and move-in availability. This is saved locally and used to personalize email drafts.

## Usage

### Search apartments

```bash
uv run python main.py search
```

With filters:

```bash
uv run python main.py search \
  --min-price 2200 \
  --max-price 3750 \
  --min-bedrooms 1 \
  --max-bedrooms 2 \
  --max-age 14 \
  --exclude-drug-houses \
  -n 20
```

### TUI keybindings

| Key | Action |
|-----|--------|
| `up` / `down` | Navigate listings |
| `e` | Open Gmail with AI-drafted visit request |
| `o` | Open listing in browser |
| `d` | Dislike — hides from all future runs |
| `c` | Contacted — hides from all future runs |
| `l` | Load more listings |
| `q` | Quit |

### Manage contacted listings

```bash
# List all contacted listings
uv run python main.py contacted

# You'll be prompted to un-mark any you want back
```

### All search options

| Flag | Description |
|------|-------------|
| `--site` | Craigslist site (default: `sfbay`) |
| `--area` | Sub-area (default: `sfc`) |
| `-q` / `--query` | Search query (e.g. `pet friendly`) |
| `-n` / `--limit` | Max results per page (default: 25) |
| `--min-price` / `--max-price` | Rent range |
| `--min-bedrooms` / `--max-bedrooms` | Bedroom range |
| `--min-sqft` / `--max-sqft` | Square footage range |
| `--max-age` | Max posting age in days |
| `--exclude-drug-houses` | Exclude Tenderloin, Bayview, Mid-Market, Civic Center |
| `--delphi-pays-rent` | Only show listings in the Delphi rent subsidy zone ($750 off) |
| `--fetch-emails` | Pre-fill the To: field by extracting CL reply emails via Chrome CDP |

## How scam detection works

Each listing gets checked for:

- **LOW $** — rent is less than 50% of the median for that bedroom count
- **NO IMG** — listing has zero photos
- **REPOST** — same title posted multiple times with different IDs
- **STALE** — posted more than 14 days ago
- **DUPE IMG** — first photo matches another listing's photo (perceptual hash, persists across runs)

## How email drafting works

Every listing gets a personalized visit request email drafted by Claude Haiku. It uses your info from first-run setup and writes in a casual, direct tone — no fluff, no copy-pasting the listing description back at the landlord.

Clicking `e` opens Gmail compose with subject + body pre-filled. With `--fetch-emails`, the To: field is also filled using the CL anonymous relay address (extracted via Chrome DevTools Protocol).

## Files

| File | Purpose |
|------|---------|
| `main.py` | CLI + TUI app |
| `craigslist.py` | CL search API client (via FlareSolverr) |
| `imgdb.py` | Perceptual image hash database |
| `approach_cdp.py`* | Chrome CDP email extraction (not committed) |

\* Create your own `approach_cdp.py` if you want `--fetch-emails` support. It needs to export `ensure_cdp()` and `get_reply_email(url)`.

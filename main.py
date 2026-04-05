import json
import re
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import quote

import anthropic
import click
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header
from textual import work

import craigslist
import imgdb

STATE_FILE = Path(__file__).parent / ".houseme_state.json"

def _get_applicant_info():
    """Load applicant info from state, or prompt on first run."""
    state = _load_state()
    info = state.get("applicant")
    if info:
        return f"Name: {info['name']}\nRole: {info['role']}\nAvailability: {info['availability']}"

    click.echo("\n  First run — tell me about yourself:\n")
    name = click.prompt("  Full name")
    role = click.prompt("  Occupation (e.g. 'AI Engineer at Delphi')")
    availability = click.prompt("  Move-in availability (e.g. 'Can move in any time')")

    state["applicant"] = {"name": name, "role": role, "availability": availability}
    _save_state(state)
    click.echo()
    return f"Name: {name}\nRole: {role}\nAvailability: {availability}"

COMPANY_ZONE = [
    (37.800, -122.441), (37.806, -122.422), (37.808, -122.418),
    (37.808, -122.410), (37.808, -122.403), (37.798, -122.398),
    (37.792, -122.394), (37.787, -122.391), (37.781, -122.390),
    (37.778, -122.392), (37.776, -122.405), (37.775, -122.420),
    (37.774, -122.435), (37.775, -122.449),
]
COMPANY_SUBSIDY = 750

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


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"disliked": [], "contacted": []}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Filters & helpers
# ---------------------------------------------------------------------------
def _is_excluded_area(post):
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


def _detect_scam_flags(results):
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


def point_in_polygon(lat, lon, polygon):
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


# ---------------------------------------------------------------------------
# Pipeline: fetch → filter → scam check → image check → draft emails
# ---------------------------------------------------------------------------
def fetch_and_process(search_opts, offset=0, seen_pids=None):
    """Run the full pipeline. Returns (new_results, total, next_offset).

    search_opts: dict with site, area, query, extra_params, delphi_pays_rent,
                 exclude_drug_houses, max_age, fetch_emails, limit.
    offset: CL API offset for pagination.
    seen_pids: set of PIDs already displayed (to avoid duplicates).
    """
    if seen_pids is None:
        seen_pids = set()

    site = search_opts["site"]
    area = search_opts["area"]
    query = search_opts["query"]
    limit = search_opts["limit"]
    extra_params = search_opts["extra_params"]
    delphi_pays_rent = search_opts["delphi_pays_rent"]
    exclude_drug_houses = search_opts["exclude_drug_houses"]
    max_age = search_opts["max_age"]
    fetch_emails = search_opts["fetch_emails"]
    applicant_info = search_opts["applicant_info"]

    state = _load_state()
    hidden_pids = set(state["disliked"] + state["contacted"])

    collected = []
    current_offset = offset

    # Keep fetching pages until we have enough results after filtering
    while len(collected) < limit:
        raw, total = craigslist.search(
            site=site, area=area, category="apa",
            query=query, offset=current_offset, **extra_params,
        )

        if not raw:
            break

        current_offset += len(raw)

        # Apply filters
        if max_age is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
            raw = [r for r in raw if r.get("posted_date") and r["posted_date"] >= cutoff]

        if exclude_drug_houses:
            raw = [r for r in raw if not _is_excluded_area(r)]

        if delphi_pays_rent:
            raw = [
                r for r in raw
                if r.get("lat") and r.get("lon")
                and point_in_polygon(r["lat"], r["lon"], COMPANY_ZONE)
            ]

        # Remove hidden and already-seen
        raw = [r for r in raw if r["pid"] not in hidden_pids and r["pid"] not in seen_pids]

        collected.extend(raw)

        # Don't fetch more than exists
        if current_offset >= total:
            break

    # Trim to limit
    collected = collected[:limit]

    if not collected:
        return [], total, current_offset

    # Scam flags
    _detect_scam_flags(collected)

    # Image fingerprint check
    imgdb.check_and_store(collected)
    for r in collected:
        if r.get("img_reuse_pids"):
            r["flags"].append("DUPE IMG")

    # Fetch reply emails
    if fetch_emails:
        from approach_cdp import ensure_cdp, get_reply_email
        ensure_cdp()
        for post in collected:
            try:
                post["reply_email"] = get_reply_email(post["url"], verbose=False)
            except Exception:
                post["reply_email"] = None

    # Draft emails via Haiku (concurrent)
    client = anthropic.Anthropic()
    first_name = applicant_info.split("\n")[0].split(":")[1].strip().split()[0]

    def _draft_email(post):
        price = post.get("price")
        price_str = f"${price:,}/mo" if price else "N/A"
        listing_info = (
            f"Title: {post.get('title', 'N/A')}\n"
            f"Price: {price_str}\n"
            f"Bedrooms: {post.get('bedrooms', 'N/A')}\n"
            f"Sqft: {post.get('sqft', 'N/A')}\n"
            f"Location: {post.get('location', '')} {post.get('neighborhood', '') or ''}\n"
            f"URL: {post.get('url', '')}"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                "Write an email requesting to visit this apartment. Match the tone and structure of this example EXACTLY:\n\n"
                "---\n"
                "Hi,\n\n"
                "I'm interested in viewing the 1-bedroom apartment in Nob Hill.\n"
                "I am an AI engineer, working at Delphi - SF-based startup, and can move in as soon as possible\n\n"
                "I would like to schedule a visit at your earliest convenience.\n"
                "Please let me know what times work for you.\n\n"
                f"Thanks,\n{first_name}\n"
                "---\n\n"
                "Rules:\n"
                "- DON'T introduce yourself by full name in the body (only sign off with first name)\n"
                "- DON'T say 'My name is' or lead with your name\n"
                "- Mention occupation and company casually mid-sentence, not as a formal introduction\n"
                "- Keep it short, natural, no fluff\n"
                "- Adapt the listing details (BR count, neighborhood) but keep the same structure\n\n"
                f"APPLICANT:\n{applicant_info}\n\n"
                f"LISTING:\n{listing_info}\n\n"
                "Output the email in this exact format:\n"
                "SUBJECT: <short clean subject line>\n\n<email body>\n"
                "The subject should be simple like 'Interested in 1BD in Nob Hill'. No spammy CL title copy-paste."
            )}],
        )
        raw = resp.content[0].text.strip()
        if raw.upper().startswith("SUBJECT:"):
            first_line, _, email_body = raw.partition("\n")
            subject_text = first_line.split(":", 1)[1].strip()
            email_body = email_body.strip()
        else:
            subject_text = f"Interested in apartment in {post.get('neighborhood') or post.get('location') or 'SF'}"
            email_body = raw
        subject = quote(subject_text)
        body = quote(email_body)
        reply_email = post.get("reply_email", "")
        post["_gmail_url"] = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&to={quote(reply_email)}&su={subject}&body={body}"
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_draft_email, post): post for post in collected}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                futures[future]["_gmail_url"] = ""

    return collected, total, current_offset


# ---------------------------------------------------------------------------
# Textual TUI
# ---------------------------------------------------------------------------
class HouseMeApp(App):
    CSS = """
    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: $accent 30%; }
    """

    BINDINGS = [
        Binding("d", "dislike", "Dislike"),
        Binding("c", "contacted", "Contacted"),
        Binding("e", "open_draft", "Email draft"),
        Binding("o", "open_listing", "Open listing"),
        Binding("l", "load_more", "Load more"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, results, total, search_opts, offset, delphi_pays_rent, filters_desc, **kwargs):
        super().__init__(**kwargs)
        self.results = list(results)
        self.total = total
        self.search_opts = search_opts
        self.offset = offset
        self.delphi_pays_rent = delphi_pays_rent
        self.filters_desc = filters_desc
        self.state = _load_state()
        self._pid_by_row_key = {}
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self):
        self.title = f"HouseMe — {self.total:,} listings"
        self.sub_title = self.filters_desc or "All listings"

        table = self.query_one(DataTable)
        cols = ["Rent"]
        if self.delphi_pays_rent:
            cols.append("Subsidy")
        cols += ["BR", "Sqft", "Title", "Location", "Posted", "Flags"]
        table.add_columns(*cols)

        self._add_rows(self.results)

    def _add_rows(self, posts):
        table = self.query_one(DataTable)
        for post in posts:
            price_raw = post.get("price")
            price = post.get("price_str") or (f"${price_raw:,}" if price_raw else "—")
            beds = str(post.get("bedrooms", "")) if post.get("bedrooms") is not None else "—"
            sqft = f"{post['sqft']:,}" if post.get("sqft") else "—"
            title = post.get("title") or "untitled"
            if len(title) > 50:
                title = title[:47] + "..."
            loc = post.get("neighborhood") or post.get("location", "")
            date = post["posted_date"].strftime("%b %d %H:%M") if post.get("posted_date") else ""
            flags = " ".join(post.get("flags", [])) or "OK"

            row = [price]
            if self.delphi_pays_rent:
                after = f"${max(0, price_raw - COMPANY_SUBSIDY):,}" if price_raw else "—"
                row.append(after)
            row += [beds, sqft, title, loc, date, flags]

            row_key = table.add_row(*row)
            self._pid_by_row_key[row_key] = post["pid"]

    def _get_selected_post(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key = list(table.rows.keys())[table.cursor_row]
        pid = self._pid_by_row_key.get(row_key)
        for post in self.results:
            if post["pid"] == pid:
                return post
        return None

    def _remove_current_row(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        row_key = list(table.rows.keys())[table.cursor_row]
        table.remove_row(row_key)

    def action_dislike(self):
        post = self._get_selected_post()
        if not post:
            return
        pid = post["pid"]
        if pid not in self.state["disliked"]:
            self.state["disliked"].append(pid)
            _save_state(self.state)
        self._remove_current_row()
        self.notify("Disliked — hidden from future runs", severity="warning")

    def action_contacted(self):
        post = self._get_selected_post()
        if not post:
            return
        pid = post["pid"]
        if pid not in self.state["contacted"]:
            self.state["contacted"].append(pid)
            _save_state(self.state)
        self._remove_current_row()
        self.notify("Marked as contacted", severity="information")

    def action_open_draft(self):
        post = self._get_selected_post()
        if not post:
            return
        gmail_url = post.get("_gmail_url")
        if gmail_url:
            webbrowser.open(gmail_url)
            self.notify("Opening Gmail draft...")
        else:
            self.notify("No draft available", severity="error")

    def action_open_listing(self):
        post = self._get_selected_post()
        if not post:
            return
        url = post.get("url", "")
        if url:
            webbrowser.open(url)
            self.notify("Opening listing...")

    def action_load_more(self):
        if self._loading:
            self.notify("Already loading...", severity="warning")
            return
        self._loading = True
        self.notify("Loading more listings...")
        self._do_load_more()

    @work(thread=True)
    def _do_load_more(self):
        seen_pids = {p["pid"] for p in self.results}
        new_results, total, new_offset = fetch_and_process(
            self.search_opts, offset=self.offset, seen_pids=seen_pids,
        )
        self.offset = new_offset
        self.total = total

        if new_results:
            self.results.extend(new_results)
            self.call_from_thread(self._add_rows, new_results)
            self.call_from_thread(
                self.notify, f"Loaded {len(new_results)} more listings"
            )
        else:
            self.call_from_thread(
                self.notify, "No more listings available", severity="warning"
            )

        self._loading = False
        self.call_from_thread(self._update_title)

    def _update_title(self):
        self.title = f"HouseMe — {self.total:,} listings ({len(self.results)} loaded)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.group()
def cli():
    """HouseMe — Craigslist apartment hunter."""
    pass


@cli.command()
@click.option("--site", default="sfbay", help="Craigslist site.")
@click.option("--area", default="sfc", help="Sub-area.")
@click.option("-q", "--query", default=None, help="Search query.")
@click.option("-n", "--limit", default=25, type=int, help="Max results per page.")
@click.option("--min-price", default=None, type=int, help="Minimum rent.")
@click.option("--max-price", default=None, type=int, help="Maximum rent.")
@click.option("--min-bedrooms", default=None, type=int, help="Min bedrooms.")
@click.option("--max-bedrooms", default=None, type=int, help="Max bedrooms.")
@click.option("--min-sqft", default=None, type=int, help="Min sqft.")
@click.option("--max-sqft", default=None, type=int, help="Max sqft.")
@click.option("--delphi-pays-rent", is_flag=True, help="Only company subsidy zone ($750 off).")
@click.option("--exclude-drug-houses", is_flag=True, help="Exclude bad neighborhoods.")
@click.option("--max-age", default=None, type=int, help="Max posting age in days.")
@click.option("--fetch-emails", is_flag=True, help="Fetch reply emails via CDP.")
def search(site, area, query, limit, min_price, max_price, min_bedrooms, max_bedrooms,
           min_sqft, max_sqft, delphi_pays_rent, exclude_drug_houses, max_age, fetch_emails):
    """Search Craigslist apartments for rent."""

    extra_params = {}
    if min_price is not None:
        extra_params["min_price"] = min_price
    if max_price is not None:
        extra_params["max_price"] = max_price
    if min_bedrooms is not None:
        extra_params["min_bedrooms"] = min_bedrooms
    if max_bedrooms is not None:
        extra_params["max_bedrooms"] = max_bedrooms
    if min_sqft is not None:
        extra_params["min_sqft"] = min_sqft
    if max_sqft is not None:
        extra_params["max_sqft"] = max_sqft

    applicant_info = _get_applicant_info()

    search_opts = {
        "site": site, "area": area, "query": query, "limit": limit,
        "extra_params": extra_params, "delphi_pays_rent": delphi_pays_rent,
        "exclude_drug_houses": exclude_drug_houses, "max_age": max_age,
        "fetch_emails": fetch_emails, "applicant_info": applicant_info,
    }

    results, total, offset = fetch_and_process(search_opts)

    if not results:
        click.echo("No results found.")
        return

    filters = []
    if max_age is not None:
        filters.append(f"<{max_age}d old")
    if delphi_pays_rent:
        filters.append("delphi zone")
    if exclude_drug_houses:
        filters.append("bad hoods excluded")
    state = _load_state()
    hidden = len(state["disliked"]) + len(state["contacted"])
    if hidden:
        filters.append(f"{hidden} hidden")
    filters_desc = ", ".join(filters) if filters else ""

    app = HouseMeApp(results, total, search_opts, offset, delphi_pays_rent, filters_desc)
    app.run()


@cli.command()
def contacted():
    """List contacted listings and optionally revert them."""
    state = _load_state()
    pids = state.get("contacted", [])

    if not pids:
        click.echo("No contacted listings.")
        return

    click.echo(f"\n  {len(pids)} contacted listing(s):\n")
    for i, pid in enumerate(pids, 1):
        url = f"https://sfbay.craigslist.org/search/apa?pid={pid}"
        click.echo(f"  {i}. PID {pid}  —  https://sfbay.craigslist.org/sfc/apa/d/listing/{pid}.html")

    click.echo()
    raw = click.prompt(
        "  Enter numbers to un-mark (e.g. 1,3), or press Enter to keep all",
        default="", show_default=False,
    )

    if not raw.strip():
        return

    to_revert = set()
    for part in raw.replace(",", " ").split():
        try:
            n = int(part)
            if 1 <= n <= len(pids):
                to_revert.add(n)
        except ValueError:
            pass

    if not to_revert:
        click.echo("  No valid numbers.")
        return

    reverted = []
    for n in sorted(to_revert, reverse=True):
        reverted.append(pids.pop(n - 1))

    state["contacted"] = pids
    _save_state(state)
    click.echo(f"  Reverted {len(reverted)} listing(s) — they'll show up in future searches again.")


if __name__ == "__main__":
    cli()

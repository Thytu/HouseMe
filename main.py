import io
import json
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import anthropic
import click
import requests
from PIL import Image
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static
from textual_image.widget import Image as ImageWidget

import craigslist
import imgdb
from filters import COMPANY_ZONE, detect_scam_flags, is_excluded_area, point_in_polygon

CL_IMG_URL = "https://images.craigslist.org/{}_600x450.jpg"
STATE_FILE = Path(__file__).parent / ".houseme_state.json"


COMPANY_SUBSIDY = 750


# ---------------------------------------------------------------------------
# Search options
# ---------------------------------------------------------------------------
@dataclass
class SearchOpts:
    site: str = "sfbay"
    area: str = "sfc"
    query: str | None = None
    limit: int = 25
    extra_params: dict = field(default_factory=dict)
    delphi_pays_rent: bool = False
    exclude_drug_houses: bool = False
    max_age: int | None = None
    fetch_emails: bool = False
    has_images: bool = False
    applicant_info: str = ""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"disliked": [], "contacted": [], "contacted_meta": {}}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_applicant_info(state):
    """Load applicant info from state, or prompt on first run."""
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


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
def _fetch_and_filter(opts: SearchOpts, offset: int, seen_pids: set, hidden_pids: set):
    """Fetch from CL API and apply all filters. Returns (filtered_results, total, next_offset)."""
    collected = []
    total = 0
    current_offset = offset

    cutoff = (datetime.now(timezone.utc) - timedelta(days=opts.max_age)) if opts.max_age is not None else None

    while len(collected) < opts.limit:
        raw, total = craigslist.search(
            site=opts.site, area=opts.area, category="apa",
            query=opts.query, offset=current_offset, **opts.extra_params,
        )

        if not raw:
            break

        current_offset += len(raw)
        click.echo(f"  Fetched {len(raw)} from CL (total: {total})", err=True)

        if cutoff is not None:
            before = len(raw)
            raw = [r for r in raw if r.get("posted_date") and r["posted_date"] >= cutoff]
            if len(raw) != before:
                click.echo(f"    max_age: {before} → {len(raw)}", err=True)

        if opts.exclude_drug_houses:
            before = len(raw)
            raw = [r for r in raw if not is_excluded_area(r)]
            if len(raw) != before:
                click.echo(f"    exclude_drug_houses: {before} → {len(raw)}", err=True)

        if opts.delphi_pays_rent:
            before = len(raw)
            no_coords = [r for r in raw if not (r.get("lat") and r.get("lon"))]
            raw = [
                r for r in raw
                if r.get("lat") and r.get("lon")
                and point_in_polygon(r["lat"], r["lon"], COMPANY_ZONE)
            ]
            if len(raw) != before:
                click.echo(f"    delphi_pays_rent: {before} → {len(raw)} ({len(no_coords)} had no coords)", err=True)

        if opts.has_images:
            before = len(raw)
            raw = [r for r in raw if r.get("image_count")]
            if len(raw) != before:
                click.echo(f"    has_images: {before} → {len(raw)}", err=True)

        before = len(raw)
        raw = [r for r in raw if r["pid"] not in hidden_pids and r["pid"] not in seen_pids]
        if len(raw) != before:
            click.echo(f"    hidden/seen: {before} → {len(raw)}", err=True)
        collected.extend(raw)

        if current_offset >= total:
            break

    return collected[:opts.limit], total, current_offset


def _flag_scams_and_dupes(results):
    """Run scam detection and image fingerprinting on results."""
    detect_scam_flags(results)
    imgdb.check_and_store(results)
    for r in results:
        if r.get("img_reuse_pids"):
            r["flags"].append("DUPE IMG")


def _fetch_reply_emails(results):
    """Fetch CL reply emails via Chrome CDP."""
    from approach_cdp import ensure_cdp, get_reply_email
    ensure_cdp()
    for post in results:
        try:
            post["reply_email"] = get_reply_email(post["url"], verbose=False)
        except Exception:
            post["reply_email"] = None


EMAIL_PROMPT = """\
Write an email requesting to visit this apartment. Match the tone and structure of this example EXACTLY:

---
Hi,

I'm interested in viewing the 1-bedroom apartment in Nob Hill.
I am an AI engineer, working at Delphi - SF-based startup, and can move in as soon as possible

I would like to schedule a visit at your earliest convenience.
Please let me know what times work for you.

Thanks,
{first_name}
---

Rules:
- DON'T introduce yourself by full name in the body (only sign off with first name)
- DON'T say 'My name is' or lead with your name
- Mention occupation and company casually mid-sentence, not as a formal introduction
- Keep it short, natural, no fluff
- Adapt the listing details (BR count, neighborhood) but keep the same structure

APPLICANT:
{applicant_info}

LISTING:
{listing_info}

Output the email in this exact format:
SUBJECT: <short clean subject line>

<email body>
The subject should be simple like 'Interested in 1BD in Nob Hill'. No spammy CL title copy-paste."""


def _draft_emails(results, applicant_info, first_name):
    """Draft visit request emails via Haiku, concurrently."""
    client = anthropic.Anthropic()

    def _draft_one(post):
        price = post.get("price")
        price_str = f"${price:,}/mo" if price else "N/A"
        listing_info = (
            f"Title: {post.get('title', 'N/A')}\n"
            f"Price: {price_str}\n"
            f"Bedrooms: {post.get('bedrooms', 'N/A')}\n"
            f"Sqft: {post.get('sqft', 'N/A')}\n"
            f"Location: {post.get('location', '')} {post.get('neighborhood') or ''}\n"
            f"URL: {post.get('url', '')}"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": EMAIL_PROMPT.format(
                first_name=first_name,
                applicant_info=applicant_info,
                listing_info=listing_info,
            )}],
        )
        raw_text = resp.content[0].text.strip()
        if raw_text.upper().startswith("SUBJECT:"):
            first_line, _, email_body = raw_text.partition("\n")
            subject_text = first_line.split(":", 1)[1].strip()
            email_body = email_body.strip()
        else:
            subject_text = f"Interested in apartment in {post.get('neighborhood') or post.get('location') or 'SF'}"
            email_body = raw_text

        reply_email = post.get("reply_email") or ""
        post["_gmail_url"] = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&to={quote(reply_email)}&su={quote(subject_text)}&body={quote(email_body)}"
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_draft_one, post): post for post in results}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  Draft failed for PID {futures[future].get('pid')}: {e}")
                futures[future]["_gmail_url"] = ""


def fetch_and_process(opts: SearchOpts, offset=0, seen_pids=None):
    """Full pipeline: fetch -> filter -> flag -> draft. Returns (results, total, next_offset)."""
    if seen_pids is None:
        seen_pids = set()

    state = _load_state()
    hidden_pids = set(state["disliked"] + state["contacted"])

    results, total, next_offset = _fetch_and_filter(opts, offset, seen_pids, hidden_pids)
    if not results:
        return [], total, next_offset

    _flag_scams_and_dupes(results)

    if opts.fetch_emails:
        _fetch_reply_emails(results)

    name_parts = state.get("applicant", {}).get("name", "").split()
    first_name = name_parts[0] if name_parts else "Applicant"
    _draft_emails(results, opts.applicant_info, first_name)

    return results, total, next_offset


# ---------------------------------------------------------------------------
# Textual TUI
# ---------------------------------------------------------------------------


class ListingDetailScreen(Screen):
    """Full-screen detail view for a single listing with inline image preview."""

    BINDINGS = [
        Binding("left", "prev_image", "Prev image"),
        Binding("right", "next_image", "Next image"),
        Binding("d", "dislike", "Dislike"),
        Binding("c", "contacted", "Contacted"),
        Binding("e", "open_draft", "Email draft"),
        Binding("o", "open_listing", "Open listing"),
        Binding("escape", "go_back", "Back"),
        Binding("q", "go_back", "Back", show=False),
    ]

    DEFAULT_CSS = """
    ListingDetailScreen {
        layout: vertical;
    }
    #detail-meta {
        height: auto;
        max-height: 5;
        padding: 0 1;
        background: $surface;
    }
    #detail-image {
        width: 1fr;
        height: 1fr;
    }
    #detail-status {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
    }
    .hidden {
        display: none;
    }
    #detail-counter {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, post: dict, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.post = post
        self._image_ids: list[str] = post.get("image_ids", [])
        self._image_cache: dict[str, Image.Image] = {}
        self._current_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._build_meta(), id="detail-meta")
        yield ImageWidget(id="detail-image", classes="hidden")
        yield Static("", id="detail-status")
        yield Static("", id="detail-counter")
        yield Footer()

    def _build_meta(self) -> Text:
        p = self.post
        price_raw = p.get("price")
        price = p.get("price_str") or (f"${price_raw:,}" if price_raw else "N/A")
        beds = f"{p['bedrooms']}BR" if p.get("bedrooms") is not None else ""
        sqft = f"{p['sqft']:,} sqft" if p.get("sqft") else ""
        loc = p.get("neighborhood") or p.get("location", "")
        top_parts = [s for s in [price, beds, sqft, loc] if s]

        title = p.get("title") or "untitled"
        flags = " ".join(p.get("flags", [])) or "OK"
        date = p["posted_date"].strftime("%b %d, %H:%M") if p.get("posted_date") else ""
        img_count = f"{len(self._image_ids)} photo{'s' if len(self._image_ids) != 1 else ''}"

        meta = Text()
        meta.append(" \u00b7 ".join(top_parts), style="bold")
        meta.append("\n")
        meta.append(title)
        meta.append("\n")
        meta.append(f"Flags: {flags}  \u00b7  Posted: {date}  \u00b7  {img_count}")
        return meta

    def on_mount(self) -> None:
        self.title = "Listing Detail"
        if self._image_ids:
            self._set_status("Loading...")
            self._update_counter()
            self._prefetch_all()
        else:
            self._set_status("No images available")
            self._update_counter()

    def _update_counter(self) -> None:
        total = len(self._image_ids)
        counter = self.query_one("#detail-counter", Static)
        if total == 0:
            counter.update("")
        else:
            counter.update(f"Image {self._current_idx + 1} / {total}")

    def _set_status(self, msg: str) -> None:
        """Show status text and hide the image widget."""
        self.query_one("#detail-image").add_class("hidden")
        status = self.query_one("#detail-status", Static)
        status.remove_class("hidden")
        status.update(msg)

    def _set_image(self, img: Image.Image) -> None:
        """Show an image and hide the status text."""
        self.query_one("#detail-status").add_class("hidden")
        image_widget = self.query_one("#detail-image", ImageWidget)
        image_widget.remove_class("hidden")
        image_widget.image = img

    @work(thread=True)
    def _prefetch_all(self) -> None:
        """Download all images concurrently. Show the first one as soon as it lands."""

        def _download(img_id: str) -> tuple[str, Image.Image | None]:
            try:
                resp = requests.get(CL_IMG_URL.format(img_id), timeout=15)
                resp.raise_for_status()
                return img_id, Image.open(io.BytesIO(resp.content))
            except Exception:
                return img_id, None

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_download, img_id): img_id
                for img_id in self._image_ids
                if img_id not in self._image_cache
            }
            for future in as_completed(futures):
                img_id, img = future.result()
                if img is not None:
                    self._image_cache[img_id] = img
                    current_id = self._image_ids[self._current_idx]
                    if img_id == current_id:
                        self.app.call_from_thread(self._show_image, img, self._current_idx)

        current_id = self._image_ids[self._current_idx]
        if current_id not in self._image_cache:
            self.app.call_from_thread(self._set_status, "Failed to load image")

    def _show_current_image(self) -> None:
        """Show the current image from cache, or Loading... if not yet available."""
        img_id = self._image_ids[self._current_idx]
        if img_id in self._image_cache:
            self._set_image(self._image_cache[img_id])
        else:
            self._set_status("Loading...")
        self._update_counter()

    def _show_image(self, img: Image.Image, idx: int) -> None:
        """Display a loaded image if it's still the current index."""
        if idx == self._current_idx:
            self._set_image(img)
            self._update_counter()

    def action_next_image(self) -> None:
        if not self._image_ids:
            return
        self._current_idx = (self._current_idx + 1) % len(self._image_ids)
        self._show_current_image()

    def action_prev_image(self) -> None:
        if not self._image_ids:
            return
        self._current_idx = (self._current_idx - 1) % len(self._image_ids)
        self._show_current_image()

    def action_dislike(self) -> None:
        app: HouseMeApp = self.app  # type: ignore[assignment]
        app.dislike_post(self.post)
        self.app.pop_screen()

    def action_contacted(self) -> None:
        app: HouseMeApp = self.app  # type: ignore[assignment]
        app.contact_post(self.post)
        self.app.pop_screen()

    def action_open_draft(self) -> None:
        gmail_url = self.post.get("_gmail_url")
        if gmail_url:
            webbrowser.open(gmail_url)
            self.notify("Opening Gmail draft...")
        else:
            self.notify("No draft available", severity="error")

    def action_open_listing(self) -> None:
        url = self.post.get("url", "")
        if url:
            webbrowser.open(url)
            self.notify("Opening listing...")

    def action_go_back(self) -> None:
        self.app.pop_screen()


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

    def __init__(self, results, total, opts: SearchOpts, delphi_pays_rent, filters_desc, **kwargs):
        super().__init__(**kwargs)
        self.results = list(results)
        self.total = total
        self.opts = opts
        self.delphi_pays_rent = delphi_pays_rent
        self.filters_desc = filters_desc
        self.state = _load_state()
        self._pid_to_post = {p["pid"]: p for p in self.results}
        self._pid_by_row_key = {}
        self._dismissed_pids: set[int] = set()
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
        return self._pid_to_post.get(pid)

    def _remove_post_row(self, pid: int) -> None:
        """Remove a row from the table by PID."""
        row_key = None
        for rk, p in self._pid_by_row_key.items():
            if p == pid:
                row_key = rk
                break
        if row_key is None:
            return
        self._pid_by_row_key.pop(row_key, None)
        self._pid_to_post.pop(pid, None)
        self.results = [r for r in self.results if r["pid"] != pid]
        self._dismissed_pids.add(pid)
        table = self.query_one(DataTable)
        table.remove_row(row_key)

    def dislike_post(self, post: dict) -> None:
        """Dislike a post: persist to state and remove from table."""
        pid = post["pid"]
        if pid not in self.state["disliked"]:
            self.state["disliked"].append(pid)
            _save_state(self.state)
        self._remove_post_row(pid)
        self.notify("Disliked — hidden from future runs", severity="warning")

    def contact_post(self, post: dict) -> None:
        """Mark a post as contacted: persist to state and remove from table."""
        pid = post["pid"]
        if pid not in self.state["contacted"]:
            self.state["contacted"].append(pid)
            meta = self.state.setdefault("contacted_meta", {})
            meta[str(pid)] = {"title": post.get("title", ""), "url": post.get("url", "")}
            _save_state(self.state)
        self._remove_post_row(pid)
        self.notify("Marked as contacted", severity="information")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        pid = self._pid_by_row_key.get(event.row_key)
        post = self._pid_to_post.get(pid) if pid is not None else None
        if post:
            self.push_screen(ListingDetailScreen(post))

    def action_dislike(self) -> None:
        post = self._get_selected_post()
        if post:
            self.dislike_post(post)

    def action_contacted(self) -> None:
        post = self._get_selected_post()
        if post:
            self.contact_post(post)

    def action_open_draft(self) -> None:
        post = self._get_selected_post()
        if not post:
            return
        gmail_url = post.get("_gmail_url")
        if gmail_url:
            webbrowser.open(gmail_url)
            self.notify("Opening Gmail draft...")
        else:
            self.notify("No draft available", severity="error")

    def action_open_listing(self) -> None:
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
        seen_pids = {p["pid"] for p in self.results} | self._dismissed_pids
        new_results, total, _ = fetch_and_process(
            self.opts, offset=0, seen_pids=seen_pids,
        )
        self.total = total

        if new_results:
            self.results.extend(new_results)
            for p in new_results:
                self._pid_to_post[p["pid"]] = p
            self.app.call_from_thread(self._add_rows, new_results)
            self.app.call_from_thread(
                self.notify, f"Loaded {len(new_results)} more listings"
            )
        else:
            self.app.call_from_thread(
                self.notify, "No more listings available", severity="warning"
            )

        self._loading = False
        self.app.call_from_thread(self._update_title)

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
@click.option("--has-images", is_flag=True, help="Only show listings with photos.")
def search(site, area, query, limit, min_price, max_price, min_bedrooms, max_bedrooms,
           min_sqft, max_sqft, delphi_pays_rent, exclude_drug_houses, max_age, fetch_emails, has_images):
    """Search Craigslist apartments for rent."""

    extra_params = {}
    for key, val in [("min_price", min_price), ("max_price", max_price),
                     ("min_bedrooms", min_bedrooms), ("max_bedrooms", max_bedrooms),
                     ("min_sqft", min_sqft), ("max_sqft", max_sqft)]:
        if val is not None:
            extra_params[key] = val

    state = _load_state()
    applicant_info = _get_applicant_info(state)

    opts = SearchOpts(
        site=site, area=area, query=query, limit=limit,
        extra_params=extra_params, delphi_pays_rent=delphi_pays_rent,
        exclude_drug_houses=exclude_drug_houses, max_age=max_age,
        fetch_emails=fetch_emails, has_images=has_images,
        applicant_info=applicant_info,
    )

    results, total, _ = fetch_and_process(opts)

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
    hidden = len(state["disliked"]) + len(state["contacted"])
    if hidden:
        filters.append(f"{hidden} hidden")
    filters_desc = ", ".join(filters) if filters else ""

    app = HouseMeApp(results, total, opts, delphi_pays_rent, filters_desc)
    app.run()


@cli.command()
def contacted():
    """List contacted listings and optionally revert them."""
    state = _load_state()
    pids = state.get("contacted", [])

    if not pids:
        click.echo("No contacted listings.")
        return

    meta = state.get("contacted_meta", {})
    click.echo(f"\n  {len(pids)} contacted listing(s):\n")
    for i, pid in enumerate(pids, 1):
        info = meta.get(str(pid), {})
        title = info.get("title", "unknown")
        url = info.get("url", "")
        if url:
            click.echo(f"  {i}. {title}  —  {url}")
        else:
            click.echo(f"  {i}. PID {pid}")

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
        pid = pids.pop(n - 1)
        reverted.append(pid)
        meta.pop(str(pid), None)

    state["contacted"] = pids
    state["contacted_meta"] = meta
    _save_state(state)
    click.echo(f"  Reverted {len(reverted)} listing(s) — they'll show up in future searches again.")


if __name__ == "__main__":
    cli()

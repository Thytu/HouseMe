"""Perceptual image hash database for detecting reused listing photos."""

import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import imagehash
import requests
from PIL import Image

DB_FILE = Path(__file__).parent / ".houseme_images.json"
CL_IMG_URL = "https://images.craigslist.org/{}_600x450.jpg"

# Max hamming distance to consider two images "the same"
HAMMING_THRESHOLD = 8

# Flag DUPE IMG when this fraction of a listing's images match other listings
DUPE_RATIO_THRESHOLD = 0.5


def _load_db() -> dict[str, list[dict]]:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def _save_db(db: dict[str, list[dict]]) -> None:
    DB_FILE.write_text(json.dumps(db, indent=2))


def _download_and_hash(image_id: str) -> str:
    """Download a CL image and return its perceptual hash string."""
    url = CL_IMG_URL.format(image_id)
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content))
    return str(imagehash.phash(img))


def check_and_store(results: list[dict]) -> int:
    """Check all images of each listing against the DB.

    Downloads and hashes every image in each listing, then computes
    what fraction are duplicates of images seen in other listings.
    Only flags DUPE IMG when the ratio exceeds DUPE_RATIO_THRESHOLD
    (default 50%), so a single agency logo reused across listings
    won't trigger a false positive.

    Returns the number of flagged listings.
    """
    db = _load_db()

    # Pre-parse all stored hashes once (avoid re-parsing per comparison)
    parsed_db: list[tuple[imagehash.ImageHash, set[int]]] = []
    for hash_str, entries in db.items():
        parsed_hash = imagehash.hex_to_hash(hash_str)
        pids = {e["pid"] for e in entries}
        parsed_db.append((parsed_hash, pids))

    # Build download tasks: (pid, image_id) for all images of all listings
    tasks: list[tuple[int, str]] = []
    for r in results:
        pid = r["pid"]
        for img_id in r.get("image_ids", []):
            tasks.append((pid, img_id))

    # Download and hash all images concurrently
    hashes: dict[int, list[tuple[str, str]]] = {}

    def _fetch(task: tuple[int, str]) -> tuple[int, str, str]:
        pid, img_id = task
        h = _download_and_hash(img_id)
        return pid, h, img_id

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, t): t for t in tasks}
        for future in as_completed(futures):
            try:
                pid, hash_str, img_id = future.result()
                hashes.setdefault(pid, []).append((hash_str, img_id))
            except Exception as e:
                failed_pid = futures[future][0]
                print(f"  Image hash failed for PID {failed_pid}: {e}")

    # Check dupes and store — all per-listing work runs concurrently
    flagged = 0
    results_by_pid = {r["pid"]: r for r in results}

    def _check_listing(pid: int, pid_hashes: list[tuple[str, str]]) -> tuple[int, int, float]:
        """Check one listing's images against the DB. Returns (pid, dupe_count, dupe_ratio)."""
        dupe_count = 0
        for hash_str, _ in pid_hashes:
            current_hash = imagehash.hex_to_hash(hash_str)
            for stored_hash, stored_pids in parsed_db:
                if current_hash - stored_hash <= HAMMING_THRESHOLD:
                    if stored_pids - {pid}:
                        dupe_count += 1
                        break
        return pid, dupe_count, dupe_count / len(pid_hashes)

    listing_tasks = [(pid, ph) for pid, ph in hashes.items() if ph]

    with ThreadPoolExecutor(max_workers=10) as pool:
        check_futures = {pool.submit(_check_listing, pid, ph): pid for pid, ph in listing_tasks}
        for future in as_completed(check_futures):
            pid, dupe_count, dupe_ratio = future.result()
            r = results_by_pid[pid]
            pid_hashes = hashes[pid]

            r["_image_hash"] = pid_hashes[0][0]

            if dupe_ratio >= DUPE_RATIO_THRESHOLD:
                r["img_reuse_pids"] = [pid]
                r["_dupe_ratio"] = dupe_ratio
                flagged += 1

    # Store all new hashes in the DB
    for pid, pid_hashes in hashes.items():
        for hash_str, img_id in pid_hashes:
            entry = {"pid": pid, "image_id": img_id}
            if hash_str in db:
                if not any(e["pid"] == pid and e["image_id"] == img_id for e in db[hash_str]):
                    db[hash_str].append(entry)
            else:
                db[hash_str] = [entry]

    _save_db(db)
    return flagged

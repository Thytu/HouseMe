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


def _load_db():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def _save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2))


def _download_and_hash(image_id):
    """Download a CL image and return its perceptual hash string."""
    url = CL_IMG_URL.format(image_id)
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content))
    return str(imagehash.phash(img))


def check_and_store(results):
    """Check first image of each listing against the DB.

    Adds 'img_reuse_pids' to any result whose first image matches
    a previously seen listing at a different address.

    Returns the number of flagged listings.
    """
    db = _load_db()  # {phash_str: [{"pid": int, "title": str, "image_id": str}, ...]}

    # Download & hash first images concurrently
    to_check = []
    for r in results:
        image_ids = r.get("image_ids", [])
        if image_ids:
            to_check.append((r, image_ids[0]))

    hashes = {}  # pid -> hash_str

    def _fetch(item):
        post, img_id = item
        h = _download_and_hash(img_id)
        return post["pid"], h, img_id

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_fetch, item) for item in to_check]
        for future in as_completed(futures):
            try:
                pid, hash_str, img_id = future.result()
                hashes[pid] = (hash_str, img_id)
            except Exception:
                pass

    flagged = 0

    for r in results:
        pid = r["pid"]
        if pid not in hashes:
            continue

        hash_str, img_id = hashes[pid]
        current_hash = imagehash.hex_to_hash(hash_str)
        title = r.get("title", "")

        # Check against all known hashes
        matches = []
        for stored_hash_str, entries in db.items():
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = current_hash - stored_hash
            if distance <= HAMMING_THRESHOLD:
                for entry in entries:
                    if entry["pid"] != pid:
                        matches.append(entry["pid"])

        if matches:
            r["img_reuse_pids"] = matches
            flagged += 1

        # Store this image in the DB
        entry = {"pid": pid, "title": title, "image_id": img_id}
        if hash_str in db:
            # Don't duplicate same PID
            if not any(e["pid"] == pid for e in db[hash_str]):
                db[hash_str].append(entry)
        else:
            db[hash_str] = [entry]

    _save_db(db)
    return flagged

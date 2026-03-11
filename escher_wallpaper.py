#!/usr/bin/env python3
"""
Escher Wallpaper Rotator

Downloads ~1000 Escher-style art images and rotates them as
your Ubuntu desktop wallpaper every hour.

Usage:
    python3 escher_wallpaper.py download   # Download ~1000 images
    python3 escher_wallpaper.py set        # Set a random wallpaper
    python3 escher_wallpaper.py install    # Install hourly systemd timer
    python3 escher_wallpaper.py uninstall  # Remove timer
    python3 escher_wallpaper.py cycle      # Force-cycle wallpaper now (resets timer)
    python3 escher_wallpaper.py status     # Show collection stats
"""

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

WALLPAPER_DIR = Path.home() / ".escher-wallpapers"
HISTORY_FILE = WALLPAPER_DIR / ".history.json"
MIN_FILE_SIZE = 80_000   # 80KB min to filter thumbnails/errors
MIN_WIDTH = 1024          # Minimum image width for wallpaper quality

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    )
}

def _generate_queries():
    """Generate a large set of diverse search queries for Escher-style art."""
    queries = set()

    # Specific Escher works (each gets multiple query variations)
    works = [
        "Relativity staircase", "Waterfall perpetual motion",
        "Drawing Hands", "Bond of Union", "Day and Night",
        "Sky and Water", "Metamorphosis", "Ascending Descending",
        "Belvedere impossible", "Hand Reflecting Sphere",
        "Print Gallery recursive", "Reptiles lithograph",
        "Circle Limit hyperbolic", "Mobius Strip ants",
        "Three Worlds", "Encounter figures", "Other World",
        "Stars polyhedra", "Curl-up creature", "Eye reflecting skull",
        "Tower of Babel", "Concave Convex", "Depth fish",
        "Dewdrop leaf", "Gallery architecture", "House of Stairs",
        "Liberation birds", "Magic Mirror", "Puddle reflection",
        "Rind spiral peel", "Snakes infinity", "Swans tessellation",
        "Castrovalva landscape", "Still Life Street", "Balcony",
        "Up Down staircase", "Gravity stars", "Knots",
        "Path of Life", "Sun Moon", "Whirlpools spiral",
        "Fish Vignette", "Horseman tessellation", "Predestination",
        "Spirals shell", "Sphere Surface", "Order Chaos",
    ]
    for work in works:
        queries.add(f"MC Escher {work} artwork")
        queries.add(f"MC Escher {work} print high resolution")
        queries.add(f"Escher {work} lithograph")

    # Style combinations
    prefixes = [
        "MC Escher", "Escher style", "Escher inspired",
        "Escher tribute", "Escher like", "in the style of MC Escher",
    ]
    subjects = [
        "impossible architecture", "impossible staircase",
        "impossible building", "tessellation pattern",
        "recursive drawing", "infinite loop", "impossible geometry",
        "optical illusion", "paradox perspective", "impossible waterfall",
        "impossible cube", "penrose triangle", "penrose staircase",
        "impossible bridge", "gravity defying", "impossible room",
        "infinite staircase", "impossible tower", "tessellation birds",
        "tessellation fish", "tessellation lizards", "impossible world",
        "recursive architecture", "fractal architecture", "impossible city",
        "impossible castle", "mathematical art", "geometric paradox",
        "impossible structure", "surreal architecture", "impossible maze",
        "impossible labyrinth", "infinite corridor", "impossible columns",
        "warped perspective", "non-euclidean geometry",
        "impossible intersection", "tessellation butterflies",
        "impossible cathedral", "impossible monastery",
        "impossible palace", "impossible library", "impossible spiral",
    ]
    styles = [
        "black and white artwork", "detailed lithograph",
        "woodcut print", "detailed drawing", "high resolution art",
        "fine art print", "detailed illustration", "monochrome artwork",
        "grayscale illustration", "pen and ink drawing",
        "engraving style art", "crosshatch drawing",
    ]
    for prefix in prefixes:
        for subject in subjects:
            queries.add(f"{prefix} {subject}")
            for style in styles[:4]:  # limit combinations
                queries.add(f"{prefix} {subject} {style}")

    # Broader impossible/mathematical art
    broad = [
        "impossible architecture artwork black white detailed",
        "impossible geometry artwork monochrome high resolution",
        "impossible object artwork detailed illustration",
        "mathematical art tessellation grayscale",
        "surreal impossible architecture drawing detailed",
        "paradox architecture artwork lithograph style",
        "non-euclidean architecture artwork black white",
        "impossible perspective artwork detailed grayscale",
        "optical illusion architecture monochrome art",
        "recursive fractal architecture artwork grayscale",
        "impossible architecture wallpaper black white 4k",
        "impossible geometry desktop wallpaper monochrome",
        "Escher wallpaper desktop black white high resolution",
        "impossible staircase wallpaper monochrome detailed",
        "tessellation wallpaper grayscale mathematical art",
        "MC Escher desktop wallpaper high resolution",
        "impossible architecture 4k wallpaper grayscale",
        "surreal architecture wallpaper monochrome detailed",
        "impossible geometry wallpaper black white 4k",
        "penrose impossible geometry artwork high resolution",
        "impossible architecture illustration grayscale",
        "mathematical impossible structure artwork monochrome",
        "droste effect recursive image black white",
        "impossible triangle artwork detailed grayscale",
        "surreal gravity architecture black white artwork",
        "infinite recursion artwork monochrome detailed",
        "paradox geometry illustration black white",
        "impossible perspective building artwork grayscale",
        "tessellation metamorphosis artwork grayscale detailed",
        "impossible architecture engraving style artwork",
    ]
    queries.update(broad)

    return list(queries)


SEARCH_QUERIES = _generate_queries()


def _make_request(url, extra_headers=None):
    """Make an HTTP request with standard headers."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=30)


def search_images(query):
    """Search DuckDuckGo Images for large, high-quality results."""
    try:
        # Step 1: Get vqd token
        encoded_q = urllib.parse.quote(query)
        url = (f"https://duckduckgo.com/"
               f"?q={encoded_q}&iax=images&ia=images")
        resp = _make_request(url)
        html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'vqd="([^"]+)"', html)
        if not m:
            return []
        vqd = m.group(1)

        # Step 2: Fetch image results (Large size filter)
        img_url = (
            f"https://duckduckgo.com/i.js"
            f"?l=us-en&o=json&q={encoded_q}"
            f"&vqd={vqd}&f=size:Large&p=1"
        )
        resp2 = _make_request(img_url, {
            "Referer": "https://duckduckgo.com/",
            "Accept": "application/json",
        })
        data = json.loads(resp2.read())
        results = data.get("results", [])

        # Filter for decent resolution
        good = []
        for r in results:
            w = r.get("width", 0)
            h = r.get("height", 0)
            src = r.get("image", "")
            if w >= MIN_WIDTH and src and not src.endswith(".svg"):
                good.append({
                    "url": src,
                    "width": w,
                    "height": h,
                    "title": r.get("title", ""),
                })
        return good

    except Exception as e:
        print(f"  Warning: search failed for '{query[:40]}': {e}")
        return []


def download_image(url, dest_path):
    """Download a single image. Returns True on success."""
    try:
        resp = _make_request(url)
        data = resp.read()
        if len(data) < MIN_FILE_SIZE:
            return False
        # Quick content-type sanity check
        ct = resp.headers.get("Content-Type", "")
        if ct and "text/html" in ct:
            return False
        dest_path.write_bytes(data)
        return True
    except Exception:
        return False


def cmd_download():
    """Download ~1000 Escher-style images."""
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)

    existing = set(p.name for p in WALLPAPER_DIR.glob("*.*")
                   if p.suffix in (".jpg", ".jpeg", ".png", ".webp"))
    print(f"Wallpaper directory: {WALLPAPER_DIR}")
    print(f"Existing images: {len(existing)}")
    print(f"Search queries: {len(SEARCH_QUERIES)}")
    print()

    # Phase 1: Collect all unique image URLs
    all_images = {}  # url_hash -> {url, title, width, height}
    for i, query in enumerate(SEARCH_QUERIES, 1):
        results = search_images(query)
        new = 0
        for img in results:
            url_hash = hashlib.md5(img["url"].encode()).hexdigest()[:16]
            if url_hash not in all_images:
                all_images[url_hash] = img
                new += 1
        if i % 25 == 0 or i == len(SEARCH_QUERIES):
            print(f"  [{i}/{len(SEARCH_QUERIES)}] unique so far: {len(all_images)}")
        time.sleep(0.5)  # Rate limit

    print(f"\nTotal unique images found: {len(all_images)}")

    # Filter already downloaded
    to_download = {
        h: img for h, img in all_images.items()
        if not any((WALLPAPER_DIR / f"{h}{ext}").exists()
                    for ext in (".jpg", ".jpeg", ".png", ".webp"))
    }
    print(f"New to download: {len(to_download)}")

    if not to_download:
        print("Nothing new to download!")
        return

    # Phase 2: Download in parallel
    downloaded = 0
    failed = 0

    def do_download(item):
        url_hash, img = item
        url = img["url"]
        # Determine extension from URL
        ext = ".jpg"
        for e in (".png", ".webp", ".jpeg"):
            if e in url.lower():
                ext = e
                break
        dest = WALLPAPER_DIR / f"{url_hash}{ext}"
        ok = download_image(url, dest)
        if not ok:
            dest.unlink(missing_ok=True)
        return url_hash, ok

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(do_download, item)
                   for item in to_download.items()]
        for i, future in enumerate(as_completed(futures), 1):
            _, ok = future.result()
            if ok:
                downloaded += 1
            else:
                failed += 1
            if i % 50 == 0 or i == len(futures):
                print(f"  Progress: {i}/{len(futures)} "
                      f"(ok: {downloaded}, fail: {failed})")

    total = sum(1 for p in WALLPAPER_DIR.iterdir()
                if p.suffix in (".jpg", ".jpeg", ".png", ".webp"))
    print(f"\nDone! Downloaded {downloaded} new images ({failed} failed)")
    print(f"Total collection: {total} images")

    if total < 500:
        print("\nTip: Run 'download' again to retry failed downloads "
              "and potentially find more images.")


def cmd_set():
    """Set a random wallpaper from the collection."""
    images = [p for p in WALLPAPER_DIR.iterdir()
              if p.suffix in (".jpg", ".jpeg", ".png", ".webp")]
    if not images:
        print("No images found! Run 'download' first.", file=sys.stderr)
        sys.exit(1)

    # Avoid recent repeats
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = []

    recent = set(history[-200:]) if len(images) > 200 else set()
    candidates = [img for img in images if img.name not in recent]
    if not candidates:
        candidates = images

    chosen = random.choice(candidates)
    uri = f"file://{chosen}"

    # Set both light and dark wallpaper + zoom mode
    for key in ("picture-uri", "picture-uri-dark"):
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.background", key, uri],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["gsettings", "set", "org.gnome.desktop.background",
         "picture-options", "zoom"],
        check=True, capture_output=True,
    )

    # Save history
    history.append(chosen.name)
    if len(history) > 500:
        history = history[-500:]
    HISTORY_FILE.write_text(json.dumps(history))

    print(f"Wallpaper set: {chosen.name}")


def cmd_install():
    """Install a systemd user timer for hourly rotation."""
    script_path = Path(__file__).resolve()
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    (service_dir / "escher-wallpaper.service").write_text(
        f"""[Unit]
Description=Set random Escher wallpaper

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script_path} set
""")

    (service_dir / "escher-wallpaper.timer").write_text(
        """[Unit]
Description=Rotate Escher wallpaper every hour

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
""")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now",
                     "escher-wallpaper.timer"], check=True)

    print("Installed escher-wallpaper.timer (hourly rotation)")
    print()
    print("Commands:")
    print("  systemctl --user status escher-wallpaper.timer")
    print("  journalctl --user -u escher-wallpaper.service")
    print(f"  python3 {script_path} set   # change now")


def cmd_uninstall():
    """Remove the systemd timer."""
    subprocess.run(["systemctl", "--user", "disable", "--now",
                     "escher-wallpaper.timer"], capture_output=True)
    service_dir = Path.home() / ".config" / "systemd" / "user"
    for f in ("escher-wallpaper.service", "escher-wallpaper.timer"):
        (service_dir / f).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                    capture_output=True)
    print("Uninstalled escher-wallpaper timer.")


def cmd_cycle():
    """Force-cycle wallpaper now and reset the hourly timer."""
    cmd_set()
    # Reset timer so the next hour starts from now
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "escher-wallpaper.timer"],
        capture_output=True, text=True,
    )
    if result.stdout.strip() == "active":
        subprocess.run(["systemctl", "--user", "restart",
                         "escher-wallpaper.timer"], capture_output=True)
        print("Timer reset (next change in ~1 hour)")


def cmd_status():
    """Show collection stats."""
    images = [p for p in WALLPAPER_DIR.iterdir()
              if p.suffix in (".jpg", ".jpeg", ".png", ".webp")
              ] if WALLPAPER_DIR.exists() else []
    if not images:
        print("No images downloaded yet. Run 'download' first.")
        return

    total_size = sum(img.stat().st_size for img in images)
    print(f"Collection: {len(images)} images")
    print(f"Total size: {total_size / (1024*1024):.1f} MB")
    print(f"Location:   {WALLPAPER_DIR}")

    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.background",
             "picture-uri"],
            capture_output=True, text=True,
        )
        print(f"Current:    {result.stdout.strip()}")
    except Exception:
        pass

    result = subprocess.run(
        ["systemctl", "--user", "is-active", "escher-wallpaper.timer"],
        capture_output=True, text=True,
    )
    print(f"Timer:      {result.stdout.strip()}")


def main():
    commands = {
        "download": cmd_download,
        "set": cmd_set,
        "cycle": cmd_cycle,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 1)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()

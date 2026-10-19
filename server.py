#!/usr/bin/env python3
"""Minimal TRMNL BYOS server: shows a random Pokémon or Yu-Gi-Oh card on every refresh.

Run:   python3 server.py
Then set the device's custom server to http://<this-computer's-LAN-IP>:2300 (no trailing slash).

Settings (environment variables):
  PORT             port to listen on (default 2300)
  CARD_SOURCES     comma list of pokemon, yugioh, local (default "pokemon")
  REFRESH_SECONDS  how often the shared card changes for everyone (default 300 = 5 minutes)
  QUEUE_SIZE       how many upcoming cards to keep downloaded and ready (default 5)
  Card price and image settings (POKEMON_MIN_PRICE, DARKNESS, BACKGROUND, ...) are described in cards.py.
"""

import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cards import POKEMON_MIN_PRICE, encode_png, fetch_priced_pokemon, http_get, http_json, render_card, render_text

PORT = int(os.environ.get("PORT", "2300"))
SOURCES = [s.strip() for s in os.environ.get("CARD_SOURCES", "pokemon").split(",") if s.strip()]
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
QUEUE_SIZE = int(os.environ.get("QUEUE_SIZE", "5"))
DEFAULT_SIZE = (800, 480)

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"    # original card downloads, reused as an offline fallback
FRAMES_DIR = ROOT / "frames"  # converted images the device downloads
LOCAL_DIR = ROOT / "cards"    # drop your own images here and add "local" to CARD_SOURCES
for d in (CACHE_DIR, FRAMES_DIR, LOCAL_DIR):
    d.mkdir(exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


LOG_FILE = ROOT / "device.log"


def log(line):
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


# ---------- fetching cards ----------

def random_pokemon():
    if POKEMON_MIN_PRICE > 0:
        return get_pokemon_pool().pick()
    # TCGdex (free, no key). Some cards have no scan yet, so retry a few times.
    for _ in range(5):
        card = http_json("https://api.tcgdex.net/v2/en/random/card")
        if card.get("image"):
            return card["id"], card["name"], card["image"] + "/high.png"
    raise RuntimeError("TCGdex returned cards without images")


class PokemonPool:
    """Every priced Pokémon card, saved to disk and rebuilt weekly. Filtered by POKEMON_MIN_PRICE when picking."""

    FILE = ROOT / "pokemon_pool.json"
    MAX_AGE = 7 * 24 * 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.cards = []
        self.recent = []
        if self.FILE.exists():
            self.cards = json.loads(self.FILE.read_text())
            print(f"[pool] loaded {len(self.cards)} priced cards from {self.FILE.name}")
        if not self.cards or time.time() - self.FILE.stat().st_mtime > self.MAX_AGE:
            threading.Thread(target=self.rebuild, daemon=True).start()

    def rebuild(self):
        print("[pool] building list of priced Pokémon cards (takes a few minutes)...")

        def use_partial(cards):
            if len(cards) > len(self.cards):  # first build: start using results right away
                with self.lock:
                    self.cards = cards

        fresh = fetch_priced_pokemon(on_progress=use_partial)
        with self.lock:
            self.cards = fresh
        self.FILE.write_text(json.dumps(fresh))
        eligible = sum(c["value"] >= POKEMON_MIN_PRICE for c in fresh)
        print(f"[pool] done: {len(fresh)} priced cards, {eligible} worth ${POKEMON_MIN_PRICE:g}+")

    def pick(self):
        with self.lock:
            eligible = [c for c in self.cards if c["value"] >= POKEMON_MIN_PRICE]
        if not eligible:
            raise RuntimeError(f"no cards worth ${POKEMON_MIN_PRICE:g}+ yet (card list still building?)")
        fresh = [c for c in eligible if c["id"] not in self.recent] or eligible
        card = random.choice(fresh)
        self.recent = (self.recent + [card["id"]])[-max(1, min(100, len(eligible) // 2)):]
        return card["id"], f"{card['name']} ({card['set']}, ${card['value']:g})", card["image"] + "/high.png"


def random_yugioh():
    data = http_json("https://db.ygoprodeck.com/api/v7/randomcard.php")
    # The endpoint has returned both a bare card and {"data": [card]} over time.
    card = data["data"][0] if isinstance(data, dict) and "data" in data else data
    return str(card["id"]), card["name"], card["card_images"][0]["image_url"]


def random_local():
    files = [p for p in LOCAL_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    if not files:
        raise RuntimeError(f"no images in {LOCAL_DIR}")
    path = random.choice(files)
    return path, path.stem


_pokemon_pool = None


def get_pokemon_pool():
    global _pokemon_pool
    if _pokemon_pool is None:
        _pokemon_pool = PokemonPool()
    return _pokemon_pool


FETCHERS = {"pokemon": random_pokemon, "yugioh": random_yugioh}


def get_card():
    """Return (path to an image file, label). Falls back to previously downloaded cards."""
    for source in random.sample(SOURCES, len(SOURCES)):
        try:
            if source == "local":
                return random_local()
            card_id, name, url = FETCHERS[source]()
            ext = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
            path = CACHE_DIR / f"{source}-{re.sub(r'[^A-Za-z0-9_-]', '_', card_id)}{ext}"
            # YGOProDeck asks that images be downloaded once rather than hotlinked.
            if not path.exists():
                path.write_bytes(http_get(url))
            return path, f"{source}: {name}"
        except Exception as e:
            print(f"[warn] {source} failed: {e}")
    cached = [p for p in CACHE_DIR.iterdir()
              if p.suffix.lower() in IMAGE_EXTS and p.name.split("-", 1)[0] in SOURCES]
    if cached:
        path = random.choice(cached)
        return path, f"cached: {path.stem}"
    raise RuntimeError("every card source failed and nothing is cached yet")


# ---------- serving frames ----------

def save_frame(img):
    filename = f"{uuid.uuid4().hex[:12]}.png"
    (FRAMES_DIR / filename).write_bytes(encode_png(img))
    # Anything older than a few card changes can't be the current card any more.
    cutoff = time.time() - max(3 * REFRESH_SECONDS, 3600)
    for p in FRAMES_DIR.glob("*.png"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
    return filename


class SharedCard:
    """One card for every device, changing every REFRESH_SECONDS on the clock.

    Cards are downloaded ahead of time into a queue saved on disk, so a change never waits on the card
    APIs and the current card survives restarts. Button presses don't advance the card: every request in
    the same time slot gets the same image, however many devices are connected.
    """

    STATE_FILE = ROOT / "queue.json"
    # Device sleep timers drift, so a wake up to this many seconds early still gets the next card.
    EARLY_GRACE = 15

    def __init__(self, queue_size):
        self.queue_size = queue_size
        self.lock = threading.Lock()
        self.need_cards = threading.Event()
        self.slot, self.card, self.queue = None, None, []
        self.frames = {}  # (width, height) -> rendered frame of the current card
        if self.STATE_FILE.exists():
            state = json.loads(self.STATE_FILE.read_text())
            usable = lambda c: bool(c) and Path(c["path"]).exists()
            self.queue = [c for c in state.get("queue", []) if usable(c)]
            if usable(state.get("card")):
                self.slot, self.card = state.get("slot"), state["card"]
        threading.Thread(target=self._fill, daemon=True).start()

    def current(self, size):
        """Return (frame filename, unix time the card next changes)."""
        slot = int((time.time() + self.EARLY_GRACE) // REFRESH_SECONDS)
        with self.lock:
            if slot != self.slot or self.card is None:
                self._advance(slot)
            if size not in self.frames:
                self.frames[size] = self._render(size)
            return self.frames[size], (slot + 1) * REFRESH_SECONDS

    def _advance(self, slot):
        if self.queue:
            self.card = self.queue.pop(0)
        else:  # queue ran dry (e.g. card APIs down): try fetching directly
            try:
                path, label = get_card()
                self.card = {"path": str(path), "label": label}
            except Exception as e:
                log(f"[queue] no card available: {e}")
                self.card = None
        self.slot, self.frames = slot, {}
        self._save()
        self.need_cards.set()
        if self.card:
            log(f"[card] now showing {self.card['label']} ({len(self.queue)} queued)")

    def _render(self, size):
        try:
            if self.card is None:
                raise RuntimeError("no card available")
            img = render_card(self.card["path"], size)
        except Exception as e:
            img = render_text(f"Couldn't load a card\n{e}", size)
        filename = save_frame(img)
        log(f"[frame] {filename} {size[0]}x{size[1]} ({(FRAMES_DIR / filename).stat().st_size // 1024} KB)")
        return filename

    def _fill(self):
        while True:
            while len(self.queue) < self.queue_size:
                try:
                    path, label = get_card()
                except Exception as e:
                    log(f"[queue] couldn't fetch a card, retrying in a minute: {e}")
                    break
                with self.lock:
                    self.queue.append({"path": str(path), "label": label})
                    self._save()
            self.need_cards.clear()
            self.need_cards.wait(timeout=60)

    def _save(self):  # caller holds self.lock
        self.STATE_FILE.write_text(json.dumps({"slot": self.slot, "card": self.card, "queue": self.queue}))


shared_card = None


# ---------- TRMNL BYOS API ----------

class Handler(BaseHTTPRequestHandler):
    def base_url(self):
        return f"http://{self.headers.get('Host', f'localhost:{PORT}')}"

    def screen_size(self):
        try:
            return int(self.headers["Width"]), int(self.headers["Height"])
        except (KeyError, TypeError, ValueError):
            return DEFAULT_SIZE

    def send_json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/setup":
            log(f"[setup] device {self.headers.get('ID')} model={self.headers.get('Model')} "
                  f"fw={self.headers.get('FW-Version')}")
            filename = save_frame(render_text("Connected to card server!", self.screen_size()))
            self.send_json({
                "status": 200,
                "api_key": "local-cards",
                "friendly_id": "CARDS",
                "image_url": f"{self.base_url()}/frames/{filename}",
                "message": "Registered",
            })
        elif path == "/api/display":
            size = self.screen_size()
            filename, changes_at = shared_card.current(size)
            # Wake just after the next change. The random spread keeps many devices from all
            # downloading at the same second.
            refresh = max(5, math.ceil(changes_at - time.time()) + random.randint(0, min(30, REFRESH_SECONDS // 10)))
            log(f"[display] device {self.headers.get('ID')} woke by {self.headers.get('Update-Source')} "
                f"{size[0]}x{size[1]} fw={self.headers.get('FW-Version')} "
                f"battery={self.headers.get('Battery-Voltage')}V rssi={self.headers.get('RSSI')} next in {refresh}s")
            self.send_json({
                "status": 0,
                "image_url": f"{self.base_url()}/frames/{filename}",
                "filename": filename,
                "refresh_rate": refresh,
                "reset_firmware": False,
                "update_firmware": False,
                "firmware_url": None,
            })
        elif path.startswith("/frames/"):
            file = FRAMES_DIR / Path(path).name
            if not file.is_file():
                self.send_error(404)
                return
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/log":
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            log(f"[device log] {body.decode(errors='replace')}")
            self.send_json({"status": 200})
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # the handlers above print the useful parts


if __name__ == "__main__":
    print(f"Card server on port {PORT}: sources={SOURCES}, refresh={REFRESH_SECONDS}s, "
          f"pokemon min price=${POKEMON_MIN_PRICE:g}")
    if "pokemon" in SOURCES and POKEMON_MIN_PRICE > 0:
        get_pokemon_pool()  # start building the card list now rather than on the first request
    shared_card = SharedCard(QUEUE_SIZE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

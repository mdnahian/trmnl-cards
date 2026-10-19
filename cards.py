"""Card fetching, pricing and e-ink rendering shared by server.py (local) and build.py (Cloudflare).

Settings (environment variables):
  POKEMON_MIN_PRICE  only show Pokémon cards worth at least this many USD (default 50; 0 = any card)
  ROTATE           degrees to turn a portrait card on a landscape screen: 90, 270 or 0 (default 90)
  DARKNESS         darken midtones: 1.0 = unchanged, higher = darker (default 1.3, or 1.1 with GRAYS=4)
  BACKGROUND       color of the space around the card: black (default), white, or 0-255 gray level
  GRAYS            2 = black and white (default), 4 = four shades of gray (didn't display on the X4 in testing)
"""

import hashlib
import io
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

POKEMON_MIN_PRICE = float(os.environ.get("POKEMON_MIN_PRICE", "50"))
ROTATE = int(os.environ.get("ROTATE", "90"))
GRAYS = int(os.environ.get("GRAYS", "2"))
_background = os.environ.get("BACKGROUND", "black").lower()
BACKGROUND = {"black": 0, "white": 255}[_background] if _background in ("black", "white") else int(_background)
# Pure black and white dithers into light-looking dots, so it needs more darkening than 4 grays.
DARKNESS = float(os.environ.get("DARKNESS", "1.1" if GRAYS > 2 else "1.3"))

TCGDEX = "https://api.tcgdex.net/v2/en"
# Commons/uncommons are never valuable; the star/diamond/crown/shiny rarities are digital-only TCG Pocket cards.
SKIP_RARITIES = {"Common", "Uncommon", "None", "One Diamond", "Two Diamond", "Three Diamond", "Four Diamond",
                 "One Star", "Two Star", "Three Star", "Crown", "One Shiny", "Two Shiny"}


# ---------- fetching ----------

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "trmnl-cards/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url):
    return json.loads(http_get(url))


def card_value_usd(card):
    """Highest TCGplayer market price across printings (1st edition, holo, ...), else Cardmarket's 30-day average."""
    pricing = card.get("pricing") or {}
    tcgplayer = pricing.get("tcgplayer") or {}
    prices = [v.get("marketPrice") for v in tcgplayer.values() if isinstance(v, dict)]
    prices = [p for p in prices if p]
    if prices:
        return max(prices)
    # Cardmarket is in EUR; close enough to USD for a "is this card valuable" cutoff.
    return (pricing.get("cardmarket") or {}).get("avg30") or 0


def fetch_priced_pokemon(log=print, on_progress=None):
    """Every Pokémon card TCGdex has a price for, as [{id, name, rarity, set, value, image}].

    Takes a few minutes (~9,000 card lookups). on_progress(cards_so_far) is called every 1,000 lookups.
    """
    ids = set()
    for rarity in http_json(f"{TCGDEX}/rarities"):
        if rarity not in SKIP_RARITIES:
            cards = http_json(f"{TCGDEX}/cards?rarity=" + urllib.parse.quote(f"eq:{rarity}"))
            ids.update(c["id"] for c in cards if c.get("image"))

    def fetch(card_id):
        try:
            card = http_json(f"{TCGDEX}/cards/{urllib.parse.quote(card_id)}")
        except Exception:
            return None
        value = card_value_usd(card)
        if not value or not card.get("image"):
            return None
        return {"id": card["id"], "name": card["name"], "rarity": card.get("rarity"),
                "set": (card.get("set") or {}).get("name"), "value": round(value, 2), "image": card["image"]}

    priced = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, card in enumerate(pool.map(fetch, sorted(ids)), 1):
            if card:
                priced.append(card)
            if i % 1000 == 0:
                log(f"[pool] checked {i}/{len(ids)} cards")
                if on_progress:
                    on_progress(list(priced))
    return priced


# ---------- converting for e-ink ----------

def settings_fingerprint():
    """Changes whenever a setting that affects how cards look changes, so cards get re-rendered."""
    settings = f"rotate={ROTATE};grays={GRAYS};background={BACKGROUND};darkness={DARKNESS};v1"
    return hashlib.sha1(settings.encode()).hexdigest()[:10]


def to_eink(img):
    if GRAYS <= 2:
        return img.convert("1")  # Pillow dithers with Floyd–Steinberg by default
    levels = [round(i * 255 / (GRAYS - 1)) for i in range(GRAYS)]
    palette = Image.new("P", (1, 1))
    palette.putpalette([v for level in levels for v in (level, level, level)])
    return img.convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)


def darken(img):
    """Card scans are bright and wash out once dithered to black and white; stretch contrast and darken midtones."""
    img = ImageOps.autocontrast(img, cutoff=1)
    if DARKNESS != 1:
        lut = [round(255 * (v / 255) ** DARKNESS) for v in range(256)]
        img = img.point(lut)
    return img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=2))


def render_card(source, size):
    """Render a card scan (file path or PIL image) centered on a screen of the given (width, height)."""
    width, height = size
    card = source if isinstance(source, Image.Image) else Image.open(source)
    card = ImageOps.exif_transpose(card).convert("RGBA")
    # Fill transparent rounded corners with the background color so they blend into the bars.
    card = Image.alpha_composite(Image.new("RGBA", card.size, (BACKGROUND,) * 3), card).convert("L")
    if width > height and card.height > card.width and ROTATE:
        card = card.rotate(ROTATE, expand=True)
    card = ImageOps.contain(card, size, Image.LANCZOS)
    card = darken(card)
    canvas = Image.new("L", size, BACKGROUND)
    canvas.paste(card, ((width - card.width) // 2, (height - card.height) // 2))
    return to_eink(canvas)


def render_text(text, size):
    canvas = Image.new("L", size, 255)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    draw.multiline_text((size[0] // 2, size[1] // 2), text, fill=0, font=font, anchor="mm", align="center")
    return to_eink(canvas)


def encode_png(img):
    """PNG bytes in the bit depth the firmware expects (1-bit, or 2-bit palette for 4 grays)."""
    buf = io.BytesIO()
    options = {"bits": {4: 2, 16: 4}[GRAYS]} if GRAYS in (4, 16) else {}
    img.save(buf, "PNG", optimize=True, **options)
    return buf.getvalue()

#!/usr/bin/env python3
"""Render $50+ Pokémon cards for e-ink and upload them to Cloudflare Workers KV for the Worker in worker/.

Run daily (the GitHub Action does). Each run:
  1. Refreshes the TCGdex price list if it's more than a week old (stored in KV as "pool").
  2. Downloads and renders only cards that haven't been rendered yet (or were rendered with different
     image settings), for every screen size, and uploads them as "frame:<card id>:<W>x<H>".
  3. Updates "manifest", the list of cards the Worker rotates through.
KV's free plan allows 1,000 writes a day, so a run stops after MAX_KV_WRITES and the next run continues.

Environment:
  CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, KV_NAMESPACE_ID   where to upload (not needed with --local-out)
  MAX_KV_WRITES      writes allowed per run (default 950)
  POKEMON_MIN_PRICE, DARKNESS, BACKGROUND, ...                    see cards.py

Usage:
  python3 build.py                                  # update Cloudflare KV
  python3 build.py --local-out build-out --limit 20 # test without Cloudflare
"""

import argparse
import base64
import io
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from cards import POKEMON_MIN_PRICE, encode_png, fetch_priced_pokemon, http_get, render_card, settings_fingerprint

SIZES = [(800, 480), (792, 528)]  # Xteink X4, Xteink X3
POOL_MAX_AGE = 7 * 24 * 3600
MAX_KV_WRITES = int(os.environ.get("MAX_KV_WRITES", "950"))
UPLOAD_BATCH = 100  # frames per bulk upload request
CHECKPOINT_EVERY = 200  # cards between manifest saves, so a crash doesn't waste the day's writes


class CloudflareKV:
    def __init__(self):
        account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        namespace = os.environ["KV_NAMESPACE_ID"]
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces/{namespace}"
        self.token = os.environ["CLOUDFLARE_API_TOKEN"]
        self.writes = 0

    def _request(self, method, path, body=None, content_type="application/json"):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    def get_json(self, key):
        try:
            return json.loads(self._request("GET", "/values/" + urllib.parse.quote(key, safe="")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def put_json(self, key, value):
        self.put_many({key: json.dumps(value).encode()})

    def put_many(self, items):
        """items: {key: bytes}. One request, but every key counts as a write."""
        body = [{"key": k, "value": base64.b64encode(v).decode(), "base64": True} for k, v in items.items()]
        response = json.loads(self._request("PUT", "/bulk", json.dumps(body).encode()))
        self.writes += len(items)
        failed = (response.get("result") or {}).get("unsuccessful_keys") or []
        if not response.get("success") or failed:
            raise RuntimeError(f"KV bulk write failed: {response.get('errors')} {failed[:5]}")


class LocalKV:
    """Writes to a folder instead of Cloudflare, plus kv-bulk.json for `wrangler kv bulk put --local`."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.bulk_file = self.folder / "kv-bulk.json"
        self.writes = 0

    def _file(self, key):
        return self.folder / (key.replace(":", "_") + (".png" if key.startswith("frame:") else ".json"))

    def get_json(self, key):
        f = self._file(key)
        return json.loads(f.read_text()) if f.exists() else None

    def put_json(self, key, value):
        self.put_many({key: json.dumps(value).encode()})

    def put_many(self, items):
        for key, data in items.items():
            self._file(key).write_bytes(data)
        self.writes += len(items)
        self._add_to_bulk(items)

    def _add_to_bulk(self, items):
        bulk = {e["key"]: e for e in json.loads(self.bulk_file.read_text())} if self.bulk_file.exists() else {}
        for key, data in items.items():
            bulk[key] = {"key": key, "value": base64.b64encode(data).decode(), "base64": True}
        self.bulk_file.write_text(json.dumps(list(bulk.values())))


def load_pool(kv, force_refresh):
    pool = kv.get_json("pool")
    if pool and not force_refresh and time.time() - pool["updated"] < POOL_MAX_AGE:
        print(f"Prices are {(time.time() - pool['updated']) / 86400:.1f} days old; not refreshing.")
        return pool, False
    print("Refreshing prices from TCGdex (takes a few minutes)...")
    pool = {"updated": time.time(), "cards": fetch_priced_pokemon()}
    kv.put_json("pool", pool)
    print(f"Found {len(pool['cards'])} priced cards.")
    return pool, True


def render_frames(card):
    """Download one card scan and render it for every screen size: {kv key: png bytes}."""
    scan = Image.open(io.BytesIO(http_get(card["image"] + "/high.webp", timeout=60)))
    return {f"frame:{card['id']}:{w}x{h}": encode_png(render_card(scan, (w, h))) for w, h in SIZES}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-out", help="write to this folder instead of Cloudflare KV")
    parser.add_argument("--limit", type=int, help="only consider the first N eligible cards (for testing)")
    parser.add_argument("--refresh-prices", action="store_true", help="refresh prices even if less than a week old")
    args = parser.parse_args()

    kv = LocalKV(args.local_out) if args.local_out else CloudflareKV()
    fingerprint = settings_fingerprint()
    pool, prices_refreshed = load_pool(kv, args.refresh_prices)
    manifest = kv.get_json("manifest") or {"rendered": {}, "active": []}
    rendered = manifest["rendered"]

    # Most valuable first, so a partly filled rotation (the first few days) shows the best cards.
    eligible = sorted((c for c in pool["cards"] if c["value"] >= POKEMON_MIN_PRICE), key=lambda c: -c["value"])
    if args.limit:
        eligible = eligible[:args.limit]
    eligible_ids = {c["id"] for c in eligible}
    todo = [c for c in eligible if rendered.get(c["id"], {}).get("settings") != fingerprint]
    print(f"{len(eligible)} cards worth ${POKEMON_MIN_PRICE:g}+; {len(todo)} need rendering "
          f"(settings {fingerprint}).")

    reshuffle = prices_refreshed

    def save_manifest():
        # The Worker walks "active" in order, so every card shows once before any repeats. Keep the order
        # between runs (reshuffled once per price refresh) and insert new cards at random positions.
        nonlocal reshuffle
        active = [i for i in manifest["active"] if i in eligible_ids and i in rendered]
        if reshuffle:
            random.shuffle(active)
            reshuffle = False
        for card_id in sorted(eligible_ids & rendered.keys() - set(active)):
            active.insert(random.randint(0, len(active)), card_id)
        manifest.update(active=active, sizes=[list(s) for s in SIZES], updated=time.time())
        kv.put_json("manifest", manifest)
        print(f"Saved manifest: {len(active)} cards in rotation.")

    budget = MAX_KV_WRITES - kv.writes - 1 - (len(todo) // CHECKPOINT_EVERY)  # keep writes for manifest saves
    pending, pending_cards, done, failed = {}, [], 0, 0

    def flush():
        if pending:
            kv.put_many(pending)
            for card in pending_cards:
                rendered[card["id"]] = {"settings": fingerprint, "name": card["name"],
                                        "set": card["set"], "value": card["value"]}
            pending.clear()
            pending_cards.clear()

    for card in todo:
        if done * len(SIZES) + len(SIZES) > budget:
            print(f"Reached this run's write budget; {len(todo) - done - failed} cards left for the next run.")
            break
        try:
            pending.update(render_frames(card))
            pending_cards.append(card)
            done += 1
        except Exception as e:
            failed += 1
            print(f"  skipped {card['id']} ({card['name']}): {e}")
            continue
        if len(pending) >= UPLOAD_BATCH:
            flush()
            print(f"  rendered {done}/{len(todo)}")
        if done % CHECKPOINT_EVERY == 0:
            flush()
            save_manifest()
    flush()
    save_manifest()
    print(f"Done: {done} cards rendered, {failed} failed, {kv.writes} KV writes this run.")


if __name__ == "__main__":
    main()

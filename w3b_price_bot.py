#!/usr/bin/env python3
"""
TornW3B (weav3r.dev) trader pricelist bot.

Adjusts your buy prices on your TornW3B pricelist based on what other traders
are paying. Talks ONLY to the TornW3B API and the read-only Torn API. It never
touches Torn's bazaar, item market or trades.

Usage:
    python3 w3b_price_bot.py --probe        # print raw API responses, change nothing
    python3 w3b_price_bot.py                # dry run (default): show what would change
    python3 w3b_price_bot.py --live         # actually update the pricelist
    python3 w3b_price_bot.py --live --loop  # run forever, every INTERVAL_SECONDS

Requires: python3, requests  (pip install requests)
Env vars: TORN_API_KEY (required), TORN_USER_ID (required)
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

# =============================================================================
# CONFIG: items and pricing
# =============================================================================

# Defaults used for any item that doesn't override them.
DEFAULTS = {
    "mode": "match_top",       # match_top | always_top | second | follow
    "step": 1,                 # always_top: add this; second: subtract this
    "max_price": None,         # hard cap in $; None = no hard cap
    "max_pct_of_market": 100,  # cap as % of Torn market value; None = no cap
    "follow_trader": None,     # follow mode: Torn user ID of trader to follow
    "follow_offset": 0,        # follow mode: added to that trader's price
    "min_price": 1,            # never go below this
}

# Torn item ID -> per-item overrides (any DEFAULTS key). {} means use defaults.
ITEMS = {
    # 206: {},                                           # Xanax, all defaults
    # 197: {"mode": "always_top", "step": 5, "max_price": 900_000},
    # 180: {"mode": "second", "step": 1},
    # 258: {"mode": "follow", "follow_trader": 123456, "follow_offset": -1},
}

INTERVAL_SECONDS = 600          # --loop cadence
STATE_DIR = os.path.expanduser("~/.w3b_price_bot")
BACKUP_FILE = os.path.join(STATE_DIR, "pricelist_backup.json")
LOG_FILE = os.path.join(STATE_DIR, "bot.log")

# =============================================================================
# CONFIG: API shape.  EVERYTHING UNVERIFIED LIVES HERE.
# Run --probe, paste the output back, and only this block should need editing.
# =============================================================================

W3B_BASE = "https://weav3r.dev/api"
TORN_BASE = "https://api.torn.com"

# How the key is sent to W3B. Options: "query" (?key=), "header" (name below),
# "bearer" (Authorization: Bearer). GUESS: query.
W3B_AUTH_STYLE = "query"
W3B_AUTH_QUERY_NAME = "key"
W3B_AUTH_HEADER_NAME = "X-API-Key"

# Endpoints. {uid}/{item} are filled in.
EP_PRICELIST_GET = "/pricelist/{uid}"          # confirmed exists (PUT); GET is a guess
EP_PRICELIST_PUT = "/pricelist/{uid}"          # confirmed from docs
EP_TRADERS = "/marketplace/{item}/traders"     # from third-party README

# Rate limit: W3B ~100 req/min per README. Stay well under.
W3B_MIN_SECONDS_BETWEEN_REQUESTS = 0.8

# Field-name candidates, tried in order. Fix after --probe.
F_TRADER_ID = ("userID", "user_id", "userId", "traderID", "trader_id", "id")
F_PRICE = ("price", "buyPrice", "buy_price", "value")
F_ITEM_ID = ("itemID", "item_id", "itemId", "id")
F_SPONSORED = ("sponsored", "isSponsored", "is_sponsored", "featured")
# Key under which the pricelist JSON holds its item list, if it's an object.
# None means the response is itself the list. Tried in order.
F_PRICELIST_ITEMS = (None, "items", "pricelist", "prices", "data")

# =============================================================================
# Plumbing
# =============================================================================

log = logging.getLogger("w3b")


class BotError(Exception):
    pass


class RateLimiter:
    def __init__(self, min_gap):
        self.min_gap = min_gap
        self.last = 0.0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last = time.monotonic()


class W3B:
    def __init__(self, key, uid):
        self.key = key
        self.uid = uid
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "w3b-price-bot/1.0"
        self.rl = RateLimiter(W3B_MIN_SECONDS_BETWEEN_REQUESTS)

    def _req(self, method, path, **kw):
        url = W3B_BASE + path
        params = kw.pop("params", {}) or {}
        headers = kw.pop("headers", {}) or {}
        if W3B_AUTH_STYLE == "query":
            params[W3B_AUTH_QUERY_NAME] = self.key
        elif W3B_AUTH_STYLE == "header":
            headers[W3B_AUTH_HEADER_NAME] = self.key
        elif W3B_AUTH_STYLE == "bearer":
            headers["Authorization"] = "Bearer " + self.key
        self.rl.wait()
        for attempt in range(3):
            r = self.s.request(method, url, params=params, headers=headers, timeout=30, **kw)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 15))
                log.warning("429 from W3B, sleeping %ss", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            raise BotError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise BotError(f"{method} {path} -> non-JSON: {r.text[:300]}")

    def get_pricelist(self):
        return self._req("GET", EP_PRICELIST_GET.format(uid=self.uid))

    def put_pricelist(self, body):
        return self._req("PUT", EP_PRICELIST_PUT.format(uid=self.uid), json=body)

    def get_traders(self, item_id):
        return self._req("GET", EP_TRADERS.format(item=item_id))


class Torn:
    def __init__(self, key):
        self.key = key
        self.s = requests.Session()

    def market_value(self, item_id):
        r = self.s.get(f"{TORN_BASE}/torn/{item_id}",
                       params={"selections": "items", "key": self.key}, timeout=30)
        d = r.json()
        if "error" in d:
            raise BotError(f"Torn API error: {d['error']}")
        return int(d["items"][str(item_id)]["market_value"])


def pick(d, names, default=None):
    """First present key from names in dict d."""
    for n in names:
        if n in d:
            return d[n]
    return default


# =============================================================================
# Pricelist shape helpers
# =============================================================================

def pricelist_items(pl):
    """Return (container_key, list_of_item_dicts) from whatever GET returned."""
    for k in F_PRICELIST_ITEMS:
        v = pl if k is None else (pl.get(k) if isinstance(pl, dict) else None)
        if isinstance(v, list):
            return k, v
        if isinstance(v, dict) and v and all(str(x).isdigit() for x in v):
            # dict keyed by item id: {"206": {...}} -> convert to list view
            return k, [dict(x, __key=str(i)) for i, x in v.items()]
    raise BotError(f"Can't find item list in pricelist JSON. Top-level keys: "
                   f"{list(pl)[:15] if isinstance(pl, dict) else type(pl)}. Run --probe.")


def find_entry(items, item_id):
    for e in items:
        if str(pick(e, F_ITEM_ID)) == str(item_id) or e.get("__key") == str(item_id):
            return e
    return None


def entry_price(e):
    p = pick(e, F_PRICE)
    if p is None:
        raise BotError(f"No price field in entry {e}. Run --probe.")
    return int(p)


def set_entry_price(e, price):
    for n in F_PRICE:
        if n in e:
            e[n] = price
            return
    raise BotError("No price field to set")


def rebuild_pricelist(original, container_key, items):
    """Put edited items back into a full-pricelist body matching original shape."""
    body = copy.deepcopy(original)
    clean = [{k: v for k, v in e.items() if k != "__key"} for e in items]
    if container_key is None:
        return clean
    src = original[container_key]
    if isinstance(src, dict):
        body[container_key] = {e.get("__key", str(pick(e, F_ITEM_ID))):
                               {k: v for k, v in e.items() if k != "__key"} for e in items}
    else:
        body[container_key] = clean
    return body


# =============================================================================
# Pricing logic
# =============================================================================

def other_traders(traders_json, my_uid):
    """Normalise trader list, drop me, sort by price desc (ignore sponsored order)."""
    lst = traders_json
    if isinstance(lst, dict):
        lst = pick(lst, ("traders", "data", "items", "results"), [])
    out = []
    for t in lst:
        if not isinstance(t, dict):
            continue
        tid = pick(t, F_TRADER_ID)
        price = pick(t, F_PRICE)
        if tid is None or price is None:
            continue
        if str(tid) == str(my_uid):
            continue
        out.append({"id": str(tid), "price": int(price),
                    "sponsored": bool(pick(t, F_SPONSORED, False))})
    out.sort(key=lambda x: x["price"], reverse=True)
    return out


def target_price(cfg, others):
    mode = cfg["mode"]
    if mode == "follow":
        t = next((o for o in others if o["id"] == str(cfg["follow_trader"])), None)
        if t is None:
            raise BotError(f"follow: trader {cfg['follow_trader']} not listed")
        return t["price"] + int(cfg["follow_offset"])
    if not others:
        raise BotError("no other traders listed")
    top = others[0]["price"]
    if mode == "match_top":
        return top
    if mode == "always_top":
        return top + int(cfg["step"])
    if mode == "second":
        second = others[1]["price"] if len(others) > 1 else 0
        want = top - int(cfg["step"])
        return max(want, second + 1)
    raise BotError(f"unknown mode {mode}")


def apply_caps(cfg, price, market_value):
    caps = []
    if cfg.get("max_price") is not None:
        caps.append(int(cfg["max_price"]))
    if cfg.get("max_pct_of_market") is not None and market_value:
        caps.append(int(market_value * cfg["max_pct_of_market"] / 100))
    capped = min([price] + caps)
    return max(capped, int(cfg.get("min_price", 1)))


# =============================================================================
# Main cycle
# =============================================================================

def backup_once(pl):
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(BACKUP_FILE):
        return
    with open(BACKUP_FILE, "w") as f:
        json.dump(pl, f, indent=2)
    log.info("Backed up pricelist to %s", BACKUP_FILE)


def run_cycle(w3b, torn, live):
    pl = w3b.get_pricelist()
    if live:
        backup_once(pl)
    ckey, items = pricelist_items(pl)
    log.info("Pricelist has %d items", len(items))

    changes = {}
    for item_id, overrides in ITEMS.items():
        cfg = {**DEFAULTS, **overrides}
        try:
            entry = find_entry(items, item_id)
            if entry is None:
                raise BotError("not on my pricelist (add it on W3B first)")
            current = entry_price(entry)
            others = other_traders(w3b.get_traders(item_id), w3b.uid)
            mv = torn.market_value(item_id) if cfg.get("max_pct_of_market") is not None else None
            want = apply_caps(cfg, target_price(cfg, others), mv)
            if want == current:
                log.info("item %s: %s stays at %d", item_id, cfg["mode"], current)
                continue
            log.info("item %s: %s %d -> %d (top other %s, mv %s)", item_id, cfg["mode"],
                     current, want, others[0]["price"] if others else None, mv)
            changes[item_id] = (entry, current, want)
        except Exception as e:  # one item failing must not stop the others
            log.error("item %s: skipped: %s", item_id, e)

    if not changes:
        log.info("No changes")
        return
    if not live:
        log.info("DRY RUN: %d change(s) not sent", len(changes))
        return

    for _, (entry, _, want) in changes.items():
        set_entry_price(entry, want)
    body = rebuild_pricelist(pl, ckey, items)
    w3b.put_pricelist(body)
    log.info("PUT sent with %d change(s)", len(changes))

    # Read-back check
    after = w3b.get_pricelist()
    _, after_items = pricelist_items(after)
    if len(after_items) != len(items):
        raise BotError(f"READ-BACK: item count changed {len(items)} -> {len(after_items)}. "
                       f"STOPPING. Restore from {BACKUP_FILE}")
    for item_id, (_, _, want) in changes.items():
        e = find_entry(after_items, item_id)
        got = entry_price(e) if e else None
        if got != want:
            raise BotError(f"READ-BACK: item {item_id} is {got}, expected {want}. STOPPING.")
    log.info("Read-back OK")


def probe(w3b, torn):
    """Print raw responses so the API shape can be confirmed. Changes nothing."""
    def show(label, fn):
        print(f"\n===== {label} =====")
        try:
            print(json.dumps(fn(), indent=2)[:4000])
        except Exception as e:
            print("ERROR:", e)
    show("GET pricelist", w3b.get_pricelist)
    first = next(iter(ITEMS), 206)
    show(f"GET traders for item {first}", lambda: w3b.get_traders(first))
    show(f"Torn market_value item {first}", lambda: torn.market_value(first))
    print("\nPaste everything above back to get the field names fixed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually send updates")
    ap.add_argument("--loop", action="store_true", help="repeat every INTERVAL_SECONDS")
    ap.add_argument("--probe", action="store_true", help="print raw API responses and exit")
    a = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)])

    key = os.environ.get("TORN_API_KEY")
    uid = os.environ.get("TORN_USER_ID")
    if not key or not uid:
        sys.exit("Set TORN_API_KEY and TORN_USER_ID")
    w3b, torn = W3B(key, uid), Torn(key)

    if a.probe:
        probe(w3b, torn)
        return
    if not ITEMS:
        sys.exit("ITEMS is empty; add items to the table")
    log.info("Mode: %s", "LIVE" if a.live else "DRY RUN")
    while True:
        try:
            run_cycle(w3b, torn, a.live)
        except BotError as e:
            log.error("%s", e)
            if "READ-BACK" in str(e):
                sys.exit(2)
        except Exception:
            log.exception("cycle failed")
        if not a.loop:
            break
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

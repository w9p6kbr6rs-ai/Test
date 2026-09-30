#!/usr/bin/env python3
"""
Torn trader pricelist bot: TornW3B (weav3r.dev) + optional Torn Exchange mirror.

Adjusts your buy prices on your TornW3B pricelist based on what other traders
are paying, mirrors the result to Torn Exchange if configured, and posts a
summary to Discord. Talks ONLY to the trader-site APIs and the read-only Torn
API. It never touches Torn's bazaar, item market or trades.

Usage:
    python3 w3b_price_bot.py --probe        # print raw API responses, change nothing
    python3 w3b_price_bot.py                # dry run (default): show what would change
    python3 w3b_price_bot.py --live         # actually update
    python3 w3b_price_bot.py --live --loop  # run forever, every INTERVAL_SECONDS

Requires: python3, requests  (pip install requests)
Env vars:
    TORN_API_KEY      required   Torn key (also used for W3B auth)
    TORN_USER_ID      required   your Torn ID
    TE_API_KEY        optional   Torn Exchange key; enables the mirror
    DISCORD_WEBHOOK   optional   Discord webhook URL; enables notifications
"""

import argparse
import copy
import json
import logging
import os
import sys
import time

import requests

# =============================================================================
# CONFIG: items and pricing
# =============================================================================

BOT_NAME = "Price bot"           # Discord embed title: "<BOT_NAME> price updates"

DEFAULTS = {
    "mode": "match_top",         # match_top | always_top | second | follow
    "step": 1,                   # always_top: add this; second: subtract this
    "max_price": None,           # hard cap in $; None = no hard cap
    "max_pct_of_market": 100,    # cap as % of Torn market value; None = no cap
    "follow_trader": None,       # follow mode: Torn user ID to follow
    "follow_offset": 0,          # follow mode: added to that trader's price
    "min_price": 1,
}

# Torn item ID -> per-item overrides (any DEFAULTS key). {} = defaults.
ITEMS = {
    # 206: {},
    # 197: {"mode": "always_top", "step": 5, "max_price": 900_000},
    # 180: {"mode": "second", "step": 1},
    # 258: {"mode": "follow", "follow_trader": 123456, "follow_offset": -1},
}

INTERVAL_SECONDS = 600
STATE_DIR = os.path.expanduser("~/.w3b_price_bot")
LOG_FILE = os.path.join(STATE_DIR, "bot.log")

# =============================================================================
# CONFIG: API shape.  EVERYTHING UNVERIFIED LIVES HERE.
# Run --probe, paste the output back, and only this block should need editing.
# Each site is a dict; the same generic client drives both.
# =============================================================================

W3B = {
    "name": "Weav3r",
    "base": "https://weav3r.dev/api",
    "auth": "query",                    # query | header | bearer   (GUESS: query)
    "auth_name": "key",                 # query param or header name
    "ep_get": "/pricelist/{uid}",       # GUESS
    "ep_put": "/pricelist/{uid}",       # confirmed from docs
    "ep_traders": "/marketplace/{item}/traders",   # from third-party README
    "min_gap": 0.8,                     # seconds between requests (~75/min < 100/min)
    "key_env": "TORN_API_KEY",
}

# Torn Exchange mirror. ENTIRELY UNVERIFIED PLACEHOLDER: I could not reach
# tornexchange.com. Enabled only when TE_API_KEY is set. --probe will show
# what's wrong; fix the four strings below.
TORN_EXCHANGE = {
    "name": "Torn Exchange",
    "base": "https://tornexchange.com/api",
    "auth": "header",
    "auth_name": "X-API-Key",
    "ep_get": "/pricelist/{uid}",
    "ep_put": "/pricelist/{uid}",
    "ep_traders": None,                 # not used for TE; W3B is the price source
    "min_gap": 1.0,
    "key_env": "TE_API_KEY",
}

TORN_BASE = "https://api.torn.com"

# Field-name candidates, tried in order. Fix after --probe.
F_TRADER_ID = ("userID", "user_id", "userId", "traderID", "trader_id", "id")
F_PRICE = ("price", "buyPrice", "buy_price", "value")
F_ITEM_ID = ("itemID", "item_id", "itemId", "id")
F_SPONSORED = ("sponsored", "isSponsored", "is_sponsored", "featured")
F_PRICELIST_ITEMS = (None, "items", "pricelist", "prices", "data")  # None = response is the list

# =============================================================================
# Plumbing
# =============================================================================

log = logging.getLogger("bot")


class BotError(Exception):
    pass


class Site:
    """Generic pricelist-site client; config dict decides URLs and auth."""

    def __init__(self, cfg, uid):
        self.cfg = cfg
        self.name = cfg["name"]
        self.uid = uid
        self.key = os.environ.get(cfg["key_env"])
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "torn-price-bot/1.1"
        self.last = 0.0

    def enabled(self):
        return bool(self.key)

    def _req(self, method, path, **kw):
        params = kw.pop("params", {}) or {}
        headers = {}
        a = self.cfg["auth"]
        if a == "query":
            params[self.cfg["auth_name"]] = self.key
        elif a == "header":
            headers[self.cfg["auth_name"]] = self.key
        elif a == "bearer":
            headers["Authorization"] = "Bearer " + self.key
        gap = time.monotonic() - self.last
        if gap < self.cfg["min_gap"]:
            time.sleep(self.cfg["min_gap"] - gap)
        self.last = time.monotonic()
        url = self.cfg["base"] + path
        for attempt in range(3):
            r = self.s.request(method, url, params=params, headers=headers, timeout=30, **kw)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 15))
                log.warning("%s: 429, sleeping %ss", self.name, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            raise BotError(f"{self.name} {method} {path} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise BotError(f"{self.name} {method} {path} -> non-JSON: {r.text[:300]}")

    def get_pricelist(self):
        return self._req("GET", self.cfg["ep_get"].format(uid=self.uid))

    def put_pricelist(self, body):
        return self._req("PUT", self.cfg["ep_put"].format(uid=self.uid), json=body)

    def get_traders(self, item_id):
        return self._req("GET", self.cfg["ep_traders"].format(item=item_id))


class Torn:
    def __init__(self, key):
        self.key = key
        self.s = requests.Session()
        self.cache = {}

    def item(self, item_id):
        """(name, market_value) from the read-only Torn API."""
        if item_id in self.cache and time.time() - self.cache[item_id][0] < 900:
            return self.cache[item_id][1]
        r = self.s.get(f"{TORN_BASE}/torn/{item_id}",
                       params={"selections": "items", "key": self.key}, timeout=30)
        d = r.json()
        if "error" in d:
            raise BotError(f"Torn API error: {d['error']}")
        it = d["items"][str(item_id)]
        res = (it.get("name", str(item_id)), int(it["market_value"]))
        self.cache[item_id] = (time.time(), res)
        return res


def pick(d, names, default=None):
    for n in names:
        if n in d:
            return d[n]
    return default


def money(n):
    return f"${n:,}"


# =============================================================================
# Pricelist shape helpers
# =============================================================================

def pricelist_items(pl):
    for k in F_PRICELIST_ITEMS:
        v = pl if k is None else (pl.get(k) if isinstance(pl, dict) else None)
        if isinstance(v, list):
            return k, v
        if isinstance(v, dict) and v and all(str(x).isdigit() for x in v):
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
    body = copy.deepcopy(original)
    clean = [{k: v for k, v in e.items() if k != "__key"} for e in items]
    if container_key is None:
        return clean
    if isinstance(original[container_key], dict):
        body[container_key] = {e.get("__key", str(pick(e, F_ITEM_ID))):
                               {k: v for k, v in e.items() if k != "__key"} for e in items}
    else:
        body[container_key] = clean
    return body


# =============================================================================
# Pricing logic
# =============================================================================

def other_traders(traders_json, my_uid):
    """Normalise, drop me, sort by price desc (sponsored ordering ignored)."""
    lst = traders_json
    if isinstance(lst, dict):
        lst = pick(lst, ("traders", "data", "items", "results"), [])
    out = []
    for t in lst:
        if not isinstance(t, dict):
            continue
        tid, price = pick(t, F_TRADER_ID), pick(t, F_PRICE)
        if tid is None or price is None or str(tid) == str(my_uid):
            continue
        out.append({"id": str(tid), "price": int(price),
                    "sponsored": bool(pick(t, F_SPONSORED, False))})
    out.sort(key=lambda x: x["price"], reverse=True)
    return out


def target_price(cfg, others):
    """Return (price, reason)."""
    mode = cfg["mode"]
    if mode == "follow":
        t = next((o for o in others if o["id"] == str(cfg["follow_trader"])), None)
        if t is None:
            raise BotError(f"follow: trader {cfg['follow_trader']} not listed")
        off = int(cfg["follow_offset"])
        return t["price"] + off, f"Following {t['id']} ({off:+d})"
    if not others:
        raise BotError("no other traders listed")
    top = others[0]["price"]
    if mode == "match_top":
        return top, "Matched highest competitor"
    if mode == "always_top":
        return top + int(cfg["step"]), f"Lead over highest competitor: {money(int(cfg['step']))}"
    if mode == "second":
        second = others[1]["price"] if len(others) > 1 else 0
        p = max(top - int(cfg["step"]), second + 1)
        return p, f"Second place: {money(top - p)} under top, above next ({money(second)})"
    raise BotError(f"unknown mode {mode}")


def apply_caps(cfg, price, market_value):
    caps = []
    if cfg.get("max_price") is not None:
        caps.append(int(cfg["max_price"]))
    if cfg.get("max_pct_of_market") is not None and market_value:
        caps.append(int(market_value * cfg["max_pct_of_market"] / 100))
    return max(min([price] + caps), int(cfg.get("min_price", 1)))


# =============================================================================
# Discord
# =============================================================================

def notify(lines):
    """Post one embed like: **Site · Item**: $old → $new (**+$d**)\\nreason"""
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url or not lines:
        return
    body = {"embeds": [{"title": f"{BOT_NAME} price updates",
                        "description": "\n\n".join(lines)[:4000]}]}
    try:
        r = requests.post(url, json=body, timeout=20)
        if r.status_code >= 400:
            log.warning("Discord webhook %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Discord webhook failed: %s", e)


def fmt(site, item_name, old, new, reason):
    return f"**{site} · {item_name}**: {money(old)} → {money(new)} (**{new - old:+,}**)\n{reason}"


# =============================================================================
# Update + read-back, shared by both sites
# =============================================================================

def backup(site, pl):
    path = os.path.join(STATE_DIR, f"backup_{site.name.replace(' ', '_')}.json")
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(pl, f, indent=2)
        log.info("%s: backed up pricelist to %s", site.name, path)
    return path


def push_changes(site, pl, ckey, items, wanted):
    """PUT full list with only `wanted` {item_id: price} changed, then read back."""
    for item_id, price in wanted.items():
        set_entry_price(find_entry(items, item_id), price)
    site.put_pricelist(rebuild_pricelist(pl, ckey, items))
    _, after = pricelist_items(site.get_pricelist())
    if len(after) != len(items):
        raise BotError(f"READ-BACK {site.name}: item count {len(items)} -> {len(after)}. STOPPING.")
    for item_id, price in wanted.items():
        e = find_entry(after, item_id)
        got = entry_price(e) if e else None
        if got != price:
            raise BotError(f"READ-BACK {site.name}: item {item_id} is {got}, expected {price}. STOPPING.")


def run_cycle(w3b, te, torn, live):
    pl = w3b.get_pricelist()
    if live:
        backup(w3b, pl)
    ckey, items = pricelist_items(pl)
    log.info("%s: pricelist has %d items", w3b.name, len(items))

    wanted, lines = {}, []
    for item_id, overrides in ITEMS.items():
        cfg = {**DEFAULTS, **overrides}
        try:
            entry = find_entry(items, item_id)
            if entry is None:
                raise BotError("not on my pricelist (add it on the site first)")
            current = entry_price(entry)
            name, mv = torn.item(item_id)
            others = other_traders(w3b.get_traders(item_id), w3b.uid)
            raw, reason = target_price(cfg, others)
            want = apply_caps(cfg, raw, mv)
            if want < raw:
                reason = f"Capped at {money(want)}; requested pricing target not reached"
            pct = cfg.get("max_pct_of_market")
            reason = f"Updated to {money(want)}; {reason}" + (f"; market cap {pct}%" if pct else "")
            if want == current:
                log.info("%s: stays at %s (%s)", name, money(current), cfg["mode"])
                continue
            log.info("%s: %s -> %s (%s)", name, money(current), money(want), reason)
            wanted[item_id] = want
            lines.append(fmt(w3b.name, name, current, want, reason))
        except Exception as e:  # one item failing must not stop the others
            log.error("item %s: skipped: %s", item_id, e)

    if not wanted:
        log.info("No changes")
        return
    if not live:
        log.info("DRY RUN: %d change(s) not sent", len(wanted))
        return

    push_changes(w3b, pl, ckey, items, wanted)
    log.info("%s: %d change(s) sent, read-back OK", w3b.name, len(wanted))

    if te.enabled():
        try:
            tpl = te.get_pricelist()
            backup(te, tpl)
            tkey, titems = pricelist_items(tpl)
            twanted = {}
            for item_id, price in wanted.items():
                e = find_entry(titems, item_id)
                if e is None:
                    log.warning("%s: item %s not on pricelist, not mirrored", te.name, item_id)
                    continue
                old = entry_price(e)
                if old != price:
                    twanted[item_id] = price
                    lines.append(fmt(te.name, torn.item(item_id)[0], old, price,
                                     "Sync confirmed (change since last confirmed sync)."))
            if twanted:
                push_changes(te, tpl, tkey, titems, twanted)
                log.info("%s: %d change(s) mirrored, read-back OK", te.name, len(twanted))
        except BotError as e:
            log.error("%s", e)
            lines.append(f"**{te.name}**: sync FAILED: {e}")
    notify(lines)


def probe(w3b, te, torn):
    def show(label, fn):
        print(f"\n===== {label} =====")
        try:
            print(json.dumps(fn(), indent=2)[:4000])
        except Exception as e:
            print("ERROR:", e)
    first = next(iter(ITEMS), 206)
    show("W3B GET pricelist", w3b.get_pricelist)
    show(f"W3B GET traders item {first}", lambda: w3b.get_traders(first))
    show(f"Torn item {first}", lambda: torn.item(first))
    if te.enabled():
        show("Torn Exchange GET pricelist", te.get_pricelist)
    else:
        print("\n(Torn Exchange skipped: TE_API_KEY not set)")
    print("\nPaste everything above back to get the field names fixed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)])
    key, uid = os.environ.get("TORN_API_KEY"), os.environ.get("TORN_USER_ID")
    if not key or not uid:
        sys.exit("Set TORN_API_KEY and TORN_USER_ID")
    w3b, te, torn = Site(W3B, uid), Site(TORN_EXCHANGE, uid), Torn(key)

    if a.probe:
        probe(w3b, te, torn)
        return
    if not ITEMS:
        sys.exit("ITEMS is empty; add items to the table")
    log.info("Mode: %s | Torn Exchange: %s | Discord: %s", "LIVE" if a.live else "DRY RUN",
             "on" if te.enabled() else "off", "on" if os.environ.get("DISCORD_WEBHOOK") else "off")
    while True:
        try:
            run_cycle(w3b, te, torn, a.live)
        except BotError as e:
            log.error("%s", e)
            if "READ-BACK" in str(e):
                notify([f"**STOPPED**: {e}"])
                sys.exit(2)
        except Exception:
            log.exception("cycle failed")
        if not a.loop:
            break
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

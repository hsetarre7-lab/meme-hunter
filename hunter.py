#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MEME PUMP HUNTER V3

Goal: detect early accumulation / pre-pump conditions rather than chase tokens
that have already pumped.

Design combines ideas researched from open-source memecoin scanners:
- multi-source discovery
- 1m/5m heat + volume acceleration
- consecutive/cluster buying
- large-buy / whale activity
- smart-money wallet tags + optional wallet-quality stats
- fresh-wallet / bundler / sniper penalties
- holder concentration + holder acceleration
- token/deployer/security gates
- score velocity and empirical outcome tracking
- Telegram alerts

This file is intentionally a clean reimplementation of the concepts, not a
copy of source code from any third-party repository.

Standard-library only. Optional data sources are enabled through env vars.
"""

import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
NETWORKS = [x.strip() for x in os.getenv("NETWORKS", "solana,base,bsc,eth").split(",") if x.strip()]
PRIMARY_NETWORK = os.getenv("PRIMARY_NETWORK", "solana")

MIN_LIQ = float(os.getenv("MIN_LIQ", "8000"))
MIN_VOL24 = float(os.getenv("MIN_VOL24", "10000"))
MIN_FDV = float(os.getenv("MIN_FDV", "50000"))
MAX_FDV = float(os.getenv("MAX_FDV", "10000000"))
MAX_AGE_H = float(os.getenv("MAX_AGE_H", "720"))
GECKO_TOP_PAGES = int(os.getenv("GECKO_TOP_PAGES", "10"))
MAX_H1_PCT = float(os.getenv("MAX_H1_PCT", "55"))
MAX_H24_PCT = float(os.getenv("MAX_H24_PCT", "350"))
MIN_LIQ_MCAP = float(os.getenv("MIN_LIQ_MCAP", "0.035"))

WATCH_MIN = float(os.getenv("WATCH_MIN_SCORE", "62"))
ALERT_MIN = float(os.getenv("ALERT_MIN_SCORE", "78"))
PREPUMP_MIN = float(os.getenv("PREPUMP_MIN_SCORE", "86"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_RUN", "4"))
COOLDOWN_H = float(os.getenv("ALERT_COOLDOWN_H", "12"))
RE_ALERT_JUMP = float(os.getenv("RE_ALERT_JUMP", "9"))

# Optional APIs / keys
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
GOPLUS_KEY = os.getenv("GOPLUS_API_KEY", "")
HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")
GMGN_KEY = os.getenv("GMGN_API_KEY", "")
GMGN_ENRICH_LIMIT = int(os.getenv("GMGN_ENRICH_LIMIT", "12"))
GMGN_WALLET_LIMIT = int(os.getenv("GMGN_WALLET_LIMIT", "2"))
RUGCHECK_LIMIT = int(os.getenv("RUGCHECK_LIMIT", "20"))

# State
STATE_DIR = os.getenv("STATE_DIR", "state")
MEMORY_FILE = os.path.join(STATE_DIR, "memory.json")
OUTCOME_FILE = os.path.join(STATE_DIR, "outcomes.jsonl")
ALERT_LOG = os.path.join(STATE_DIR, "alerts_log.jsonl")
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "72"))
OUTCOME_DAYS = int(os.getenv("OUTCOME_DAYS", "21"))

# Data sources
GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com"
GMGN_BASE = "https://gmgn.ai"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
HELIUS_BASE = "https://api.helius.xyz/v0"

GOPLUS_CHAIN = {"eth": "1", "bsc": "56", "base": "8453"}
GT_NETWORK = {"solana": "solana", "eth": "eth", "bsc": "bsc", "base": "base"}
NATIVE = {"SOL", "WSOL", "USDC", "USDT", "ETH", "WETH", "WBTC", "WBNB", "BNB", "DAI"}
JUNK_SYMBOL = re.compile(r"(https?://|www\.|t\.me/|discord\.gg/|@|\.com|\.xyz)", re.I)

# ---------------------------------------------------------------------------
# GENERIC HELPERS
# ---------------------------------------------------------------------------
def now_ts():
    return int(time.time())


def fnum(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def inum(x, default=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def safe_ratio(a, b, default=0.0):
    return a / b if b else default


def pct(a, b):
    return safe_ratio(a, b) * 100.0


def age_hours(ts):
    t = fnum(ts)
    if not t:
        return 999.0
    if t > 10_000_000_000:
        t /= 1000.0
    return max(0.0, (time.time() - t) / 3600.0)


def parse_iso_age(value):
    if not value:
        return 999.0
    try:
        s = str(value).replace("Z", "+00:00")
        return max(0.0, (time.time() - datetime.fromisoformat(s).timestamp()) / 3600.0)
    except Exception:
        return 999.0


def deep_find(obj, names, default=None):
    wanted = {str(x).lower() for x in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted:
                return v
        for v in obj.values():
            got = deep_find(v, names, None)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = deep_find(v, names, None)
            if got is not None:
                return got
    return default


def unwrap_data(data):
    if isinstance(data, dict):
        # Common API envelopes.
        for key in ("data", "result", "response"):
            val = data.get(key)
            if isinstance(val, (dict, list)):
                return val
    return data


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_json(url, params=None, headers=None, timeout=20, retries=2, method="GET", body=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    hdr = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "meme-pump-hunter/3.0",
    }
    if headers:
        hdr.update(headers)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdr.setdefault("content-type", "application/json")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "ignore")
                return json.loads(raw)
        except Exception as e:
            last = e
            code = getattr(e, "code", None)
            if code == 429:
                time.sleep(2.0 + attempt * 2.0)
            else:
                time.sleep(0.8 + attempt * 1.2)
    print(f"[warn] http failed {url[:120]} -> {last}")
    return None


def http_text(url, params=None, headers=None, timeout=15):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    try:
        req = urllib.request.Request(url, headers=headers or {"user-agent": "meme-pump-hunter/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("bad state")
    except Exception:
        st = {}
    st.setdefault("installed", False)
    st.setdefault("tokens", {})
    st.setdefault("signals", {})
    st.setdefault("last_digest", "")
    st.setdefault("stats", {})
    return st


def prune_state(st):
    cutoff = time.time() - 45 * 86400
    tokens = st.get("tokens", {})
    for k in list(tokens):
        if fnum(tokens[k].get("last_seen")) < cutoff:
            del tokens[k]
    if len(tokens) > 5000:
        keep = sorted(tokens.items(), key=lambda kv: -fnum(kv[1].get("last_seen")))[:5000]
        st["tokens"] = dict(keep)
    signals = st.get("signals", {})
    for k in list(signals):
        if fnum(signals[k].get("created_at")) < cutoff:
            del signals[k]
    if len(signals) > 1500:
        keep = sorted(signals.items(), key=lambda kv: -fnum(kv[1].get("created_at")))[:1500]
        st["signals"] = dict(keep)


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    prune_state(st)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))


def append_jsonl(path, row, max_bytes=2_000_000):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] log write: {e}")
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - max_bytes // 2))
                data = f.read().decode("utf-8", "ignore")
            pos = data.find("\n")
            data = data[pos + 1:] if pos >= 0 else data
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CANDIDATE MODEL
# ---------------------------------------------------------------------------
def blank_candidate(net, token, symbol="?"):
    return {
        "net": net,
        "token": token,
        "symbol": symbol.upper(),
        "name": symbol,
        "pool": "",
        "src": "",
        "age_h": 999.0,
        "price": 0.0,
        "fdv": 0.0,
        "liq": 0.0,
        "vol1m": 0.0,
        "vol5m": 0.0,
        "vol1h": 0.0,
        "vol6h": 0.0,
        "vol24h": 0.0,
        "buys1m": 0,
        "sells1m": 0,
        "buys5m": 0,
        "sells5m": 0,
        "buys1h": 0,
        "sells1h": 0,
        "txns1h": 0,
        "buyers1h": 0,
        "holders": 0,
        "chg_m1": 0.0,
        "chg_m5": 0.0,
        "chg_h1": 0.0,
        "chg_h6": 0.0,
        "chg_h24": 0.0,
        "boosts": 0,
        "profiled": False,
        "socials": False,
        "url": "",
        # Smart money / on-chain enrichment
        "smart_count": 0,
        "kol_count": 0,
        "whale_count": 0,
        "fresh_count": 0,
        "bundler_count": 0,
        "sniper_count": 0,
        "rat_count": 0,
        "top10_rate": 0.0,
        "smart_buy_usd": 0.0,
        "smart_sell_usd": 0.0,
        "cluster_wallets": 0,
        "cluster_buy_usd": 0.0,
        "big_buys": 0,
        "avg_big_buy": 0.0,
        "max_big_buy": 0.0,
        "wallet_quality": 0.0,
        "bundle_pct": 0.0,
        "dev_sold": False,
        "mint_revoked": None,
        "freeze_revoked": None,
        "rug_risk": 50.0,
        "honeypot": False,
        "security_notes": [],
        "gmgn": False,
        "rugcheck": False,
        "helius": False,
        "sec_drop": False,
        "score": 0.0,
        "probability": 0.0,
        "sc": {},
        "key": "",
    }


def merge_candidate(dst, src):
    for k in ("liq", "fdv", "vol1m", "vol5m", "vol1h", "vol6h", "vol24h", "holders", "smart_count", "kol_count", "whale_count", "fresh_count", "bundler_count", "sniper_count", "rat_count", "top10_rate", "smart_buy_usd", "smart_sell_usd", "cluster_wallets", "cluster_buy_usd", "big_buys", "avg_big_buy", "max_big_buy"):
        if k in src:
            if k == "top10_rate":
                dst[k] = max(dst.get(k, 0), fnum(src[k]))
            else:
                dst[k] = max(dst.get(k, 0), fnum(src[k]))
    for k in ("price", "age_h", "chg_m1", "chg_m5", "chg_h1", "chg_h6", "chg_h24"):
        if fnum(src.get(k)) or dst.get(k) == 0:
            dst[k] = src.get(k, dst.get(k))
    for k in ("symbol", "name", "pool", "url"):
        if src.get(k):
            dst[k] = src[k]
    for k in ("profiled", "socials", "gmgn"):
        dst[k] = bool(dst.get(k) or src.get(k))
    if src.get("src"):
        dst["src"] = "+".join(sorted(set(filter(None, (dst.get("src", "") + "+" + src["src"]).split("+")))))


# ---------------------------------------------------------------------------
# DISCOVERY: GECKO + DEXSCREENER + GMGN RANK
# ---------------------------------------------------------------------------
def discover_gecko(cands):
    for net in NETWORKS:
        gt = GT_NETWORK.get(net)
        if not gt:
            continue
        for kind, path, extra in (
            ("gt_new", f"/networks/{gt}/new_pools", {}),
            ("gt_trend", f"/networks/{gt}/trending_pools", {"duration": "1h"}),
        ):
            data = http_json(f"{GT_BASE}{path}", {"page": "1", "include": "base_token", **extra})
            if not data:
                continue
            inc = data.get("included") or []
            tmap = {i.get("id", ""): (i.get("attributes") or {}) for i in inc if i.get("type") == "token"}
            for p in data.get("data") or []:
                a = p.get("attributes") or {}
                rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
                addr = str(rel.get("id") or "").split("_", 1)[-1].lower()
                if not addr:
                    continue
                tok = tmap.get(rel.get("id"), {})
                sym = str(tok.get("symbol") or addr[:6]).upper()
                if sym in NATIVE:
                    continue
                c = blank_candidate(net, addr, sym)
                c["name"] = tok.get("name") or sym
                c["pool"] = a.get("address") or ""
                c["liq"] = fnum(a.get("reserve_in_usd"))
                c["fdv"] = fnum(a.get("fdv_usd")) or fnum(a.get("market_cap_usd"))
                c["vol1h"] = fnum((a.get("volume_usd") or {}).get("h1"))
                c["vol6h"] = fnum((a.get("volume_usd") or {}).get("h6"))
                c["vol24h"] = fnum((a.get("volume_usd") or {}).get("h24"))
                tx = a.get("transactions") or {}
                for tf, buys, sells in (("h1", "buys1h", "sells1h"),):
                    row = tx.get(tf) or {}
                    c[buys] = inum(row.get("buys"))
                    c[sells] = inum(row.get("sells"))
                    c["txns1h"] = c[buys] + c[sells]
                pc = a.get("price_change_percentage") or {}
                c["chg_h1"] = fnum(pc.get("h1"))
                c["chg_h6"] = fnum(pc.get("h6"))
                c["chg_h24"] = fnum(pc.get("h24"))
                c["age_h"] = parse_iso_age(a.get("pool_created_at"))
                c["src"] = kind
                key = f"{net}:{addr}"
                if key in cands:
                    merge_candidate(cands[key], c)
                else:
                    cands[key] = c
            time.sleep(0.4)


def discover_gecko_top(cands):
    """
    Broaden discovery beyond only new/trending pools.

    GeckoTerminal exposes /networks/{network}/pools sorted by 24h volume
    or transaction count. Paging these lists lets the hunter see actively
    traded pools that may be days/weeks old, while MAX_AGE_H controls the
    final age window.
    """
    for net in NETWORKS:
        gt = GT_NETWORK.get(net)
        if not gt:
            continue

        seen_pages = set()
        for sort_name in ("h24_volume_usd_desc", "h24_tx_count_desc"):
            for page in range(1, GECKO_TOP_PAGES + 1):
                page_key = (sort_name, page)
                if page_key in seen_pages:
                    continue
                seen_pages.add(page_key)

                data = http_json(
                    f"{GT_BASE}/networks/{gt}/pools",
                    {
                        "page": str(page),
                        "sort": sort_name,
                        "include": "base_token",
                    },
                )
                if not data:
                    break

                inc = data.get("included") or []
                tmap = {
                    i.get("id", ""): (i.get("attributes") or {})
                    for i in inc
                    if i.get("type") == "token"
                }

                rows = data.get("data") or []
                if not rows:
                    break

                for p in rows:
                    a = p.get("attributes") or {}
                    rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
                    addr = str(rel.get("id") or "").split("_", 1)[-1].lower()
                    if not addr:
                        continue

                    tok = tmap.get(rel.get("id"), {})
                    sym = str(tok.get("symbol") or addr[:6]).upper()
                    if sym in NATIVE:
                        continue

                    age_h = parse_iso_age(a.get("pool_created_at"))
                    if age_h > MAX_AGE_H:
                        continue

                    c = blank_candidate(net, addr, sym)
                    c["name"] = tok.get("name") or sym
                    c["pool"] = a.get("address") or ""
                    c["liq"] = fnum(a.get("reserve_in_usd"))
                    c["fdv"] = fnum(a.get("fdv_usd")) or fnum(a.get("market_cap_usd"))
                    c["vol1h"] = fnum((a.get("volume_usd") or {}).get("h1"))
                    c["vol6h"] = fnum((a.get("volume_usd") or {}).get("h6"))
                    c["vol24h"] = fnum((a.get("volume_usd") or {}).get("h24"))

                    tx = a.get("transactions") or {}
                    row = tx.get("h1") or {}
                    c["buys1h"] = inum(row.get("buys"))
                    c["sells1h"] = inum(row.get("sells"))
                    c["txns1h"] = c["buys1h"] + c["sells1h"]

                    pc = a.get("price_change_percentage") or {}
                    c["chg_h1"] = fnum(pc.get("h1"))
                    c["chg_h6"] = fnum(pc.get("h6"))
                    c["chg_h24"] = fnum(pc.get("h24"))
                    c["age_h"] = age_h
                    c["src"] = "gt_top_" + sort_name

                    key = f"{net}:{addr}"
                    if key in cands:
                        merge_candidate(cands[key], c)
                    else:
                        cands[key] = c

                time.sleep(0.35)



def dex_pair_to_candidate(pair, source):
    chain = str(pair.get("chainId") or "").lower()
    net = {"solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base"}.get(chain)
    if not net:
        return None
    base = pair.get("baseToken") or {}
    addr = str(base.get("address") or "").lower()
    sym = str(base.get("symbol") or addr[:6]).upper()
    if not addr or sym in NATIVE:
        return None
    c = blank_candidate(net, addr, sym)
    c["name"] = base.get("name") or sym
    c["pool"] = pair.get("pairAddress") or ""
    c["liq"] = fnum((pair.get("liquidity") or {}).get("usd"))
    c["fdv"] = fnum(pair.get("fdv")) or fnum(pair.get("marketCap"))
    vol = pair.get("volume") or {}
    c["vol1h"] = fnum(vol.get("h1"))
    c["vol6h"] = fnum(vol.get("h6"))
    c["vol24h"] = fnum(vol.get("h24"))
    tx = pair.get("txns") or {}
    r = tx.get("h1") or {}
    c["buys1h"] = inum(r.get("buys"))
    c["sells1h"] = inum(r.get("sells"))
    c["txns1h"] = c["buys1h"] + c["sells1h"]
    pc = pair.get("priceChange") or {}
    c["chg_h1"] = fnum(pc.get("h1"))
    c["chg_h6"] = fnum(pc.get("h6"))
    c["chg_h24"] = fnum(pc.get("h24"))
    c["age_h"] = age_hours(pair.get("pairCreatedAt"))
    c["url"] = pair.get("url") or ""
    c["src"] = source
    return c


def discover_dex(cands):
    # Boosts and token profiles are useful as discovery hints, but are deliberately
    # capped later and never treated as proof of organic demand.
    for endpoint, source in (("/token-boosts/latest/v1", "ds_boost"), ("/token-profiles/latest/v1", "ds_profile")):
        data = http_json(f"{DS_BASE}{endpoint}")
        if not isinstance(data, list):
            continue
        for row in data[:100]:
            addr = str(row.get("tokenAddress") or "").lower()
            chain = str(row.get("chainId") or "").lower()
            net = {"solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base"}.get(chain)
            if not addr or not net:
                continue
            key = f"{net}:{addr}"
            c = blank_candidate(net, addr, str(row.get("symbol") or addr[:6]))
            c["src"] = source
            c["boosts"] = inum(row.get("amount")) if source == "ds_boost" else 0
            c["profiled"] = source == "ds_profile"
            c["socials"] = bool(row.get("links") or row.get("url") or row.get("icon"))
            if key in cands:
                merge_candidate(cands[key], c)
            else:
                # Fetch pairs for unknown addresses only indirectly; don't make
                # hundreds of per-token calls. It will be filtered if market data
                # is absent.
                cands[key] = c
        time.sleep(0.4)


def gmgn_unwrap_rank(data):
    # Public GMGN endpoints can wrap data multiple times.
    x = data
    for _ in range(4):
        if isinstance(x, dict) and isinstance(x.get("data"), (dict, list)):
            x = x["data"]
        else:
            break
    if isinstance(x, dict):
        for key in ("rank", "tokens", "list"):
            if isinstance(x.get(key), list):
                return x[key]
    return x if isinstance(x, list) else []


def discover_gmgn(cands):
    # Public read-only ranking endpoint documented by community tooling. If GMGN
    # blocks GitHub Actions, the scanner simply continues with other sources.
    for tf in ("1m", "5m", "1h"):
        url = f"{GMGN_BASE}/defi/quotation/v1/rank/sol/swaps/{tf}"
        params = {"orderby": "smartmoney" if tf != "1m" else "swaps", "direction": "desc"}
        if GMGN_KEY:
            params["apikey"] = GMGN_KEY
        data = http_json(url, params=params, headers={"accept": "application/json", "user-agent": "Mozilla/5.0"}, timeout=18, retries=1)
        rows = gmgn_unwrap_rank(data)
        for row in rows[:120]:
            addr = str(row.get("address") or row.get("token_address") or "").lower()
            if not addr:
                continue
            key = f"solana:{addr}"
            c = blank_candidate("solana", addr, str(row.get("symbol") or addr[:6]))
            c["name"] = row.get("name") or c["symbol"]
            c["price"] = fnum(row.get("price"))
            c["liq"] = fnum(row.get("liquidity"))
            c["fdv"] = fnum(row.get("marketcap")) or fnum(row.get("market_cap"))
            c["holders"] = inum(row.get("holder_count"))
            c["smart_count"] = inum(row.get("smartmoney")) or inum(row.get("smart_money_count")) or inum(row.get("smart_degen_count"))
            c["kol_count"] = inum(row.get("renowned_count"))
            c["buys1h"] = inum(row.get("buys")) if tf == "1h" else c["buys1h"]
            c["sells1h"] = inum(row.get("sells")) if tf == "1h" else c["sells1h"]
            c["txns1h"] = c["buys1h"] + c["sells1h"]
            c["vol1h"] = fnum(row.get("volume")) if tf == "1h" else c["vol1h"]
            # GMGN's 1m/5m rows are retained as acceleration anchors.
            if tf == "1m":
                c["vol1m"] = fnum(row.get("volume"))
                c["chg_m1"] = fnum(row.get("change1m"))
                c["buys1m"] = inum(row.get("buys"))
                c["sells1m"] = inum(row.get("sells"))
            elif tf == "5m":
                c["vol5m"] = fnum(row.get("volume"))
                c["chg_m5"] = fnum(row.get("change5m"))
                c["buys5m"] = inum(row.get("buys"))
                c["sells5m"] = inum(row.get("sells"))
            c["chg_h1"] = fnum(row.get("change1h")) if tf == "1h" else c["chg_h1"]
            c["age_h"] = age_hours(row.get("open_timestamp") or row.get("openTimestamp"))
            c["gmgn"] = True
            c["src"] = "gmgn_" + tf
            if key in cands:
                merge_candidate(cands[key], c)
            else:
                cands[key] = c
        time.sleep(0.5)


def discover():
    cands = {}
    discover_gecko(cands)
    discover_gecko_top(cands)
    discover_dex(cands)
    if PRIMARY_NETWORK == "solana" or "solana" in NETWORKS:
        discover_gmgn(cands)

    # Remove candidates with absolutely no market evidence. Keep social/boost-only
    # entries out of expensive enrichment calls.
    result = {}
    for key, c in cands.items():
        if c["liq"] <= 0 and c["vol1h"] <= 0 and c["fdv"] <= 0:
            continue
        result[key] = c
    return result


# ---------------------------------------------------------------------------
# GMGN / ON-CHAIN ENRICHMENT
# ---------------------------------------------------------------------------
def gmgn_token_top_buyers(token):
    if not token:
        return []
    url = f"{GMGN_BASE}/defi/quotation/v1/tokens/top_buyers/sol/{token}"
    data = http_json(url, headers={"accept": "application/json", "user-agent": "Mozilla/5.0"}, timeout=15, retries=1)
    x = unwrap_data(data)
    if isinstance(x, dict):
        for key in ("list", "buyers", "rank", "data"):
            if isinstance(x.get(key), list):
                return x[key]
    return x if isinstance(x, list) else []


def normalize_buyer(row):
    if not isinstance(row, dict):
        return None
    addr = row.get("address") or row.get("wallet") or row.get("maker") or row.get("account_address")
    if not addr:
        return None
    tags = row.get("maker_token_tags") or row.get("wallet_tags") or row.get("tags") or row.get("tag") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = {str(x).lower() for x in tags}
    usd = fnum(row.get("buy_volume_cur")) or fnum(row.get("buy_volume")) or fnum(row.get("buy_usd")) or fnum(row.get("amount_usd")) or fnum(row.get("usd_value"))
    return {
        "address": str(addr),
        "usd": usd,
        "tags": tags,
        "fresh": bool(tags & {"fresh_wallet", "fresh"}),
        "bundler": bool(tags & {"bundler", "bundle"}),
        "sniper": bool(tags & {"sniper"}),
        "smart": bool(tags & {"smart_degen", "smartmoney", "smart_money"}),
        "kol": bool(tags & {"renowned", "kol"}),
        "rat": bool(tags & {"rat_trader", "rat"}),
    }


def wallet_stats(wallet):
    if not wallet:
        return None
    # Public endpoint observed in GMGN-compatible tooling. It may be unavailable
    # from Actions; failure is intentionally non-fatal.
    url = f"{GMGN_BASE}/api/v1/wallet_stat/sol/{wallet}/7d"
    data = http_json(url, headers={"accept": "application/json", "user-agent": "Mozilla/5.0"}, timeout=12, retries=1)
    x = unwrap_data(data)
    return x if isinstance(x, dict) else None


def enrich_gmgn(cands):
    sol = [c for c in cands.values() if c["net"] == "solana" and (c["gmgn"] or c["liq"] >= MIN_LIQ)]
    # Only deeply enrich the strongest candidates to protect rate limits.
    sol.sort(key=lambda c: (c["smart_count"], c["vol1h"], c["liq"]), reverse=True)
    for c in sol[:GMGN_ENRICH_LIMIT]:
        buyers = gmgn_token_top_buyers(c["token"])
        if not buyers:
            continue
        parsed = [normalize_buyer(x) for x in buyers]
        parsed = [x for x in parsed if x]
        if not parsed:
            continue
        c["cluster_wallets"] = len({x["address"] for x in parsed if x["smart"]})
        c["smart_count"] = max(c["smart_count"], sum(x["smart"] for x in parsed))
        c["kol_count"] = max(c["kol_count"], sum(x["kol"] for x in parsed))
        c["fresh_count"] = sum(x["fresh"] for x in parsed)
        c["bundler_count"] = sum(x["bundler"] for x in parsed)
        c["sniper_count"] = sum(x["sniper"] for x in parsed)
        c["rat_count"] = sum(x["rat"] for x in parsed)
        smart = [x for x in parsed if x["smart"]]
        c["smart_buy_usd"] = sum(x["usd"] for x in smart)
        c["cluster_buy_usd"] = sum(x["usd"] for x in smart)
        amounts = sorted([x["usd"] for x in parsed if x["usd"] > 0], reverse=True)
        c["big_buys"] = sum(1 for x in amounts if x >= 500)
        c["avg_big_buy"] = statistics.mean(amounts[:10]) if amounts else 0.0
        c["max_big_buy"] = amounts[0] if amounts else 0.0
        # Sample wallet quality only for a few distinct smart wallets.
        quality_scores = []
        for w in list(dict.fromkeys(x["address"] for x in smart))[:GMGN_WALLET_LIMIT]:
            ws = wallet_stats(w)
            if not ws:
                continue
            win = fnum(ws.get("winrate"))
            if win <= 1:
                win *= 100
            pnl = fnum(ws.get("pnl")) or fnum(ws.get("total_profit"))
            realized = fnum(ws.get("realized_profit"))
            q = clamp(win * 0.65 + (50 + math.tanh(pnl / 10000.0) * 50) * 0.35)
            if realized < -100:
                q -= 10
            quality_scores.append(clamp(q))
        if quality_scores:
            c["wallet_quality"] = statistics.mean(quality_scores)
        c["gmgn"] = True
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# SECURITY: RUGCHECK + GOPLUS
# ---------------------------------------------------------------------------
def enrich_rugcheck(cands):
    for c in sorted([x for x in cands.values() if x["net"] == "solana"], key=lambda z: (z["smart_count"], z["vol1h"], z["liq"]), reverse=True)[:RUGCHECK_LIMIT]:
        data = http_json(f"{RUGCHECK_BASE}/tokens/{c['token']}/report/summary", timeout=18, retries=1)
        if not data:
            # fallback endpoint
            data = http_json(f"{RUGCHECK_BASE}/tokens/{c['token']}/report", timeout=18, retries=1)
        if not data:
            continue
        c["rugcheck"] = True
        score = fnum(deep_find(data, {"score"}, None), 50)
        # RugCheck uses a lower-is-better risk score in many reports.
        c["rug_risk"] = clamp(score, 0, 100)
        risks = deep_find(data, {"risks"}, [])
        if isinstance(risks, list):
            for r in risks[:12]:
                if isinstance(r, dict):
                    name = r.get("name") or r.get("description") or r.get("level")
                    if name:
                        c["security_notes"].append(str(name))
        # Extract authority and holder concentration when available.
        mint = deep_find(data, {"mintAuthority", "mint_authority"}, None)
        freeze = deep_find(data, {"freezeAuthority", "freeze_authority"}, None)
        if mint is not None:
            c["mint_revoked"] = not bool(mint)
        if freeze is not None:
            c["freeze_revoked"] = not bool(freeze)
        top10 = deep_find(data, {"topHolders", "top_10_holder_rate", "top10HolderRate"}, None)
        if isinstance(top10, (int, float)):
            c["top10_rate"] = fnum(top10)
            if c["top10_rate"] > 1:
                c["top10_rate"] /= 100.0
        if c["rug_risk"] >= 70:
            c["sec_drop"] = True
        time.sleep(0.3)


def enrich_goplus(cands):
    if not GOPLUS_KEY:
        return
    for c in sorted([x for x in cands.values() if x["net"] in GOPLUS_CHAIN], key=lambda z: (z["vol1h"], z["liq"]), reverse=True)[:RUGCHECK_LIMIT]:
        chain = GOPLUS_CHAIN[c["net"]]
        data = http_json(f"{GOPLUS_BASE}/token_security/{chain}", {"contract_addresses": c["token"]}, headers={"Authorization": GOPLUS_KEY}, timeout=15, retries=1)
        if not data:
            continue
        rows = data.get("result") or {}
        row = rows.get(c["token"]) or rows.get(c["token"].lower())
        if not isinstance(row, dict):
            continue
        honeypot = str(row.get("is_honeypot") or "0") in {"1", "true", "yes"}
        c["honeypot"] = honeypot
        if honeypot:
            c["sec_drop"] = True
        for field, out in (("buy_tax", "buy_tax"), ("sell_tax", "sell_tax")):
            tax = fnum(row.get(field))
            if tax > 1:
                tax /= 100
            if tax > 0.10:
                c["sec_drop"] = True
                c["security_notes"].append(f"{out}>{tax:.0%}")
        if str(row.get("is_blacklisted") or "0") in {"1", "true"}:
            c["sec_drop"] = True
            c["security_notes"].append("blacklist")


# ---------------------------------------------------------------------------
# DERIVED METRICS
# ---------------------------------------------------------------------------
def derive_metrics(c, prev):
    c["buy_pressure_1h"] = safe_ratio(c["buys1h"], c["buys1h"] + c["sells1h"]) * 100
    c["buy_pressure_5m"] = safe_ratio(c["buys5m"], c["buys5m"] + c["sells5m"]) * 100
    c["heat"] = safe_ratio(c["vol1m"], c["vol5m"]) * 100 if c["vol5m"] > 0 else 0
    c["vol_accel_1h"] = safe_ratio(c["vol1h"], c["vol6h"] / 6.0) if c["vol6h"] > 0 else 0
    c["vol_liq"] = safe_ratio(c["vol1h"], c["liq"])
    c["txn_accel"] = safe_ratio(c["txns1h"], max(1, prev.get("txns1h", 0))) if prev else 0
    c["holder_accel"] = 0.0
    if c["holders"] and prev and fnum(prev.get("holders")) > 0:
        dt_h = max(1 / 12, (time.time() - fnum(prev.get("ts", time.time()))) / 3600.0)
        c["holder_accel"] = ((c["holders"] - fnum(prev["holders"])) / fnum(prev["holders"])) * 100 / dt_h
    c["score_velocity"] = 0.0
    if prev and fnum(prev.get("score_ts")):
        dt_h = max(1 / 12, (time.time() - fnum(prev["score_ts"])) / 3600.0)
        c["score_velocity"] = (c.get("prev_score", 0) - fnum(prev.get("score", 0))) / dt_h
    c["anti_chase"] = 0.0
    if c["chg_h1"] > 35:
        c["anti_chase"] += 35
    if c["heat"] > 90 and c["vol5m"] < max(1000, c["liq"] * 0.02):
        c["anti_chase"] += 25
    if c["vol_liq"] > 2.0:
        c["anti_chase"] += 20
    c["anti_chase"] = clamp(c["anti_chase"])


# ---------------------------------------------------------------------------
# SCORING ENGINE
# ---------------------------------------------------------------------------
def score_candidate(c, prev):
    # 1. Smart-money accumulation: real wallet tags > generic volume.
    cluster = clamp(c["cluster_wallets"] / 4 * 100)
    smart_count = clamp(c["smart_count"] / 6 * 100)
    smart_direction = clamp((c["smart_buy_usd"] - c["smart_sell_usd"]) / max(c["smart_buy_usd"] + c["smart_sell_usd"], 1) * 50 + 50)
    wallet_q = c["wallet_quality"] if c["wallet_quality"] > 0 else 50
    SM = 0.40 * cluster + 0.25 * smart_count + 0.20 * smart_direction + 0.15 * wallet_q

    # 2. Trade pressure / sequence.
    BP = clamp((c["buy_pressure_1h"] - 50) / 30 * 100)
    bp5 = clamp((c["buy_pressure_5m"] - 50) / 30 * 100)
    consecutive = clamp((c["buys5m"] - c["sells5m"] * 0.5) / 12 * 100)
    B = 0.45 * BP + 0.35 * bp5 + 0.20 * consecutive

    # 3. Heat / acceleration. Heat is best in a building zone, not at 100%.
    heat = c["heat"]
    heat_score = 0 if heat <= 15 else 35 if heat < 30 else 70 if heat < 48 else 95 if heat < 75 else 75 if heat < 100 else 35
    accel = clamp((c["vol_accel_1h"] - 0.7) / 2.5 * 100)
    txn = clamp((c["txn_accel"] - 0.8) / 2.5 * 100) if c["txn_accel"] else 0
    T = 0.50 * heat_score + 0.35 * accel + 0.15 * txn

    # 4. Holder growth.
    H = clamp(50 + c["holder_accel"] * 12)
    if c["holders"] > 0 and c["smart_count"] >= 2:
        H += 8
    if c["top10_rate"] > 0.55:
        H -= 35
    elif c["top10_rate"] > 0.40:
        H -= 18
    H = clamp(H)

    # 5. Large-buy / whale structure.
    big = clamp(c["big_buys"] / 5 * 100)
    avg = clamp(c["avg_big_buy"] / max(1, c["liq"] * 0.015) * 100)
    maxb = clamp(c["max_big_buy"] / max(1, c["liq"] * 0.05) * 100)
    W = 0.45 * big + 0.35 * avg + 0.20 * maxb

    # 6. Liquidity / market cap quality.
    liq_score = clamp((c["liq"] - MIN_LIQ) / max(MIN_LIQ, 60000) * 100)
    ratio = safe_ratio(c["liq"], c["fdv"])
    ratio_score = clamp(ratio / 0.12 * 100)
    L = 0.55 * liq_score + 0.45 * ratio_score

    # 7. Price structure: early positive movement preferred; punish vertical moves.
    p5 = c["chg_m5"]
    p1 = c["chg_h1"]
    structure = 50
    if 0 <= p5 <= 8:
        structure += 25
    elif 8 < p5 <= 20:
        structure += 15
    elif p5 > 30:
        structure -= 25
    if 0 <= p1 <= 25:
        structure += 20
    elif p1 > 50:
        structure -= 25
    if c["chg_h6"] > 120:
        structure -= 20
    P = clamp(structure)

    # 8. Safety.
    safety = 100 - c["rug_risk"]
    if c["mint_revoked"] is False:
        safety -= 25
    if c["freeze_revoked"] is False:
        safety -= 25
    if c["top10_rate"] > 0.60:
        safety -= 25
    safety -= min(25, c["bundler_count"] * 5)
    safety -= min(15, c["sniper_count"] * 2)
    safety = clamp(safety)

    # 9. Organic / discovery signals. Boosts are capped hard.
    O = 50
    if c["profiled"]:
        O += 8
    if c["socials"]:
        O += 8
    if c["boosts"]:
        O += min(10, c["boosts"] / 10)
    if c["fresh_count"] > 5:
        O -= 15
    O = clamp(O)

    # 10. Early-stage preference.
    A = 50
    if c["age_h"] <= 2:
        A = 92
    elif c["age_h"] <= 6:
        A = 85
    elif c["age_h"] <= 18:
        A = 75
    elif c["age_h"] <= 48:
        A = 65
    elif c["age_h"] <= 120:
        A = 52

    raw = (
        0.20 * SM +
        0.14 * B +
        0.15 * T +
        0.10 * H +
        0.10 * W +
        0.08 * L +
        0.08 * P +
        0.09 * safety +
        0.03 * O +
        0.03 * A
    )

    # Explicit anti-FOMO penalty.
    raw -= c["anti_chase"] * 0.25

    # Cluster is a major differentiator. A token with several smart wallets
    # entering together gets a controlled boost; one wallet does not.
    if c["cluster_wallets"] >= 3:
        raw += 4
    if c["cluster_wallets"] >= 5:
        raw += 3
    if c["bundler_count"] >= 4 and c["cluster_wallets"] < 2:
        raw -= 8
    if c["rat_count"] >= 2:
        raw -= 7
    if c["dev_sold"]:
        raw += 2

    total = clamp(raw)
    components = {
        "SM": SM, "B": B, "T": T, "H": H, "W": W,
        "L": L, "P": P, "S": safety, "O": O, "A": A,
    }
    return total, components


# ---------------------------------------------------------------------------
# OUTCOME / BACKTEST MEMORY
# ---------------------------------------------------------------------------
def update_outcomes(st, cands):
    now = now_ts()
    changed = False
    for key, sig in list(st.get("signals", {}).items()):
        if now - fnum(sig.get("created_at")) > OUTCOME_DAYS * 86400:
            continue
        c = cands.get(key)
        if not c:
            continue
        entry = fnum(sig.get("entry_fdv"))
        cur = fnum(c.get("fdv"))
        if not entry or not cur:
            continue
        mult = cur / entry
        sig["last_mult"] = mult
        sig["max_mult"] = max(fnum(sig.get("max_mult", 1)), mult)
        age_min = (now - fnum(sig.get("created_at"))) / 60
        for horizon in (15, 60, 360, 1440):
            if age_min >= horizon and not sig.get(f"m{horizon}"):
                sig[f"m{horizon}"] = mult
                append_jsonl(OUTCOME_FILE, {
                    "ts": now, "key": key, "symbol": sig.get("symbol"),
                    "score": sig.get("score"), "horizon_min": horizon,
                    "multiple": mult,
                })
                changed = True
    return changed


def historical_hit_rate(st):
    rows = []
    for sig in st.get("signals", {}).values():
        if fnum(sig.get("score")) < ALERT_MIN:
            continue
        if fnum(sig.get("m60")):
            rows.append(fnum(sig["m60"]))
    if not rows:
        return None
    return {
        "n": len(rows),
        "avg": statistics.mean(rows),
        "hit_1_25": sum(x >= 1.25 for x in rows) / len(rows) * 100,
        "hit_2x": sum(x >= 2 for x in rows) / len(rows) * 100,
    }


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("\n" + text + "\n")
        return True
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as e:
        print(f"[warn] telegram: {e}")
        return False


def usd(x):
    x = fnum(x)
    if x >= 1e9:
        return f"${x/1e9:.2f}B"
    if x >= 1e6:
        return f"${x/1e6:.2f}M"
    if x >= 1e3:
        return f"${x/1e3:.1f}K"
    return f"${x:.0f}"


def build_alert(c, label, hist=None):
    safety = 100 - c["rug_risk"]
    url = c.get("url") or (f"https://dexscreener.com/{'solana' if c['net']=='solana' else c['net']}/{c['pool']}" if c.get("pool") else "")
    lines = [
        f"<b>🏹 {label}</b>",
        f"<b>${c['symbol']}</b> · {c['net']}",
        f"Score: <b>{c['score']:.0f}/100</b> · Pump probability: <b>{c['probability']:.0f}%</b>",
        "",
        f"🧠 Smart money: <b>{c['smart_count']}</b> · cluster: <b>{c['cluster_wallets']}</b>",
        f"🐋 Big buys: <b>{c['big_buys']}</b> · avg: <b>{usd(c['avg_big_buy'])}</b>",
        f"📈 Heat 1m/5m: <b>{c['heat']:.0f}%</b> · vol accel: <b>{c['vol_accel_1h']:.1f}x</b>",
        f"🟢 Buy pressure: <b>{c['buy_pressure_1h']:.0f}%</b> · holders: <b>{inum(c['holders']):,}</b>",
        f"💧 Liq: <b>{usd(c['liq'])}</b> · MC/FDV: <b>{usd(c['fdv'])}</b>",
        f"⏱ Age: <b>{c['age_h']:.1f}h</b> · 1h: <b>{c['chg_h1']:+.1f}%</b> · 5m: <b>{c['chg_m5']:+.1f}%</b>",
        f"🛡 Safety: <b>{safety:.0f}/100</b> · top10: <b>{c['top10_rate']:.0%}</b>",
    ]
    risks = []
    if c["fresh_count"]:
        risks.append(f"fresh:{c['fresh_count']}")
    if c["bundler_count"]:
        risks.append(f"bundler:{c['bundler_count']}")
    if c["sniper_count"]:
        risks.append(f"sniper:{c['sniper_count']}")
    if c["anti_chase"]:
        risks.append("anti-chase")
    if risks:
        lines.append("⚠️ " + " · ".join(risks))
    if hist:
        lines.append(f"📊 Historical 60m: {hist['hit_1_25']:.0f}% ≥1.25x · {hist['hit_2x']:.0f}% ≥2x (n={hist['n']})")
    if url:
        lines.append(f"🔗 <a href=\"{url}\">Chart</a>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def hard_gate(c):
    if JUNK_SYMBOL.search(c["symbol"]):
        return False, "junk-symbol"
    if c["liq"] < MIN_LIQ:
        return False, "low-liquidity"
    if c["vol24h"] and c["vol24h"] < MIN_VOL24:
        return False, "low-volume"
    if c["fdv"] and not (MIN_FDV <= c["fdv"] <= MAX_FDV):
        return False, "fdv-band"
    if c["fdv"] and c["liq"] / c["fdv"] < MIN_LIQ_MCAP:
        return False, "thin-liquidity"
    if c["age_h"] > MAX_AGE_H:
        return False, "too-old"
    if c["chg_h1"] > MAX_H1_PCT or c["chg_h24"] > MAX_H24_PCT:
        return False, "already-pumped"
    return True, "ok"


def main():
    start = time.time()
    st = load_state()
    now = now_ts()

    if not st.get("installed"):
        msg = (
            "<b>🏹 MEME PUMP HUNTER V3 فعال شد</b>\n\n"
            "هسته جدید روی accumulation، smart-money cluster، volume heat، holder acceleration و security تمرکز دارد.\n"
            "این سیستم سیگنال می‌دهد؛ خرید خودکار ندارد."
        )
        if tg_send(msg):
            st["installed"] = True

    print("🏹 V3 discovery:", NETWORKS)
    cands = discover()
    print("discovered:", len(cands))

    stats = {}
    alive = {}
    for key, c in cands.items():
        ok, reason = hard_gate(c)
        if not ok:
            stats[reason] = stats.get(reason, 0) + 1
            continue
        alive[key] = c
    print("after gates:", len(alive), stats)

    # Expensive enrichment only after cheap gates.
    enrich_gmgn(alive)
    enrich_rugcheck(alive)
    enrich_goplus(alive)
    alive = {k: c for k, c in alive.items() if not c["sec_drop"] and not c["honeypot"]}
    print("after safety:", len(alive))

    # Score + state snapshot.
    for key, c in alive.items():
        prev = st["tokens"].get(key, {})
        derive_metrics(c, prev)
        score, components = score_candidate(c, prev)
        c["score"] = score
        c["sc"] = components
        c["key"] = key
        # Conservative conversion from score to probability. It is a model score,
        # not a statistically calibrated probability until enough outcome data exists.
        c["probability"] = clamp(100 / (1 + math.exp(-(score - 70) / 7)))
        ent = {
            "ts": now,
            "last_seen": now,
            "score": score,
            "score_ts": now,
            "prev_score": fnum(prev.get("score")),
            "holders": c["holders"],
            "txns1h": c["txns1h"],
            "fdv": c["fdv"],
            "liq": c["liq"],
            "vol1h": c["vol1h"],
            "smart_count": c["smart_count"],
            "cluster_wallets": c["cluster_wallets"],
            "best": max(fnum(prev.get("best")), score),
            "history": (prev.get("history") or [])[-(HISTORY_MAX - 1):],
        }
        ent["history"].append({
            "ts": now,
            "score": round(score, 2),
            "fdv": c["fdv"],
            "liq": c["liq"],
            "vol1m": c["vol1m"],
            "vol5m": c["vol5m"],
            "vol1h": c["vol1h"],
            "holders": c["holders"],
            "smart": c["smart_count"],
            "cluster": c["cluster_wallets"],
        })
        st["tokens"][key] = ent

    update_outcomes(st, alive)

    ranked = sorted(alive.values(), key=lambda x: x["score"], reverse=True)
    print("\nTOP CANDIDATES")
    for c in ranked[:15]:
        print(f"{c['score']:5.1f} ${c['symbol']:<12} sm={c['smart_count']} cl={c['cluster_wallets']} heat={c['heat']:.0f}% volA={c['vol_accel_1h']:.1f}x age={c['age_h']:.1f}h")

    hist = historical_hit_rate(st)
    alerts = 0
    alert_keys = []
    for c in ranked:
        if alerts >= MAX_ALERTS:
            break
        if c["score"] < WATCH_MIN:
            break
        prev = st["tokens"].get(c["key"], {})
        # State already updated, so use alert metadata stored separately.
        meta = st.setdefault("alert_meta", {}).get(c["key"], {})
        last_alert = fnum(meta.get("ts"))
        last_score = fnum(meta.get("score"))
        due = (now - last_alert) >= COOLDOWN_H * 3600 or (c["score"] - last_score) >= RE_ALERT_JUMP
        if not due:
            continue
        if c["score"] < ALERT_MIN and c["cluster_wallets"] < 2 and c["smart_count"] < 2:
            continue

        if c["score"] >= PREPUMP_MIN and c["cluster_wallets"] >= 3:
            label = "🔥 PRE-PUMP / SMART-MONEY CLUSTER"
        elif c["score"] >= ALERT_MIN:
            label = "🟠 EARLY ACCUMULATION"
        else:
            label = "🟡 WATCH"

        text = build_alert(c, label, hist)
        if tg_send(text):
            alerts += 1
            alert_keys.append(c["key"])
            st.setdefault("alert_meta", {})[c["key"]] = {"ts": now, "score": c["score"]}
            st["signals"][f"{c['key']}:{now}"] = {
                "key": c["key"],
                "symbol": c["symbol"],
                "created_at": now,
                "score": c["score"],
                "entry_fdv": c["fdv"],
                "entry_liq": c["liq"],
                "smart_count": c["smart_count"],
                "cluster_wallets": c["cluster_wallets"],
                "heat": c["heat"],
            }
            append_jsonl(ALERT_LOG, {
                "ts": now,
                "key": c["key"],
                "symbol": c["symbol"],
                "score": c["score"],
                "probability": c["probability"],
                "smart_count": c["smart_count"],
                "cluster_wallets": c["cluster_wallets"],
                "fdv": c["fdv"],
                "liq": c["liq"],
                "heat": c["heat"],
                "vol_accel": c["vol_accel_1h"],
            })

    # A compact digest is useful for debugging Actions without spamming Telegram.
    st["stats"] = {
        "last_run": now,
        "runtime_s": round(time.time() - start, 2),
        "discovered": len(cands),
        "alive": len(alive),
        "alerts": alerts,
        "top_score": ranked[0]["score"] if ranked else 0,
        "top_symbol": ranked[0]["symbol"] if ranked else "",
    }
    save_state(st)
    print(f"done in {time.time()-start:.1f}s | alerts={alerts} | top={ranked[0]['symbol'] if ranked else '-'}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print("FATAL:", repr(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)

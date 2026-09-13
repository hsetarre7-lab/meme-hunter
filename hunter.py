#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏹 MEME PUMP HUNTER — serverless & 100% free
Discovery (GeckoTerminal new/trending pools + DexScreener boosts/profiles)
→ hard gates → safety (GoPlus for EVM, RugCheck for Solana)
→ weighted scoring (0-100) → Telegram alerts.
State persists in state/memory.json (committed back by GitHub Actions).
Python 3.9+ — standard library only, no pip install required.
"""
import json, os, re, sys, time, math, html, traceback
import urllib.request, urllib.parse
from datetime import datetime, timezone

# ── Config (overridable via GitHub Secrets / env vars) ─────────────────────
NETWORKS     = os.getenv("NETWORKS", "solana,eth,bsc,base").split(",")
MIN_LIQ      = float(os.getenv("MIN_LIQ", "10000"))
MIN_VOL24    = float(os.getenv("MIN_VOL24", "20000"))
MIN_TXN1H    = int(os.getenv("MIN_TXN1H", "40"))
MIN_BUYERS1H = int(os.getenv("MIN_BUYERS1H", "10"))
MIN_FDV      = float(os.getenv("MIN_FDV", "70000"))
MAX_FDV      = float(os.getenv("MAX_FDV", "12000000"))
MIN_LIQ_MCAP = float(os.getenv("MIN_LIQ_MCAP", "0.05"))
MAX_AGE_H    = float(os.getenv("MAX_AGE_H", "168"))
EXCL_H6      = float(os.getenv("EXCL_H6_PCT", "300"))
EXCL_H24     = float(os.getenv("EXCL_H24_PCT", "450"))
ALERT_MIN    = float(os.getenv("ALERT_MIN_SCORE", "75"))
WATCH_MIN    = float(os.getenv("WATCH_MIN_SCORE", "62"))
MAX_ALERTS   = int(os.getenv("MAX_ALERTS_PER_RUN", "4"))
COOLDOWN_H   = float(os.getenv("ALERT_COOLDOWN_H", "20"))
RE_JUMP      = float(os.getenv("RE_ALERT_JUMP", "12"))
RUGCHECK_MAX = int(os.getenv("RUGCHECK_MAX", "25"))
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID", "")
GOPLUS_KEY   = os.getenv("GOPLUS_API_KEY", "")
STATE_DIR    = os.getenv("STATE_DIR", "state")

GT_BASE   = "https://api.geckoterminal.com/api/v2"
DS_BASE   = "https://api.dexscreener.com"
GT_TO_DS  = {"solana": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base"}
DS_TO_GT  = {v: k for k, v in GT_TO_DS.items()}
GOPLUS_CH = {"eth": "1", "bsc": "56", "base": "8453"}
NATIVE    = {"USDT","USDC","DAI","WETH","WSOL","WBTC","SOL","ETH","WBNB"}
JUNK      = re.compile(r"(https?://|t\.me/|www\.|@|\$|\.com|\.xyz)", re.I)

# ── HTTP helper (retry + backoff, 429-aware) ────────────────────────────────
def http_json(url, params=None, timeout=25, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "accept": "application/json",
                "user-agent": "meme-pump-hunter/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            err = e
            pause = 8 * (i + 1) if getattr(e, "code", None) == 429 else 1.5 * (i + 1)
            time.sleep(pause)
    print(f"[warn] GET fail: {url[:110]} -> {err}")
    return None

def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

# ── State (the agent's memory — a JSON file inside the repo) ────────────────
def load_state():
    try:
        with open(os.path.join(STATE_DIR, "memory.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tokens": {}, "last_digest": ""}

def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    now = time.time()
    st["tokens"] = {k: v for k, v in st["tokens"].items()
                    if now - v.get("last_seen", 0) < 45 * 86400}
    if len(st["tokens"]) > 4000:
        st["tokens"] = dict(sorted(st["tokens"].items(),
                                   key=lambda kv: -kv[1].get("last_seen", 0))[:4000])
    with open(os.path.join(STATE_DIR, "memory.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)

def append_log(rows):
    if not rows:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "alerts_log.jsonl"), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

# ── [1] DISCOVERY ───────────────────────────────────────────────────────────
def discover():
    cands = {}

    def upsert(c):
        key = f'{c["net"]}:{c["token"]}'
        old = cands.get(key)
        if old:
            old["liq"]      = max(old["liq"], c["liq"])
            old["fdv"]      = max(old["fdv"], c["fdv"])
            old["boosts"]   = max(old["boosts"], c["boosts"])
            old["profiled"] = old["profiled"] or c["profiled"]
            old["socials"]  = old["socials"] or c["socials"]
            old["src"]     += "+" + c["src"]
        else:
            cands[key] = c

    # ── GeckoTerminal: brand-new pools + trending (1h) ──
    for net in NETWORKS:
        for kind, path, extra in (
                ("new",   f"/networks/{net}/new_pools",      {}),
                ("trend", f"/networks/{net}/trending_pools", {"duration": "1h"})):
            data = http_json(f"{GT_BASE}{path}", {"page": "1", "include": "base_token", **extra})
            time.sleep(1.2)
            if not data:
                continue
            inc  = data.get("included") or []
            tmap = {i.get("id", ""): (i.get("attributes") or {})
                    for i in inc if i.get("type") == "token"}
            for p in data.get("data") or []:
                a  = p.get("attributes") or {}
                bd = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {})
                bid  = bd.get("id") or ""
                addr = bid.split("_", 1)[-1].lower()
                if not addr:
                    continue
                tok = tmap.get(bid) or {}
                sym = (tok.get("symbol") or addr[:6]).upper()
                if sym in NATIVE:
                    continue
                tx  = (a.get("transactions") or {}).get("h1") or {}
                vol = a.get("volume_usd") or {}
                pc  = a.get("price_change_percentage") or {}
                age_h = 999.0
                try:
                    created = (a.get("pool_created_at") or "").replace("Z", "+00:00")
                    age_h = (time.time() - datetime.fromisoformat(created).timestamp()) / 3600
                except Exception:
                    pass
                upsert({
                    "net": net, "token": addr,
                    "symbol": sym, "name": tok.get("name") or sym,
                    "pool": a.get("address", ""),
                    "liq": fnum(a.get("reserve_in_usd")),
                    "fdv": fnum(a.get("fdv_usd")) or fnum(a.get("market_cap_usd")),
                    "vol1h": fnum(vol.get("h1")), "vol6h": fnum(vol.get("h6")),
                    "vol24h": fnum(vol.get("h24")),
                    "chg_m5": fnum(pc.get("m5")), "chg_h1": fnum(pc.get("h1")),
                    "chg_h6": fnum(pc.get("h6")), "chg_h24": fnum(pc.get("h24")),
                    "buys1h": int(fnum(tx.get("buys"))), "sells1h": int(fnum(tx.get("sells"))),
                    "buyers1h": int(fnum(tx.get("buyers"))),
                    "txns1h": int(fnum(tx.get("buys"))) + int(fnum(tx.get("sells"))),
                    "age_h": age_h, "boosts": 0, "profiled": False, "socials": False,
                    "src": kind, "sec": {}, "x": 50.0, "sec_note": "", "holders": None,
                })

    # ── DexScreener marketing feeds (paid boosts + fresh profiles) ──
    boosts, profs = {}, {}
    for ep, kind in (("token-boosts/latest/v1", "b"), ("token-boosts/top/v1", "b"),
                     ("token-profiles/latest/v1", "p"), ("token-profiles/recent-updates/v1", "p")):
        rows = http_json(f"{DS_BASE}/{ep}") or []
        time.sleep(1.0)
        for r in rows:
            ch, ta = r.get("chainId", ""), (r.get("tokenAddress") or "").lower()
            if ch not in DS_TO_GT or DS_TO_GT[ch] not in NETWORKS:
                continue
            if kind == "b":
                k = (ch, ta)
                boosts[k] = max(boosts.get(k, 0), int(fnum(r.get("totalAmount"))))
            else:
                profs[(ch, ta)] = bool(r.get("links")) or bool(r.get("icon"))

    want = {}
    for (ch, ta), amt in boosts.items():
        want.setdefault(ch, {})[ta] = max(want.get(ch, {}).get(ta, 0), amt)
    for (ch, ta) in profs:
        want.setdefault(ch, {}).setdefault(ta, 0)

    for ch, addrs in want.items():
        lst = list(addrs.items())
        for i in range(0, len(lst), 25):
            chunk_items = lst[i:i + 25]
            chunk = ",".join(ta for ta, _ in chunk_items)
            data = http_json(f"{DS_BASE}/latest/dex/tokens/{chunk}") or {}
            time.sleep(0.8)
            wanted = {ta for ta, _ in chunk_items}
            best = {}
            for pr in data.get("pairs") or []:
                ta = ((pr.get("baseToken") or {}).get("address") or "").lower()
                if ta not in wanted:
                    continue
                liq = fnum((pr.get("liquidity") or {}).get("usd"))
                if ta not in best or liq > best[ta][0]:
                    best[ta] = (liq, pr)
            for ta, (liq, pr) in best.items():
                net = DS_TO_GT[ch]
                tx1 = (pr.get("txns") or {}).get("h1") or {}
                vol, pc = pr.get("volume") or {}, pr.get("priceChange") or {}
                created = pr.get("pairCreatedAt")
                age_h = (time.time() - created / 1000) / 3600 if created else 999.0
                info = pr.get("info") or {}
                upsert({
                    "net": net, "token": ta,
                    "symbol": ((pr.get("baseToken") or {}).get("symbol") or ta[:6]).upper(),
                    "name": (pr.get("baseToken") or {}).get("name") or ta[:6],
                    "pool": pr.get("pairAddress", ""), "liq": liq,
                    "fdv": fnum(pr.get("fdv")) or fnum(pr.get("marketCap")),
                    "vol1h": fnum(vol.get("h1")), "vol6h": fnum(vol.get("h6")),
                    "vol24h": fnum(vol.get("h24")),
                    "chg_m5": fnum(pc.get("m5")), "chg_h1": fnum(pc.get("h1")),
                    "chg_h6": fnum(pc.get("h6")), "chg_h24": fnum(pc.get("h24")),
                    "buys1h": int(fnum(tx1.get("buys"))), "sells1h": int(fnum(tx1.get("sells"))),
                    "buyers1h": 0,
                    "txns1h": int(fnum(tx1.get("buys"))) + int(fnum(tx1.get("sells"))),
                    "age_h": age_h,
                    "boosts": max(int(fnum((pr.get("boosts") or {}).get("active"))),
                                  want[ch].get(ta, 0)),
                    "profiled": profs.get((ch, ta), False),
                    "socials": bool(info.get("socials")) or bool(info.get("websites")),
                    "src": "ds", "sec": {}, "x": 50.0, "sec_note": "", "holders": None,
                })
    return list(cands.values())

# ── [3] SAFETY (GoPlus for EVM chains, RugCheck for Solana) ─────────────────
def check_goplus(cands):
    by_net = {}
    for c in cands:
        if c["net"] in GOPLUS_CH:
            by_net.setdefault(c["net"], []).append(c)
    for net, group in by_net.items():
        for i in range(0, len(group), 20):
            chunk = group[i:i + 20]
            params = {"contract_addresses": ",".join(c["token"] for c in chunk)}
            if GOPLUS_KEY:
                params["api_key"] = GOPLUS_KEY
            url = f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_CH[net]}"
            data = http_json(url, params) or {}
            time.sleep(1.0)
            res = data.get("result") or {}
            for c in chunk:
                g = res.get(c["token"]) or {}
                c["sec"] = g
                note, pen = [], 0.0
                if fnum(g.get("buy_tax")) > 0.15 or fnum(g.get("sell_tax")) > 0.15:
                    c["sec"]["drop"] = "tax>15%"
                if g.get("is_honeypot") == "1":
                    c["sec"]["drop"] = "honeypot"
                if g.get("transfer_pausable") == "1":
                    c["sec"]["drop"] = "pausable"
                if g.get("is_mintable") == "1":
                    note.append("mintable");     pen -= 35
                if g.get("hidden_owner") == "1":
                    note.append("hidden-owner"); pen -= 30
                if g.get("trading_cooldown") == "1":
                    note.append("cooldown");     pen -= 25
                if g.get("slippage_modifiable") == "1":
                    note.append("slip-mod");     pen -= 25
                if g.get("is_airdrop_scam") == "1":
                    note.append("airdrop-scam"); pen -= 50
                if int(fnum(g.get("honeypot_with_same_creator"))) > 0:
                    note.append("hp-creator");   pen -= 40
                hc = int(fnum(g.get("holder_count")))
                if hc:
                    c["holders"] = hc
                c["x"] = clamp(60 + pen)
                c["sec_note"] = ",".join(note) or "GoPlus clean"

def check_rugcheck(cands):
    sol = [c for c in cands if c["net"] == "solana" and not c["sec"].get("drop")]
    sol.sort(key=lambda c: -c["vol1h"])
    for c in sol[:RUGCHECK_MAX]:
        r = http_json(f"https://api.rugcheck.xyz/v1/tokens/{c['token']}/report/summary") or {}
        time.sleep(0.5)
        levels = [(x.get("level") or "").lower() for x in (r.get("risks") or [])]
        if "danger" in levels:
            c["sec"]["drop"] = "rugcheck-danger"
        if r.get("mintAuthority") or r.get("freezeAuthority"):
            c["sec"]["drop"] = "mint/freeze-authority"
        warns = sum(1 for lv in levels if lv in ("warn", "warning"))
        sn = fnum(r.get("score_normalised"), 50)
        lp = fnum(r.get("lpLockedPct"))
        top = fnum(r.get("topHoldersPct"), 100)
        x = 60.0 + clamp((35 - sn) / 35 * 30) - min(warns, 5) * 8
        note = f"RugCheck sn={sn:.0f}"
        if lp >= 95:
            x += 15
            note += f" | LP locked {lp:.0f}%"
        if top <= 30:
            x += 10
        c["x"] = clamp(x)
        c["sec_note"] = c["sec_note"] or note

# ── [4] SCORING ─────────────────────────────────────────────────────────────
def bell(x, lo, hi, p1, p2):
    if x <= lo or x >= hi:
        return 0.0
    if p1 <= x <= p2:
        return 100.0
    return clamp(min((x - lo) / (p1 - lo), (hi - x) / (hi - p2)) * 100)

def score(c, prev):
    r = c["vol1h"] / max(c["liq"], 1.0)
    V = (clamp(r / 0.02 * 30) if r < 0.02 else
         clamp(30 + (r - 0.02) / 0.13 * 50) if r < 0.15 else
         100 if r <= 0.60 else
         clamp(100 - (r - 0.6) / 1.4 * 60) if r <= 2.0 else clamp(40 - (r - 2) * 10, 5))
    b, s = c["buys1h"], c["sells1h"]
    B = clamp(((b / (b + s)) - 0.45) / 0.35 * 100) if (b + s) > 0 else 0.0
    T = clamp(((c["vol1h"] * 6) / c["vol6h"] - 0.7) / 1.6 * 100) if c["vol6h"] > 0 \
        else (60 if c["vol1h"] > 0 else 0)
    A = 0.7 * bell(c["chg_h1"], -5, 90, 4, 25) + 0.3 * bell(c["chg_m5"], -3, 25, 0.5, 8)
    mc = c["fdv"]
    M1 = clamp((math.log10(10_000_000) - math.log10(max(mc, 1))) /
               (math.log10(10_000_000) - math.log10(150_000)) * 100) if mc > 0 else 30
    M2 = min(clamp(c["liq"] / mc / 0.15 * 100), 100) if mc > 0 else 0
    M = 0.6 * M1 + 0.4 * M2
    L = clamp((c["liq"] - 8000) / 60000 * 100)
    bb = c["boosts"]
    S = clamp((25 if bb >= 1 else 0) + (20 if bb >= 10 else 0) + (25 if bb >= 50 else 0)
              + (15 if c["profiled"] else 0) + (15 if c["socials"] else 0))
    X = c["x"]
    H = 50.0
    if c["holders"] and prev and prev.get("holders") and prev.get("holders_ts"):
        dt = (time.time() - prev["holders_ts"]) / 3600
        if dt >= 1.5 and prev["holders"] > 0:
            g = (c["holders"] - prev["holders"]) / prev["holders"] * 100 / dt
            H = clamp(50 + g * 20)
    return {"V": V, "B": B, "T": T, "A": A, "M": M, "L": L, "S": S, "X": X, "H": H}

W = {"V": .18, "B": .14, "T": .12, "A": .11, "M": .11, "L": .05, "S": .11, "X": .08, "H": .10}

# ── ALERTS ──────────────────────────────────────────────────────────────────
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        print("\n" + text + "\n")
        return True
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text,
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as e:
        print(f"[warn] telegram: {e}")
        return False

def usd(x):
    x = fnum(x)
    if x >= 1e6:
        return f"${x/1e6:.1f}M"
    if x >= 1e3:
        return f"${x/1e3:.1f}K"
    return f"${x:.0f}"

def build_alert(c, total):
    bp = c["buys1h"] / max(c["buys1h"] + c["sells1h"], 1) * 100
    ds = f"https://dexscreener.com/{GT_TO_DS[c['net']]}/{c['pool'] or c['token']}"
    e, lvl = ("🚀", "سیگنال قوی") if total >= 85 else ("🔥", "ورود زودهنگام")
    return (f"{e} <b>PUMP HUNTER — {lvl} ({total:.0f}/100)</b>\n\n"
            f"🪙 <b>{html.escape(c['symbol'])}</b> | {c['net'].upper()} | سن {c['age_h']:.1f}h\n"
            f"💵 FDV {usd(c['fdv'])} | 💧 نقدینگی {usd(c['liq'])}\n"
            f"📊 حجم 1h {usd(c['vol1h'])} / 24h {usd(c['vol24h'])} | "
            f"⚡ V={c['vol1h']/max(c['liq'],1):.2f}\n"
            f"🛒 فشار خرید 1h: {bp:.0f}% ({c['buys1h']}/{c['buys1h']+c['sells1h']})\n"
            f"📈 1h {c['chg_h1']:+.1f}% | 5m {c['chg_m5']:+.1f}%\n"
            f"📣 بوست: {c['boosts']}"
            f"{' | پروفایل ✅' if c['profiled'] else ''}"
            f"{' | سوشال ✅' if c['socials'] else ''}\n"
            f"🛡 {html.escape(c['sec_note'] or 'n/a')}\n"
            f"🧮 V{c['sc']['V']:.0f} B{c['sc']['B']:.0f} T{c['sc']['T']:.0f} "
            f"A{c['sc']['A']:.0f} M{c['sc']['M']:.0f} S{c['sc']['S']:.0f} "
            f"X{c['sc']['X']:.0f} H{c['sc']['H']:.0f}\n"
            f"🔗 {ds}")

# ── MAIN ────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    st = load_state()
    now = int(time.time())

    # پیام خوش‌آمد فقط بار اول — برای اینکه مطمئن شوی اتصال تلگرام درست است
    if not st.get("installed"):
        if tg_send("✅ <b>PUMP HUNTER فعال شد!</b> 🏹\n\nاز این لحظه هر ۳۰ دقیقه بازار را "
                   "اسکن می‌کنم. سیگنال‌های قوی (75+) همین‌جا می‌آیند.\n"
                   "نکته: اولین سیگنال ممکن است چند ساعت طول بکشد — بازار باید شرایطش را بسازد."):
            st["installed"] = True
            save_state(st)

    print(f"🏹 hunting on {NETWORKS} ...")
    cands = discover()
    print(f"discovered: {len(cands)}")

    stats = {}
    def drop(c, why):
        stats[why] = stats.get(why, 0) + 1

    alive = []
    for c in cands:
        if not c["token"]:
            drop(c, "no-token"); continue
        if JUNK.search(c["symbol"]):
            drop(c, "spam-symbol"); continue
        if c["liq"] < MIN_LIQ:
            drop(c, "liq<min"); continue
        if c["vol24h"] < MIN_VOL24:
            drop(c, "vol24<min"); continue
        if c["txns1h"] < MIN_TXN1H:
            drop(c, "txns1h<min"); continue
        if c["buyers1h"] and c["buyers1h"] < MIN_BUYERS1H:
            drop(c, "buyers<min"); continue
        if not (MIN_FDV <= c["fdv"] <= MAX_FDV):
            drop(c, "fdv-band"); continue
        if c["fdv"] > 0 and c["liq"] / c["fdv"] < MIN_LIQ_MCAP:
            drop(c, "liq/mcap"); continue
        if c["age_h"] > MAX_AGE_H:
            drop(c, "too-old"); continue
        if c["chg_h6"] > EXCL_H6 or c["chg_h24"] > EXCL_H24:
            drop(c, "already-pumped"); continue
        alive.append(c)
    print(f"after hard gates: {len(alive)} | dropped: {stats}")

    check_goplus(alive)
    alive = [c for c in alive if not c["sec"].get("drop")]
    check_rugcheck(alive)
    alive = [c for c in alive if not c["sec"].get("drop")]
    print(f"after safety: {len(alive)}")

    for c in alive:
        key = f'{c["net"]}:{c["token"]}'
        prev = st["tokens"].get(key, {})
        sc = score(c, prev)
        c["score"] = sum(W[k] * v for k, v in sc.items())
        c["sc"], c["key"] = sc, key
        ent = prev or {"first_seen": now}
        ent.update({"last_seen": now, "best": max(ent.get("best", 0), c["score"])})
        if c["holders"]:
            ent["holders"], ent["holders_ts"] = c["holders"], now
        st["tokens"][key] = ent

    alive.sort(key=lambda c: -c["score"])
    print("\n═══ TOP CANDIDATES ═══")
    for c in alive[:12]:
        bp = c["buys1h"] / max(c["buys1h"] + c["sells1h"], 1) * 100
        print(f'{c["score"]:5.1f} {c["net"]:6} {c["symbol"][:10]:10} '
              f'liq={usd(c["liq"]):>8} fdv={usd(c["fdv"]):>8} '
              f'vol1h={usd(c["vol1h"]):>8} bp={bp:3.0f}% '
              f'1h={c["chg_h1"]:+6.1f}% age={c["age_h"]:5.1f}h boost={c["boosts"]}')

    alerts, log_rows = [], []
    for c in alive:
        if c["score"] < ALERT_MIN:
            continue
        prev = st["tokens"][c["key"]]
        if ((now - prev.get("last_alert", 0)) >= COOLDOWN_H * 3600
                or (c["score"] - prev.get("best_at_alert", 0)) >= RE_JUMP):
            if len(alerts) < MAX_ALERTS:
                alerts.append(c)
                log_rows.append({"ts": now, "score": round(c["score"], 1),
                                 "net": c["net"], "symbol": c["symbol"],
                                 "token": c["token"], "liq": c["liq"], "fdv": c["fdv"]})
                st["tokens"][c["key"]]["last_alert"] = now
                st["tokens"][c["key"]]["best_at_alert"] = c["score"]

    for c in alerts:
        tg_send(build_alert(c, c["score"]))
        time.sleep(1.0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    watch = [c for c in alive if WATCH_MIN <= c["score"] < ALERT_MIN][:8]
    if watch and st.get("last_digest") != today:
        tg_send("👀 <b>واچ‌لیست امروز — " + today + "</b>\n\n" + "\n".join(
            f'• {html.escape(c["symbol"])} ({c["net"]}) — {c["score"]:.0f}/100 | '
            f'liq {usd(c["liq"])} | 1h {c["chg_h1"]:+.1f}%' for c in watch))
        st["last_digest"] = today

    append_log(log_rows)
    save_state(st)
    print(f"\ndone in {time.time()-t0:.0f}s | alerts: {len(alerts)}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

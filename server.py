"""
Meme Scanner Web Server
FastAPI backend that runs all scanner logic and streams live data via WebSockets.
"""

import asyncio
import json
import os
import time
import urllib.request
import urllib.parse
import websockets
from collections import deque
from datetime import datetime
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

MC_LIMIT_USD    = 50_000        # pre-graduation MC cap for buy signals
MC_GRAD_LIMIT   = 250_000       # post-migration cap — graduated tokens can run higher
MC_MIN_USD      = 6_000         # ignore anything below $6k
NEAR_GRAD_PCT   = 65.0          # curve % threshold to count as "near graduation"
GRAD_VSOL       = 85.0
MAX_TRACK_PP    = 120           # track more pump.fun tokens (catches near-grad ones)
GT_POLL_SEC     = 20
SOL_REFRESH_SEC = 60
MAX_DISPLAY     = 60
MC_HISTORY_LEN  = 6

ALERT_SCORE     = int(os.getenv("ALERT_SCORE", "72"))
BUY_SCORE_MIN   = 72
STRONG_BUY_MIN  = 84

# Buy signal hard limits
MIN_BUYS_TOTAL    = 10
MIN_BUY_RATIO     = 0.55
MAX_TOP10_PCT     = 50
MAX_CREATOR_SELLS = 3
BUNDLE_WINDOW_SEC = 15
BUNDLE_WALLET_MIN = 3
MIN_LIQ_USD       = 1_000
MIN_AGE_PP_SEC    = 3 * 60      # pump.fun: skip first 3 min (bot dump zone)
MAX_AGE_PP_SEC    = 45 * 60     # pump.fun: up to 45 min (was 20) — catches slow burners
MAX_AGE_GT_SEC    = 12 * 3600   # gecko/migrated tokens: up to 12h post-migration

TG_TOKEN   = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

_HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")
SOLANA_RPC  = (
    f"https://rpc.helius.xyz/?api-key={_HELIUS_KEY}"
    if _HELIUS_KEY else
    "https://api.mainnet-beta.solana.com"
)

TARGET_DEXES = {
    # Bonding curve launchpads
    "pump-fun":          "Pump.fun",
    "pumpswap":          "PumpSwap",       # graduated pump.fun
    "moonit":            "Moonshot",
    "raydium-launchlab": "LaunchLab",
    "meteora-dbc":       "Meteora DBC",    # Bags, Believe, Bonkers, Dynamic BC
    "meteora-damm-v2":   "Meteora v2",
    "meteora":           "Meteora",        # Believe graduated tokens
    "boop-fun":          "Boop.fun",
    "token-mill":        "TokenMill",
    "heaven":            "Heaven",
    "daos-fun":          "Daos.fun",
    "virtuals-solana":   "Virtuals",
    "zora":              "Zora",
    "wavebreak":         "Wavebreak",
    "humidifi":          "Humidifi",
    "byreal":            "Byreal",
}

CREATOR_BLACKLIST: set = set(filter(None, os.getenv("CREATOR_BLACKLIST", "").split(",")))
KEYWORD_BLACKLIST = {"test", "rug", "scam", "honeypot", "fake", "ponzi"}
WATCHLIST: set = set(filter(None, os.getenv("WATCHLIST", "").split(",")))

# ── State ─────────────────────────────────────────────────────────────────────

sol_price_usd  = 88.0
tokens: dict   = {}
total_seen     = 0
stats          = {"pump": 0, "other": 0, "errors": 0}
buy_signals    = deque(maxlen=20)
alerted_mints: set = set()
holder_cache: dict = {}
mc_history: dict   = {}

# WebSocket clients
connected_clients: Set[WebSocket] = set()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI()

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 8) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        stats["errors"] += 1
        return None


def fetch_post(url: str, data: dict, timeout: int = 8) -> Optional[dict]:
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        stats["errors"] += 1
        return None


# ── SOL price ─────────────────────────────────────────────────────────────────

async def refresh_sol_price():
    global sol_price_usd
    while True:
        data = fetch("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
        if data:
            sol_price_usd = data.get("solana", {}).get("usd", sol_price_usd)
        await asyncio.sleep(SOL_REFRESH_SEC)


# ── Token constructors ────────────────────────────────────────────────────────

def _is_blacklisted(name: str, symbol: str, creator: str) -> bool:
    combined = (name + " " + symbol).lower()
    if any(kw in combined for kw in KEYWORD_BLACKLIST):
        return True
    if creator and creator in CREATOR_BLACKLIST:
        return True
    return False


def new_pumpfun_token(d: dict) -> dict:
    return {
        "id":             d["mint"],
        "mint":           d["mint"],
        "platform":       "Pump.fun",
        "symbol":         d.get("symbol", "?")[:12],
        "name":           d.get("name", "?")[:20],
        "creator":        d.get("traderPublicKey", ""),
        "init_sol":       float(d.get("solAmount", 0)),
        "mc_sol":         float(d.get("marketCapSol", 28)),
        "v_sol":          float(d.get("vSolInBondingCurve", 30)),
        "peak_mc_sol":    float(d.get("marketCapSol", 28)),
        "first_seen_mc":  float(d.get("marketCapSol", 28)) * sol_price_usd,
        "buys":           0,
        "sells":          0,
        "buy_sol":        0.0,
        "sell_sol":       0.0,
        "traders":        set(),
        "has_twitter":    False,
        "has_telegram":   False,
        "created_at":     datetime.now(),
        "last_trade":     None,
        "source":         "websocket",
        "dead":           False,
        "graduated":      False,
        "creator_sells":  0,
        "top10_pct":      None,
        "holder_checked": False,
        "prev_score":     None,
        "mc_trend":       0,
        "liq_usd":        0.0,
        # Bundler detection: tracked in real-time via WS, then confirmed on-chain
        "early_buys":     [],
        "bundled":        False,
        "bundle_wallets": 0,
        "bundle_checked": False,
    }


def new_gt_token(pool: dict, dex_name: str, token_attrs: dict) -> dict:
    attrs = pool.get("attributes", {})
    mc = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
    try:
        created_at = datetime.fromisoformat(
            attrs.get("pool_created_at", "").replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except Exception:
        created_at = datetime.now()

    pc   = attrs.get("price_change_percentage", {})
    txns = attrs.get("transactions", {})
    vol  = attrs.get("volume_usd", {})

    buys_h1  = int((txns.get("h1") or {}).get("buys",  0))
    sells_h1 = int((txns.get("h1") or {}).get("sells", 0))
    vol_h1   = float(vol.get("h1") or 0)
    liq      = float(attrs.get("reserve_in_usd") or 0)

    name = token_attrs.get("name", attrs.get("name", "?"))
    sym  = token_attrs.get("symbol", "?")

    has_twitter  = bool(token_attrs.get("twitter_handle"))
    has_telegram = bool(token_attrs.get("telegram_handle"))

    return {
        "id":             pool["id"],
        "mint":           "",
        "platform":       dex_name,
        "symbol":         sym[:12],
        "name":           name[:20],
        "creator":        "",
        "init_sol":       0.0,
        "mc_usd":         mc,
        "mc_sol":         mc / sol_price_usd if sol_price_usd > 0 else 0,
        "peak_mc_usd":    mc,
        "first_seen_mc":  mc,
        "v_sol":          0.0,
        "buys":           buys_h1,
        "sells":          sells_h1,
        "buy_sol":        vol_h1 / sol_price_usd if sol_price_usd > 0 else 0,
        "sell_sol":       0.0,
        "liq_usd":        liq,
        "traders":        set(),
        "has_twitter":    has_twitter,
        "has_telegram":   has_telegram,
        "created_at":     created_at,
        "last_trade":     datetime.now(),
        "source":         "gecko",
        "dead":           False,
        "graduated":      dex_name in ("PumpSwap", "Raydium", "Meteora"),
        "pc_m5":          float(pc.get("m5") or 0),
        "pc_h1":          float(pc.get("h1") or 0),
        "creator_sells":  0,
        "top10_pct":      None,
        "holder_checked": False,
        "prev_score":     None,
        "mc_trend":       0,
        "early_buys":     [],
        "bundled":        False,
        "bundle_wallets": 0,
        "bundle_checked": False,
    }


# ── Trade update ──────────────────────────────────────────────────────────────

def apply_trade(mint: str, d: dict):
    t = tokens.get(mint)
    if not t:
        return
    tx     = d.get("txType", "")
    sol    = float(d.get("solAmount", 0))
    mc     = float(d.get("marketCapSol", t["mc_sol"]))
    vsol   = float(d.get("vSolInBondingCurve", t.get("v_sol", 30)))
    trader = d.get("traderPublicKey", "")

    t["mc_sol"]     = mc
    t["v_sol"]      = vsol
    t["last_trade"] = datetime.now()
    t["peak_mc_sol"] = max(t.get("peak_mc_sol", mc), mc)

    if tx == "buy":
        t["buys"]    += 1
        t["buy_sol"] += sol
        if trader:
            t["traders"].add(trader)
        # Bundler detection: record buys within launch window
        age_sec = (datetime.now() - t["created_at"]).total_seconds()
        if age_sec <= BUNDLE_WINDOW_SEC and trader:
            early = t.setdefault("early_buys", [])
            early.append((time.time(), trader))
            unique_early = len({w for _, w in early})
            t["bundle_wallets"] = unique_early
            if unique_early >= BUNDLE_WALLET_MIN:
                t["bundled"] = True
    elif tx == "sell":
        t["sells"]    += 1
        t["sell_sol"] += sol
        if trader and trader == t.get("creator"):
            t["creator_sells"] += 1

    if vsol >= GRAD_VSOL:
        t["graduated"] = True

    peak = t.get("peak_mc_sol", mc)
    if peak > 0 and mc < peak * 0.40:
        t["dead"] = True

    _update_mc_trend(t)


def _update_mc_trend(t: dict):
    tid  = t["id"]
    mc   = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    hist = mc_history.setdefault(tid, deque(maxlen=MC_HISTORY_LEN))
    hist.append((time.time(), mc))
    if len(hist) >= 3:
        recent = list(hist)[-3:]
        first, last = recent[0][1], recent[-1][1]
        if last > first * 1.05:
            t["mc_trend"] = 1
        elif last < first * 0.95:
            t["mc_trend"] = -1
        else:
            t["mc_trend"] = 0


# ── On-chain: Holder concentration ───────────────────────────────────────────

async def fetch_holder_concentration(mint: str) -> Optional[float]:
    if not mint or len(mint) < 32:
        return None

    resp = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fetch_post(SOLANA_RPC, {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint, {"commitment": "confirmed"}],
        }, timeout=6),
    )
    if not resp or "result" not in resp:
        return None
    accounts = resp["result"].get("value", [])

    supply_resp = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fetch_post(SOLANA_RPC, {
            "jsonrpc": "2.0", "id": 2,
            "method": "getTokenSupply",
            "params": [mint],
        }, timeout=6),
    )
    if not supply_resp or "result" not in supply_resp:
        return None
    supply_info = supply_resp["result"].get("value", {})
    total = float(supply_info.get("uiAmount") or 0)
    if total <= 0:
        return None

    top10 = sum(float(a.get("uiAmount") or 0) for a in accounts[:10])
    return round((top10 / total) * 100, 1)


async def detect_bundle_onchain(address: str) -> tuple:
    """
    Fetch first ~50 signatures for a mint/pool address and count how many
    transactions landed within BUNDLE_WINDOW_SEC of the earliest one.
    Returns (bundled: bool, wallet_count: int).
    Works for any platform — pump.fun, Moonshot, Meteora, etc.
    """
    if not address or len(address) < 32:
        return False, 0

    resp = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fetch_post(SOLANA_RPC, {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 50, "commitment": "confirmed"}],
        }, timeout=10),
    )
    if not resp or "result" not in resp:
        return False, 0

    sigs = resp["result"]
    if not sigs:
        return False, 0

    # Signatures come newest-first; for a new token the last entries are launch txns
    block_times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
    if not block_times:
        return False, 0

    earliest = min(block_times)
    # Count how many txns landed within the bundle window of the very first one
    early_count = sum(1 for bt in block_times if bt <= earliest + BUNDLE_WINDOW_SEC)

    bundled = early_count >= BUNDLE_WALLET_MIN
    return bundled, early_count


async def enrich_holders_loop():
    while True:
        try:
            for t in list(tokens.values()):
                mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
                if t.get("dead") or mc_usd > MC_LIMIT_USD * 1.5:
                    t["holder_checked"] = True
                    t["bundle_checked"] = True
                    continue

                # ── Holder concentration ──────────────────────────────────────
                if not t.get("holder_checked"):
                    mint = t.get("mint", "")
                    if not mint:
                        t["holder_checked"] = True
                    else:
                        pct = await fetch_holder_concentration(mint)
                        t["top10_pct"]      = pct
                        t["holder_checked"] = True
                        await asyncio.sleep(0.4)

                # ── Bundle detection (all platforms via on-chain RPC) ─────────
                if not t.get("bundle_checked"):
                    # For pump.fun we already track in real-time via WS,
                    # but re-check on-chain if the window has passed to catch
                    # cases we missed. For all GT platforms, this is the only check.
                    address = t.get("mint") or ""
                    # GT pool address lives in id as "solana_ADDR" — extract it
                    if not address and "_" in t.get("id", ""):
                        parts = t["id"].split("_")
                        if len(parts) >= 2 and len(parts[1]) > 30:
                            address = parts[1]

                    if not address:
                        t["bundle_checked"] = True
                    else:
                        bundled, count = await detect_bundle_onchain(address)
                        # Don't overwrite a more severe real-time result
                        if count > t.get("bundle_wallets", 0):
                            t["bundle_wallets"] = count
                        if bundled:
                            t["bundled"] = True
                        t["bundle_checked"] = True
                        await asyncio.sleep(0.4)

        except Exception:
            stats["errors"] += 1
        await asyncio.sleep(10)


# ── Scoring ───────────────────────────────────────────────────────────────────

def score(t: dict) -> int:
    s   = {}
    src = t.get("source", "websocket")

    age_sec = max(1, (datetime.now() - t["created_at"]).total_seconds())
    buys    = t.get("buys", 0)
    bpm     = buys / (age_sec / 60)

    if   bpm >= 10: s["vel"] = 100
    elif bpm >= 5:  s["vel"] = 82
    elif bpm >= 2:  s["vel"] = 62
    elif bpm >= 0.5:s["vel"] = 38
    else:           s["vel"] = 12

    if src == "websocket" and age_sec <= 300:
        v_sol     = t.get("v_sol", 30)
        curve_pct = max(0, (v_sol - 28) / (GRAD_VSOL - 28))
        if curve_pct >= 0.20:
            s["vel"] = min(100, s["vel"] + 15)

    total = t.get("buys", 0) + t.get("sells", 0)
    if total >= 3:
        ratio = t["buys"] / total
        if   ratio >= 0.75: s["pressure"] = 100
        elif ratio >= 0.62: s["pressure"] = 82
        elif ratio >= 0.50: s["pressure"] = 55
        elif ratio >= 0.40: s["pressure"] = 28
        else:               s["pressure"] = 8
    else:
        s["pressure"] = 50

    ib = t.get("init_sol", 0)
    if src == "websocket":
        if   5  > ib >= 2:   s["init"] = 100
        elif 2  > ib >= 1:   s["init"] = 82
        elif 10 > ib >= 5:   s["init"] = 70
        elif 1  > ib >= 0.3: s["init"] = 45
        elif ib >= 10:       s["init"] = 40
        else:                s["init"] = 12
    else:
        s["init"] = 55

    ut = len(t.get("traders", set()))
    if   ut >= 50: s["traders"] = 100
    elif ut >= 20: s["traders"] = 82
    elif ut >= 10: s["traders"] = 65
    elif ut >= 5:  s["traders"] = 45
    elif ut >= 2:  s["traders"] = 28
    else:          s["traders"] = 15

    s["social"] = (55 if t.get("has_twitter") else 0) + (45 if t.get("has_telegram") else 0)

    if src == "websocket":
        mc_now  = t.get("mc_sol", 28)
        mc_init = max(28, t.get("init_sol", 0) + 28)
        gain    = (mc_now - mc_init) / mc_init if mc_init > 0 else 0
    else:
        gain = t.get("pc_h1", 0) / 100

    if   gain >= 2.0: s["mom"] = 100
    elif gain >= 1.0: s["mom"] = 85
    elif gain >= 0.5: s["mom"] = 68
    elif gain >= 0.1: s["mom"] = 45
    elif gain >= 0:   s["mom"] = 30
    else:             s["mom"] = 10

    final = round(
        s["vel"]      * 0.25 +
        s["pressure"] * 0.25 +
        s["init"]     * 0.20 +
        s["traders"]  * 0.10 +
        s["social"]   * 0.10 +
        s["mom"]      * 0.10
    )

    if t.get("graduated"):               final = min(100, final + 8)
    if t.get("dead"):                    final = max(0,   final - 45)
    if t.get("creator_sells", 0) >= 3:   final = max(0,   final - 30)  # heavy selling by dev
    elif t.get("creator_sells", 0) >= 1: final = max(0,   final - 10)  # light selling — small penalty
    if t.get("bundled"):
        bw = t.get("bundle_wallets", 0)
        penalty = min(50, 20 + bw * 3)   # scales with number of bundle wallets
        final = max(0, final - penalty)

    top10 = t.get("top10_pct")
    if top10 is not None:
        if top10 >= 80:   final = max(0, final - 40)   # extreme concentration = almost certain rug
        elif top10 >= 60: final = max(0, final - 25)
        elif top10 >= 50: final = max(0, final - 10)

    # MC too low penalty (< $6k = bot zone, high rug risk)
    mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    if mc_usd < MC_MIN_USD:
        final = max(0, final - 30)

    if src == "websocket" and age_sec > 600:
        v_sol     = t.get("v_sol", 30)
        curve_pct = (v_sol - 28) / (GRAD_VSOL - 28)
        if curve_pct < 0.10:
            final = max(0, final - 30)

    return max(0, min(100, final))


def score_breakdown(t: dict) -> dict:
    src     = t.get("source", "websocket")
    age_sec = max(1, (datetime.now() - t["created_at"]).total_seconds())
    buys    = t.get("buys", 0)
    bpm     = buys / (age_sec / 60)
    total   = buys + t.get("sells", 0)
    ratio   = buys / total if total >= 3 else 0.5
    return {
        "bpm":       round(bpm, 1),
        "pressure":  f"{ratio*100:.0f}%",
        "init_sol":  t.get("init_sol", 0),
        "traders":   len(t.get("traders", set())),
        "top10_pct": t.get("top10_pct"),
        "dev_sells": t.get("creator_sells", 0),
        "age_s":     int(age_sec),
    }


# ── BUY Signal engine ─────────────────────────────────────────────────────────

def buy_signal(t: dict) -> Optional[str]:
    """
    Three-stage signal engine:
      NEAR GRAD  — pump.fun token filling fast, almost at 85 SOL graduation
      STRONG BUY — pre-graduation token with great fundamentals
      BUY        — solid setup at any stage (pre-grad, near-grad, or migrated)
      MIGRATED   — recently graduated/migrated token with post-migration momentum
    """
    if t.get("dead"):
        return None

    sc      = score(t)
    src     = t.get("source", "websocket")
    age_sec = max(1, (datetime.now() - t["created_at"]).total_seconds())
    buys    = t.get("buys", 0)
    sells   = t.get("sells", 0)
    total   = buys + sells
    bpm     = buys / (age_sec / 60)
    ratio   = buys / total if total > 0 else 0.0
    mc_usd  = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    top10   = t.get("top10_pct")
    liq     = t.get("liq_usd", 0)
    graduated = t.get("graduated", False)

    # Curve % for pump.fun tokens
    v_sol     = t.get("v_sol", 30)
    curve_pct = min(100.0, max(0.0, (v_sol - 28) / (GRAD_VSOL - 28) * 100)) if src == "websocket" else 0.0

    # ── Universal hard disqualifiers ──────────────────────────────────────────
    if mc_usd < MC_MIN_USD:
        return None
    if t.get("creator_sells", 0) >= MAX_CREATOR_SELLS:
        return None
    if t.get("bundled") and t.get("bundle_wallets", 0) >= 5:
        return None
    if top10 is not None and top10 >= MAX_TOP10_PCT:
        return None
    if sc < BUY_SCORE_MIN:
        return None

    # ── Stage 1: NEAR GRAD — pump.fun almost full ────────────────────────────
    # Token has earned its way to near-graduation through organic buying.
    # Age doesn't matter — if it's 2h old and still filling, that's commitment.
    if src == "websocket" and curve_pct >= NEAR_GRAD_PCT:
        if total >= MIN_BUYS_TOTAL and ratio >= 0.50 and mc_usd <= MC_GRAD_LIMIT:
            if curve_pct >= 85.0 and total >= 20:
                return "STRONG BUY"   # basically about to migrate any moment
            return "NEAR GRAD"

    # ── Stage 2: MIGRATED — recently graduated token with momentum ────────────
    if graduated and mc_usd <= MC_GRAD_LIMIT:
        if age_sec > MAX_AGE_GT_SEC:
            return None
        if src == "gecko" and liq > 0 and liq < MIN_LIQ_USD * 3:
            return None  # migrated tokens need decent liquidity
        if total >= MIN_BUYS_TOTAL and ratio >= MIN_BUY_RATIO and bpm >= 1.5:
            if sc >= STRONG_BUY_MIN and ratio >= 0.65:
                return "STRONG BUY"
            return "MIGRATED"

    # ── Stage 3: Pre-graduation standard signals ──────────────────────────────
    if mc_usd > MC_LIMIT_USD:
        return None   # non-graduated token over $50k = skip

    # Age gate for pre-grad pump.fun
    if src == "websocket":
        if age_sec < MIN_AGE_PP_SEC:
            return None
        if age_sec > MAX_AGE_PP_SEC and curve_pct < NEAR_GRAD_PCT:
            return None  # old AND not near grad = stale

    if src == "gecko":
        if age_sec > MAX_AGE_GT_SEC:
            return None
        if liq > 0 and liq < MIN_LIQ_USD:
            return None

    if total < MIN_BUYS_TOTAL:
        return None
    if ratio < MIN_BUY_RATIO:
        return None

    # STRONG BUY
    if (sc >= STRONG_BUY_MIN and bpm >= 5 and ratio >= 0.68
            and total >= 20 and (top10 is None or top10 < 35)
            and MC_MIN_USD * 1.3 <= mc_usd <= MC_LIMIT_USD * 0.85):
        return "STRONG BUY"

    # BUY
    if sc >= BUY_SCORE_MIN and bpm >= 2 and ratio >= MIN_BUY_RATIO:
        return "BUY"

    return None


def build_buy_reason(t: dict) -> str:
    bd   = score_breakdown(t)
    bits = []
    if bd["bpm"] >= 5:
        bits.append(f"velocity {bd['bpm']} b/min")
    pct = float(bd["pressure"].rstrip("%"))
    if pct >= 60:
        bits.append(f"{bd['pressure']} buy pressure")
    if bd["dev_sells"] == 0:
        bits.append("dev not selling")
    if bd["top10_pct"] is not None and bd["top10_pct"] < 60:
        bits.append(f"healthy holders ({bd['top10_pct']}%)")
    if t.get("has_twitter") and t.get("has_telegram"):
        bits.append("full social")
    mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    bits.append(f"MC ${mc_usd/1000:.1f}K")
    return " · ".join(bits) if bits else "meets thresholds"


def token_url(t: dict) -> str:
    mid = t.get("id", "")
    if t["platform"] == "Pump.fun":
        return f"https://pump.fun/{mid}"
    return f"https://dexscreener.com/solana/{mid}"


# ── Alerts ────────────────────────────────────────────────────────────────────

def send_desktop_alert(t: dict, signal: str):
    try:
        sym    = t.get("symbol", "?")
        mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
        msg    = f"{signal}: ${sym}  MC ${mc_usd/1000:.1f}K  Score {score(t)}"
        os.system(
            f'osascript -e \'display notification "{msg}" '
            f'with title "Meme Scanner" sound name "Glass"\''
        )
    except Exception:
        pass


def send_telegram_alert(t: dict, signal: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        sym    = t.get("symbol", "?")
        name   = t.get("name",   "?")
        sc     = score(t)
        mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
        reason = build_buy_reason(t)
        url    = token_url(t)
        bd     = score_breakdown(t)
        emoji  = "🚨🔥" if signal == "STRONG BUY" else "🚀"
        text   = (
            f"{emoji} *{signal}* — ${sym} ({name})\n"
            f"Score: *{sc}/100*  |  MC: *${mc_usd/1000:.1f}K*\n"
            f"Platform: {t['platform']}\n"
            f"Velocity: {bd['bpm']} b/min  |  Pressure: {bd['pressure']}\n"
            f"Holders top-10: {bd['top10_pct']}%  |  Dev sells: {bd['dev_sells']}\n"
            f"Reason: {reason}\n"
            f"[Chart]({url})"
        )
        fetch_post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )
    except Exception:
        pass


async def check_and_alert(t: dict):
    tid = t["id"]
    if tid in alerted_mints:
        return
    sig = buy_signal(t)
    if not sig:
        return

    alerted_mints.add(tid)
    mc_usd  = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    reason  = build_buy_reason(t)
    buy_signals.appendleft({
        "time":     datetime.now().isoformat(),
        "symbol":   t.get("symbol", "?"),
        "platform": t.get("platform", "?"),
        "signal":   sig,
        "score":    score(t),
        "mc_usd":   mc_usd,
        "reason":   reason,
        "url":      token_url(t),
    })

    send_desktop_alert(t, sig)
    asyncio.get_event_loop().run_in_executor(None, send_telegram_alert, t, sig)


# ── Serialization ─────────────────────────────────────────────────────────────

def serialize_token(t: dict) -> dict:
    mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    age_sec = int((datetime.now() - t["created_at"]).total_seconds())

    # Capture first_seen_mc the first time we serialize above the visible threshold
    if mc_usd >= MC_MIN_USD and not t.get("first_seen_mc"):
        t["first_seen_mc"] = mc_usd
    bpm = t.get("buys", 0) / max(1, age_sec / 60)
    v_sol = t.get("v_sol", 30)
    curve_pct = 0.0
    if t.get("source") == "websocket":
        curve_pct = min(1.0, max(0, (v_sol - 28) / (GRAD_VSOL - 28))) * 100

    return {
        "id":            t["id"],
        "symbol":        t.get("symbol", "?"),
        "name":          t.get("name", "?"),
        "platform":      t.get("platform", "?"),
        "score":         score(t),
        "signal":        buy_signal(t),
        "mc_usd":        round(mc_usd, 2),
        "mc_trend":      t.get("mc_trend", 0),
        "buys":          t.get("buys", 0),
        "sells":         t.get("sells", 0),
        "bpm":           round(bpm, 1),
        "top10_pct":     t.get("top10_pct"),
        "liq_usd":       round(t.get("liq_usd", 0), 2),
        "has_twitter":   t.get("has_twitter", False),
        "has_telegram":  t.get("has_telegram", False),
        "age_s":         age_sec,
        "init_sol":      round(t.get("init_sol", 0), 3),
        "creator_sells":  t.get("creator_sells", 0),
        "graduated":      t.get("graduated", False),
        "curve_pct":      round(curve_pct, 1),
        "traders_count":  len(t.get("traders", set())),
        "url":            token_url(t),
        "dead":           t.get("dead", False),
        "source":         t.get("source", "websocket"),
        "bundled":        t.get("bundled", False),
        "bundle_wallets": t.get("bundle_wallets", 0),
        "ca":             t.get("mint") or t.get("id", ""),
        "first_seen_mc":  round(t.get("first_seen_mc", 0), 2),
    }


def build_snapshot() -> dict:
    visible = []
    for t in tokens.values():
        if t.get("dead"):
            continue
        mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
        graduated = t.get("graduated", False)
        # Pre-graduation: $6k–$50k
        # Near-graduation: any MC (pump.fun filling fast)
        # Migrated/graduated: up to $250k
        v_sol = t.get("v_sol", 30)
        curve_pct = min(100.0, max(0.0, (v_sol - 28) / (GRAD_VSOL - 28) * 100))
        near_grad = t.get("source") == "websocket" and curve_pct >= NEAR_GRAD_PCT

        in_range = (
            (MC_MIN_USD <= mc_usd <= MC_LIMIT_USD)           # normal pre-grad
            or (near_grad and mc_usd <= MC_GRAD_LIMIT)       # near graduation
            or (graduated and mc_usd <= MC_GRAD_LIMIT)       # migrated
            or t["id"] in WATCHLIST
        )
        if in_range:
            visible.append(t)

    visible.sort(key=lambda x: (
        x["id"] not in WATCHLIST,
        0 if buy_signal(x) == "STRONG BUY" else (1 if buy_signal(x) == "BUY" else 2),
        -score(x),
    ))

    return {
        "sol_price":   round(sol_price_usd, 2),
        "total_seen":  total_seen,
        "tokens":      [serialize_token(t) for t in visible[:MAX_DISPLAY]],
        "buy_signals": list(buy_signals),
        "stats":       dict(stats),
    }


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def broadcast():
    if not connected_clients:
        return
    snapshot = build_snapshot()
    msg = json.dumps(snapshot)
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


# ── GeckoTerminal poller ──────────────────────────────────────────────────────

async def poll_gecko():
    global total_seen
    while True:
        try:
            await asyncio.sleep(GT_POLL_SEC)
            data = fetch(
                "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
                "?page=1&include=dex,base_token",
            )
            if not data:
                continue

            included  = data.get("included", [])
            dex_map   = {i["id"]: i["attributes"].get("identifier", i["id"])
                         for i in included if i["type"] == "dex"}
            token_map = {i["id"]: i["attributes"]
                         for i in included if i["type"] == "token"}

            for pool in data.get("data", []):
                rels     = pool.get("relationships", {})
                dex_id   = rels.get("dex", {}).get("data", {}).get("id", "")
                if dex_id not in TARGET_DEXES:
                    continue

                dex_name = TARGET_DEXES[dex_id]
                pool_id  = pool["id"]
                token_id = rels.get("base_token", {}).get("data", {}).get("id", "")
                token_attrs = token_map.get(token_id, {})

                name = token_attrs.get("name", "")
                sym  = token_attrs.get("symbol", "")
                if _is_blacklisted(name, sym, ""):
                    continue

                mc_usd = float(
                    pool["attributes"].get("market_cap_usd") or
                    pool["attributes"].get("fdv_usd") or 0
                )
                # Skip sub-$4k and over-cap — not worth tracking
                if mc_usd < MC_MIN_USD * 0.65 or mc_usd > MC_LIMIT_USD * 2:
                    continue

                if pool_id not in tokens:
                    tok = new_gt_token(pool, dex_name, token_attrs)
                    parts = token_id.split("_")
                    if len(parts) >= 2:
                        tok["mint"] = parts[1] if len(parts[1]) > 30 else ""
                    tokens[pool_id] = tok
                    total_seen += 1
                    stats["other"] += 1
                else:
                    t = tokens[pool_id]
                    t["mc_usd"] = mc_usd
                    t["mc_sol"] = mc_usd / max(1, sol_price_usd)
                    if mc_usd > t.get("peak_mc_usd", 0):
                        t["peak_mc_usd"] = mc_usd
                    attrs  = pool["attributes"]
                    txns   = attrs.get("transactions", {})
                    t["buys"]  = int((txns.get("h1") or {}).get("buys",  t["buys"]))
                    t["sells"] = int((txns.get("h1") or {}).get("sells", t["sells"]))
                    pc = attrs.get("price_change_percentage", {})
                    t["pc_m5"] = float(pc.get("m5") or 0)
                    t["pc_h1"] = float(pc.get("h1") or 0)
                    _update_mc_trend(t)

                await check_and_alert(tokens[pool_id])

            await broadcast()

        except Exception:
            stats["errors"] += 1
            await asyncio.sleep(5)


# ── PumpPortal WebSocket ──────────────────────────────────────────────────────

async def run_pumpportal():
    global total_seen
    tracked: list = []

    async for ws in websockets.connect(
        "wss://pumpportal.fun/api/data",
        ping_interval=20, ping_timeout=10,
    ):
        try:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))

            async for raw in ws:
                msg     = json.loads(raw)
                tx_type = msg.get("txType")
                mint    = msg.get("mint")
                if not mint:
                    continue

                if tx_type == "create":
                    name    = msg.get("name", "")
                    sym     = msg.get("symbol", "")
                    creator = msg.get("traderPublicKey", "")
                    if _is_blacklisted(name, sym, creator):
                        continue

                    total_seen += 1
                    stats["pump"] += 1
                    tokens[mint] = new_pumpfun_token(msg)

                    await ws.send(json.dumps({
                        "method": "subscribeTokenTrade",
                        "keys":   [mint],
                    }))
                    tracked.append(mint)

                    if len(tracked) > MAX_TRACK_PP:
                        old = tracked.pop(0)
                        await ws.send(json.dumps({
                            "method": "unsubscribeTokenTrade",
                            "keys":   [old],
                        }))
                        t = tokens.get(old)
                        if t and (t["dead"] or t["graduated"] or
                                  (datetime.now() - t["created_at"]).total_seconds() > 900):
                            tokens.pop(old, None)
                            mc_history.pop(old, None)

                elif tx_type in ("buy", "sell"):
                    if mint in tokens:
                        apply_trade(mint, msg)
                        await check_and_alert(tokens[mint])

                await broadcast()

        except websockets.exceptions.ConnectionClosed:
            await asyncio.sleep(2)
        except Exception:
            stats["errors"] += 1
            await asyncio.sleep(2)


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        # Send initial state immediately
        await websocket.send_text(json.dumps(build_snapshot()))
        # Keep connection alive
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
    except Exception:
        connected_clients.discard(websocket)


@app.get("/api/state")
async def get_state():
    return build_snapshot()


# Serve React build — must be after API routes
FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")

if os.path.exists(FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
else:
    @app.get("/")
    async def serve_placeholder():
        return {"message": "Frontend not built yet. Run: cd frontend && npm run build"}


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_sol_price())
    asyncio.create_task(run_pumpportal())
    asyncio.create_task(poll_gecko())
    asyncio.create_task(enrich_holders_loop())


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )

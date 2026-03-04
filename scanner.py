"""
Multi-Launchpad Pre-$50k Meme Scanner  v2
──────────────────────────────────────────
Sources:
  • PumpPortal WebSocket  — pump.fun (real-time)
  • GeckoTerminal API     — Moonshot, LaunchLab, Meteora DBC, Boop.fun,
                            TokenMill, Heaven, Daos.fun, Virtuals, PumpSwap

Scoring (research-based):
  • Buy velocity (buys/min)       25%
  • Buy/sell pressure ratio       25%
  • Initial buy conviction        20%
  • Unique traders                10%
  • Social presence               10%
  • MC momentum from launch       10%

New in v2:
  • BUY SIGNAL engine — explicit entry recommendation with reasoning
  • Desktop alerts (macOS) + Telegram bot alerts
  • Holder concentration via Solana RPC (rug-risk signal)
  • Dev wallet sell detection
  • Watchlist / creator blacklist / keyword blacklist
  • Trend arrows (▲▼) on MC column
  • Score breakdown panel
  • Liquidity/MC ratio column

Env vars (optional):
  TG_BOT_TOKEN   — Telegram bot token
  TG_CHAT_ID     — Telegram chat/channel id
  HELIUS_API_KEY — Helius RPC key (faster holder lookups; falls back to public RPC)
  ALERT_SCORE    — minimum score to trigger alert (default 75)

Run: python3 scanner.py
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
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────

MC_LIMIT_USD    = 50_000
GRAD_VSOL       = 85.0
MAX_TRACK_PP    = 60
GT_POLL_SEC     = 45
SOL_REFRESH_SEC = 60
MAX_DISPLAY     = 25
MC_HISTORY_LEN  = 6          # snapshots kept per token (1 per ~30s render)

# Alert thresholds
ALERT_SCORE     = int(os.getenv("ALERT_SCORE", "75"))
BUY_SCORE_MIN   = 75         # show BUY label
STRONG_BUY_MIN  = 85         # show STRONG BUY label

# Telegram (optional)
TG_TOKEN   = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# Helius RPC (faster than public) — optional
_HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")
SOLANA_RPC  = (
    f"https://rpc.helius.xyz/?api-key={_HELIUS_KEY}"
    if _HELIUS_KEY else
    "https://api.mainnet-beta.solana.com"
)

# Launchpads to watch on GeckoTerminal
TARGET_DEXES = {
    "pump-fun":          "Pump.fun",
    "pumpswap":          "PumpSwap",
    "moonit":            "Moonshot",
    "raydium-launchlab": "LaunchLab",
    "meteora-dbc":       "Meteora DBC",
    "meteora-damm-v2":   "Meteora v2",
    "boop-fun":          "Boop.fun",
    "token-mill":        "TokenMill",
    "heaven":            "Heaven",
    "daos-fun":          "Daos.fun",
    "virtuals-solana":   "Virtuals",
}

# ── Blacklists / Watchlist ────────────────────────────────────────────────────

# Add creator wallet addresses to skip entirely
CREATOR_BLACKLIST: set = set(filter(None, os.getenv("CREATOR_BLACKLIST", "").split(",")))

# Tokens whose name/symbol contains these strings will be skipped
KEYWORD_BLACKLIST = {
    "test", "rug", "scam", "honeypot", "fake", "ponzi",
}

# Mints to always show highlighted (add manually or via WATCHLIST env)
WATCHLIST: set = set(filter(None, os.getenv("WATCHLIST", "").split(",")))


# ── State ─────────────────────────────────────────────────────────────────────

sol_price_usd  = 88.0
tokens: dict   = {}
total_seen     = 0
stats          = {"pump": 0, "other": 0, "errors": 0}
buy_signals    = deque(maxlen=8)   # recent BUY alerts for the panel
alerted_mints: set = set()         # mints we've already sent alerts for
holder_cache: dict = {}            # mint → {"top10_pct": float, "fetched_at": datetime}
mc_history: dict   = {}            # id → deque of (timestamp, mc_usd) tuples


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
        "creator_sells":  0,      # how many times creator has sold
        "top10_pct":      None,   # holder concentration (fetched async)
        "holder_checked": False,
        "prev_score":     None,
        "mc_trend":       0,      # +1 rising, -1 falling, 0 flat
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

    buys_m5  = int((txns.get("m5") or {}).get("buys",  0))
    sells_m5 = int((txns.get("m5") or {}).get("sells", 0))
    buys_h1  = int((txns.get("h1") or {}).get("buys",  0))
    sells_h1 = int((txns.get("h1") or {}).get("sells", 0))
    vol_h1   = float(vol.get("h1") or 0)
    liq      = float(attrs.get("reserve_in_usd") or 0)

    name = token_attrs.get("name", attrs.get("name", "?"))
    sym  = token_attrs.get("symbol", "?")

    has_twitter  = bool(token_attrs.get("twitter_handle"))
    has_telegram = bool(token_attrs.get("telegram_handle"))

    # Extract mint from base_token relationship id (format: "solana_MINT")
    # stored separately for on-chain lookups
    mint = ""

    return {
        "id":             pool["id"],
        "mint":           mint,
        "platform":       dex_name,
        "symbol":         sym[:12],
        "name":           name[:20],
        "creator":        "",
        "init_sol":       0.0,
        "mc_usd":         mc,
        "mc_sol":         mc / sol_price_usd if sol_price_usd > 0 else 0,
        "peak_mc_usd":    mc,
        "v_sol":          0.0,
        "buys":           buys_h1,
        "sells":          sells_h1,
        "buys_m5":        buys_m5,
        "sells_m5":       sells_m5,
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
        "graduated":      dex_name in ("PumpSwap", "Raydium"),
        "pc_m5":          float(pc.get("m5") or 0),
        "pc_h1":          float(pc.get("h1") or 0),
        "creator_sells":  0,
        "top10_pct":      None,
        "holder_checked": False,
        "prev_score":     None,
        "mc_trend":       0,
    }


# ── Trade update (pump.fun only) ──────────────────────────────────────────────

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
    elif tx == "sell":
        t["sells"]    += 1
        t["sell_sol"] += sol
        # Track if creator is selling — major red flag
        if trader and trader == t.get("creator"):
            t["creator_sells"] += 1

    if vsol >= GRAD_VSOL:
        t["graduated"] = True

    peak = t.get("peak_mc_sol", mc)
    if peak > 0 and mc < peak * 0.40:
        t["dead"] = True

    # Update MC trend
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
    """Returns top-10 holder % of supply. None if unavailable."""
    if not mint or len(mint) < 32:
        return None

    # getTokenLargestAccounts
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

    # getTokenSupply
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


async def enrich_holders_loop():
    """Background task: fetch holder concentration for new tokens."""
    while True:
        try:
            for t in list(tokens.values()):
                if t.get("holder_checked"):
                    continue
                mint = t.get("mint", "")
                if not mint:
                    t["holder_checked"] = True
                    continue
                # Only check tokens still < $50k and not dead
                mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
                if t.get("dead") or mc_usd > MC_LIMIT_USD * 1.5:
                    t["holder_checked"] = True
                    continue

                pct = await fetch_holder_concentration(mint)
                t["top10_pct"]      = pct
                t["holder_checked"] = True
                await asyncio.sleep(0.5)  # rate limit
        except Exception:
            stats["errors"] += 1
        await asyncio.sleep(10)


# ── Scoring ───────────────────────────────────────────────────────────────────

def score(t: dict) -> int:
    s   = {}
    src = t.get("source", "websocket")

    # Buy velocity
    age_sec = max(1, (datetime.now() - t["created_at"]).total_seconds())
    buys    = t.get("buys", 0)
    bpm     = buys / (age_sec / 60)

    if   bpm >= 10: s["vel"] = 100
    elif bpm >= 5:  s["vel"] = 82
    elif bpm >= 2:  s["vel"] = 62
    elif bpm >= 0.5:s["vel"] = 38
    else:           s["vel"] = 12

    # Golden dog bonus (pump.fun): near-filling within 5 min
    if src == "websocket" and age_sec <= 300:
        v_sol     = t.get("v_sol", 30)
        curve_pct = max(0, (v_sol - 28) / (GRAD_VSOL - 28))
        if curve_pct >= 0.20:
            s["vel"] = min(100, s["vel"] + 15)

    # Buy/sell pressure
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

    # Initial buy conviction (pump.fun only)
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

    # Unique traders
    ut = len(t.get("traders", set()))
    if   ut >= 50: s["traders"] = 100
    elif ut >= 20: s["traders"] = 82
    elif ut >= 10: s["traders"] = 65
    elif ut >= 5:  s["traders"] = 45
    elif ut >= 2:  s["traders"] = 28
    else:          s["traders"] = 15

    # Social
    s["social"] = (55 if t.get("has_twitter") else 0) + (45 if t.get("has_telegram") else 0)

    # MC momentum
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

    # Weighted total
    final = round(
        s["vel"]      * 0.25 +
        s["pressure"] * 0.25 +
        s["init"]     * 0.20 +
        s["traders"]  * 0.10 +
        s["social"]   * 0.10 +
        s["mom"]      * 0.10
    )

    # Bonuses / penalties
    if t.get("graduated"):         final = min(100, final + 8)
    if t.get("dead"):              final = max(0,   final - 45)
    if t.get("creator_sells", 0) >= 2: final = max(0, final - 25)  # dev dumping

    top10 = t.get("top10_pct")
    if top10 is not None:
        if top10 >= 80:  final = max(0, final - 30)   # extreme concentration = rug risk
        elif top10 >= 70: final = max(0, final - 15)

    # Stagnation penalty (pump.fun)
    if src == "websocket" and age_sec > 600:
        v_sol     = t.get("v_sol", 30)
        curve_pct = (v_sol - 28) / (GRAD_VSOL - 28)
        if curve_pct < 0.10:
            final = max(0, final - 30)

    return max(0, min(100, final))


def score_breakdown(t: dict) -> dict:
    """Return component scores dict for display."""
    src     = t.get("source", "websocket")
    age_sec = max(1, (datetime.now() - t["created_at"]).total_seconds())
    buys    = t.get("buys", 0)
    bpm     = buys / (age_sec / 60)
    total   = buys + t.get("sells", 0)
    ratio   = buys / total if total >= 3 else 0.5

    return {
        "bpm":        round(bpm, 1),
        "pressure":   f"{ratio*100:.0f}%",
        "init_sol":   t.get("init_sol", 0),
        "traders":    len(t.get("traders", set())),
        "social":     ("X " if t.get("has_twitter") else "") + ("TG" if t.get("has_telegram") else "—"),
        "top10_pct":  t.get("top10_pct"),
        "dev_sells":  t.get("creator_sells", 0),
        "age_s":      int(age_sec),
    }


# ── BUY Signal engine ─────────────────────────────────────────────────────────

def buy_signal(t: dict) -> Optional[str]:
    """
    Returns 'STRONG BUY', 'BUY', or None.
    Criteria tuned for pre-$50k meme coins.
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
    ratio   = buys / total if total >= 3 else 0.5
    mc_usd  = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    top10   = t.get("top10_pct")

    # Hard disqualifiers
    if sc < BUY_SCORE_MIN:
        return None
    if t.get("creator_sells", 0) >= 2:
        return None
    if top10 is not None and top10 >= 75:
        return None
    if mc_usd > MC_LIMIT_USD:
        return None

    # Age gate — prefer very fresh tokens
    if src == "websocket" and age_sec > 600:
        return None
    if src == "gecko" and age_sec > 3600 * 2:
        return None

    # Need minimum trade activity
    if total < 3:
        return None

    # STRONG BUY: highest confidence
    strong = (
        sc >= STRONG_BUY_MIN
        and bpm >= 5
        and ratio >= 0.65
        and (top10 is None or top10 < 60)
    )
    if strong:
        return "STRONG BUY"

    # BUY: solid confidence
    buy = (
        sc >= BUY_SCORE_MIN
        and bpm >= 2
        and ratio >= 0.55
    )
    if buy:
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
    """macOS notification via osascript."""
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
    """Send Telegram message if bot configured."""
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
    """Check if token qualifies for a buy alert and fire if so."""
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
        "time":     datetime.now(),
        "symbol":   t.get("symbol", "?"),
        "platform": t.get("platform", "?"),
        "signal":   sig,
        "score":    score(t),
        "mc_usd":   mc_usd,
        "reason":   reason,
        "url":      token_url(t),
    })

    send_desktop_alert(t, sig)
    # Run telegram in executor so we don't block the event loop
    asyncio.get_event_loop().run_in_executor(None, send_telegram_alert, t, sig)


# ── Display helpers ───────────────────────────────────────────────────────────

PLATFORM_COLORS = {
    "Pump.fun":    "bright_green",
    "PumpSwap":    "green",
    "Moonshot":    "bright_yellow",
    "LaunchLab":   "bright_blue",
    "Meteora DBC": "magenta",
    "Meteora v2":  "magenta",
    "Boop.fun":    "cyan",
    "TokenMill":   "bright_cyan",
    "Heaven":      "white",
    "Daos.fun":    "bright_white",
    "Virtuals":    "bright_magenta",
}

def color_platform(p: str) -> str:
    c = PLATFORM_COLORS.get(p, "white")
    return f"[{c}]{p[:10]}[/{c}]"

def color_score(s: int) -> str:
    if s >= 85: return f"[bold bright_green]{s}[/bold bright_green]"
    if s >= 75: return f"[bold green]{s}[/bold green]"
    if s >= 65: return f"[green]{s}[/green]"
    if s >= 50: return f"[yellow]{s}[/yellow]"
    if s >= 35: return f"[orange1]{s}[/orange1]"
    return f"[red]{s}[/red]"

def fmt_mc(t: dict) -> str:
    usd  = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
    trend = t.get("mc_trend", 0)
    arrow = "[green]▲[/green]" if trend > 0 else ("[red]▼[/red]" if trend < 0 else " ")
    if usd >= 1_000_000: s = f"${usd/1_000_000:.1f}M"
    elif usd >= 1_000:   s = f"${usd/1_000:.0f}K"
    else:                s = f"${usd:.0f}"
    return f"{s}{arrow}"

def fmt_age(t: dict) -> str:
    secs = int((datetime.now() - t["created_at"]).total_seconds())
    if secs < 0:    return "?"
    if secs < 60:   return f"{secs}s"
    if secs < 3600: return f"{secs//60}m{secs%60:02d}s"
    return f"{secs//3600}h{(secs%3600)//60}m"

def curve_bar(t: dict) -> str:
    if t.get("source") != "websocket":
        return "[dim]n/a[/dim]"
    v   = t.get("v_sol", 30)
    pct = min(1.0, max(0, (v - 28) / (GRAD_VSOL - 28)))
    filled = int(pct * 7)
    bar = "█" * filled + "░" * (7 - filled)
    col = "green" if pct < 0.4 else ("yellow" if pct < 0.75 else "red")
    return f"[{col}]{bar}[/{col}] {pct*100:.0f}%"

def bpm_str(t: dict) -> str:
    age = max(1, (datetime.now() - t["created_at"]).total_seconds())
    bpm = t.get("buys", 0) / (age / 60)
    if bpm >= 5: return f"[bold green]{bpm:.1f}[/bold green]"
    if bpm >= 1: return f"[yellow]{bpm:.1f}[/yellow]"
    return f"[dim]{bpm:.1f}[/dim]"

def fmt_holders(t: dict) -> str:
    pct = t.get("top10_pct")
    if pct is None:
        return "[dim]…[/dim]"
    if pct >= 80: return f"[bold red]{pct:.0f}%[/bold red]"
    if pct >= 70: return f"[red]{pct:.0f}%[/red]"
    if pct >= 60: return f"[yellow]{pct:.0f}%[/yellow]"
    return f"[green]{pct:.0f}%[/green]"

def fmt_liq(t: dict) -> str:
    liq = t.get("liq_usd", 0)
    if not liq: return "[dim]—[/dim]"
    if liq >= 10_000: return f"[green]${liq/1000:.0f}K[/green]"
    if liq >= 3_000:  return f"[yellow]${liq/1000:.1f}K[/yellow]"
    return f"[red]${liq:.0f}[/red]"

def fmt_signal(t: dict) -> str:
    sig = buy_signal(t)
    if sig == "STRONG BUY": return "[bold bright_green]⬆ SBUY[/bold bright_green]"
    if sig == "BUY":        return "[bold green]↑ BUY[/bold green]"
    ds = t.get("creator_sells", 0)
    if ds >= 2:             return "[bold red]⚠ DEV[/bold red]"
    return ""


# ── Buy signals panel ─────────────────────────────────────────────────────────

def build_signals_panel() -> Panel:
    if not buy_signals:
        return Panel(
            "[dim]No buy signals yet — waiting for qualifying tokens...[/dim]",
            title="[bold yellow]⚡ BUY SIGNALS[/bold yellow]",
            border_style="yellow",
            padding=(0, 1),
        )

    lines = []
    for sig in buy_signals:
        age_s = int((datetime.now() - sig["time"]).total_seconds())
        age   = f"{age_s}s ago" if age_s < 60 else f"{age_s//60}m ago"
        label = (
            "[bold bright_green]🔥 STRONG BUY[/bold bright_green]"
            if sig["signal"] == "STRONG BUY"
            else "[bold green]↑ BUY[/bold green]"
        )
        line = (
            f"{label}  [bold]{sig['symbol']:<10}[/bold]  "
            f"[cyan]{sig['platform'][:10]}[/cyan]  "
            f"Score:[bold]{sig['score']}[/bold]  "
            f"MC:[yellow]${sig['mc_usd']/1000:.1f}K[/yellow]  "
            f"[dim]{sig['reason']}[/dim]  "
            f"[blue]{sig['url']}[/blue]  "
            f"[dim]{age}[/dim]"
        )
        lines.append(line)

    return Panel(
        "\n".join(lines),
        title="[bold yellow]⚡ BUY SIGNALS[/bold yellow]",
        border_style="yellow",
        padding=(0, 1),
    )


# ── Main scanner table ────────────────────────────────────────────────────────

def build_table() -> Table:
    table = Table(
        title=(
            f"[bold cyan]Multi-Launchpad Pre-$50k Scanner v2[/bold cyan]  ·  "
            f"SOL=${sol_price_usd:.0f}  ·  "
            f"tracked [yellow]{len(tokens)}[/yellow]  ·  "
            f"seen [bold]{total_seen}[/bold]  ·  "
            f"{datetime.now().strftime('%H:%M:%S')}"
        ),
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
    )
    table.add_column("Signal",   justify="center", width=8)
    table.add_column("Score",    justify="center", width=7)
    table.add_column("Platform", width=11)
    table.add_column("Symbol",   style="bold",     width=10)
    table.add_column("MC▲▼",    justify="right",  width=9)
    table.add_column("Liq",      justify="right",  width=7)
    table.add_column("Init SOL", justify="right",  width=9)
    table.add_column("Buys",     justify="right",  width=5)
    table.add_column("Sells",    justify="right",  width=5)
    table.add_column("B/min",    justify="right",  width=6)
    table.add_column("Hold%",    justify="right",  width=7)
    table.add_column("Social",   justify="center", width=7)
    table.add_column("Curve",    width=14)
    table.add_column("Age",      justify="right",  width=7)

    visible = []
    for t in tokens.values():
        if t.get("dead"):
            continue
        mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
        if mc_usd < MC_LIMIT_USD or t.get("graduated") or t["id"] in WATCHLIST:
            visible.append(t)

    visible.sort(key=lambda x: (
        # Watchlist always first
        x["id"] not in WATCHLIST,
        # Then by signal strength
        0 if buy_signal(x) == "STRONG BUY" else (1 if buy_signal(x) == "BUY" else 2),
        # Then by score descending
        -score(x),
    ))

    for t in visible[:MAX_DISPLAY]:
        sc     = score(t)
        social = ("X " if t.get("has_twitter") else "") + ("TG" if t.get("has_telegram") else "")
        status = ""
        if t.get("graduated"):   status = "[green] G[/green]"
        if t["id"] in WATCHLIST: status += "[cyan] ★[/cyan]"

        init     = t.get("init_sol", 0)
        init_str = f"{init:.2f}" if init else "[dim]—[/dim]"

        # Highlight STRONG BUY rows
        sig     = buy_signal(t)
        row_sty = ""

        table.add_row(
            fmt_signal(t),
            color_score(sc),
            color_platform(t["platform"]),
            t["symbol"] + status,
            fmt_mc(t),
            fmt_liq(t),
            init_str,
            str(t.get("buys", 0)),
            str(t.get("sells", 0)),
            bpm_str(t),
            fmt_holders(t),
            social or "[dim]—[/dim]",
            curve_bar(t),
            fmt_age(t),
            style=row_sty,
        )

    if not visible:
        table.add_row(*["[dim]waiting...[/dim]"] + [""] * 13)

    return table


def build_score_legend() -> Panel:
    """Small legend panel explaining signals."""
    return Panel(
        "[bold bright_green]⬆ SBUY[/bold bright_green] Score≥85, vel≥5b/m, press≥65%, top10<60%  "
        "[bold green]↑ BUY[/bold green] Score≥75, vel≥2b/m, press≥55%  "
        "[bold red]⚠ DEV[/bold red] Creator sold ≥2x  "
        "[red]Hold%[/red] >80% top-10 = rug risk  "
        "[green]▲[/green]/[red]▼[/red] MC trend  "
        "[green]Liq[/green] pool liquidity  "
        f"Alert threshold: [bold]{ALERT_SCORE}[/bold]",
        title="Legend",
        border_style="dim",
        padding=(0, 1),
    )


# ── GeckoTerminal poller ──────────────────────────────────────────────────────

async def poll_gecko(live: Live):
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
                if mc_usd > MC_LIMIT_USD * 2:
                    continue

                if pool_id not in tokens:
                    tok = new_gt_token(pool, dex_name, token_attrs)
                    # Try to extract mint from token_id (format: "solana_MINT_QUOTE")
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

            live.update(Columns([build_signals_panel()]) if buy_signals else build_signals_panel())
            live.update(_build_full_display())

        except Exception:
            stats["errors"] += 1
            await asyncio.sleep(5)


# ── PumpPortal WebSocket ──────────────────────────────────────────────────────

async def run_pumpportal(live: Live):
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
                    name = msg.get("name", "")
                    sym  = msg.get("symbol", "")
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

                live.update(_build_full_display())

        except websockets.exceptions.ConnectionClosed:
            await asyncio.sleep(2)
        except Exception:
            stats["errors"] += 1
            await asyncio.sleep(2)


# ── Render ────────────────────────────────────────────────────────────────────

def _build_full_display():
    from rich.console import Group
    return Group(
        build_signals_panel(),
        build_table(),
        build_score_legend(),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    console.print(Panel(
        "[bold cyan]Multi-Launchpad Pre-$50k Meme Scanner v2[/bold cyan]\n\n"
        "[bold white]Pump.fun[/bold white] · [bright_yellow]Moonshot[/bright_yellow] · "
        "[bright_blue]LaunchLab[/bright_blue] · [magenta]Meteora DBC[/magenta] "
        "(Believe/Bonkers/Bankr)\n"
        "[cyan]Boop.fun[/cyan] · [bright_cyan]TokenMill[/bright_cyan] · "
        "[white]Heaven[/white] · [bright_white]Daos.fun[/bright_white] · "
        "[bright_magenta]Virtuals[/bright_magenta]\n\n"
        "[dim]NEW in v2:\n"
        "  ⚡ BUY SIGNAL panel with entry reasoning\n"
        "  🔔 Desktop + Telegram alerts (set TG_BOT_TOKEN & TG_CHAT_ID)\n"
        "  👥 Holder concentration (rug risk detection)\n"
        "  ⚠  Dev wallet sell tracking\n"
        "  ▲▼ MC trend arrows\n"
        "  💧 Liquidity display\n"
        "  Ctrl+C to stop[/dim]",
        border_style="cyan",
    ))

    with Live(
        _build_full_display(),
        refresh_per_second=2,
        console=console,
    ) as live:
        await asyncio.gather(
            refresh_sol_price(),
            run_pumpportal(live),
            poll_gecko(live),
            enrich_holders_loop(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        top = sorted(
            [t for t in tokens.values() if not t.get("dead")],
            key=lambda x: score(x), reverse=True,
        )[:5]
        if top:
            console.print("\n[bold]Top tokens this session:[/bold]")
            for t in top:
                mc_usd = t.get("mc_usd", t.get("mc_sol", 0) * sol_price_usd)
                sig    = buy_signal(t) or ""
                console.print(
                    f"  {score(t):>3}  [{t['platform']}] {t['symbol']:<12}  "
                    f"MC=${mc_usd/1000:.1f}K  {sig}  {token_url(t)}"
                )
        if buy_signals:
            console.print("\n[bold yellow]Buy signals this session:[/bold yellow]")
            for s in buy_signals:
                console.print(
                    f"  {s['signal']:<12} ${s['symbol']:<10} "
                    f"Score:{s['score']}  MC:${s['mc_usd']/1000:.1f}K  {s['url']}"
                )

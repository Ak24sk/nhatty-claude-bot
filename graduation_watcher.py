"""
graduation_watcher.py
----------------------
Standalone 24/7 worker (separate from the Streamlit app) that:

1. Connects to PumpPortal's free WebSocket feed (wss://pumpportal.fun/api/data)
   and listens for:
     - subscribeNewToken   -> new pump.fun token launches
     - subscribeMigration  -> instant graduation events (free, real-time)

2. Periodically re-checks each recently-launched token's on-chain bonding
   curve account (via your own Helius RPC — no extra cost) to compute
   progress toward the ~85 SOL graduation threshold, and sends an
   "early warning" alert once a token crosses a configurable threshold.

3. Sends alerts to Telegram via a bot.

4. Runs a tiny Flask HTTP server alongside it so a free host (e.g. Render
   free tier) has something to answer keep-alive pings on, to help an
   external uptime pinger (e.g. UptimeRobot) keep this process awake.

Environment variables required:
    TELEGRAM_BOT_TOKEN   - from @BotFather
    TELEGRAM_CHAT_ID     - your personal chat id
    HELIUS_RPC_URL       - your Helius Solana RPC endpoint

Optional:
    EARLY_WARNING_PCT    - default 80 (percent of 85 SOL threshold)
    POLL_INTERVAL_SEC    - default 30 (how often to re-check tracked tokens)
    TRACK_WINDOW_MIN     - default 120 (stop tracking tokens older than this,
                            since very few graduate after that point)
"""

import base64
import json
import os
import struct
import threading
import time
from datetime import datetime, timezone

import requests
import websocket  # from websocket-client package
from flask import Flask
from solders.pubkey import Pubkey

from modules import rugcheck as rc

MOBULA_API_KEY = os.environ.get("MOBULA_API_KEY", "")
MOBULA_BASE_URL = "https://api.mobula.io/api/2"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "")

EARLY_WARNING_PCT = float(os.environ.get("EARLY_WARNING_PCT", "80"))
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "30"))
TRACK_WINDOW_MIN = int(os.environ.get("TRACK_WINDOW_MIN", "120"))

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

GRADUATION_SOL_LAMPORTS = 85 * 1_000_000_000  # ~85 SOL, in lamports

# Bonding curve account byte layout (little-endian):
#   0-8   discriminator
#   8-16  virtualTokenReserves (u64)
#   16-24 virtualSolReserves (u64)
#   24-32 realTokenReserves (u64)
#   32-40 realSolReserves (u64)
#   40-48 tokenTotalSupply (u64)
#   48    complete (bool)
REAL_SOL_RESERVES_OFFSET = 32
COMPLETE_OFFSET = 48

# ---------------------------------------------------------------------------
# Shared state (thread-safe via lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
tracked_tokens = {}  # mint -> {"created_at": epoch_seconds, "name": str, "symbol": str, "warned": bool}


# ---------------------------------------------------------------------------
# Dev history (Mobula) - reused for graduation alerts
# ---------------------------------------------------------------------------

def get_dev_history_summary(creator_wallet: str) -> str:
    """Returns a short human-readable dev history string, or '' if unavailable."""
    if not creator_wallet or not MOBULA_API_KEY:
        return ""
    try:
        resp = requests.get(
            f"{MOBULA_BASE_URL}/wallet/deployer",
            params={"wallet": creator_wallet, "blockchain": "solana"},
            headers={"Authorization": MOBULA_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[dev history] Mobula lookup failed: {e}")
        return ""

    tokens = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(tokens, list) or not tokens:
        return ""

    total = len(tokens)
    migrated = sum(1 for t in tokens if (t.get("token") or {}).get("bonded"))
    rate = round((migrated / total * 100), 1) if total else 0.0
    flag = " ⚠️ low grad rate" if rate < 20 else ""
    return f"Dev: {total} launched, {migrated} graduated ({rate}%){flag}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] Missing bot token or chat id, skipping alert:", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except requests.RequestException as e:
        print("[telegram] Failed to send alert:", e)


# ---------------------------------------------------------------------------
# On-chain bonding curve progress check (via Helius RPC — free, your own key)
# ---------------------------------------------------------------------------

def get_bonding_curve_pda(mint: str) -> str:
    mint_pubkey = Pubkey.from_string(mint)
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)],
        PUMP_PROGRAM_ID,
    )
    return str(pda)


def get_bonding_curve_progress(mint: str) -> dict | None:
    """Returns {'pct': float, 'complete': bool} or None on failure."""
    if not HELIUS_RPC_URL:
        return None
    try:
        curve_address = get_bonding_curve_pda(mint)
        resp = requests.post(
            HELIUS_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [curve_address, {"encoding": "base64"}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        value = (data.get("result") or {}).get("value")
        if not value:
            return None
        raw = base64.b64decode(value["data"][0])
        if len(raw) < COMPLETE_OFFSET + 1:
            return None

        real_sol_reserves = struct.unpack_from("<Q", raw, REAL_SOL_RESERVES_OFFSET)[0]
        complete = bool(raw[COMPLETE_OFFSET])
        pct = min(100.0, (real_sol_reserves / GRADUATION_SOL_LAMPORTS) * 100)
        return {"pct": round(pct, 1), "complete": complete}
    except Exception as e:
        print(f"[curve check] Error for {mint}: {e}")
        return None


# ---------------------------------------------------------------------------
# Background poller: re-checks tracked tokens for early-warning threshold
# ---------------------------------------------------------------------------

def poller_loop():
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        now = time.time()
        with _lock:
            stale = [
                m for m, info in tracked_tokens.items()
                if now - info["created_at"] > TRACK_WINDOW_MIN * 60
            ]
            for m in stale:
                del tracked_tokens[m]
            snapshot = dict(tracked_tokens)

        for mint, info in snapshot.items():
            if info.get("warned"):
                continue
            progress = get_bonding_curve_progress(mint)
            if not progress:
                continue
            if progress["complete"]:
                # Migration event should catch this too, but as a fallback:
                continue
            if progress["pct"] >= EARLY_WARNING_PCT:
                send_telegram(
                    f"⚠️ *Early warning* — {info.get('symbol', '?')} "
                    f"({mint[:6]}...{mint[-4:]}) is at *{progress['pct']}%* "
                    f"toward graduation.\n`{mint}`"
                )
                with _lock:
                    if mint in tracked_tokens:
                        tracked_tokens[mint]["warned"] = True


# ---------------------------------------------------------------------------
# PumpPortal WebSocket listener (auto-reconnects on drop)
# ---------------------------------------------------------------------------

def on_message(ws, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    tx_type = data.get("txType")

    if tx_type == "create":
        mint = data.get("mint")
        if mint:
            with _lock:
                tracked_tokens[mint] = {
                    "created_at": time.time(),
                    "name": data.get("name", "?"),
                    "symbol": data.get("symbol", "?"),
                    "warned": False,
                }
            print(f"[new token] {data.get('symbol')} - {mint}")

    elif tx_type == "migrate" or data.get("type") == "migration":
        mint = data.get("mint")
        if mint:
            info = tracked_tokens.get(mint, {})
            try:
                creator = rc.get_creator_address(mint)
            except Exception as e:
                print(f"[migration] creator lookup failed for {mint}: {e}")
                creator = None
            print(f"[migration] mint={mint} creator={creator}")
            dev_line = get_dev_history_summary(creator) if creator else ""
            print(f"[migration] dev_line={dev_line!r}")
            message = (
                f"🎓 *GRADUATED* — {info.get('symbol', '?')} just migrated to a DEX.\n"
                f"`{mint}`"
            )
            if dev_line:
                message += f"\n{dev_line}"
            send_telegram(message)
            with _lock:
                tracked_tokens.pop(mint, None)


def on_open(ws):
    print("[ws] Connected to PumpPortal")
    ws.send(json.dumps({"method": "subscribeNewToken"}))
    ws.send(json.dumps({"method": "subscribeMigration"}))


def on_error(ws, error):
    print("[ws] Error:", error)


def on_close(ws, code, msg):
    print("[ws] Closed, will reconnect in 5s...")


def ws_loop():
    while True:
        try:
            ws = websocket.WebSocketApp(
                PUMPPORTAL_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print("[ws] Crashed:", e)
        time.sleep(5)


# ---------------------------------------------------------------------------
# Flask keep-alive endpoint
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def health():
    with _lock:
        count = len(tracked_tokens)
    return {
        "status": "ok",
        "tracked_tokens": count,
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=ws_loop, daemon=True).start()
    threading.Thread(target=poller_loop, daemon=True).start()

    send_telegram("🚀 Graduation watcher started.")

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

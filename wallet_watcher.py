"""
wallet_watcher.py
------------------
Standalone 24/7 worker (same pattern as graduation_watcher.py) that:

1. Loads followed wallets from watched_wallets.txt (one per line,
   "label,address" or bare "address").
2. Opens a single WebSocket to Helius and subscribes to each wallet via
   logsSubscribe, so Solana pushes a notification the instant a matching
   transaction confirms - no polling delay.
3. On each notification, fetches the full transaction (one fast,
   unthrottled Helius RPC call) and classifies it:
     - BUY/SELL      if a real swap/AMM/aggregator program was involved
     - RECEIVED/SENT if it was a plain transfer (airdrop, claim, etc.)
   with SOL amount, USD value, and the token's live market cap.
4. Separately tracks a rolling window of buy events and sell events, and
   fires a louder alert when 2+ followed wallets buy - or sell - the same
   token within the window.
5. Persists seen (signature, wallet) pairs + recent event history so
   restarts do not replay old alerts.

Environment variables required:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HELIUS_RPC_URL

Optional:
    CONVERGENCE_WINDOW_MIN  - default 30
    CONVERGENCE_MIN_WALLETS - default 2
    WALLETS_FILE            - default "watched_wallets.txt"
    STATE_FILE              - default "wallet_watcher_state.json"
    SOL_PRICE_REFRESH_SEC   - default 45
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
import websocket  # from websocket-client package
from flask import Flask

from modules.solana_client import SolanaClient, SolanaRpcError
from modules import buy_sell as bs
from modules import convergence as conv
from modules import gecko_price as gpr

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "")
HELIUS_WS_URL = HELIUS_RPC_URL.replace("https://", "wss://", 1)

CONVERGENCE_WINDOW_MIN = int(os.environ.get("CONVERGENCE_WINDOW_MIN", "30"))
CONVERGENCE_MIN_WALLETS = int(os.environ.get("CONVERGENCE_MIN_WALLETS", "2"))
SOL_PRICE_REFRESH_SEC = int(os.environ.get("SOL_PRICE_REFRESH_SEC", "45"))
BACKSTOP_POLL_INTERVAL_SEC = int(os.environ.get("BACKSTOP_POLL_INTERVAL_SEC", "90"))
BACKSTOP_SIGNATURES_PER_WALLET = int(os.environ.get("BACKSTOP_SIGNATURES_PER_WALLET", "15"))

WALLETS_FILE = os.environ.get("WALLETS_FILE", "watched_wallets.txt")
STATE_FILE = os.environ.get("STATE_FILE", "wallet_watcher_state.json")
EVENT_HISTORY_MAX_AGE_SEC = CONVERGENCE_WINDOW_MIN * 60 * 3

_client = SolanaClient(HELIUS_RPC_URL)
_lock = threading.Lock()
_alerted_clusters = set()
_sol_usd_price = None  # refreshed in the background, never blocks an alert
_subscription_map = {}  # subscription_id (int) -> (label, address)
_pending_subs = {}      # request id (int) -> (label, address)
_last_backstop_run = None  # ISO timestamp of the last backstop sweep, for /


def load_wallets():
    if not os.path.exists(WALLETS_FILE):
        print(f"[wallets] {WALLETS_FILE} not found - tracking 0 wallets.")
        return []
    wallets = []
    with open(WALLETS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                label, addr = line.split(",", 1)
                wallets.append((label.strip(), addr.strip()))
            else:
                wallets.append((line[:6], line))
    return wallets


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["seen_keys"] = set(data.get("seen_keys", []))
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen_keys": set(), "buy_events": [], "sell_events": []}


def save_state(state):
    to_save = {
        "seen_keys": list(state["seen_keys"])[-5000:],
        "buy_events": state["buy_events"],
        "sell_events": state["sell_events"],
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f)


_state = load_state()


def _escape_markdown(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


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


def sol_price_loop():
    global _sol_usd_price
    while True:
        try:
            data = gpr.get_token_market_data(bs.WSOL_MINT)
            price = data.get("price_usd") if data else None
            if price:
                _sol_usd_price = float(price)
        except Exception as e:
            print(f"[sol price] refresh failed (keeping last known value): {e}")
        time.sleep(SOL_PRICE_REFRESH_SEC)


def estimate_market_cap(mint, price_sol_per_token):
    try:
        supply = _client.get_token_supply(mint)
        total_supply = float(supply.get("uiAmount") or 0)
    except (SolanaRpcError, TypeError, ValueError):
        return None, None
    if not total_supply or price_sol_per_token is None:
        return None, None
    mc_sol = price_sol_per_token * total_supply
    mc_usd = mc_sol * _sol_usd_price if _sol_usd_price else None
    return mc_sol, mc_usd


def _format_usd(value):
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.2f}"


def check_convergence(events, direction_label, emoji):
    if not events:
        return
    scored = [{**e, "combined_score": 50} for e in events]
    clusters = conv.detect_convergence(
        scored,
        window_seconds=CONVERGENCE_WINDOW_MIN * 60,
        min_wallets=CONVERGENCE_MIN_WALLETS,
    )
    for c in clusters:
        key = (direction_label, c["token_mint"], c["window_start"])
        if key in _alerted_clusters:
            continue
        _alerted_clusters.add(key)
        send_telegram(
            f"{emoji} *{c['wallet_count']} wallets {direction_label} the same token* {emoji}\n"
            f"`{c['token_mint']}`\n"
            f"Wallets: {', '.join(c['wallets'])}"
        )


def handle_signature(label, addr, sig):
    seen_key = f"{sig}:{addr}"
    with _lock:
        if seen_key in _state["seen_keys"]:
            return
        _state["seen_keys"].add(seen_key)

    try:
        tx = _client.get_transaction(sig)
    except SolanaRpcError as e:
        print(f"[tx fetch] failed for {sig}: {e}")
        return
    if not tx:
        return

    wallet_display = f"{_escape_markdown(label)} ({addr[:4]}...{addr[-4:]})"
    block_time = tx.get("blockTime") or time.time()
    is_swap = bs.is_market_swap(tx)

    for buy in bs.detect_buys_any_token(tx, addr):
        mint = buy["token_mint"]
        sol_amt = abs(buy["sol_delta"])
        token_amt = buy["token_delta"]

        if is_swap:
            price_per_token = (sol_amt / token_amt) if token_amt else None
            _mc_sol, mc_usd = estimate_market_cap(mint, price_per_token)
            usd_amt = sol_amt * _sol_usd_price if _sol_usd_price else None
            send_telegram(
                f"🟢 *BUY* — {wallet_display}\n`{mint}`\n"
                f"Spent ≈ {sol_amt:.4f} SOL ({_format_usd(usd_amt)})\n"
                f"Market cap ≈ {_format_usd(mc_usd)}"
            )
        else:
            send_telegram(
                f"📥 *RECEIVED* — {wallet_display}\n`{mint}`\n"
                f"{token_amt:,.2f} tokens (no swap detected - transfer/airdrop)"
            )
        with _lock:
            _state["buy_events"].append({
                "wallet": wallet_display, "token_mint": mint,
                "block_time": block_time, "signature": sig,
            })

    for sell in bs.detect_sells_any_token(tx, addr):
        mint = sell["token_mint"]
        sol_amt = sell["sol_delta"]
        token_amt = abs(sell["token_delta"])

        if is_swap:
            price_per_token = (sol_amt / token_amt) if token_amt else None
            _mc_sol, mc_usd = estimate_market_cap(mint, price_per_token)
            usd_amt = sol_amt * _sol_usd_price if _sol_usd_price else None
            send_telegram(
                f"🔴 *SELL* — {wallet_display}\n`{mint}`\n"
                f"Received ≈ {sol_amt:.4f} SOL ({_format_usd(usd_amt)})\n"
                f"Market cap ≈ {_format_usd(mc_usd)}"
            )
        else:
            send_telegram(
                f"📤 *SENT* — {wallet_display}\n`{mint}`\n"
                f"{token_amt:,.2f} tokens (no swap detected - plain transfer)"
            )
        with _lock:
            _state["sell_events"].append({
                "wallet": wallet_display, "token_mint": mint,
                "block_time": block_time, "signature": sig,
            })

    with _lock:
        now = time.time()
        cutoff = now - EVENT_HISTORY_MAX_AGE_SEC
        _state["buy_events"] = [e for e in _state["buy_events"] if e["block_time"] >= cutoff]
        _state["sell_events"] = [e for e in _state["sell_events"] if e["block_time"] >= cutoff]
        save_state(_state)

    check_convergence(_state["buy_events"], "BUY", "🟢🟢")
    check_convergence(_state["sell_events"], "SELL", "🔴🔴")


def backstop_scan_once():
    """Safety net alongside the WebSocket listener - re-checks each wallet's
    recent signatures via RPC and runs anything not already in seen_keys
    through handle_signature(), which already dedupes. No-op unless the
    WebSocket actually missed something during a reconnect gap."""
    global _last_backstop_run
    wallets = load_wallets()
    for label, addr in wallets:
        try:
            sigs = _client.get_signatures_for_address(addr, limit=BACKSTOP_SIGNATURES_PER_WALLET)
        except SolanaRpcError as e:
            print(f"[backstop] signature fetch failed for {label}: {e}")
            continue
        for sig_info in sigs or []:
            sig = sig_info.get("signature") if isinstance(sig_info, dict) else sig_info
            if not sig:
                continue
            seen_key = f"{sig}:{addr}"
            with _lock:
                already_seen = seen_key in _state["seen_keys"]
            if already_seen:
                continue
            print(f"[backstop] catching missed signature for {label}: {sig}")
            try:
                handle_signature(label, addr, sig)
            except Exception as e:
                print(f"[backstop] error processing {sig} for {label}: {e}")
    _last_backstop_run = datetime.now(timezone.utc).isoformat()


def backstop_poll_loop():
    while True:
        time.sleep(BACKSTOP_POLL_INTERVAL_SEC)
        try:
            backstop_scan_once()
        except Exception as e:
            print(f"[backstop] loop error (will retry next cycle): {e}")


def on_open(ws):
    print("[ws] Connected to Helius - subscribing to wallets...")
    _subscription_map.clear()
    _pending_subs.clear()
    wallets = load_wallets()
    for i, (label, addr) in enumerate(wallets):
        req_id = i + 1
        _pending_subs[req_id] = (label, addr)
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
        }))
    print(f"[ws] Sent {len(wallets)} subscription requests.")


def on_message(ws, message):
       try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return
       if "error" in data:
        print("[ws] SERVER ERROR REPLY:", data)

    if "id" in data and "result" in data and data["id"] in _pending_subs:
        label, addr = _pending_subs.pop(data["id"])
        _subscription_map[data["result"]] = (label, addr)
        return

    if data.get("method") == "logsNotification":
        params = data.get("params", {})
        sub_id = params.get("subscription")
        wallet_info = _subscription_map.get(sub_id)
        if not wallet_info:
            return
        label, addr = wallet_info
        sig = (params.get("result") or {}).get("value", {}).get("signature")
        if not sig:
            return
        try:
            handle_signature(label, addr, sig)
        except Exception as e:
            print(f"[handler] error processing {sig} for {label}: {e}")


def on_error(ws, error):
    print("[ws] Error:", error)


def on_close(ws, code, msg):
    print("[ws] Closed, will reconnect in 5s...")


def ws_loop():
    while True:
        try:
            ws = websocket.WebSocketApp(
                HELIUS_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print("[ws] Crashed:", e)
        time.sleep(5)


app = Flask(__name__)


@app.route("/")
def health():
    return {
        "status": "ok",
        "tracked_wallets": len(load_wallets()),
        "active_subscriptions": len(_subscription_map),
        "sol_usd_price": _sol_usd_price,
        "last_backstop_scan": _last_backstop_run,
        "time": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    threading.Thread(target=sol_price_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()
    threading.Thread(target=backstop_poll_loop, daemon=True).start()
    send_telegram("👛 Wallet watcher started (real-time mode).")
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

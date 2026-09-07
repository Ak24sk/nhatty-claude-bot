"""
buy_sell.py
Heuristic buy/sell detection from a wallet's parsed transaction history.
Checks native SOL AND wrapped SOL (WSOL) since Jupiter-routed swaps
commonly move value through a WSOL token account, not native lamports.
"""

from dataclasses import dataclass
from typing import Optional, List, Dict

LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class TradeEvent:
    signature: str
    slot: int
    block_time: Optional[int]
    action: str
    sol_delta: float
    token_delta: float


def _token_balance_delta(meta, owner, mint):
    pre = post = 0.0
    for bal in meta.get("preTokenBalances") or []:
        if bal.get("owner") == owner and bal.get("mint") == mint:
            pre = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
    for bal in meta.get("postTokenBalances") or []:
        if bal.get("owner") == owner and bal.get("mint") == mint:
            post = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
    return post - pre


def _find_owner_balances(tx, owner, mint):
    meta = tx.get("meta") or {}
    message = (tx.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []

    native_sol_delta = None
    for idx, key in enumerate(account_keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == owner:
            pre_balances = meta.get("preBalances") or []
            post_balances = meta.get("postBalances") or []
            if idx < len(pre_balances) and idx < len(post_balances):
                native_sol_delta = (post_balances[idx] - pre_balances[idx]) / LAMPORTS_PER_SOL
            break

    wsol_delta = _token_balance_delta(meta, owner, WSOL_MINT)
    token_delta = _token_balance_delta(meta, owner, mint)

    if native_sol_delta is None and wsol_delta == 0:
        return None, None

    combined_sol_delta = (native_sol_delta or 0.0) + wsol_delta
    return combined_sol_delta, token_delta


def classify_transaction(tx, owner, mint):
    if not tx or not tx.get("meta"):
        return None

    signature = None
    sigs = ((tx.get("transaction") or {}).get("signatures")) or []
    if sigs:
        signature = sigs[0]

    sol_delta, token_delta = _find_owner_balances(tx, owner, mint)
    if sol_delta is None:
        return None

    if sol_delta < 0 and token_delta > 0:
        action = "BUY"
    elif sol_delta > 0 and token_delta < 0:
        action = "SELL"
    else:
        action = "UNKNOWN"

    return TradeEvent(
        signature=signature or "unknown",
        slot=tx.get("slot", 0),
        block_time=tx.get("blockTime"),
        action=action,
        sol_delta=round(sol_delta, 6),
        token_delta=round(token_delta, 6),
    )


def summarize_trades(trades):
    buys = [t for t in trades if t.action == "BUY"]
    sells = [t for t in trades if t.action == "SELL"]
    return {
        "total_trades": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "unknown_count": len(trades) - len(buys) - len(sells),
        "total_sol_spent_on_buys": round(-sum(t.sol_delta for t in buys), 6),
        "total_sol_received_on_sells": round(sum(t.sol_delta for t in sells), 6),
        "first_trade_time": min((t.block_time for t in trades if t.block_time), default=None),
        "last_trade_time": max((t.block_time for t in trades if t.block_time), default=None),
    }
